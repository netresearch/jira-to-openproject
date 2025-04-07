"""
Account migration module for Jira to OpenProject migration.
Handles the migration of Tempo timesheet accounts as custom fields in OpenProject.
"""

import os
import sys
import json
import re
import requests
from typing import Dict, List, Any, Optional, TYPE_CHECKING

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.clients.openproject_rails_client import OpenProjectRailsClient
from src import config
from src.display import ProgressTracker, console
from src.migrations.base_migration import BaseMigration

# Get logger from config
logger = config.logger

class AccountMigration(BaseMigration):
    """
    Handles the migration of accounts from Tempo timesheet to OpenProject.

    This class is responsible for:
    1. Extracting account information from Tempo timesheet in Jira
    2. Creating custom fields in OpenProject to store account information
    3. Mapping these accounts to be used later when creating Jira projects and work packages

    The approach is:
    - Tempo Company → OpenProject top-level project (created by company_migration.py)
    - Tempo Account → Custom field in OpenProject projects and work packages
    - Jira Project → OpenProject project with account information stored in custom fields
    """

    def __init__(
        self,
        jira_client: Optional[JiraClient] = None,
        op_client: Optional[OpenProjectClient] = None,
        op_rails_client: Optional['OpenProjectRailsClient'] = None,
    ) -> None:
        """
        Initialize the account migration process.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client
            op_rails_client: Initialized OpenProjectRailsClient instance
        """
        super().__init__(jira_client, op_client)

        self.op_rails_client = op_rails_client or OpenProjectRailsClient()
        self.tempo_accounts = []
        self.op_projects = []
        self.company_mapping = {}
        self.account_mapping = {}
        self.account_custom_field_id = None

        # Base Tempo API URL - typically {JIRA_URL}/rest/tempo-accounts/1 for Server
        self.tempo_api_base = f"{self.jira_client.jira_config.get('url', '').rstrip('/')}/rest/tempo-accounts/1"

        self.tempo_auth_headers = {
            "Authorization": f"Bearer {self.jira_client.jira_config.get('api_token', '')}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        self._load_data()

    def _load_data(self) -> None:
        """Load existing data from JSON files."""
        self.tempo_accounts = self._load_from_json("tempo_accounts.json", [])
        self.op_projects = self._load_from_json("openproject_projects.json", [])
        self.company_mapping = self._load_from_json("company_mapping.json", {})
        self.account_mapping = self._load_from_json("account_mapping.json", {})

        analysis_data = self._load_from_json("account_mapping_analysis.json", {})
        self.account_custom_field_id = analysis_data.get("custom_field_id")

        self.logger.info(f"Loaded {len(self.tempo_accounts)=} Tempo accounts")
        self.logger.info(f"Loaded {len(self.op_projects)=} OpenProject projects")
        self.logger.info(f"Loaded {len(self.company_mapping)=} company mappings")
        self.logger.info(f"Loaded {len(self.account_mapping)=} account mappings")
        if self.account_custom_field_id:
            self.logger.info(f"Loaded existing {self.account_custom_field_id=}")

    def load_company_mapping(self) -> Dict[str, Any]:
        """
        Load the company mapping created by the company migration.

        Returns:
            Dictionary mapping Tempo company IDs to OpenProject project IDs
        """
        mapping_path = os.path.join(self.data_dir, "company_mapping.json")

        if os.path.exists(mapping_path):
            with open(mapping_path, "r") as f:
                self.company_mapping = json.load(f)
            self.logger.info(f"Loaded company mapping with {len(self.company_mapping)} entries", extra={"markup": True})
            return self.company_mapping
        else:
            self.logger.warning("No company mapping found. Accounts will be created as top-level projects.", extra={"markup": True})
            return {}

    def extract_tempo_accounts(self) -> List[Dict[str, Any]]:
        """
        Extract account information from Tempo timesheet in Jira.

        Returns:
            List of Tempo account dictionaries
        """
        self.logger.info("Extracting accounts from Tempo timesheet...", extra={"markup": True})

        if not self.jira_client.connect():
            self.logger.error("Failed to connect to Jira", extra={"markup": True})
            return []

        try:
            # Use expand=true to get linked projects in the same request
            accounts_endpoint = f"{self.tempo_api_base}/account?expand=true"

            response = requests.get(
                accounts_endpoint,
                headers=self.tempo_auth_headers,
                verify=config.migration_config.get("ssl_verify", True),
            )

            if response.status_code == 200:
                accounts = response.json()
                self.logger.info(f"Retrieved {len(accounts)} Tempo accounts with expanded links", extra={"markup": True})

                for account in accounts:
                    account_links = account.get("links", [])
                    account["linked_projects"] = account_links

                self.tempo_accounts = accounts
                self._save_to_json(accounts, "tempo_accounts.json")

                return accounts
            else:
                self.logger.error(
                    f"Failed to get Tempo accounts. Status code: {response.status_code}, Response: {response.text}",
                    extra={"markup": True}
                )
                return []
        except Exception as e:
            self.logger.error(f"Failed to extract Tempo accounts: {str(e)}", extra={"markup": True})
            return []

    def extract_openproject_projects(self) -> List[Dict[str, Any]]:
        """
        Extract projects from OpenProject.

        Returns:
            List of OpenProject project dictionaries
        """
        self.logger.info("Extracting projects from OpenProject...", extra={"markup": True})

        try:
            self.op_projects = self.op_client.get_projects()
        except Exception as e:
            self.logger.warning(f"Failed to get projects from OpenProject: {str(e)}", extra={"markup": True})
            self.logger.warning("Using an empty list of projects for OpenProject", extra={"markup": True})
            self.op_projects = []

        self.logger.info(f"Extracted {len(self.op_projects)} projects from OpenProject", extra={"markup": True})

        self._save_to_json(self.op_projects, "openproject_projects.json")

        return self.op_projects

    def create_account_mapping(self) -> Dict[str, Any]:
        """
        Create a mapping between Tempo accounts and OpenProject sub-projects.

        Returns:
            Dictionary mapping Tempo account IDs to OpenProject project IDs
        """
        self.logger.info("Creating account mapping...", extra={"markup": True})

        if not self.tempo_accounts:
            self.extract_tempo_accounts()

        if not self.op_projects:
            self.extract_openproject_projects()

        op_projects_by_name = {
            project.get("name", "").lower(): project for project in self.op_projects
        }

        jira_project_mapping = self._load_from_json("jira_project_mapping.json", {})

        mapping = {}
        for tempo_account in self.tempo_accounts:
            tempo_id = tempo_account.get("id")
            tempo_key = tempo_account.get("key")
            tempo_name = tempo_account.get("name", "")
            tempo_name_lower = tempo_name.lower()
            company_id = tempo_account.get("companyId")
            default_project = tempo_account.get("default_project", {})
            default_project_key = default_project.get("key")

            op_project = op_projects_by_name.get(tempo_name_lower, None)
            match_method = "name" if op_project else "none"

            if not op_project and default_project_key and default_project_key in jira_project_mapping:
                op_project_id = jira_project_mapping[default_project_key].get("openproject_id")
                if op_project_id:
                    op_project = next((p for p in self.op_projects if str(p.get("id")) == str(op_project_id)), None)
                    match_method = "default_project"

            if op_project:
                mapping[tempo_id] = {
                    "tempo_id": tempo_id,
                    "tempo_key": tempo_key,
                    "tempo_name": tempo_name,
                    "company_id": company_id,
                    "default_project_key": default_project_key,
                    "openproject_id": op_project.get("id"),
                    "openproject_identifier": op_project.get("identifier"),
                    "openproject_name": op_project.get("name"),
                    "parent_id": op_project.get("_links", {}).get("parent", {}).get("href", "").split("/")[-1]
                    if op_project.get("_links", {}).get("parent", {}).get("href") else None,
                    "matched_by": match_method,
                }
            else:
                mapping[tempo_id] = {
                    "tempo_id": tempo_id,
                    "tempo_key": tempo_key,
                    "tempo_name": tempo_name,
                    "company_id": company_id,
                    "default_project_key": default_project_key,
                    "openproject_id": None,
                    "openproject_identifier": None,
                    "openproject_name": None,
                    "parent_id": None,
                    "matched_by": "none",
                }

        self.account_mapping = mapping
        self._save_to_json(mapping, "account_mapping.json")

        total_accounts = len(mapping)
        matched_accounts = sum(
            1 for account in mapping.values() if account["matched_by"] != "none"
        )
        name_matched = sum(
            1 for account in mapping.values() if account["matched_by"] == "name"
        )
        default_project_matched = sum(
            1 for account in mapping.values() if account["matched_by"] == "default_project"
        )
        match_percentage = (
            (matched_accounts / total_accounts) * 100 if total_accounts > 0 else 0
        )

        self.logger.info(f"Account mapping created for {total_accounts} accounts", extra={"markup": True})
        self.logger.info(
            f"Successfully matched {matched_accounts} accounts ({match_percentage:.1f}%)",
            extra={"markup": True}
        )
        if name_matched > 0:
            self.logger.info(f"  - {name_matched} accounts matched by name", extra={"markup": True})
        if default_project_matched > 0:
            self.logger.info(f"  - {default_project_matched} accounts matched by default project", extra={"markup": True})

        self.logger.info(
            f"Unmatched accounts: {total_accounts - matched_accounts} - these will be available in the custom field but not linked to projects",
            extra={"markup": True}
        )

        return mapping

    def create_account_custom_field(self) -> Optional[int]:
        """
        Create a custom field in OpenProject to store Tempo account information.

        Returns:
            ID of the created custom field or None if creation failed
        """
        self.logger.info("Ensuring 'Tempo Account' custom field exists in OpenProject...")

        if self.account_custom_field_id is not None:
            self.logger.info(f"Using pre-loaded custom field ID: {self.account_custom_field_id}")
            return self.account_custom_field_id

        if config.migration_config.get("dry_run", False):
            self.logger.info("DRY RUN: Would check/create custom field for Tempo accounts")
            self.account_custom_field_id = 9999  # Dummy ID for dry run
            return self.account_custom_field_id

        try:
            self.logger.info("Checking if 'Tempo Account' custom field exists via Rails...")
            existing_id = self.op_rails_client.get_custom_field_id_by_name('Tempo Account') # type: ignore

            if existing_id is not None:
                self.logger.info(f"Custom field 'Tempo Account' already exists with ID: {existing_id}")
                self.account_custom_field_id = existing_id
                self._save_custom_field_id(existing_id)
                return existing_id

            self.logger.info("Creating 'Tempo Account' custom field via Rails client...")
            if not self.tempo_accounts:
                self.extract_tempo_accounts()
            possible_values = [acc.get("name") for acc in self.tempo_accounts if acc.get("name")]

            create_command = f"""
            cf = CustomField.new(
              name: 'Tempo Account',
              field_format: 'list',
              is_required: false,
              searchable: true,
              editable: true,
              visible: true,
              type: 'WorkPackageCustomField',
              possible_values: {json.dumps(possible_values)},
              description: 'Account from Tempo timesheet in Jira'
            )
            cf.save!
            cf.id
            """

            result = self.op_rails_client.execute(create_command) # type: ignore

            if result['status'] == 'success' and result['output'] is not None:
                new_id = result['output']
                self.logger.success(f"Successfully created 'Tempo Account' custom field with ID: {new_id}", extra={"markup": True})
                self.account_custom_field_id = new_id

                self.logger.info("Making custom field available for all work package types...", extra={"markup": True})
                activate_command = f"""
                cf = CustomField.find({new_id})
                cf.is_for_all = true
                cf.save!
                Type.all.each do |type|
                  type.custom_fields << cf unless type.custom_fields.include?(cf)
                  type.save!
                end
                true
                """

                activate_result = self.op_rails_client.execute(activate_command) # type: ignore
                if activate_result['status'] == 'success':
                    self.logger.success("Custom field activated for all work package types", extra={"markup": True})
                else:
                    self.logger.warning(f"Failed to activate custom field for all types: {activate_result.get('error')}", extra={"markup": True})

                return new_id
            else:
                error = result.get('error', 'Unknown error')
                self.logger.error(f"Failed to create 'Tempo Account' custom field: {error}", extra={"markup": True})
                return None
        except Exception as e:
            self.logger.error(f"Error creating custom field: {str(e)}", extra={"markup": True})
            return None

    def migrate_accounts(self) -> Dict[str, Any]:
        """
        Migrate Tempo accounts to OpenProject as custom field values.

        Returns:
            Updated mapping between Tempo accounts and OpenProject custom field values
        """
        self.logger.info("Starting account migration...", extra={"markup": True})

        if not self.tempo_accounts:
            self.extract_tempo_accounts()

        if not self.account_custom_field_id:
            self.create_account_custom_field()

        if not self.account_mapping:
            self.create_account_mapping()

        accounts_to_process = list(self.account_mapping.values())
        total_accounts = len(accounts_to_process)
        matched_accounts = 0

        with ProgressTracker("Migrating accounts", total_accounts, "Recent Accounts") as tracker:
            for i, account in enumerate(accounts_to_process):
                tempo_name = account["tempo_name"]
                tracker.update_description(f"Processing account: {tempo_name}")

                if isinstance(self.account_custom_field_id, str) and self.account_custom_field_id.strip().isdigit():
                    self.account_custom_field_id = int(self.account_custom_field_id.strip())

                account["custom_field_id"] = self.account_custom_field_id

                if account["openproject_id"] is not None:
                    matched_accounts += 1
                    tracker.add_log_item(f"Matched: {tempo_name} to project ID {account['openproject_id']}")
                else:
                    tracker.add_log_item(f"Unmatched: {tempo_name}")

                tracker.increment()

        self._save_to_json(self.account_mapping, "account_mapping.json")

        self.logger.info(f"Account migration complete: {total_accounts} accounts added to custom field", extra={"markup": True})
        self.logger.info(
            f"Found matches for {matched_accounts} accounts ({matched_accounts / total_accounts * 100:.1f}% of total)",
            extra={"markup": True}
        )
        self.logger.info(
            f"{total_accounts - matched_accounts} accounts were added to the custom field but not linked to any existing project",
            extra={"markup": True}
        )

        if config.migration_config.get("dry_run", False):
            self.logger.info("DRY RUN: No custom fields were actually created or updated in OpenProject", extra={"markup": True})

        return self.account_mapping

    def analyze_account_mapping(self) -> Dict[str, Any]:
        """
        Analyze the account mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        if not self.account_mapping:
            mapping_path = os.path.join(self.data_dir, "account_mapping.json")
            if os.path.exists(mapping_path):
                with open(mapping_path, "r") as f:
                    self.account_mapping = json.load(f)
            else:
                self.logger.error(
                    "No account mapping found. Run create_account_mapping() first.",
                    extra={"markup": True}
                )
                return {}

        analysis = {
            "total_accounts": len(self.account_mapping),
            "matched_accounts": sum(
                1
                for account in self.account_mapping.values()
                if account.get("custom_field_id") is not None
            ),
            "custom_field_id": self.account_custom_field_id,
            "unmatched_accounts": sum(
                1
                for account in self.account_mapping.values()
                if account.get("custom_field_id") is None
            ),
            "unmatched_details": [
                {
                    "tempo_id": account["tempo_id"],
                    "tempo_key": account["tempo_key"],
                    "tempo_name": account["tempo_name"],
                    "company_id": account.get("company_id"),
                    "default_project_key": account.get("default_project_key")
                }
                for account in self.account_mapping.values()
                if account.get("custom_field_id") is None
            ],
        }

        analysis["match_methods"] = {
            "name": sum(1 for account in self.account_mapping.values() if account.get("matched_by") == "name"),
            "default_project": sum(1 for account in self.account_mapping.values() if account.get("matched_by") == "default_project"),
            "none": sum(1 for account in self.account_mapping.values() if account.get("matched_by") == "none")
        }

        total = analysis["total_accounts"]
        if total > 0:
            analysis["match_percentage"] = (analysis["matched_accounts"] / total) * 100
        else:
            analysis["match_percentage"] = 0

        analysis["summary"] = {
            "total_tempo_accounts": len(self.tempo_accounts),
            "total_openproject_projects": len(self.op_projects),
            "accounts_matched_to_projects": sum(1 for account in self.account_mapping.values() if account.get("openproject_id") is not None),
            "accounts_without_project_match": sum(1 for account in self.account_mapping.values() if account.get("openproject_id") is None),
            "custom_field_created": self.account_custom_field_id is not None,
            "custom_field_id": self.account_custom_field_id
        }

        self._save_to_json(analysis, "account_mapping_analysis.json")

        self.logger.info("=== Account Migration Summary ===", extra={"markup": True})
        self.logger.info(f"Total Tempo accounts processed: {analysis['summary']['total_tempo_accounts']}", extra={"markup": True})
        self.logger.info(f"OpenProject projects available: {analysis['summary']['total_openproject_projects']}", extra={"markup": True})
        self.logger.info(f"Accounts matched to projects: {analysis['summary']['accounts_matched_to_projects']} ({analysis['match_percentage']:.1f}%)", extra={"markup": True})
        if analysis["match_methods"]["name"] > 0:
            self.logger.info(f"  - Matched by name: {analysis['match_methods']['name']}", extra={"markup": True})
        if analysis["match_methods"]["default_project"] > 0:
            self.logger.info(f"  - Matched by default project: {analysis['match_methods']['default_project']}", extra={"markup": True})
        self.logger.info(f"Accounts added to custom field but not matched to projects: {analysis['summary']['accounts_without_project_match']}", extra={"markup": True})
        self.logger.info(f"Custom field ID in OpenProject: {analysis['summary']['custom_field_id']}", extra={"markup": True})
        self.logger.info("=====================", extra={"markup": True})

        return analysis

    def _save_custom_field_id(self, cf_id: int) -> None:
        """Save the custom field ID, typically within the analysis file."""
        analysis_data = self._load_from_json("account_mapping_analysis.json", {})
        analysis_data["custom_field_id"] = cf_id
        self._save_to_json(analysis_data, "account_mapping_analysis.json")
        self.logger.debug(f"Saved account custom field ID ({cf_id}) to analysis file.")
