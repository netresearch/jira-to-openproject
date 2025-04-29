"""
Tempo account migration module for Jira to OpenProject migration.
Handles the migration of Tempo Timesheet accounts from Jira to OpenProject.
"""

import json
import os
import re
from typing import Any

import requests

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import ProgressTracker

# Get logger from config
logger = config.logger
# Get batch size from migration config
batch_size = config.migration_config.get("batch_size", 1000)
# Get Jira connection settings
jira_url = config.jira_config.get("url")
jira_username = config.jira_config.get("username")
jira_api_token = config.jira_config.get("api_token")


class TempoAccountMigration:
    """
    Handles the migration of Tempo Timesheet accounts from Jira to OpenProject.

    This class is responsible for:
    1. Extracting Tempo account information from Jira
    2. Creating corresponding organizations/companies in OpenProject
    3. Mapping Tempo account data between the systems
    4. Migrating Tempo worklog data to OpenProject time entries
    """

    def __init__(self):
        """
        Initialize the Tempo account migration tools.
        """
        self.jira_client = JiraClient()
        self.op_client = OpenProjectClient()
        self.accounts = []
        self.account_mapping = {}
        self.custom_field_id = None

        # Use the centralized config for var directories
        self.data_dir = config.get_path("data")

        # Base Tempo API URL - typically {jira_url}/rest/tempo-accounts/1 for Server
        # or a separate endpoint for Cloud
        self.tempo_api_base = (
            f"{config.jira_config.get('url', '').rstrip('/')}/rest/tempo-accounts/1"
        )

        # Setup auth for Tempo API
        self.tempo_auth_headers = {
            "Authorization": f"Bearer {config.jira_config.get('api_token', '')}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def extract_tempo_accounts(self) -> list[dict[str, Any]]:
        """
        Extract Tempo account information from Jira.

        Returns:
            List of Tempo account dictionaries
        """
        logger.info("Extracting Tempo accounts from Jira...", extra={"markup": True})

        try:
            # Call the Tempo API to get all accounts
            accounts_endpoint = f"{self.tempo_api_base}/account"

            response = requests.get(
                accounts_endpoint,
                headers=self.tempo_auth_headers,
                verify=False,  # Only use in development
            )

            if response.status_code == 200:
                accounts = response.json()
                logger.info(
                    f"Retrieved {len(accounts)} Tempo accounts", extra={"markup": True}
                )

                # Save the accounts to a file
                self.accounts = accounts
                self._save_to_json(accounts, "tempo_accounts.json")

                return accounts
            else:
                logger.error(
                    f"Failed to get Tempo accounts. Status code: {response.status_code}, Response: {response.text}",
                    extra={"markup": True},
                )

                return []

        except Exception as e:
            logger.error(
                f"Failed to extract Tempo accounts: {str(e)}", extra={"markup": True}
            )

            return []

    def extract_openproject_companies(self) -> list[dict[str, Any]]:
        """
        Extract company information from OpenProject.

        Returns:
            List of company dictionaries
        """
        logger.info("Extracting companies from OpenProject...", extra={"markup": True})

        # Get companies from OpenProject
        try:
            self.op_companies = self.op_client.get_companies()
        except Exception as e:
            logger.warning(
                f"Failed to get companies from OpenProject: {str(e)}",
                extra={"markup": True},
            )
            logger.warning(
                "Using an empty list of companies for OpenProject",
                extra={"markup": True},
            )
            self.op_companies = []

        # Log the number of companies found
        logger.info(
            f"Extracted {len(self.op_companies)} companies from OpenProject",
            extra={"markup": True},
        )

        # Save companies to file for later reference
        self._save_to_json(self.op_companies, "openproject_companies.json")

        return self.op_companies

    def create_account_mapping(self) -> dict[str, Any]:
        """
        Create a mapping between Tempo accounts and OpenProject companies.

        This method creates a mapping based on account names.

        Returns:
            Dictionary mapping Tempo account IDs to OpenProject company IDs
        """
        logger.info(
            "Creating Tempo account to OpenProject company mapping...",
            extra={"markup": True},
        )

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
            op_company = op_companies_by_name.get(tempo_name_lower, None)

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

        logger.info(
            f"Account mapping created for {total_accounts} Tempo accounts",
            extra={"markup": True},
        )
        logger.info(
            f"Successfully matched {matched_accounts} accounts ({match_percentage:.1f}%)",
            extra={"markup": True},
        )

        return mapping

    def create_company_in_openproject(
        self, tempo_account: dict[str, Any]
    ) -> dict[str, Any] | None:
        """
        Create a company in OpenProject based on a Tempo account.

        Args:
            tempo_account: The Tempo account data

        Returns:
            The created OpenProject company or None if creation failed
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
            f"Creating company in OpenProject: {name} (Identifier: {identifier})",
            extra={"markup": True},
        )

        if config.migration_config.get("dry_run", False):
            logger.info(
                f"DRY RUN: Would create company: {name}", extra={"markup": True}
            )
            # Return a placeholder for dry run
            return {
                "id": None,
                "name": name,
                "identifier": identifier,
                "description": description,
            }

        # Create the company in OpenProject
        try:
            company, was_created = self.op_client.create_company(
                name=name, identifier=identifier, description=description
            )

            if company:
                if was_created:
                    logger.info(
                        f"Successfully created company: {name}", extra={"markup": True}
                    )
                else:
                    logger.info(
                        f"Found existing company with identifier '{identifier}' for: {name}",
                        extra={"markup": True},
                    )
                return company
            else:
                logger.error(
                    f"Failed to create company: {name}", extra={"markup": True}
                )
                return None
        except Exception as e:
            logger.error(
                f"Error creating company {name}: {str(e)}", extra={"markup": True}
            )
            return None

    def migrate_accounts(self) -> dict[str, Any]:
        """
        Migrate Tempo accounts to OpenProject projects.

        Returns:
            Updated mapping between Tempo accounts and OpenProject projects
        """
        logger.info("Starting Tempo account migration...", extra={"markup": True})

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
            f"Found {len(accounts_to_process)} accounts that need to be created in OpenProject",
            extra={"markup": True},
        )

        with ProgressTracker(
            "Migrating accounts", len(accounts_to_process), "Recent Accounts"
        ) as tracker:
            for i, (tempo_id, mapping) in enumerate(accounts_to_process):
                # Find the Tempo account definition
                tempo_account = next(
                    (a for a in self.accounts if str(a.get("id")) == str(tempo_id)),
                    None,
                )

                if not tempo_account:
                    logger.warning(
                        f"Could not find Tempo account definition for ID: {tempo_id}",
                        extra={"markup": True},
                    )
                    tracker.add_log_item(
                        f"Skipped: Unknown Tempo account ID {tempo_id}"
                    )
                    tracker.increment()
                    continue

                account_name = tempo_account.get("name", "Unknown")
                tracker.update_description(f"Creating account: {account_name}")

                # Create the company in OpenProject
                op_company = self.create_company_in_openproject(tempo_account)

                if op_company:
                    # Update the mapping
                    mapping["openproject_id"] = op_company.get("id")
                    mapping["openproject_name"] = op_company.get("name")
                    mapping["matched_by"] = "created"
                    tracker.add_log_item(
                        f"Created: {account_name} (ID: {op_company.get('id')})"
                    )
                else:
                    tracker.add_log_item(f"Failed: {account_name}")

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
            f"Tempo account migration complete for {total_accounts} accounts",
            extra={"markup": True},
        )
        logger.info(
            f"Successfully matched {matched_accounts} accounts ({matched_accounts / total_accounts * 100:.1f}% of total)",
            extra={"markup": True},
        )
        logger.info(
            f"- Existing matches: {matched_accounts - created_accounts}",
            extra={"markup": True},
        )
        logger.info(f"- Newly created: {created_accounts}", extra={"markup": True})

        return self.account_mapping

    def analyze_account_mapping(self) -> dict[str, Any]:
        """
        Analyze the account mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        if not self.account_mapping:
            mapping_path = os.path.join(self.data_dir, "tempo_account_mapping.json")
            if os.path.exists(mapping_path):
                with open(mapping_path) as f:
                    self.account_mapping = json.load(f)
            else:
                logger.error(
                    "No account mapping found. Run create_account_mapping() first.",
                    extra={"markup": True},
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
        logger.info("Account mapping analysis complete", extra={"markup": True})
        logger.info(
            f"Total accounts: {analysis['total_accounts']}", extra={"markup": True}
        )
        logger.info(
            f"Matched accounts: {analysis['matched_accounts']} ({analysis['match_percentage']:.1f}%)",
            extra={"markup": True},
        )
        logger.info(
            f"- Matched by name: {analysis['matched_by_name']}", extra={"markup": True}
        )
        logger.info(
            f"- Created in OpenProject: {analysis['matched_by_creation']}",
            extra={"markup": True},
        )
        logger.info(
            f"Unmatched accounts: {analysis['unmatched_accounts']}",
            extra={"markup": True},
        )

        return analysis

    def _save_to_json(self, data: Any, filename: str) -> None:
        """
        Save data to a JSON file.

        Args:
            data: Data to save
            filename: Name of the file to save to
        """
        filepath = os.path.join(self.data_dir, filename)
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Saved data to {filepath}", extra={"markup": True})


def run_tempo_account_migration() -> None:
    """
    Run the Tempo account migration as a standalone script.
    """
    logger.info("Starting Tempo account migration", extra={"markup": True})
    migration = TempoAccountMigration()

    # Extract accounts/companies from both systems
    migration.extract_tempo_accounts()
    migration.extract_openproject_companies()

    # Create mapping and migrate accounts
    migration.create_account_mapping()
    migration.migrate_accounts()

    # Analyze account mapping
    migration.analyze_account_mapping()

    logger.info("Tempo account migration complete", extra={"markup": True})
