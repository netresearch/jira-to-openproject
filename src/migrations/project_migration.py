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
        self._created_projects = 0

        self.account_custom_field_id = None

        # Load existing data if available
        self.jira_projects = self._load_from_json(JIRA_PROJECTS_FILE) or []
        self.op_projects = self._load_from_json(OP_PROJECTS_FILE) or []
        self.project_mapping = self._load_from_json(PROJECT_MAPPING_FILE) or {}

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

    def create_project_in_openproject(
        self, jira_project: Dict[str, Any], account_id: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Create a project in OpenProject based on a Jira project.

        Args:
            jira_project: Jira project data
            account_id: Optional ID of the associated Tempo account

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
            }

        try:
            project_result, was_created = self.op_client.create_project(
                name=jira_name,
                identifier=identifier,
                description=jira_description
            )

            if not project_result:
                logger.error(f"Failed to create project: {jira_name}")
                return None

            if was_created:
                logger.info(f"Created new project: {jira_name} with identifier {identifier}")
            else:
                logger.info(f"Using existing project: {jira_name} with identifier {identifier}")

            if account_name and self.account_custom_field_id:
                cf_success = self.op_client.set_project_custom_field(
                    project_id=project_result.get("id"),
                    custom_field_id=self.account_custom_field_id,
                    value=account_name
                )

            if account_name:
                project_result["account_name"] = account_name

            return project_result
        except Exception as e:
            error_msg = str(e)
            if "422 Client Error" in error_msg and "Unprocessable Entity" in error_msg:
                logger.warning(f"Project {jira_name} may already exist with a different identifier")
            else:
                logger.error(f"Error creating project {jira_name}: {error_msg}")
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

            op_project = None
            if jira_name.lower() in op_projects_by_name:
                op_project = op_projects_by_name[jira_name.lower()]
            elif potential_identifier in op_projects_by_identifier:
                op_project = op_projects_by_identifier[potential_identifier]
            else:
                account_id = None
                if jira_key in self.project_account_mapping:
                    account_id = self.project_account_mapping[jira_key]

                op_project = self.create_project_in_openproject(jira_project, account_id)

            if op_project:
                mapping[jira_key] = {
                    "jira_key": jira_key,
                    "jira_name": jira_project.get("name"),
                    "openproject_id": op_project.get("id"),
                    "openproject_identifier": op_project.get("identifier"),
                    "openproject_name": op_project.get("name"),
                    "account_id": self.project_account_mapping.get(jira_key),
                    "account_name": op_project.get("account_name"),
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
                    "account_id": self.project_account_mapping.get(jira_key),
                    "account_name": None,
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
            "failed_projects": sum(
                1 for p in self.project_mapping.values() if p.get("status") == "failed"
            ),
            "failed_details": [
                {"jira_key": p.get("jira_key"), "jira_name": p.get("jira_name")}
                for p in self.project_mapping.values()
                if p.get("status") == "failed"
            ],
        }

        total = analysis["total_projects"]
        if total > 0:
            analysis["migration_percentage"] = (
                analysis["migrated_projects"] / total
            ) * 100
        else:
            analysis["migration_percentage"] = 0

        self._save_to_json(analysis, "project_mapping_analysis.json")

        logger.info(f"Project mapping analysis complete")
        logger.info(f"Total projects: {analysis['total_projects']}")
        logger.info(
            f"Migrated projects: {analysis['migrated_projects']} ({analysis['migration_percentage']:.1f}%)"
        )
        logger.info(f"- Newly created: {analysis['new_projects']}")
        logger.info(f"- Already existing: {analysis['existing_projects']}")
        logger.info(f"- With account information: {analysis['projects_with_accounts']}")
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

            # Extract project-account mapping
            project_account_mapping = self.extract_project_account_mapping(force=force)

            # Migrate projects if not in dry run mode
            if not dry_run:
                result = self.migrate_projects()
            else:
                self.logger.warning("Dry run mode - not creating projects", extra={"markup": True})
                # Count how many projects are already mapped
                mapped_count = sum(1 for proj in self.project_mapping.values() if proj.get("openproject_id"))
                result = {
                    "status": "success",
                    "created_count": 0,
                    "matched_count": mapped_count,
                    "skipped_count": 0,
                    "failed_count": 0,
                    "total_count": len(jira_projects)
                }

            # Analyze results
            analysis = self.analyze_project_mapping()

            return {
                "status": result.get("status", "success"),
                "success_count": result.get("created_count", 0) + result.get("matched_count", 0),
                "failed_count": result.get("failed_count", 0),
                "total_count": len(jira_projects),
                "jira_projects_count": len(jira_projects),
                "op_projects_count": len(op_projects),
                "mapped_projects_count": len(self.project_mapping),
                "analysis": analysis
            }
        except Exception as e:
            self.logger.error(f"Error during project migration: {str(e)}", extra={"markup": True, "traceback": True})
            return {
                "status": "failed",
                "error": str(e),
                "success_count": 0,
                "failed_count": len(self.jira_projects) if self.jira_projects else 0,
                "total_count": len(self.jira_projects) if self.jira_projects else 0
            }
