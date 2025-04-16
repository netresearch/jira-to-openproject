"""
Project migration module for Jira to OpenProject migration.
Handles the migration of projects and their hierarchies from Jira to OpenProject.
"""

import os
import sys
import json
import re
from typing import Dict, List, Any, Optional

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.clients.openproject_rails_client import OpenProjectRailsClient
from src import config
from src.display import process_with_progress
from src.migrations.base_migration import BaseMigration

# Get logger from config
logger = config.logger

# Constants for filenames
PROJECT_MAPPING_FILE = "project_mapping.json"
JIRA_PROJECTS_FILE = "jira_projects.json"
OP_PROJECTS_FILE = "openproject_projects.json"
ACCOUNT_MAPPING_FILE = "account_mapping.json"
PROJECT_ACCOUNT_MAPPING_FILE = "project_account_mapping.json"
TEMPO_ACCOUNTS_FILE = "tempo_accounts.json"
COMPANY_MAPPING_FILE = "company_mapping.json"


class ProjectMigration(BaseMigration):
    """
    Handles the migration of projects from Jira to OpenProject.

    This class is responsible for:
    1. Extracting projects from Jira
    2. Creating corresponding projects in OpenProject
    3. Setting account information as custom field values
    4. Creating a mapping between Jira and OpenProject project IDs for later use

    The structure created in OpenProject is:
    - Top-level projects representing Tempo companies (created by company_migration.py)
    - Jira projects as sub-projects under their respective Tempo company parent projects
    - Projects with account information stored in custom fields
    """

    def __init__(
        self,
        jira_client: 'JiraClient',
        op_client: 'OpenProjectClient',
        op_rails_client: Optional['OpenProjectRailsClient'] = None,
    ):
        """
        Initialize the project migration tools.

        Args:
            jira_client: Initialized Jira client instance.
            op_client: Initialized OpenProject client instance.
            op_rails_client: Optional instance of OpenProjectRailsClient for direct migration.
        """
        super().__init__(jira_client, op_client, op_rails_client)
        self.jira_projects = []
        self.op_projects = []
        self.project_mapping = {}
        self.account_mapping = {}
        self.project_account_mapping = {}
        self.company_mapping = {}
        self._created_projects = 0

        self.account_custom_field_id = None

        # Load existing data if available
        self.jira_projects = self._load_from_json(JIRA_PROJECTS_FILE) or []
        self.op_projects = self._load_from_json(OP_PROJECTS_FILE) or []
        self.project_mapping = self._load_from_json(PROJECT_MAPPING_FILE) or {}
        self.company_mapping = self._load_from_json(COMPANY_MAPPING_FILE) or {}

    def extract_jira_projects(self, force: bool = False) -> List[Dict[str, Any]]:
        """
        Extract projects from Jira.

        Returns:
            List of Jira projects
        """
        if not force and not config.migration_config.get("force", False):
             cached_projects = self._load_from_json(JIRA_PROJECTS_FILE, default=None)
             if cached_projects:
                 logger.info(f"Using cached Jira projects from {JIRA_PROJECTS_FILE}")
                 self.jira_projects = cached_projects
                 return self.jira_projects

        logger.info("Extracting projects from Jira...")

        self.jira_projects = self.jira_client.get_projects()

        logger.info(f"Extracted {len(self.jira_projects)} projects from Jira")

        self._save_to_json(self.jira_projects, JIRA_PROJECTS_FILE)

        return self.jira_projects

    def extract_openproject_projects(self, force: bool = False) -> List[Dict[str, Any]]:
        """
        Extract projects from OpenProject.

        Returns:
            List of OpenProject project dictionaries
        """
        if not force and not config.migration_config.get("force", False):
            cached_projects = self._load_from_json(OP_PROJECTS_FILE, default=None)
            if cached_projects:
                 logger.info(f"Using cached OpenProject projects from {OP_PROJECTS_FILE}")
                 self.op_projects = cached_projects
                 return self.op_projects

        logger.info("Extracting projects from OpenProject...")

        self.op_projects = self.op_client.get_projects()

        logger.info(f"Extracted {len(self.op_projects)} projects from OpenProject")

        self._save_to_json(self.op_projects, OP_PROJECTS_FILE)

        return self.op_projects

    def load_account_mapping(self) -> Dict[str, Any]:
        """
        Load the account mapping created by the account migration.

        Returns:
            Dictionary mapping Tempo account IDs to OpenProject custom field data
        """
        self.account_mapping = self._load_from_json(ACCOUNT_MAPPING_FILE, default={})
        if self.account_mapping:
            logger.info(f"Loaded account mapping with {len(self.account_mapping)} entries.")
            for account in self.account_mapping.values():
                if account.get("custom_field_id"):
                    self.account_custom_field_id = account.get("custom_field_id")
                    break

            logger.info(f"Account custom field ID: {self.account_custom_field_id}")
            return self.account_mapping
        else:
            logger.warning("No account mapping found. Account information won't be migrated.")
            return {}

    def load_company_mapping(self) -> Dict[str, Any]:
        """
        Load the company mapping created by the company migration.

        Returns:
            Dictionary mapping Tempo company IDs to OpenProject project IDs
        """
        self.company_mapping = self._load_from_json(COMPANY_MAPPING_FILE, default={})
        if self.company_mapping:
            company_count = len(self.company_mapping)
            matched_count = sum(1 for c in self.company_mapping.values() if c.get("openproject_id"))
            logger.info(f"Loaded company mapping with {company_count} entries, {matched_count} matched to OpenProject.")
            return self.company_mapping
        else:
            logger.warning("No company mapping found. Projects won't be organized hierarchically.")
            return {}

    def extract_project_account_mapping(self, force: bool = False) -> Dict[str, Any]:
        """
        Extract the mapping between Jira projects and Tempo accounts.

        Args:
            force: If True, re-extract data even if it exists locally.

        Returns:
            Dictionary mapping project keys to account IDs.
        """
        # Load existing data unless forced to refresh
        if self.project_account_mapping and not force and not config.migration_config.get("force", False):
            logger.info(f"Using cached project-account mapping from {PROJECT_ACCOUNT_MAPPING_FILE}")
            return self.project_account_mapping

        logger.info("Extracting project-account mapping...")

        # Load account mapping data
        self.load_account_mapping()

        if not self.jira_projects:
            logger.warning("No Jira projects found - extract Jira projects first")
            return {}

        # Get accounts for each project
        mapping = {}

        try:
            # Use expanded accounts data to get richer information
            accounts = self.jira_client.get_tempo_accounts(expand=True)

            if not accounts:
                logger.warning("No Tempo accounts found while creating project-account mapping")
                return {}

            # Build project-to-account mapping from account data
            for account in accounts:
                if "projects" not in account or not account["projects"]:
                    continue

                for project_item in account["projects"]:
                    project_key = project_item.get("key")

                    if not project_key:
                        continue

                    if project_key not in mapping:
                        mapping[project_key] = []

                    # Add this account to the project's list
                    account_data = {
                        "id": str(account.get("id")),
                        "key": account.get("key"),
                        "name": account.get("name"),
                    }
                    mapping[project_key].append(account_data)

            logger.info(f"Mapped {len(mapping)} projects to Tempo accounts")

            # Save the mapping
            self.project_account_mapping = mapping
            self._save_to_json(mapping, PROJECT_ACCOUNT_MAPPING_FILE)

            return mapping

        except Exception as e:
            logger.error(f"Error extracting project-account mapping: {str(e)}", exc_info=True)
            return {}

    def find_parent_company_for_project(self, jira_project: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Find the appropriate parent company for a Jira project.

        This looks for a Tempo company that should be the parent of this project
        based on project account associations.

        Args:
            jira_project: The Jira project to find a parent company for

        Returns:
            A dictionary with company information or None if no parent found
        """
        jira_key = jira_project.get("key")

        # Check if this project has associated accounts
        if not self.project_account_mapping or jira_key not in self.project_account_mapping:
            logger.debug(f"No account mapping found for project {jira_key}")
            return None

        # Get the associated accounts
        project_accounts = self.project_account_mapping.get(jira_key, [])
        if not project_accounts:
            logger.debug(f"Project {jira_key} has no associated accounts")
            return None

        # Handle case where project_accounts is an integer or string instead of a list
        if isinstance(project_accounts, (int, str)):
            account_id = str(project_accounts)
            # Search for a company with this account ID
            for company_id, company_data in self.company_mapping.items():
                if not company_data.get("openproject_id"):
                    continue
                # Match by account ID (simplified approach)
                if account_id == str(company_data.get("tempo_id", "")):
                    return company_data
            return None

        # Use the first account's company as the parent
        # Logic could be enhanced for multiple accounts if needed
        for account in project_accounts:
            account_id = account.get("id")
            account_name = account.get("name", "")
            account_key = account.get("key", "")

            # Search for a company with this account
            for company_id, company_data in self.company_mapping.items():
                if not company_data.get("openproject_id"):
                    continue

                # Match by account name or key (simplified approach - could be improved)
                if (account_key and account_key.startswith(company_data.get("tempo_key", ""))) or \
                   (account_name and account_name.startswith(company_data.get("tempo_name", ""))):
                    return company_data

        logger.debug(f"No parent company found for project {jira_key}")
        return None

    def create_project_in_openproject(
        self, jira_project: Dict[str, Any], account_id: Optional[int] = None,
        parent_id: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Create a project in OpenProject based on a Jira project.

        Args:
            jira_project: Jira project data
            account_id: Optional ID of the associated Tempo account
            parent_id: Optional ID of the parent project in OpenProject

        Returns:
            OpenProject project data or None if creation failed
        """
        jira_key = jira_project.get("key", "")
        jira_name = jira_project.get("name", "")
        jira_description = jira_project.get("description", "")

        identifier = re.sub(r"[^a-zA-Z0-9]", "-", jira_key.lower())
        if not identifier[0].isalpha():
            identifier = "p-" + identifier
        identifier = identifier[:100]

        account_name = None
        if account_id and str(account_id) in self.account_mapping:
            account_name = self.account_mapping[str(account_id)].get("tempo_name")

        if config.migration_config.get("dry_run", False):
            return {
                "id": None,
                "name": jira_name,
                "identifier": identifier,
                "jira_key": jira_key,
                "account_name": account_name,
                "parent_id": parent_id
            }

        # Check if project exists by name or identifier
        project = self.op_client.find_project_by_name_or_identifier(jira_name, identifier)

        if project:
            logger.info(f"Project '{jira_name}' already exists in OpenProject with ID {project.get('id')}")

            # Update with parent if needed
            if parent_id and not project.get("_links", {}).get("parent", {}).get("href"):
                logger.info(f"Updating project {jira_name} to set parent ID {parent_id}")
                try:
                    updated_project = self.op_client.update_project(
                        project.get("id"),
                        {"parent": parent_id}
                    )
                    if updated_project:
                        logger.info(f"Successfully updated parent for project {jira_name}")
                        project = updated_project
                except Exception as e:
                    logger.error(f"Failed to update parent for project {jira_name}: {str(e)}")

            # Add account_name to the project dict
            if account_name:
                project["account_name"] = account_name

            return project

        # Create project
        project_data = {
            "name": jira_name,
            "identifier": identifier,
            "description": {"raw": jira_description} if jira_description else {"raw": ""},
        }

        # Add parent if specified
        if parent_id:
            project_data["parent"] = parent_id
            logger.info(f"Setting parent project ID {parent_id} for {jira_name}")

        logger.info(f"Creating project '{jira_name}' in OpenProject")
        try:
            project = self.op_client.create_project(project_data)
            if project:
                self._created_projects += 1
                logger.info(f"Successfully created project '{jira_name}' with ID {project.get('id')}")

                # Add account_name to the project dict
                if account_name:
                    project["account_name"] = account_name

                # Add account custom field value if available
                if self.account_custom_field_id and account_name:
                    try:
                        self.op_client.update_project_custom_field(
                            project_id=project.get("id"),
                            custom_field_id=self.account_custom_field_id,
                            value=account_name,
                        )
                        logger.info(f"Added account '{account_name}' to project '{jira_name}'")
                    except Exception as e:
                        logger.error(f"Failed to set account for project {jira_name}: {str(e)}")
                return project
            else:
                logger.error(f"Failed to create project '{jira_name}' in OpenProject")
                return None
        except Exception as e:
            logger.error(f"Error creating project '{jira_name}': {str(e)}")
            return None

    def migrate_projects(self) -> Dict[str, Any]:
        """
        Migrate projects from Jira to OpenProject.

        Returns:
            Dictionary mapping Jira project keys to OpenProject project IDs
        """
        logger.info("Starting project migration...")

        if not self.jira_projects:
            self.extract_jira_projects()

        if not self.op_projects:
            self.extract_openproject_projects()

        if not self.account_mapping:
            self.load_account_mapping()

        if not self.project_account_mapping:
            self.extract_project_account_mapping()

        if not self.company_mapping:
            self.load_company_mapping()

        op_projects_by_name = {
            project.get("name", "").lower(): project for project in self.op_projects
        }
        op_projects_by_identifier = {
            project.get("identifier", "").lower(): project
            for project in self.op_projects
        }

        mapping = {}

        def process_project(jira_project, context):
            jira_key = jira_project.get("key")
            jira_name = jira_project.get("name", "")

            potential_identifier = re.sub(r"[^a-zA-Z0-9]", "-", jira_key.lower())
            if not potential_identifier[0].isalpha():
                potential_identifier = "p-" + potential_identifier
            potential_identifier = potential_identifier[:100]

            # Find existing project
            op_project = None
            if jira_name.lower() in op_projects_by_name:
                op_project = op_projects_by_name[jira_name.lower()]
            elif potential_identifier in op_projects_by_identifier:
                op_project = op_projects_by_identifier[potential_identifier]

            # Find parent company for hierarchical structure
            parent_company = None
            parent_id = None
            if not op_project:  # Only look for parent if creating a new project
                parent_company = self.find_parent_company_for_project(jira_project)
                if parent_company:
                    parent_id = parent_company.get("openproject_id")
                    logger.info(f"Found parent company for {jira_key}: {parent_company.get('tempo_name')} (ID: {parent_id})")

            # Get account ID if available
            account_id = None
            if jira_key in self.project_account_mapping:
                accounts = self.project_account_mapping[jira_key]
                # Handle case where accounts might be an integer instead of a list
                if isinstance(accounts, int) or isinstance(accounts, str):
                    account_id = accounts
                elif isinstance(accounts, list) and len(accounts) > 0:
                    account_id = accounts[0].get("id")

            # Create or update project
            if not op_project:
                op_project = self.create_project_in_openproject(
                    jira_project,
                    account_id=account_id,
                    parent_id=parent_id
                )

            if op_project:
                mapping[jira_key] = {
                    "jira_key": jira_key,
                    "jira_name": jira_project.get("name"),
                    "openproject_id": op_project.get("id"),
                    "openproject_identifier": op_project.get("identifier"),
                    "openproject_name": op_project.get("name"),
                    "account_id": account_id,
                    "account_name": op_project.get("account_name"),
                    "parent_id": parent_id,
                    "parent_name": parent_company.get("tempo_name") if parent_company else None,
                    "created_new": op_project.get("id")
                    not in [p.get("id") for p in self.op_projects],
                }

                if op_project.get("id") not in [p.get("id") for p in self.op_projects]:
                    return jira_name
            else:
                mapping[jira_key] = {
                    "jira_key": jira_key,
                    "jira_name": jira_project.get("name"),
                    "openproject_id": None,
                    "openproject_identifier": None,
                    "openproject_name": None,
                    "account_id": account_id,
                    "account_name": None,
                    "parent_id": parent_id,
                    "parent_name": parent_company.get("tempo_name") if parent_company else None,
                    "created_new": False,
                    "failed": True,
                }

            return None

        process_with_progress(
            items=self.jira_projects,
            process_func=process_project,
            description="Migrating projects",
            log_title="Projects Being Created",
            item_name_func=lambda project: project.get("name", "Unknown")
        )

        self.project_mapping = mapping
        self._save_to_json(mapping, PROJECT_MAPPING_FILE)

        analysis = self.analyze_project_mapping()

        if config.migration_config.get("dry_run", False):
            logger.info("DRY RUN: No projects were actually created in OpenProject")

        return mapping

    def analyze_project_mapping(self) -> Dict[str, Any]:
        """
        Analyze the project mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        if not self.project_mapping:
            if os.path.exists(os.path.join(self.data_dir, PROJECT_MAPPING_FILE)):
                with open(
                    os.path.join(self.data_dir, PROJECT_MAPPING_FILE), "r"
                ) as f:
                    self.project_mapping = json.load(f)
            else:
                logger.error("No project mapping found. Run migrate_projects() first.")
                return {}

        analysis = {
            "total_projects": len(self.project_mapping),
            "migrated_projects": sum(
                1
                for p in self.project_mapping.values()
                if p.get("openproject_id") is not None
            ),
            "new_projects": sum(
                1 for p in self.project_mapping.values() if p.get("created_new", False)
            ),
            "existing_projects": sum(
                1
                for p in self.project_mapping.values()
                if p.get("openproject_id") is not None
                and not p.get("created_new", False)
            ),
            "projects_with_accounts": sum(
                1
                for p in self.project_mapping.values()
                if p.get("account_name") is not None
            ),
            "projects_with_parent": sum(
                1
                for p in self.project_mapping.values()
                if p.get("parent_id") is not None
            ),
            "failed_projects": sum(
                1 for p in self.project_mapping.values() if p.get("failed", False)
            ),
            "failed_details": [
                {"jira_key": p.get("jira_key"), "jira_name": p.get("jira_name")}
                for p in self.project_mapping.values()
                if p.get("failed", False)
            ],
        }

        total = analysis["total_projects"]
        if total > 0:
            analysis["migration_percentage"] = (
                analysis["migrated_projects"] / total
            ) * 100
            analysis["hierarchical_percentage"] = (
                analysis["projects_with_parent"] / total
            ) * 100
        else:
            analysis["migration_percentage"] = 0
            analysis["hierarchical_percentage"] = 0

        self._save_to_json(analysis, "project_mapping_analysis.json")

        logger.info(f"Project mapping analysis complete")
        logger.info(f"Total projects: {analysis['total_projects']}")
        logger.info(
            f"Migrated projects: {analysis['migrated_projects']} ({analysis['migration_percentage']:.1f}%)"
        )
        logger.info(f"- Newly created: {analysis['new_projects']}")
        logger.info(f"- Already existing: {analysis['existing_projects']}")
        logger.info(f"- With account information: {analysis['projects_with_accounts']}")
        logger.info(f"- With parent company: {analysis['projects_with_parent']} ({analysis['hierarchical_percentage']:.1f}%)")
        logger.info(f"Failed projects: {analysis['failed_projects']}")

        return analysis

    def run(self, dry_run: bool = False, force: bool = False, mappings=None) -> Dict[str, Any]:
        """
        Run the project migration process.

        Args:
            dry_run: If True, don't actually create projects in OpenProject
            force: If True, force extraction of data even if it already exists
            mappings: Optional mappings object (not used in this migration)

        Returns:
            Dictionary with migration results
        """
        self.logger.info("Starting project migration", extra={"markup": True})

        try:
            # Extract data
            jira_projects = self.extract_jira_projects(force=force)
            op_projects = self.extract_openproject_projects(force=force)

            # Load account mapping (dependency)
            account_mapping = self.load_account_mapping()

            # Load company mapping (dependency for hierarchy)
            company_mapping = self.load_company_mapping()

            # Extract project-account mapping
            project_account_mapping = self.extract_project_account_mapping(force=force)

            # Migrate projects if not in dry run mode
            if not dry_run:
                result = self.migrate_projects()
            else:
                self.logger.warning("Dry run mode - not creating projects", extra={"markup": True})
                result = self.analyze_project_mapping()

            self.logger.info("Project migration completed", extra={"markup": True})
            return result

        except Exception as e:
            self.logger.error(f"Error in project migration: {str(e)}", exc_info=True)
            return {
                "error": str(e),
                "projects_migrated": self._created_projects,
            }
