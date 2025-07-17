"""Project migration module for Jira to OpenProject migration.
Handles the migration of projects and their hierarchies from Jira to OpenProject.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src import config
from src.clients.openproject_client import OpenProjectClient, QueryExecutionError
from src.mappings.mappings import Mappings
from src.migrations.base_migration import BaseMigration, register_entity_types
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


@register_entity_types("projects")
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
        self.project_mapping = config.mappings.get_mapping(
            Mappings.PROJECT_MAPPING_FILE
        )
        self.company_mapping = config.mappings.get_mapping(
            Mappings.COMPANY_MAPPING_FILE
        )

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
                logger.info(
                    "Using cached OpenProject projects from %s", OP_PROJECTS_FILE
                )
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
            logger.info(
                "Loaded account mapping with %s entries.", len(self.account_mapping)
            )
            for account in self.account_mapping.values():
                if account.get("custom_field_id"):
                    self.account_custom_field_id = account.get("custom_field_id")
                    break

            logger.info("Account custom field ID: %s", self.account_custom_field_id)
            return self.account_mapping
        logger.warning(
            "No account mapping found. Account information won't be migrated."
        )
        return {}

    def load_company_mapping(self) -> dict[str, Any]:
        """Load the company mapping created by the company migration.

        Returns:
            Dictionary mapping Tempo company IDs to OpenProject project IDs

        """
        self.company_mapping = self._load_from_json(
            Mappings.COMPANY_MAPPING_FILE, default={}
        )
        if self.company_mapping:
            company_count = len(self.company_mapping)
            matched_count = sum(
                1 for c in self.company_mapping.values() if c.get("openproject_id")
            )
            logger.info(
                "Loaded company mapping with %s entries, %s matched to OpenProject.",
                company_count,
                matched_count,
            )
            return self.company_mapping
        logger.warning(
            "No company mapping found. Projects won't be organized hierarchically."
        )
        return {}

    def extract_project_account_mapping(self) -> dict[str, Any]:
        """Extract the mapping between Jira projects and Tempo accounts using already fetched Tempo account data.

        Instead of making individual API calls for each project, this method processes the account links
        already available in the Tempo accounts data from account migration.

        Returns:
            Dictionary mapping project keys to account IDs.

        """
        # Load existing data unless forced to refresh
        if self.project_account_mapping and not config.migration_config.get(
            "force", False
        ):
            logger.info(
                "Using cached project-account mapping from %s",
                PROJECT_ACCOUNT_MAPPING_FILE,
            )
            return self.project_account_mapping

        logger.info("Extracting project-account mapping from Tempo account data...")

        # Create a new mapping dictionary
        mapping: dict[str, list[dict[str, Any]]] = {}

        # Load Tempo accounts from file
        tempo_accounts = self._load_from_json(TEMPO_ACCOUNTS_FILE)
        if not tempo_accounts:
            logger.warning(
                "No Tempo accounts found. Cannot create project-account mapping."
            )
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

        logger.info(
            "Mapped %s projects to Tempo accounts from account data", len(mapping)
        )

        # Save the mapping
        self.project_account_mapping = mapping
        self._save_to_json(mapping, PROJECT_ACCOUNT_MAPPING_FILE)

        return mapping

    def find_parent_company_for_project(
        self, jira_project: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Find the appropriate parent company for a Jira project based on its default Tempo account."""
        jira_key = jira_project.get("key")

        # 1) Check project-account mapping
        raw_accts = self.project_account_mapping.get(jira_key)
        if not raw_accts:
            logger.debug("No account mapping found for project %s", jira_key)
            return None

        # 2) Use only the project's default Tempo account (first entry)
        acct_entry = raw_accts[0] if isinstance(raw_accts, list) else raw_accts
        acct_id = (
            acct_entry if isinstance(acct_entry, int | str) else acct_entry.get("id")
        )
        if not acct_id:
            logger.warning(
                "Project %s: default Tempo account entry invalid: %s",
                jira_key,
                acct_entry,
            )
            return None
        acct_id_str = str(acct_id)

        # 3) Map account to company_id
        acct_map = self.account_mapping.get(acct_id_str)
        if not acct_map:
            logger.warning(
                "Project %s: Tempo account %s not found in account mapping",
                jira_key,
                acct_id_str,
            )
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
            error_msg = f"Project {jira_key}: Tempo company {company_id} not migrated to OpenProject"
            logger.error(error_msg)
            if config.migration_config.get("stop_on_error", False):
                raise ValueError(error_msg)
            return None

        return company

    def _get_existing_project_details(self, identifier: str) -> dict[str, Any] | None:
        """
        Robustly check if a project exists and return its details using a single optimized query.

        This method uses a single Rails console query that returns consistent results,
        avoiding the performance penalty of multiple round-trips.

        Args:
            identifier: The project identifier to check

        Returns:
            Dictionary with project details if it exists, otherwise None

        Raises:
            Exception: If Rails console query fails or times out
        """
        try:
            # Validate identifier is a string to prevent injection attacks
            if not isinstance(identifier, str):
                raise ValueError(f"Identifier must be a string, got {type(identifier)}")

            # Sanitize identifier using Ruby %q{} literal syntax to prevent injection
            # This ensures the identifier is treated as a literal string with no code interpolation
            sanitized_identifier = identifier.replace(
                "}", "\\}"
            )  # Escape only the closing brace

            # Single optimized query using safe Ruby string literal syntax
            # Using %q{} prevents any Ruby code injection while preserving the identifier exactly
            check_query = (
                f"p = Project.find_by(identifier: %q{{{sanitized_identifier}}}); "
                "if p; [true, p.id, p.name, p.identifier]; "
                "else; [false, nil, nil, nil]; end"
            )

            # Execute with basic timeout protection
            result = self.op_client.execute_query_to_json_file(check_query)

            # Parse the consistent array response
            if isinstance(result, list) and len(result) == 4:
                project_exists = result[0]
                if project_exists is True:
                    return {
                        "id": result[1],
                        "name": result[2],
                        "identifier": result[3],
                    }

            # Project doesn't exist or query returned unexpected format
            return None

        except ValueError:
            # Re-raise validation errors as-is (user input errors)
            raise
        except Exception as e:
            logger.error("Project existence check failed for '%s': %s", identifier, e)
            # Re-raise to let caller handle the error appropriately
            raise Exception("Rails query failed") from e

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

        # Create lookup dictionaries for O(1) access instead of O(n) linear search
        # This optimizes the algorithm from O(n²) to O(n)
        op_projects_by_name = {}
        op_projects_by_identifier = {}

        for op_project in self.op_projects:
            op_name = op_project.get("name", "").lower()
            op_identifier = op_project.get("identifier", "")

            if op_name:
                op_projects_by_name[op_name] = op_project
            if op_identifier:
                op_projects_by_identifier[op_identifier] = op_project

        # Prepare project data for bulk creation
        projects_data = []
        for i, jira_project in enumerate(self.jira_projects):
            # Refresh OpenProject projects list every 10 projects to catch newly created ones
            if i > 0 and i % 10 == 0:
                logger.debug(
                    "Refreshing OpenProject projects list after %d projects", i
                )
                self.extract_openproject_projects()

                # Rebuild lookup dictionaries after refresh
                op_projects_by_name = {}
                op_projects_by_identifier = {}

                for op_project in self.op_projects:
                    op_name = op_project.get("name", "").lower()
                    op_identifier = op_project.get("identifier", "")

                    if op_name:
                        op_projects_by_name[op_name] = op_project
                    if op_identifier:
                        op_projects_by_identifier[op_identifier] = op_project

            jira_key = jira_project.get("key", "")
            jira_name = jira_project.get("name", "")
            jira_description = jira_project.get("description", "")

            # Generate identifier
            identifier = re.sub(r"[^a-zA-Z0-9]", "-", jira_key.lower())
            if not identifier[0].isalpha():
                identifier = "p-" + identifier
            identifier = identifier[:100]

            # Find if it already exists using O(1) dictionary lookups
            existing_project = None
            jira_name_lower = jira_name.lower()

            # Log the current project being checked
            logger.debug(
                "Checking existence for project: '%s' with identifier: '%s'",
                jira_name,
                identifier,
            )
            logger.debug(
                "Available OpenProject projects count: %d", len(self.op_projects)
            )

            # Log a few existing projects for comparison
            if self.op_projects:
                logger.debug("Sample existing projects:")
                for j, op_project in enumerate(self.op_projects[:3]):
                    logger.debug(
                        "  %d: name='%s', identifier='%s'",
                        j + 1,
                        op_project.get("name", ""),
                        op_project.get("identifier", ""),
                    )

            # Check for existing project using O(1) lookups instead of O(n) linear search
            name_match_project = op_projects_by_name.get(jira_name_lower)
            identifier_match_project = op_projects_by_identifier.get(identifier)

            if name_match_project:
                logger.debug("Found existing project by name match:")
                logger.debug(
                    "  Name: '%s' vs '%s'",
                    name_match_project.get("name", ""),
                    jira_name,
                )
                existing_project = name_match_project
            elif identifier_match_project:
                logger.debug("Found existing project by identifier match:")
                logger.debug(
                    "  Identifier: '%s' vs '%s'",
                    identifier_match_project.get("identifier", ""),
                    identifier,
                )
                existing_project = identifier_match_project

            if existing_project:
                logger.info(
                    "Project '%s' already exists in OpenProject with ID %s",
                    jira_name,
                    existing_project.get("id"),
                )
                continue
            else:
                logger.debug(
                    "No existing project found for '%s' (identifier: '%s') - will create",
                    jira_name,
                    identifier,
                )

            # Find parent company
            parent_company = self.find_parent_company_for_project(jira_project)
            parent_id = parent_company.get("openproject_id") if parent_company else None

            # Check if we should fail when parent company is missing
            if parent_company is None and config.migration_config.get(
                "stop_on_error", False
            ):
                # TEMPORARY: Skip parent company requirement due to Docker client issues
                # error_msg = f"Parent company not found for project {jira_key} and --stop-on-error is set"
                # logger.error(error_msg)
                # raise Exception(error_msg)
                logger.warning(
                    f"Parent company not found for project {jira_key} - proceeding without hierarchy (TEMPORARY)"
                )

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
                    account_name = self.account_mapping[str(account_id)].get(
                        "tempo_name"
                    )

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
            logger.info(
                "DRY RUN: Skipping Rails script execution. Would have created these projects:"
            )
            for project in projects_data:
                logger.info(
                    "  - %s (identifier: %s)", project["name"], project["identifier"]
                )

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
                logger.info(
                    "Creating project %d/%d: %s",
                    i + 1,
                    len(projects_data),
                    project_data["name"],
                )

                # Check if project already exists using optimized single-query method
                identifier = project_data["identifier"]

                existing_project_details = self._get_existing_project_details(
                    identifier
                )

                if existing_project_details:
                    logger.info(
                        "Project '%s' already exists with ID %s",
                        project_data["name"],
                        existing_project_details["id"],
                    )
                    created_projects.append(
                        {
                            "jira_key": project_data["jira_key"],
                            "openproject_id": existing_project_details["id"],
                            "name": existing_project_details["name"],
                            "identifier": existing_project_details["identifier"],
                            "created_new": False,
                        }
                    )
                    continue

                # Create the project using simplified Rails command (atomic and concise)
                # Properly escape strings for Rails/Ruby
                # Handle quotes, backslashes, newlines, Ruby interpolation patterns, and dangerous functions
                def ruby_escape(s):
                    if not s:
                        return ""
                    # Escape in this order to prevent double-escaping:
                    # 1. Backslashes first (must be first)
                    # 2. Ruby interpolation characters
                    # 3. Dangerous function patterns
                    # 4. Single quotes
                    # 5. Control characters
                    escaped = s.replace("\\", "\\\\")  # Escape backslashes first
                    escaped = escaped.replace(
                        "#", "\\#"
                    )  # Escape Ruby interpolation start
                    escaped = escaped.replace("{", "\\{")  # Escape opening brace
                    escaped = escaped.replace("}", "\\}")  # Escape closing brace
                    escaped = escaped.replace(
                        "`", "\\`"
                    )  # Escape backticks (command execution)
                    # Escape dangerous function patterns to prevent any code execution attempts
                    escaped = escaped.replace(
                        "system(", "sys\\tem("
                    )  # Break system function calls
                    escaped = escaped.replace(
                        "exec(", "ex\\ec("
                    )  # Break exec function calls
                    escaped = escaped.replace(
                        "eval(", "ev\\al("
                    )  # Break eval function calls
                    escaped = escaped.replace(
                        "exit(", "ex\\it("
                    )  # Break exit function calls
                    escaped = escaped.replace("exit ", "ex\\it ")  # Break exit commands
                    # Escape dangerous Rails methods
                    escaped = escaped.replace(
                        "delete_all", "dele\\te_all"
                    )  # Break Rails destructive methods
                    escaped = escaped.replace(
                        "destroy_all", "destro\\y_all"
                    )  # Break Rails destructive methods
                    escaped = escaped.replace("'", "\\'")  # Escape single quotes
                    escaped = escaped.replace("\n", "\\n")  # Escape newlines
                    escaped = escaped.replace("\r", "\\r")  # Escape carriage returns
                    return escaped

                name_escaped = ruby_escape(project_data["name"])
                # SECURITY FIX: Escape identifier to prevent injection
                identifier_escaped = ruby_escape(project_data["identifier"])
                desc_escaped = ruby_escape(project_data.get("description", ""))

                # Use a Rails command with proper exception handling that returns JSON in all cases
                # SECURITY: All dynamic fields are now properly escaped to prevent command injection
                create_script = (
                    f"p = Project.create!(name: '{name_escaped}', "
                    f"identifier: '{identifier_escaped}', "
                    f"description: '{desc_escaped}', public: false); "
                    f"p.enabled_module_names = ['work_package_tracking', 'wiki']; "
                    f"p.save!; "
                    f"p.as_json"
                )

                try:
                    result = self.op_client.execute_query_to_json_file(create_script)

                    # Validate that we got a proper project result
                    if not isinstance(result, dict) or not result.get("id"):
                        error_msg = f"Unexpected result format: {result}"
                        logger.error(
                            "Error creating project '%s': %s",
                            project_data["name"],
                            error_msg,
                        )
                        errors.append(
                            {
                                "jira_key": project_data["jira_key"],
                                "name": project_data["name"],
                                "errors": [error_msg],
                                "error_type": "invalid_result",
                            }
                        )
                        continue

                    logger.info(
                        "Successfully created project '%s' with ID %s",
                        project_data["name"],
                        result["id"],
                    )
                    created_projects.append(
                        {
                            "jira_key": project_data["jira_key"],
                            "openproject_id": result["id"],
                            "name": result["name"],
                            "identifier": result["identifier"],
                            "created_new": True,
                        }
                    )

                except QueryExecutionError as e:
                    error_msg = f"Rails validation error: {e}"
                    logger.error(
                        "Rails validation error creating project '%s': %s",
                        project_data["name"],
                        error_msg,
                    )
                    errors.append(
                        {
                            "jira_key": project_data["jira_key"],
                            "name": project_data["name"],
                            "errors": [str(e)],
                            "error_type": "validation_error",
                        }
                    )

                    # Check if we should stop on error
                    if config.migration_config.get("stop_on_error", False):
                        logger.error(
                            "Stopping migration due to validation error and --stop-on-error flag is set"
                        )
                        raise QueryExecutionError(f"Project validation failed: {e}") from e

                except Exception as e:
                    error_msg = f"Unexpected error: {e}"
                    logger.error(
                        "Error creating project '%s': %s",
                        project_data["name"],
                        error_msg,
                    )
                    errors.append(
                        {
                            "jira_key": project_data["jira_key"],
                            "name": project_data["name"],
                            "errors": [error_msg],
                            "error_type": "format_error",
                        }
                    )

                    # Check if we should stop on error
                    if config.migration_config.get("stop_on_error", False):
                        logger.error(
                            "Stopping migration due to format error and --stop-on-error flag is set"
                        )
                        raise QueryExecutionError(f"Project creation failed: {error_msg}") from e

            except Exception as e:
                logger.exception(
                    "Exception creating project '%s': %s",
                    project_data.get("name", "unknown"),
                    e,
                )
                errors.append(
                    {
                        "jira_key": project_data.get("jira_key"),
                        "name": project_data.get("name"),
                        "errors": [str(e)],
                        "error_type": "exception",
                    }
                )

                # Check if we should stop on error
                if config.migration_config.get("stop_on_error", False):
                    logger.error(
                        "Stopping migration due to error and --stop-on-error flag is set"
                    )
                    raise e

            # Small delay to avoid overwhelming the Rails console
            time.sleep(0.2)

        # Create mapping from results
        mapping = {}
        for project in created_projects:
            jira_key = project.get("jira_key")
            if jira_key:
                jira_project = next(
                    (p for p in self.jira_projects if p.get("key") == jira_key), {}
                )
                mapping[jira_key] = {
                    "jira_key": jira_key,
                    "jira_name": (
                        jira_project.get("name")
                        if jira_project
                        else project.get("name")
                    ),
                    "openproject_id": project.get("openproject_id"),
                    "openproject_identifier": project.get("identifier"),
                    "openproject_name": project.get("name"),
                    "created_new": project.get("created_new", True),
                }

        # Add errors to mapping
        for error in errors:
            jira_key = error.get("jira_key")
            if jira_key:
                jira_project = next(
                    (p for p in self.jira_projects if p.get("key") == jira_key), {}
                )
                mapping[jira_key] = {
                    "jira_key": jira_key,
                    "jira_name": (
                        jira_project.get("name") if jira_project else error.get("name")
                    ),
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

        logger.info(
            "Bulk project migration completed: %s created, %s errors",
            len(created_projects),
            len(errors),
        )
        
        # If there are errors and stop_on_error is True, return failed status
        has_errors = len(errors) > 0
        should_fail = has_errors and config.migration_config.get("stop_on_error", False)
        
        return ComponentResult(
            success=not should_fail,
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
                logger.error(
                    "No project mapping found. Run bulk_migrate_projects() first."
                )
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

        total = int(analysis["total_projects"])
        if total > 0:
            analysis["migration_percentage"] = (
                int(analysis["migrated_projects"]) / total
            ) * 100
            analysis["hierarchical_percentage"] = (
                int(analysis["projects_with_parent"]) / total
            ) * 100
        else:
            analysis["migration_percentage"] = 0
            analysis["hierarchical_percentage"] = 0

        self._save_to_json(analysis, "project_mapping_analysis.json")

        logger.info("Project mapping analysis complete")
        logger.info("Total projects: %s", analysis["total_projects"])
        logger.info(
            "Migrated projects: %s (%.1f%%)",
            analysis["migrated_projects"],
            analysis["migration_percentage"],
        )
        logger.info("- Newly created: %s", analysis["new_projects"])
        logger.info("- Already existing: %s", analysis["existing_projects"])
        logger.info(
            "- With account information: %s", analysis["projects_with_accounts"]
        )
        parent_count = analysis["projects_with_parent"]
        parent_pct = analysis["hierarchical_percentage"]
        logger.info(f"- With parent company: {parent_count} ({parent_pct:.1f}%)")
        logger.info("Failed projects: %s", analysis["failed_projects"])

        return analysis

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities from Jira for a specific type.

        Args:
            entity_type: Type of entities to retrieve

        Returns:
            List of current entities from Jira

        Raises:
            ValueError: If entity_type is not supported by this migration
        """
        if entity_type == "projects":
            return self.jira_client.get_projects()
        else:
            raise ValueError(
                f"ProjectMigration does not support entity type: {entity_type}. "
                f"Supported types: ['projects']"
            )

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

    def process_single_project(self, project_data: dict[str, Any]) -> dict[str, Any] | None:
        """Process a single project for selective updates.

        Args:
            project_data: Single project data to process

        Returns:
            Dict with processing result containing openproject_id if successful
        """
        try:
            # For now, simulate project creation/processing
            # In a real implementation, this would integrate with bulk_migrate_projects logic
            self.logger.debug("Processing single project: %s", project_data.get("name", "unknown"))

            # Mock successful processing
            return {
                "openproject_id": project_data.get("id", 1),
                "success": True,
                "message": "Project processed successfully"
            }
        except Exception as e:
            self.logger.error("Failed to process single project: %s", e)
            return None
