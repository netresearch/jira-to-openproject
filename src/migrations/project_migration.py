"""
Project migration module for Jira to OpenProject migration.
Handles the migration of projects and their hierarchies from Jira to OpenProject.
"""

import json
import os
import re
from typing import Any, Optional

from src.models import ComponentResult
from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.clients.openproject_rails_client import OpenProjectRailsClient
from src.mappings.mappings import Mappings
from src.migrations.base_migration import BaseMigration

# Get logger from config
logger = config.logger

# Constants for filenames
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
    - Jira projects as sub-projects under their respective Tempo company parent projects
    - Projects with account information stored in custom fields
    """

    def __init__(
        self,
        jira_client: "JiraClient",
        op_client: "OpenProjectClient",
        op_rails_client: Optional["OpenProjectRailsClient"] = None,
    ):
        """
        Initialize the project migration tools.

        Args:
            jira_client: Initialized Jira client instance.
            op_client: Initialized OpenProject client instance.
            op_rails_client: Optional instance of OpenProjectRailsClient for direct migration.
        """
        super().__init__(jira_client, op_client, op_rails_client)
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

    def extract_jira_projects(self, force: bool = False) -> list[dict[str, Any]]:
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

    def extract_openproject_projects(self, force: bool = False) -> list[dict[str, Any]]:
        """
        Extract projects from OpenProject.

        Returns:
            List of OpenProject project dictionaries
        """
        if not force and not config.migration_config.get("force", False):
            cached_projects = self._load_from_json(OP_PROJECTS_FILE, default=None)
            if cached_projects:
                logger.info(
                    f"Using cached OpenProject projects from {OP_PROJECTS_FILE}"
                )
                self.op_projects = cached_projects
                return self.op_projects

        logger.info("Extracting projects from OpenProject...")

        self.op_projects = self.op_client.get_projects()

        logger.info(f"Extracted {len(self.op_projects)} projects from OpenProject")

        self._save_to_json(self.op_projects, OP_PROJECTS_FILE)

        return self.op_projects

    def load_account_mapping(self) -> dict[str, Any]:
        """
        Load the account mapping created by the account migration.

        Returns:
            Dictionary mapping Tempo account IDs to OpenProject custom field data
        """
        self.account_mapping = self._load_from_json(ACCOUNT_MAPPING_FILE, default={})
        if self.account_mapping:
            logger.info(
                f"Loaded account mapping with {len(self.account_mapping)} entries."
            )
            for account in self.account_mapping.values():
                if account.get("custom_field_id"):
                    self.account_custom_field_id = account.get("custom_field_id")
                    break

            logger.info(f"Account custom field ID: {self.account_custom_field_id}")
            return self.account_mapping
        else:
            logger.warning(
                "No account mapping found. Account information won't be migrated."
            )
            return {}

    def load_company_mapping(self) -> dict[str, Any]:
        """
        Load the company mapping created by the company migration.

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
                f"Loaded company mapping with {company_count} entries, {matched_count} matched to OpenProject."
            )
            return self.company_mapping
        else:
            logger.warning(
                "No company mapping found. Projects won't be organized hierarchically."
            )
            return {}

    def extract_project_account_mapping(self, force: bool = False) -> dict[str, Any]:
        """
        Extract the mapping between Jira projects and Tempo accounts using already fetched Tempo account data.

        Instead of making individual API calls for each project, this method processes the account links
        already available in the Tempo accounts data from account migration.

        Args:
            force: If True, re-extract data even if it exists locally.

        Returns:
            Dictionary mapping project keys to account IDs.
        """
        # Load existing data unless forced to refresh
        if (
            self.project_account_mapping
            and not force
            and not config.migration_config.get("force", False)
        ):
            logger.info(
                f"Using cached project-account mapping from {PROJECT_ACCOUNT_MAPPING_FILE}"
            )
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
                mapping[project_key].append({
                    "id": str(acct_id),
                    "key": acct_key,
                    "name": acct_name,
                    "default": link.get("defaultAccount", False)
                })

        # For each project, prioritize default accounts
        for proj, accts in mapping.items():
            # Sort to put default accounts first
            mapping[proj] = sorted(accts, key=lambda a: not a.get("default"))

        logger.info(f"Mapped {len(mapping)} projects to Tempo accounts from account data")

        # Save the mapping
        self.project_account_mapping = mapping
        self._save_to_json(mapping, PROJECT_ACCOUNT_MAPPING_FILE)

        return mapping

    def find_parent_company_for_project(
        self, jira_project: dict[str, Any]
    ) -> dict[str, Any] | None:
        """
        Find the appropriate parent company for a Jira project based on its default Tempo account.
        """
        jira_key = jira_project.get("key")

        # 1) Check project-account mapping
        raw_accts = self.project_account_mapping.get(jira_key)
        if not raw_accts:
            logger.debug(f"No account mapping found for project {jira_key}")
            return None

        # 2) Use only the project's default Tempo account (first entry)
        acct_entry = raw_accts[0] if isinstance(raw_accts, list) else raw_accts
        acct_id = (
            acct_entry if isinstance(acct_entry, (int, str)) else acct_entry.get("id")
        )
        if not acct_id:
            logger.warning(
                f"Project {jira_key}: default Tempo account entry invalid: {acct_entry}"
            )
            return None
        acct_id_str = str(acct_id)

        # 3) Map account to company_id
        acct_map = self.account_mapping.get(acct_id_str)
        if not acct_map:
            logger.warning(
                f"Project {jira_key}: Tempo account {acct_id_str} not found in account mapping"
            )
            return None
        company_id = acct_map.get("company_id")
        if not company_id:
            logger.warning(
                f"Project {jira_key}: Tempo account {acct_id_str} missing company_id in acct_map: {acct_map}"
            )
            return None

        # 4) Map company_id to OpenProject project
        company = self.company_mapping.get(str(company_id))
        if not company or not company.get("openproject_id"):
            logger.warning(
                f"Project {jira_key}: Tempo company {company_id} not migrated to OpenProject"
            )
            return None

        return company

    def bulk_migrate_projects(self) -> ComponentResult:
        """
        Migrate projects from Jira to OpenProject in bulk using Rails console.
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
        for jira_project in self.jira_projects:
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
            for op_project in self.op_projects:
                if (
                    op_project.get("name", "").lower() == jira_name.lower()
                    or op_project.get("identifier", "") == identifier
                ):
                    existing_project = op_project
                    break

            if existing_project:
                logger.info(
                    f"Project '{jira_name}' already exists in OpenProject with ID {existing_project.get('id')}"
                )
                continue

            # Find parent company
            parent_company = self.find_parent_company_for_project(jira_project)
            parent_id = parent_company.get("openproject_id") if parent_company else None

            # Get account ID if available
            account_id = None
            account_name = None
            if jira_key in self.project_account_mapping:
                accounts = self.project_account_mapping[jira_key]
                if isinstance(accounts, int) or isinstance(accounts, str):
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

        # Write projects data to a temp file
        temp_file_path = os.path.join(self.data_dir, "projects_data.json")
        with open(temp_file_path, "w") as f:
            json.dump(projects_data, f, indent=2)

        # Transfer file to the container
        container_temp_path = "/tmp/projects_data.json"
        if not self.op_client.rails_client.transfer_file_to_container(
            temp_file_path, container_temp_path
        ):
            logger.error(
                "Failed to transfer projects data file to container. Aborting."
            )
            return ComponentResult(
                success=False,
                message="Failed to transfer projects data file to container. Aborting.",
            )

        # Check for dry run
        if config.migration_config.get("dry_run", False):
            logger.info(
                "DRY RUN: Skipping Rails script execution. Would have created these projects:"
            )
            for project in projects_data:
                logger.info(
                    f"  - {project['name']} (identifier: {project['identifier']})"
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
            logger.info(f"DRY RUN: Would have created {len(projects_data)} projects")
            return ComponentResult(
                success=True,
                message=f"DRY RUN: Would have created {len(projects_data)} projects",
            )

        # Create Ruby script for bulk project creation
        header_script = f"""
        # Ruby variables from Python
        projects_file_path = '{container_temp_path}'
        result_file_path = '/tmp/projects_result.json'
        """

        main_script = """
        begin
          require 'json'

          # Load the data from the JSON file
          projects_data = JSON.parse(File.read(projects_file_path))
          puts "Loaded #{projects_data.length} projects from JSON file"

          created_projects = []
          errors = []

          # Create each project
          projects_data.each do |project_attrs|
            begin
              # Store Jira data for mapping
              jira_key = project_attrs['jira_key']
              account_name = project_attrs['account_name']
              account_id = project_attrs['account_id']

              # Check if project already exists
              existing = Project.find_by(identifier: project_attrs['identifier'])
              if existing
                puts "Project with identifier '#{project_attrs['identifier']}' already exists (ID: #{existing.id})"
                created_projects << {
                  'jira_key' => jira_key,
                  'openproject_id' => existing.id,
                  'name' => existing.name,
                  'identifier' => existing.identifier,
                  'created_new' => false
                }
                next
              end

              # Create project object
              project = Project.new(
                name: project_attrs['name'],
                identifier: project_attrs['identifier'],
                description: project_attrs['description'],
                public: false
              )

              # Set parent if specified
              if project_attrs['parent_id']
                parent = Project.find_by(id: project_attrs['parent_id'])
                if parent
                  project.parent = parent
                else
                  puts "Warning: Parent project ID #{project_attrs['parent_id']} not found"
                end
              end

              # Enable default modules
              project.enabled_module_names = ['work_package_tracking', 'wiki']

              # Save the project
              if project.save
                puts "Created project ##{project.id}: #{project.name}"

                # Set account custom field if available
                if account_name
                  begin
                    account_cf = CustomField.where(type: 'ProjectCustomField', name: 'Account').first
                    if account_cf
                      project.custom_values.create(custom_field_id: account_cf.id, value: account_name)
                      puts "Added account '#{account_name}' to project '#{project.name}'"
                    else
                      puts "Warning: Account custom field not found"
                    end
                  rescue => cf_error
                    puts "Error setting account custom field: #{cf_error.message}"
                  end
                end

                created_projects << {
                  'jira_key' => jira_key,
                  'openproject_id' => project.id,
                  'name' => project.name,
                  'identifier' => project.identifier,
                  'created_new' => true
                }
              else
                errors << {
                  'jira_key' => jira_key,
                  'name' => project_attrs['name'],
                  'errors' => project.errors.full_messages,
                  'error_type' => 'validation_error'
                }
                puts "Error creating project: #{project.errors.full_messages.join(', ')}"
              end
            rescue => e
              errors << {
                'jira_key' => project_attrs['jira_key'],
                'name' => project_attrs['name'],
                'errors' => [e.message],
                'error_type' => 'exception'
              }
              puts "Exception: #{e.message}"
            end
          end

          # Write results to result file
          result = {
            'status' => 'success',
            'created' => created_projects,
            'errors' => errors,
            'created_count' => created_projects.length,
            'error_count' => errors.length,
            'total' => projects_data.length
          }

          File.write(result_file_path, result.to_json)
          puts "Results written to #{result_file_path}"

          # Also return the result for direct capture
          result
        rescue => e
          error_result = {
            'status' => 'error',
            'message' => e.message,
            'backtrace' => e.backtrace[0..5]
          }

          # Try to save error to file
          begin
            File.write(result_file_path, error_result.to_json)
          rescue => write_error
            puts "Failed to write error to file: #{write_error.message}"
          end

          # Return error result
          error_result
        end
        """

        # Execute the Ruby script
        result = self.op_client.rails_client.execute(header_script + main_script)

        if result.get("status") != "success":
            logger.error(
                f"Rails error during bulk project creation: {result.get('error', 'Unknown error')}"
            )
            logger.error("Bulk project migration failed. Aborting.")
            return ComponentResult(
                success=False,
                message=f"Rails error during bulk project creation: {result.get('error', 'Unknown error')}",
            )

        # Get the results
        created_projects = []
        errors = []

        # Try to get results from direct output first
        output = result.get("output")
        if isinstance(output, dict) and output.get("status") == "success":
            created_projects = output.get("created", [])
            errors = output.get("errors", [])
        else:
            # Try to get the result file from the container
            result_file_container = "/tmp/projects_result.json"
            result_file_local = os.path.join(self.data_dir, "projects_result.json")

            if self.op_client.rails_client.transfer_file_from_container(
                result_file_container, result_file_local
            ):
                try:
                    with open(result_file_local) as f:
                        result_data = json.load(f)
                        if result_data.get("status") == "success":
                            created_projects = result_data.get("created", [])
                            errors = result_data.get("errors", [])
                except Exception as e:
                    logger.error(f"Error reading result file: {str(e)}")

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
            f"Bulk project migration completed: {len(created_projects)} created, {len(errors)} errors"
        )
        return ComponentResult(
            success=True,
            message=f"Bulk project migration completed: {len(created_projects)} created, {len(errors)} errors",
        )

    def analyze_project_mapping(self) -> dict[str, Any]:
        """
        Analyze the project mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        if not self.project_mapping:
            if os.path.exists(
                os.path.join(self.data_dir, Mappings.PROJECT_MAPPING_FILE)
            ):
                with open(
                    os.path.join(self.data_dir, Mappings.PROJECT_MAPPING_FILE)
                ) as f:
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
        logger.info(f"Total projects: {analysis['total_projects']}")
        logger.info(
            f"Migrated projects: {analysis['migrated_projects']} ({analysis['migration_percentage']:.1f}%)"
        )
        logger.info(f"- Newly created: {analysis['new_projects']}")
        logger.info(f"- Already existing: {analysis['existing_projects']}")
        logger.info(f"- With account information: {analysis['projects_with_accounts']}")
        logger.info(
            f"- With parent company: {analysis['projects_with_parent']} ({analysis['hierarchical_percentage']:.1f}%)"
        )
        logger.info(f"Failed projects: {analysis['failed_projects']}")

        return analysis

    def run(
        self, dry_run: bool = False, force: bool = False, mappings: Optional[Mappings] = None
    ) -> ComponentResult:
        """
        Run the project migration.

        Args:
            dry_run: If True, don't actually create or update anything
            force: If True, force recalculation of data even if it already exists
            mappings: Optional mappings reference for cross-reference during migration

        Returns:
            ComponentResult with migration results
        """
        # Set dry run in config
        if dry_run:
            logger.info("Running in dry run mode - no changes will be made")
            if not config.migration_config.get("dry_run", False):
                config.migration_config["dry_run"] = dry_run

        # Extract Jira projects
        self.extract_jira_projects(force=force)

        # Extract OpenProject projects
        self.extract_openproject_projects(force=force)

        # Load account mapping - won't do anything if it doesn't exist
        self.load_account_mapping()

        # Load company mapping - won't do anything if it doesn't exist
        self.load_company_mapping()

        # Extract project-account mapping
        self.extract_project_account_mapping(force=force)

        # Always use Rails bulk migration for project creation
        if dry_run:
            logger.info(
                "DRY RUN: no projects will be created or modified in OpenProject"
            )
        logger.info("Running bulk project migration via Rails console")
        return self.bulk_migrate_projects()
