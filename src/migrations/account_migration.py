"""
Account migration module for Jira to OpenProject migration.
Handles the migration of Tempo timesheet accounts as custom fields in OpenProject.
"""

import os
import sys
import json
from typing import Dict, List, Any, Optional

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.clients.openproject_rails_client import OpenProjectRailsClient
from src import config
from src.display import ProgressTracker
from src.migrations.base_migration import BaseMigration

# Constants for filenames
ACCOUNT_MAPPING_FILE = "account_mapping.json"
TEMPO_ACCOUNTS_FILE = "tempo_accounts.json"
OP_PROJECTS_FILE = "openproject_projects.json"
COMPANY_MAPPING_FILE = "company_mapping.json"

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
        jira_client: 'JiraClient',
        op_client: 'OpenProjectClient',
        op_rails_client: Optional['OpenProjectRailsClient'] = None,
    ):
        """
        Initialize the account migration tools.

        Args:
            jira_client: Initialized Jira client instance.
            op_client: Initialized OpenProject client instance.
            op_rails_client: Optional instance of OpenProjectRailsClient for direct migration.
        """
        super().__init__(jira_client, op_client, op_rails_client)
        self.tempo_accounts = []
        self.op_projects = []
        self.account_mapping = {}
        self.company_mapping = {}
        self._created_accounts = 0

        self.account_custom_field_id = None

        # Load existing data if available
        self.tempo_accounts = self._load_from_json(TEMPO_ACCOUNTS_FILE) or []
        self.op_projects = self._load_from_json(OP_PROJECTS_FILE) or []
        self.account_mapping = self._load_from_json(ACCOUNT_MAPPING_FILE) or {}

    def _load_data(self) -> None:
        """Load existing data from JSON files."""
        self.tempo_accounts = self._load_from_json(TEMPO_ACCOUNTS_FILE) or []
        self.op_projects = self._load_from_json(OP_PROJECTS_FILE) or []
        self.company_mapping = self._load_from_json(COMPANY_MAPPING_FILE) or {}
        self.account_mapping = self._load_from_json(ACCOUNT_MAPPING_FILE) or {}

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
        mapping_path = os.path.join(self.data_dir, COMPANY_MAPPING_FILE)

        if os.path.exists(mapping_path):
            with open(mapping_path, "r") as f:
                self.company_mapping = json.load(f)
            self.logger.info(f"Loaded company mapping with {len(self.company_mapping)} entries", extra={"markup": True})
            return self.company_mapping
        else:
            self.logger.warning("No company mapping found. Accounts will be created as top-level projects.", extra={"markup": True})
            return {}

    def extract_tempo_accounts(self, force: bool = False) -> List[Dict[str, Any]]:
        """
        Extracts Tempo accounts using the JiraClient.

        Args:
            force: If True, re-extract data even if it exists locally.

        Returns:
            List of Tempo account dictionaries.
        """
        if self.tempo_accounts and not force and not config.migration_config.get("force", False):
            self.logger.info(f"Using cached Tempo accounts from {TEMPO_ACCOUNTS_FILE}")
            return self.tempo_accounts

        self.logger.info("Extracting Tempo accounts...")

        try:
            # Get accounts from Tempo API
            accounts = self.jira_client.get_tempo_accounts(expand=True)

            if accounts is not None:
                self.logger.info(f"Retrieved {len(accounts)} Tempo accounts")
                self.tempo_accounts = accounts
                self._save_to_json(accounts, TEMPO_ACCOUNTS_FILE)
                return accounts
            else:
                self.logger.error("Failed to retrieve Tempo accounts using JiraClient.")
                return []

        except Exception as e:
            self.logger.error(f"Failed to extract Tempo accounts: {str(e)}", exc_info=True)
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

        self._save_to_json(self.op_projects, OP_PROJECTS_FILE)

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
        self._save_to_json(mapping, ACCOUNT_MAPPING_FILE)

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

        self._save_to_json(self.account_mapping, ACCOUNT_MAPPING_FILE)

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
            mapping_path = os.path.join(self.data_dir, ACCOUNT_MAPPING_FILE)
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
        """
        Save the custom field ID to the analysis file.

        Args:
            cf_id: The custom field ID to save
        """
        analysis = self._load_from_json("account_mapping_analysis.json", {})
        analysis["custom_field_id"] = cf_id
        self._save_to_json(analysis, "account_mapping_analysis.json")
        self.account_custom_field_id = cf_id

    def run(self, dry_run: bool = False, force: bool = False, mappings=None) -> Dict[str, Any]:
        """
        Run the account migration process.

        Args:
            dry_run: If True, don't actually create or modify accounts in OpenProject
            force: If True, force extraction of data even if it already exists
            mappings: Optional mappings object (not used in this migration)

        Returns:
            Dictionary with migration results
        """
        self.logger.info("Starting account migration", extra={"markup": True})

        try:
            # Load existing data
            self._load_data()

            # Load company mapping (dependency)
            company_mapping = self.load_company_mapping()
            if not company_mapping:
                self.logger.warning("No company mapping found. Run company migration first.", extra={"markup": True})

            # Extract data
            tempo_accounts = self.extract_tempo_accounts(force=force)
            op_projects = self.extract_openproject_projects()

            # Create mapping
            mapping = self.create_account_mapping()

            # Create custom field and migrate accounts if not in dry run mode
            if not dry_run:
                # Create custom field
                custom_field_id = self.create_account_custom_field()
                if custom_field_id:
                    self.logger.success(f"Created/found Tempo account custom field with ID: {custom_field_id}", extra={"markup": True})
                else:
                    self.logger.error("Failed to create Tempo account custom field", extra={"markup": True})

                # Migrate accounts
                result = self.migrate_accounts()
            else:
                self.logger.warning("Dry run mode - not creating Tempo account custom field or accounts", extra={"markup": True})
                result = {
                    "status": "success",
                    "matched_count": sum(1 for account in mapping.values() if account["matched_by"] != "none"),
                    "created_count": 0,
                    "skipped_count": 0,
                    "failed_count": 0
                }

            # Analyze results
            analysis = self.analyze_account_mapping()

            return {
                "status": result.get("status", "success"),
                "success_count": result.get("matched_count", 0) + result.get("created_count", 0),
                "failed_count": result.get("failed_count", 0),
                "total_count": len(tempo_accounts),
                "tempo_accounts_count": len(tempo_accounts),
                "op_projects_count": len(op_projects),
                "mapped_accounts_count": len(mapping),
                "custom_field_id": self.account_custom_field_id,
                "analysis": analysis
            }
        except Exception as e:
            self.logger.error(f"Error during account migration: {str(e)}", extra={"markup": True, "traceback": True})
            return {
                "status": "failed",
                "error": str(e),
                "success_count": 0,
                "failed_count": len(self.tempo_accounts) if self.tempo_accounts else 0,
                "total_count": len(self.tempo_accounts) if self.tempo_accounts else 0
            }
