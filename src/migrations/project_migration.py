"""Project migration module for Jira to OpenProject migration.
Handles the migration of projects and their hierarchies from Jira to OpenProject.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
import time

from src import config
from src.clients.openproject_client import OpenProjectClient
from src.mappings.mappings import Mappings
from src.migrations.base_migration import BaseMigration
from src.models import ComponentResult

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient

# Get logger from config
logger = config.logger

# Constants for filenames
JIRA_PROJECTS_FILE = "jira_projects.json"
OP_PROJECTS_FILE = "openproject_projects.json"
ACCOUNT_MAPPING_FILE = "account_mapping.json"
PROJECT_ACCOUNT_MAPPING_FILE = "project_account_mapping.json"
TEMPO_ACCOUNTS_FILE = "tempo_accounts.json"


class ProjectMigration(BaseMigration):
    """Handles the migration of projects from Jira to OpenProject.

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
        jira_client: JiraClient,
        op_client: OpenProjectClient,
    ) -> None:
        """Initialize the project migration tools.

        Args:
            jira_client: Initialized Jira client instance.
            op_client: Initialized OpenProject client instance.

        """
        super().__init__(jira_client, op_client)
        self.jira_projects: list[dict[str, Any]] = []
        self.op_projects: list[dict[str, Any]] = []
        self.project_mapping: dict[str, Any] = {}
        self.account_mapping: dict[str, Any] = {}
        self.project_account_mapping: dict[str, list[dict[str, Any]]] = {}
        self.company_mapping: dict[str, Any] = {}
        self._created_projects = 0

        self.account_custom_field_id = None

        # Load existing data if available
        self.jira_projects = self._load_from_json(JIRA_PROJECTS_FILE) or []
        self.op_projects = self._load_from_json(OP_PROJECTS_FILE) or []
        self.project_mapping = config.mappings.get_mapping(Mappings.PROJECT_MAPPING_FILE)
        self.company_mapping = config.mappings.get_mapping(Mappings.COMPANY_MAPPING_FILE)

    def extract_jira_projects(self) -> list[dict[str, Any]]:
        """Extract projects from Jira.

        Returns:
            List of Jira projects

        """
        if not config.migration_config.get("force", False):
            cached_projects = self._load_from_json(JIRA_PROJECTS_FILE, default=None)
            if cached_projects:
                logger.info("Using cached Jira projects from %s", JIRA_PROJECTS_FILE)
                self.jira_projects = cached_projects
                return self.jira_projects

        logger.info("Extracting projects from Jira...")

        self.jira_projects = self.jira_client.get_projects()

        logger.info("Extracted %s projects from Jira", len(self.jira_projects))

        self._save_to_json(self.jira_projects, JIRA_PROJECTS_FILE)

        return self.jira_projects

    def extract_openproject_projects(self) -> list[dict[str, Any]]:
        """Extract projects from OpenProject.

        Returns:
            List of OpenProject project dictionaries

        """
        if not config.migration_config.get("force", False):
            cached_projects = self._load_from_json(OP_PROJECTS_FILE, default=None)
            if cached_projects:
                logger.info("Using cached OpenProject projects from %s", OP_PROJECTS_FILE)
                self.op_projects = cached_projects
                return self.op_projects

        logger.info("Extracting projects from OpenProject...")

        self.op_projects = self.op_client.get_projects()

        logger.info("Extracted %s projects from OpenProject", len(self.op_projects))

        self._save_to_json(self.op_projects, OP_PROJECTS_FILE)

        return self.op_projects

    def load_account_mapping(self) -> dict[str, Any]:
        """Load the account mapping created by the account migration.

        Returns:
            Dictionary mapping Tempo account IDs to OpenProject custom field data

        """
        self.account_mapping = self._load_from_json(ACCOUNT_MAPPING_FILE, default={})
        if self.account_mapping:
            logger.info("Loaded account mapping with %s entries.", len(self.account_mapping))
            for account in self.account_mapping.values():
                if account.get("custom_field_id"):
                    self.account_custom_field_id = account.get("custom_field_id")
                    break

            logger.info("Account custom field ID: %s", self.account_custom_field_id)
            return self.account_mapping
        logger.warning("No account mapping found. Account information won't be migrated.")
        return {}

    def load_company_mapping(self) -> dict[str, Any]:
        """Load the company mapping created by the company migration.

        Returns:
            Dictionary mapping Tempo company IDs to OpenProject project IDs

        """
        self.company_mapping = self._load_from_json(Mappings.COMPANY_MAPPING_FILE, default={})
        if self.company_mapping:
            company_count = len(self.company_mapping)
            matched_count = sum(1 for c in self.company_mapping.values() if c.get("openproject_id"))
            logger.info(
                "Loaded company mapping with %s entries, %s matched to OpenProject.",
                company_count,
                matched_count,
            )
            return self.company_mapping
        logger.warning("No company mapping found. Projects won't be organized hierarchically.")
        return {}

    def extract_project_account_mapping(self) -> dict[str, Any]:
        """Extract the mapping between Jira projects and Tempo accounts using already fetched Tempo account data.

        Instead of making individual API calls for each project, this method processes the account links
        already available in the Tempo accounts data from account migration.

        Returns:
            Dictionary mapping project keys to account IDs.

        """
        # Load existing data unless forced to refresh
        if self.project_account_mapping and not config.migration_config.get("force", False):
            logger.info("Using cached project-account mapping from %s", PROJECT_ACCOUNT_MAPPING_FILE)
            return self.project_account_mapping

        logger.info("Extracting project-account mapping from Tempo account data...")

        # Create a new mapping dictionary
        mapping: dict[str, list[dict[str, Any]]] = {}

        # Load Tempo accounts from file
        tempo_accounts = self._load_from_json(TEMPO_ACCOUNTS_FILE)
        if not tempo_accounts:
            logger.warning("No Tempo accounts found. Cannot create project-account mapping.")
            return {}

        # Process each account and its project links
        for account in tempo_accounts:
            acct_id = account.get("id")
            acct_key = account.get("key")
            acct_name = account.get("name")

            if not acct_id:
                continue

            # Get all project links for this account
            links = account.get("links", [])
            for link in links:
                # Only process project links (not customer links, etc.)
                if link.get("scopeType") != "PROJECT":
                    continue

                # Get project key
                project_key = link.get("key")
                if not project_key:
                    continue

                # Create project entry if it doesn't exist
                if project_key not in mapping:
                    mapping[project_key] = []

                # Add account info to this project
                mapping[project_key].append(
                    {
                        "id": str(acct_id),
                        "key": acct_key,
                        "name": acct_name,
                        "default": link.get("defaultAccount", False),
                    },
                )

        # For each project, prioritize default accounts
        for proj, accts in mapping.items():
            # Sort to put default accounts first
            mapping[proj] = sorted(accts, key=lambda a: not a.get("default"))

        logger.info("Mapped %s projects to Tempo accounts from account data", len(mapping))

        # Save the mapping
        self.project_account_mapping = mapping
        self._save_to_json(mapping, PROJECT_ACCOUNT_MAPPING_FILE)

        return mapping

    def find_parent_company_for_project(self, jira_project: dict[str, Any]) -> dict[str, Any] | None:
        """Find the appropriate parent company for a Jira project based on its default Tempo account."""
        jira_key = jira_project.get("key")

        # 1) Check project-account mapping
        raw_accts = self.project_account_mapping.get(jira_key)
        if not raw_accts:
            logger.debug("No account mapping found for project %s", jira_key)
            return None

        # 2) Use only the project's default Tempo account (first entry)
        acct_entry = raw_accts[0] if isinstance(raw_accts, list) else raw_accts
        acct_id = acct_entry if isinstance(acct_entry, int | str) else acct_entry.get("id")
        if not acct_id:
            logger.warning("Project %s: default Tempo account entry invalid: %s", jira_key, acct_entry)
            return None
        acct_id_str = str(acct_id)

        # 3) Map account to company_id
        acct_map = self.account_mapping.get(acct_id_str)
        if not acct_map:
            logger.warning("Project %s: Tempo account %s not found in account mapping", jira_key, acct_id_str)
            return None
        company_id = acct_map.get("company_id")
        if not company_id:
            logger.warning(
                f"Project {jira_key}: Tempo account {acct_id_str} missing company_id in acct_map: {acct_map}",
            )
            return None

        # 4) Map company_id to OpenProject project
        company = self.company_mapping.get(str(company_id))
        if not company or not company.get("openproject_id"):
            logger.warning("Project %s: Tempo company %s not migrated to OpenProject", jira_key, company_id)
            return None

        return company

    def bulk_migrate_projects(self) -> ComponentResult:
        """Migrate projects from Jira to OpenProject in bulk using Rails console.
        This is more efficient than creating each project individually with API calls.

        Returns:
            Dictionary mapping Jira project keys to OpenProject project IDs

        """
        logger.info("Starting bulk project migration using Rails client...")

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

        # Prepare project data for bulk creation
        projects_data = []
        for i, jira_project in enumerate(self.jira_projects):
            # Refresh OpenProject projects list every 10 projects to catch newly created ones
            if i > 0 and i % 10 == 0:
                logger.debug(
                    "Refreshing OpenProject projects list after %d projects", i
                )
                self.extract_openproject_projects()

            jira_key = jira_project.get("key", "")
            jira_name = jira_project.get("name", "")
            jira_description = jira_project.get("description", "")

            # Generate identifier
            identifier = re.sub(r"[^a-zA-Z0-9]", "-", jira_key.lower())
            if not identifier[0].isalpha():
                identifier = "p-" + identifier
            identifier = identifier[:100]

            # Find if it already exists
            existing_project = None

            # Log the current project being checked
            logger.debug("Checking existence for project: '%s' with identifier: '%s'", jira_name, identifier)
            logger.debug("Available OpenProject projects count: %d", len(self.op_projects))

            # Log a few existing projects for comparison
            if self.op_projects:
                logger.debug("Sample existing projects:")
                for j, op_project in enumerate(self.op_projects[:3]):
                    logger.debug(
                        "  %d: name='%s', identifier='%s'",
                        j+1,
                        op_project.get("name", ""),
                        op_project.get("identifier", "")
                    )

            for op_project in self.op_projects:
                op_name = op_project.get("name", "").lower()
                op_identifier = op_project.get("identifier", "")

                # Check for match
                name_match = op_name == jira_name.lower()
                identifier_match = op_identifier == identifier

                if name_match or identifier_match:
                    logger.debug("Found existing project match:")
                    logger.debug("  Name match: %s ('%s' vs '%s')", name_match, op_name, jira_name.lower())
                    logger.debug("  Identifier match: %s ('%s' vs '%s')", identifier_match, op_identifier, identifier)
                    existing_project = op_project
                    break

            if existing_project:
                logger.info(
                    "Project '%s' already exists in OpenProject with ID %s",
                    jira_name,
                    existing_project.get("id"),
                )
                continue
            else:
                logger.debug("No existing project found for '%s' (identifier: '%s') - will create", jira_name, identifier)

            # Find parent company
            parent_company = self.find_parent_company_for_project(jira_project)
            parent_id = parent_company.get("openproject_id") if parent_company else None

            # Get account ID if available
            account_id = None
            account_name = None
            if jira_key in self.project_account_mapping:
                accounts = self.project_account_mapping[jira_key]
                if isinstance(accounts, (int, str)):
                    account_id = accounts
                elif isinstance(accounts, list) and len(accounts) > 0:
                    account_id = accounts[0].get("id")

                if account_id and str(account_id) in self.account_mapping:
                    account_name = self.account_mapping[str(account_id)].get("tempo_name")

            # Add to projects data
            project_data = {
                "name": jira_name,
                "identifier": identifier,
                "description": jira_description or "",
                "parent_id": parent_id,
                "jira_key": jira_key,
                "account_name": account_name,
                "account_id": account_id,
                "public": False,
                "status": "ON_TRACK",
            }
            projects_data.append(project_data)

        if not projects_data:
            logger.info("No new projects to create")
            return ComponentResult(
                success=True,
                message="No new projects to create",
            )

        # Check for dry run
        if config.migration_config.get("dry_run", False):
            logger.info("DRY RUN: Skipping Rails script execution. Would have created these projects:")
            for project in projects_data:
                logger.info("  - %s (identifier: %s)", project["name"], project["identifier"])

            # Create a dummy mapping for dry run
            mapping = {}
            for project in projects_data:
                jira_key = project.get("jira_key")
                mapping[jira_key] = {
                    "jira_key": jira_key,
                    "jira_name": project.get("name"),
                    "openproject_id": None,  # None for dry run
                    "openproject_identifier": project.get("identifier"),
                    "openproject_name": project.get("name"),
                    "created_new": False,
                    "dry_run": True,
                }

            self.project_mapping = mapping
            self._save_to_json(mapping, Mappings.PROJECT_MAPPING_FILE)
            logger.info("DRY RUN: Would have created %s projects", len(projects_data))
            return ComponentResult(
                success=True,
                message=f"DRY RUN: Would have created {len(projects_data)} projects",
            )

        # Create projects individually using simple Rails commands
        # This is more reliable than complex bulk scripts
        created_projects = []
        errors = []

        logger.info("Creating %d projects individually...", len(projects_data))

        for i, project_data in enumerate(projects_data):
            try:
                logger.info("Creating project %d/%d: %s", i+1, len(projects_data), project_data["name"])

                # Check if project already exists with a more robust check
                identifier = project_data['identifier']

                # First try: Look for the project by identifier with detailed output
                check_query = f"p = Project.find_by(identifier: '{identifier}'); p ? p.as_json : nil"
                existing = self.op_client.execute_query_to_json_file(check_query)

                # Handle both JSON responses and scalar strings that indicate existence
                project_exists = False
                project_id = None
                project_name = None
                project_identifier = None

                if existing:
                    if isinstance(existing, dict) and existing.get("id"):
                        # Proper JSON response
                        project_exists = True
                        project_id = existing["id"]
                        project_name = existing["name"]
                        project_identifier = existing["identifier"]
                    elif isinstance(existing, str):
                        # Any non-empty string response could indicate project exists
                        # Let's do a more explicit check
                        exists_query = f"Project.exists?(identifier: '{identifier}')"
                        exists_result = self.op_client.execute_query_to_json_file(exists_query)

                        if (exists_result is True or
                            (isinstance(exists_result, str) and
                             exists_result.strip().lower() in ['true', 't'])):
                            project_exists = True
                            # Get the project details separately
                            detail_query = f"Project.find_by(identifier: '{identifier}').as_json"
                            details = self.op_client.execute_query_to_json_file(detail_query)
                            if isinstance(details, dict) and details.get("id"):
                                project_id = details["id"]
                                project_name = details["name"]
                                project_identifier = details["identifier"]
                            else:
                                # If we can't get details but know it exists, try a simpler query
                                simple_query = (
                                    f"p = Project.find_by(identifier: '{identifier}'); "
                                    "[p.id, p.name, p.identifier]"
                                )
                                simple_result = self.op_client.execute_query_to_json_file(simple_query)
                                if isinstance(simple_result, list) and len(simple_result) >= 3:
                                    project_id = simple_result[0]
                                    project_name = simple_result[1]
                                    project_identifier = simple_result[2]

                if project_exists:
                    logger.info("Project '%s' already exists with ID %s", project_data["name"], project_id or "unknown")
                    created_projects.append({
                        "jira_key": project_data["jira_key"],
                        "openproject_id": project_id,
                        "name": project_name or project_data["name"],
                        "identifier": project_identifier or project_data["identifier"],
                        "created_new": False
                    })
                    continue

                # Create the project using simplified Rails command (atomic and concise)
                # Properly escape strings for Rails/Ruby
                # Handle quotes, backslashes, and newlines
                def ruby_escape(s):
                    if not s:
                        return ""
                    # First escape backslashes, then single quotes
                    return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")

                name_escaped = ruby_escape(project_data['name'])
                desc_escaped = ruby_escape(project_data.get('description', ''))

                # Use a single-line Rails command for better reliability
                create_script = (
                    f"p = Project.create!(name: '{name_escaped}', "
                    f"identifier: '{project_data['identifier']}', "
                    f"description: '{desc_escaped}', public: false); "
                    f"p.enabled_module_names = ['work_package_tracking', 'wiki']; "
                    f"p.save!; p.as_json"
                )

                result = self.op_client.execute_query_to_json_file(create_script)

                if isinstance(result, dict) and result.get("id"):
                    logger.info("Successfully created project '%s' with ID %s", project_data["name"], result["id"])
                    created_projects.append({
                        "jira_key": project_data["jira_key"],
                        "openproject_id": result["id"],
                        "name": result["name"],
                        "identifier": result["identifier"],
                        "created_new": True
                    })
                else:
                    error_msg = f"Failed to create project: {result}"
                    logger.error("Error creating project '%s': %s", project_data["name"], error_msg)
                    errors.append({
                        "jira_key": project_data["jira_key"],
                        "name": project_data["name"],
                        "errors": [error_msg],
                        "error_type": "creation_error"
                    })

                    # Check if we should stop on error
                    if config.migration_config.get("stop_on_error", False):
                        logger.error("Stopping migration due to creation error and --stop-on-error flag is set")
                        raise Exception(f"Project creation failed: {error_msg}")

            except Exception as e:
                logger.exception("Exception creating project '%s': %s", project_data.get("name", "unknown"), e)
                errors.append({
                    "jira_key": project_data.get("jira_key"),
                    "name": project_data.get("name"),
                    "errors": [str(e)],
                    "error_type": "exception"
                })

                # Check if we should stop on error
                if config.migration_config.get("stop_on_error", False):
                    logger.error("Stopping migration due to error and --stop-on-error flag is set")
                    raise e

            # Small delay to avoid overwhelming the Rails console
            time.sleep(0.2)

        # Create mapping from results
        mapping = {}
        for project in created_projects:
            jira_key = project.get("jira_key")
            if jira_key:
                jira_project = next((p for p in self.jira_projects if p.get("key") == jira_key), {})
                mapping[jira_key] = {
                    "jira_key": jira_key,
                    "jira_name": (jira_project.get("name") if jira_project else project.get("name")),
                    "openproject_id": project.get("openproject_id"),
                    "openproject_identifier": project.get("identifier"),
                    "openproject_name": project.get("name"),
                    "created_new": project.get("created_new", True),
                }

        # Add errors to mapping
        for error in errors:
            jira_key = error.get("jira_key")
            if jira_key:
                jira_project = next((p for p in self.jira_projects if p.get("key") == jira_key), {})
                mapping[jira_key] = {
                    "jira_key": jira_key,
                    "jira_name": (jira_project.get("name") if jira_project else error.get("name")),
                    "openproject_id": None,
                    "openproject_identifier": None,
                    "openproject_name": None,
                    "created_new": False,
                    "failed": True,
                    "error": ", ".join(error.get("errors", [])),
                }

        # Save the mapping
        self.project_mapping = mapping
        self._save_to_json(mapping, Mappings.PROJECT_MAPPING_FILE)

        logger.info("Bulk project migration completed: %s created, %s errors", len(created_projects), len(errors))
        return ComponentResult(
            success=True,
            message=f"Bulk project migration completed: {len(created_projects)} created, {len(errors)} errors",
        )

    def analyze_project_mapping(self) -> dict[str, Any]:
        """Analyze the project mapping to identify potential issues.

        Returns:
            Dictionary with analysis results

        """
        if not self.project_mapping:
            mapping_file = Path(self.data_dir) / Mappings.PROJECT_MAPPING_FILE
            if mapping_file.exists():
                with mapping_file.open() as f:
                    self.project_mapping = json.load(f)
            else:
                logger.error("No project mapping found. Run bulk_migrate_projects() first.")
                return {}

        analysis = {
            "total_projects": len(self.project_mapping),
            "migrated_projects": sum(1 for p in self.project_mapping.values() if p.get("openproject_id") is not None),
            "new_projects": sum(1 for p in self.project_mapping.values() if p.get("created_new", False)),
            "existing_projects": sum(
                1
                for p in self.project_mapping.values()
                if p.get("openproject_id") is not None and not p.get("created_new", False)
            ),
            "projects_with_accounts": sum(
                1 for p in self.project_mapping.values() if p.get("account_name") is not None
            ),
            "projects_with_parent": sum(1 for p in self.project_mapping.values() if p.get("parent_id") is not None),
            "failed_projects": sum(1 for p in self.project_mapping.values() if p.get("failed", False)),
            "failed_details": [
                {"jira_key": p.get("jira_key"), "jira_name": p.get("jira_name")}
                for p in self.project_mapping.values()
                if p.get("failed", False)
            ],
        }

        total = int(analysis["total_projects"])
        if total > 0:
            analysis["migration_percentage"] = (int(analysis["migrated_projects"]) / total) * 100
            analysis["hierarchical_percentage"] = (int(analysis["projects_with_parent"]) / total) * 100
        else:
            analysis["migration_percentage"] = 0
            analysis["hierarchical_percentage"] = 0

        self._save_to_json(analysis, "project_mapping_analysis.json")

        logger.info("Project mapping analysis complete")
        logger.info("Total projects: %s", analysis["total_projects"])
        logger.info("Migrated projects: %s (%.1f%%)", analysis["migrated_projects"], analysis["migration_percentage"])
        logger.info("- Newly created: %s", analysis["new_projects"])
        logger.info("- Already existing: %s", analysis["existing_projects"])
        logger.info("- With account information: %s", analysis["projects_with_accounts"])
        logger.info(
            f"- With parent company: {analysis['projects_with_parent']} ({analysis['hierarchical_percentage']:.1f}%)",
        )
        logger.info("Failed projects: %s", analysis["failed_projects"])

        return analysis

    def run(self) -> ComponentResult:
        """Run the project migration.

        Returns:
            ComponentResult with migration results

        """
        # Extract Jira projects
        self.extract_jira_projects()

        # Extract OpenProject projects
        self.extract_openproject_projects()

        # Load account mapping - won't do anything if it doesn't exist
        self.load_account_mapping()

        # Load company mapping - won't do anything if it doesn't exist
        self.load_company_mapping()

        # Extract project-account mapping
        self.extract_project_account_mapping()

        # Always use Rails bulk migration for project creation
        logger.info("Running bulk project migration via Rails console")
        return self.bulk_migrate_projects()
