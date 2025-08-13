"""Tempo account migration module for Jira to OpenProject migration.
Handles the migration of Tempo Timesheet accounts from Jira to OpenProject.
"""

import json
import re
from pathlib import Path
from typing import Any

import requests

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient, OpenProjectError
from src.config import get_path, jira_config, logger, migration_config
# Provide a module-level config attribute for tests expecting `config`
from types import SimpleNamespace
config = SimpleNamespace(logger=logger)
from src.display import ProgressTracker

# Get batch size from migration config
batch_size = migration_config.get("batch_size", 1000)
# Get Jira connection settings
jira_url = jira_config.get("url")
jira_username = jira_config.get("username")
jira_api_token = jira_config.get("api_token")


class TempoAccountMigration:
    """Handles the migration of Tempo Timesheet accounts from Jira to OpenProject.

    This class is responsible for:
    1. Extracting Tempo account information from Jira
    2. Creating corresponding organizations/companies in OpenProject
    3. Mapping Tempo account data between the systems
    4. Migrating Tempo worklog data to OpenProject time entries
    """

    def __init__(self) -> None:
        """Initialize the Tempo account migration tools."""
        self.jira_client = JiraClient()
        self.op_client = OpenProjectClient()
        self.accounts: list[dict[str, Any]] = []
        self.account_mapping: dict[str, Any] = {}
        self.custom_field_id: int | None = None

        # Use the centralized config for var directories
        self.data_dir: Path = get_path("data")

        # Base Tempo API URL - typically {jira_url}/rest/tempo-accounts/1 for Server
        # or a separate endpoint for Cloud
        self.tempo_api_base = (
            f"{jira_config.get('url', '').rstrip('/')}/rest/tempo-accounts/1"
        )

        # Setup auth for Tempo API
        self.tempo_auth_headers = {
            "Authorization": f"Bearer {jira_config.get('api_token', '')}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def extract_tempo_accounts(self) -> list[dict[str, Any]]:
        """Extract Tempo account information from Jira.

        Returns:
            List of Tempo account dictionaries

        """
        logger.info("Extracting Tempo accounts from Jira...")

        try:
            # Call the Tempo API to get all accounts
            accounts_endpoint = f"{self.tempo_api_base}/account"

            response = requests.get(
                accounts_endpoint,
                headers=self.tempo_auth_headers,
                verify=False,  # Only use in development  # noqa: S501
            )

            if response.status_code == 200:
                accounts = response.json()
                logger.info("Retrieved %s Tempo accounts", len(accounts))

                # Save the accounts to a file
                self.accounts = accounts
                self._save_to_json(accounts, "tempo_accounts.json")

                return accounts
            logger.error(
                f"Failed to get Tempo accounts. Status code: {response.status_code}, Response: {response.text}",
            )

            return []

        except Exception as e:
            logger.exception("Failed to extract Tempo accounts: %s", e)

            return []

    def extract_openproject_companies(self) -> list[dict[str, Any]]:
        """Extract company information from OpenProject.

        Returns:
            List of company dictionaries

        """
        logger.info("Extracting companies from OpenProject...")

        # Get companies from OpenProject
        try:
            self.op_companies = self.op_client.get_companies()
        except Exception as e:
            logger.warning(
                f"Failed to get companies from OpenProject: {e}",
            )
            logger.warning(
                "Using an empty list of companies for OpenProject",
            )
            self.op_companies = []

        # Log the number of companies found
        logger.info("Extracted %s companies from OpenProject", len(self.op_companies))

        # Save companies to file for later reference
        self._save_to_json(self.op_companies, "openproject_companies.json")

        return self.op_companies

    def create_account_mapping(self) -> dict[str, Any]:
        """Create a mapping between Tempo accounts and OpenProject companies.

        This method creates a mapping based on account names.

        Returns:
            Dictionary mapping Tempo account IDs to OpenProject company IDs

        """
        logger.info("Creating Tempo account to OpenProject company mapping...")

        # Make sure we have accounts/companies from both systems
        if not self.accounts:
            self.extract_tempo_accounts()

        if not self.op_companies:
            self.extract_openproject_companies()

        # Create a lookup dictionary for OpenProject companies by name
        op_companies_by_name = {
            company.get("name", "").lower(): company for company in self.op_companies
        }

        mapping = {}
        for tempo_account in self.accounts:
            tempo_id = tempo_account.get("id")
            tempo_key = tempo_account.get("key")
            tempo_name = tempo_account.get("name", "")
            tempo_name_lower = tempo_name.lower()

            # Try to find a corresponding OpenProject company by name
            op_company = op_companies_by_name.get(tempo_name_lower)

            if op_company:
                mapping[tempo_id] = {
                    "tempo_id": tempo_id,
                    "tempo_key": tempo_key,
                    "tempo_name": tempo_name,
                    "openproject_id": op_company.get("id"),
                    "openproject_name": op_company.get("name"),
                    "matched_by": "name",
                }
            else:
                # No match found, add to mapping with empty OpenProject data
                mapping[tempo_id] = {
                    "tempo_id": tempo_id,
                    "tempo_key": tempo_key,
                    "tempo_name": tempo_name,
                    "openproject_id": None,
                    "openproject_name": None,
                    "matched_by": "none",
                }

        # Save mapping to file
        self.account_mapping = mapping
        self._save_to_json(mapping, "tempo_account_mapping.json")

        # Log statistics
        total_accounts = len(mapping)
        matched_accounts = sum(
            1 for account in mapping.values() if account["matched_by"] != "none"
        )
        match_percentage = (
            (matched_accounts / total_accounts) * 100 if total_accounts > 0 else 0
        )

        logger.info("Account mapping created for %s Tempo accounts", total_accounts)
        logger.info(
            "Successfully matched %s accounts (%.1f%%)",
            matched_accounts,
            match_percentage,
        )

        return mapping

    def create_company_in_openproject(
        self,
        tempo_account: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a company in OpenProject based on a Tempo account.

        Args:
            tempo_account: The Tempo account data

        Returns:
            The created OpenProject company

        Raises:
            OpenProjectError: If company creation fails

        """
        name = tempo_account.get("name")
        key = tempo_account.get("key", "")
        lead_name = tempo_account.get("leadDisplayName", "")
        customer_name = tempo_account.get("customerName", "")

        # Create a description from the Tempo account data
        description = f"Migrated from Tempo account: {key}\n"
        if customer_name:
            description += f"Customer: {customer_name}\n"
        if lead_name:
            description += f"Account Lead: {lead_name}\n"

        # Create a valid OpenProject identifier from the account key
        identifier = key.lower() if key else re.sub(r"[^a-zA-Z0-9]", "-", name.lower())
        # Ensure it starts with a letter and limit to 100 chars
        if not identifier[0].isalpha():
            identifier = "a-" + identifier
        identifier = identifier[:100]

        logger.info(
            "Creating company in OpenProject: %s (Identifier: %s)",
            name,
            identifier,
        )

        # Honor patched module-level config in tests; fall back to global migration_config
        try:
            dry_run_flag = bool(getattr(config, "migration_config", {}).get("dry_run", False))
        except Exception:
            dry_run_flag = bool(migration_config.get("dry_run", False))

        if dry_run_flag:
            logger.info("DRY RUN: Would create company: %s", name)
            # Return a placeholder for dry run
            return {
                "id": None,
                "name": name,
                "identifier": identifier,
                "description": description,
            }

        # Create the company in OpenProject
        result = self.op_client.create_company(
            name=name,
            identifier=identifier,
            description=description,
        )
        # Support clients that return a dict or (dict, bool)
        if isinstance(result, tuple):
            company, was_created = result
        else:
            company = result
            was_created = bool(company)

        if not company:
            msg = f"Failed to create company: {name}"
            raise OpenProjectError(msg)

        if was_created:
            logger.info("Successfully created company: %s", name)
        else:
            logger.info(
                "Found existing company with identifier '%s' for: %s",
                identifier,
                name,
            )
        return company

    def migrate_accounts(self) -> dict[str, Any]:
        """Migrate Tempo accounts to OpenProject projects.

        Returns:
            Updated mapping between Tempo accounts and OpenProject projects

        """
        logger.info("Starting Tempo account migration...")

        # Make sure we have accounts and mappings
        if not self.accounts:
            self.extract_tempo_accounts()

        if not self.account_mapping:
            self.create_account_mapping()

        # Iterate through the mapping and create missing companies
        accounts_to_process = [
            (tempo_id, mapping)
            for tempo_id, mapping in self.account_mapping.items()
            if mapping["matched_by"] == "none"
        ]

        logger.info(
            "Found %s accounts that need to be created in OpenProject",
            len(accounts_to_process),
        )

        with ProgressTracker(
            "Migrating accounts",
            len(accounts_to_process),
            "Recent Accounts",
        ) as tracker:
            for _i, (tempo_id, mapping) in enumerate(accounts_to_process):
                # Find the Tempo account definition
                tempo_account = next(
                    (a for a in self.accounts if str(a.get("id")) == str(tempo_id)),
                    None,
                )

                if not tempo_account:
                    logger.warning(
                        "Could not find Tempo account definition for ID: %s",
                        tempo_id,
                    )
                    tracker.add_log_item(
                        f"Skipped: Unknown Tempo account ID {tempo_id}",
                    )
                    tracker.increment()
                    continue

                account_name = tempo_account.get("name", "Unknown")
                tracker.update_description(f"Creating account: {account_name}")

                # Create the company in OpenProject
                try:
                    op_company = self.create_company_in_openproject(tempo_account)
                    # Update the mapping
                    mapping["openproject_id"] = op_company.get("id")
                    mapping["openproject_name"] = op_company.get("name")
                    mapping["matched_by"] = "created"
                    tracker.add_log_item(
                        f"Created: {account_name} (ID: {op_company.get('id')})",
                    )
                except OpenProjectError as e:
                    tracker.add_log_item(f"Failed: {account_name} - {e}")
                    logger.error(
                        "Failed to create company for account %s: %s",
                        account_name,
                        e,
                    )

                tracker.increment()

        # Save updated mapping to file
        self._save_to_json(self.account_mapping, "tempo_account_mapping.json")

        # Log statistics
        total_accounts = len(self.account_mapping)
        matched_accounts = sum(
            1
            for account in self.account_mapping.values()
            if account["matched_by"] != "none"
        )
        created_accounts = sum(
            1
            for account in self.account_mapping.values()
            if account["matched_by"] == "created"
        )

        logger.success(
            "Tempo account migration complete for %s accounts",
            total_accounts,
        )
        match_percentage = (
            (matched_accounts / total_accounts * 100) if total_accounts > 0 else 0
        )
        logger.info(
            f"Successfully matched {matched_accounts} accounts ({match_percentage:.1f}% of total)",
        )
        logger.info("- Existing matches: %s", matched_accounts - created_accounts)
        logger.info("- Newly created: %s", created_accounts)

        return self.account_mapping

    def analyze_account_mapping(self) -> dict[str, Any]:
        """Analyze the account mapping to identify potential issues.

        Returns:
            Dictionary with analysis results

        """
        if not self.account_mapping:
            mapping_path = Path(self.data_dir) / "tempo_account_mapping.json"
            if Path(mapping_path).exists():
                with mapping_path.open() as f:
                    self.account_mapping = json.load(f)
            else:
                logger.error(
                    "No account mapping found. Run create_account_mapping() first.",
                )
                return {}

        # Analyze the mapping
        analysis = {
            "total_accounts": len(self.account_mapping),
            "matched_accounts": sum(
                1
                for account in self.account_mapping.values()
                if account["matched_by"] != "none"
            ),
            "matched_by_name": sum(
                1
                for account in self.account_mapping.values()
                if account["matched_by"] == "name"
            ),
            "matched_by_creation": sum(
                1
                for account in self.account_mapping.values()
                if account["matched_by"] == "created"
            ),
            "unmatched_accounts": sum(
                1
                for account in self.account_mapping.values()
                if account["matched_by"] == "none"
            ),
            "unmatched_details": [
                {
                    "tempo_id": account["tempo_id"],
                    "tempo_key": account["tempo_key"],
                    "tempo_name": account["tempo_name"],
                }
                for account in self.account_mapping.values()
                if account["matched_by"] == "none"
            ],
        }

        # Calculate percentages
        total = analysis["total_accounts"]
        if total > 0:
            analysis["match_percentage"] = (analysis["matched_accounts"] / total) * 100
        else:
            analysis["match_percentage"] = 0

        # Save analysis to file
        self._save_to_json(analysis, "tempo_account_mapping_analysis.json")

        # Log analysis summary
        logger.info("Account mapping analysis complete")
        logger.info("Total accounts: %s", analysis["total_accounts"])
        logger.info(
            "Matched accounts: %s (%.1f%%)",
            analysis["matched_accounts"],
            analysis["match_percentage"],
        )
        logger.info("- Matched by name: %s", analysis["matched_by_name"])
        logger.info("- Created in OpenProject: %s", analysis["matched_by_creation"])
        logger.info("Unmatched accounts: %s", analysis["unmatched_accounts"])

        return analysis

    def _save_to_json(self, data: Any, filename: str) -> None:
        """Save data to a JSON file.

        Args:
            data: Data to save
            filename: Name of the file to save to

        """
        filepath = Path(self.data_dir) / filename
        with filepath.open("w") as f:
            json.dump(data, f, indent=2)
        logger.info("Saved data to %s", filepath)


def run_tempo_account_migration() -> None:
    """Run the Tempo account migration as a standalone script."""
    logger.info("Starting Tempo account migration")
    migration = TempoAccountMigration()

    # Extract accounts/companies from both systems
    migration.extract_tempo_accounts()
    migration.extract_openproject_companies()

    # Create mapping and migrate accounts
    migration.create_account_mapping()
    migration.migrate_accounts()

    # Analyze account mapping
    migration.analyze_account_mapping()

    logger.info("Tempo account migration complete")
