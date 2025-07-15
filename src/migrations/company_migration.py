"""Company migration module for Jira to OpenProject migration.

Handles the migration of Tempo timesheet companies from Jira to OpenProject as top-level projects.
"""

import json
import re
from pathlib import Path
from typing import Any

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.mappings.mappings import Mappings
from src.migrations.base_migration import BaseMigration
from src.models import ComponentResult, MigrationError
from src.utils import data_handler


class CompanyMigration(BaseMigration):
    """Handles the migration of companies from Tempo timesheet to OpenProject.

    This class is responsible for:
    1. Extracting company information from Tempo timesheet in Jira
    2. Creating corresponding top-level projects in OpenProject
    3. Mapping these companies to be used later when creating projects with account metadata

    The approach is:
    - Tempo Company → OpenProject top-level project
    - Tempo Account → Custom field in OpenProject projects and work packages
    - Jira Project → OpenProject project with account information stored in custom fields
    """

    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
        data_dir: str | None = None,
    ) -> None:
        """Initialize the company migration.

        Args:
            jira_client: JiraClient instance.
            op_client: OpenProjectClient instance.
            data_dir: Path to data directory for storing mappings.

        """
        super().__init__(jira_client, op_client)

        # Setup file paths
        self.tempo_companies_file = self.data_dir / Mappings.TEMPO_COMPANIES_FILE
        self.op_projects_file = self.data_dir / Mappings.OP_PROJECTS_FILE

        # Data storage
        self.tempo_companies: dict[str, Any] = {}
        self.op_projects: list[dict[str, Any]] = []
        self.company_mapping: dict[str, Any] = {}
        self._created_companies: int = 0  # Initialize counter for created companies

        # Logging
        self.logger.debug(
            "CompanyMigration initialized with data dir: %s",
            self.data_dir,
        )

        # Load existing data if available
        loaded_companies = self._load_from_json(Mappings.TEMPO_COMPANIES_FILE)
        if loaded_companies:
            # If loaded data is a list, convert to dictionary with id as key
            if isinstance(loaded_companies, list):
                self.tempo_companies = {}
                for company in loaded_companies:
                    if isinstance(company, dict) and "id" in company:
                        self.tempo_companies[str(company["id"])] = company
            elif isinstance(loaded_companies, dict):
                self.tempo_companies = loaded_companies
            else:
                self.tempo_companies = {}
        else:
            self.tempo_companies = {}

        self.op_projects = self._load_from_json(Mappings.OP_PROJECTS_FILE) or []
        self.company_mapping = self._load_from_json(Mappings.COMPANY_MAPPING_FILE) or {}

    def _extract_tempo_companies(self) -> dict[str, Any]:
        """Extract companies from Tempo API.

        Returns:
            Dictionary of Tempo companies.

        Raises:
            MigrationError: If companies cannot be extracted from Tempo

        """
        # Check if we already have companies loaded
        if self.tempo_companies:
            self.logger.info("Using %d companies from memory", len(self.tempo_companies))
            return self.tempo_companies

        # Try to load from file
        loaded_data = data_handler.load_dict(self.tempo_companies_file)
        if loaded_data:
            # If loaded data is a dictionary, use it directly
            if isinstance(loaded_data, dict):
                self.tempo_companies = loaded_data
            # If loaded data is a list (after JSON serialization/deserialization), convert back to dict
            elif isinstance(loaded_data, list):
                # Convert list to dictionary with id as key
                self.tempo_companies = {}
                for company in loaded_data:
                    if not isinstance(company, dict):
                        self.logger.warning(
                            "Skipping non-dictionary company: %s",
                            company,
                        )
                        continue

                    # Get ID from either 'id' or 'tempo_id' field
                    company_id = None
                    if "id" in company:
                        company_id = str(company["id"])
                    elif "tempo_id" in company:
                        company_id = str(company["tempo_id"])
                        # Add 'id' field if missing but 'tempo_id' exists
                        company["id"] = company_id

                    if company_id:
                        self.tempo_companies[company_id] = company
                    else:
                        self.logger.warning(
                            "Skipping company without id or tempo_id: %s",
                            company,
                        )

            self.logger.info("Loaded %d companies from cache", len(self.tempo_companies))
            return self.tempo_companies

        self.logger.info("Extracting Tempo companies...")

        # Get companies from Tempo API
        companies = self.jira_client.get_tempo_customers()

        if not companies:
            error_msg = "No companies found in Tempo"
            self.logger.error(error_msg)
            raise MigrationError(error_msg)

        self.logger.info("Found %d companies in Tempo", len(companies))

        # Process companies into dictionary
        self.tempo_companies = {}
        for company in companies:
            company_id = str(company.get("id"))
            self.tempo_companies[company_id] = {
                "id": company_id,
                "key": company.get("key", "").strip(),
                "name": company.get("name", "").strip(),
                "lead": (company.get("lead", {}).get("key") if company.get("lead") else None),
                "status": company.get("status", "ACTIVE"),
                "_raw": company,
            }

        # Save to file
        self._save_to_json(self.tempo_companies, Mappings.TEMPO_COMPANIES_FILE)
        self.logger.info(
            "Saved %d companies to %s",
            len(self.tempo_companies),
            self.tempo_companies_file,
        )

        return self.tempo_companies

    def _extract_openproject_projects(self) -> list[dict[str, Any]]:
        """Extract projects from OpenProject.

        Args:
            force: If True, forces re-extraction even if cached data exists.

        Returns:
            List of OpenProject project dictionaries

        Raises:
            MigrationError: If projects cannot be extracted from OpenProject

        """
        self.logger.info("Extracting projects from OpenProject...")

        try:
            self.op_projects = self.op_client.get_projects()

            # It's OK if there are no projects yet - this might be the initial migration
            if self.op_projects is None:
                self.op_projects = []

            self.logger.info(
                "Extracted %d projects from OpenProject",
                len(self.op_projects),
            )
            self._save_to_json(self.op_projects, Mappings.OP_PROJECTS_FILE)

            return self.op_projects
        except Exception as e:
            error_msg = f"Failed to get projects from OpenProject: {e!s}"
            self.logger.exception(error_msg)
            raise MigrationError(error_msg) from e

    def create_company_mapping(self) -> dict[str, Any]:
        """Create a mapping between Tempo companies and OpenProject top-level projects.

        Returns:
            Dictionary mapping Tempo company IDs to OpenProject project IDs

        Raises:
            MigrationError: If required data is missing

        """
        self.logger.info("Creating company mapping...")

        if not self.tempo_companies:
            self._extract_tempo_companies()

        if not self.op_projects:
            self._extract_openproject_projects()

        top_level_projects = [
            project for project in self.op_projects if project.get("_links", {}).get("parent", {}).get("href") is None
        ]

        op_projects_by_name = {project.get("name", "").lower(): project for project in top_level_projects}

        mapping = {}
        # Handle both dictionary and list types for tempo_companies
        tempo_companies_list = (
            self.tempo_companies.values() if isinstance(self.tempo_companies, dict) else self.tempo_companies
        )

        for tempo_company in tempo_companies_list:
            if not isinstance(tempo_company, dict):
                self.logger.warning(
                    "Skipping non-dictionary tempo_company: %s",
                    tempo_company,
                )
                continue

            # Get ID from either 'id' or 'tempo_id' field
            tempo_id = tempo_company.get("id")
            if not tempo_id and "tempo_id" in tempo_company:
                tempo_id = tempo_company.get("tempo_id")
                # Add 'id' if missing but 'tempo_id' exists
                tempo_company["id"] = tempo_id

            if not tempo_id:
                self.logger.warning(
                    "Skipping company without ID: %s",
                    tempo_company,
                )
                continue

            tempo_id = str(tempo_id)  # Ensure it's a string
            tempo_key = tempo_company.get("key", "")
            tempo_name = tempo_company.get("name", f"Unknown Company {tempo_id}")
            tempo_name_lower = tempo_name.lower()

            op_project = op_projects_by_name.get(tempo_name_lower)

            if op_project:
                mapping[tempo_id] = {
                    "tempo_id": tempo_id,
                    "tempo_key": tempo_key,
                    "tempo_name": tempo_name,
                    "openproject_id": op_project.get("id"),
                    "openproject_identifier": op_project.get("identifier"),
                    "openproject_name": op_project.get("name"),
                    "matched_by": "name",
                }
            else:
                mapping[tempo_id] = {
                    "tempo_id": tempo_id,
                    "tempo_key": tempo_key,
                    "tempo_name": tempo_name,
                    "openproject_id": None,
                    "openproject_identifier": None,
                    "openproject_name": None,
                    "matched_by": "none",
                }

        self.company_mapping = mapping
        self._save_to_json(mapping, Mappings.COMPANY_MAPPING_FILE)

        total_companies = len(mapping)
        matched_companies = sum(1 for company in mapping.values() if company["matched_by"] != "none")
        match_percentage = (matched_companies / total_companies) * 100 if total_companies > 0 else 0

        self.logger.info(
            "Company mapping created for %d companies",
            total_companies,
        )
        self.logger.info(
            "Successfully matched %d companies (%.1f%%)",
            matched_companies,
            match_percentage,
        )
        return mapping

    def analyze_company_mapping(self) -> dict[str, Any]:
        """Analyze the company mapping to identify potential issues.

        Returns:
            Dictionary with analysis results

        Raises:
            MigrationError: If company mapping does not exist

        """
        if not self.company_mapping:
            mapping_path = Path(self.data_dir).joinpath(Mappings.COMPANY_MAPPING_FILE)
            if mapping_path.exists():
                with mapping_path.open("r") as f:
                    self.company_mapping = json.load(f)
            else:
                error_msg = "No company mapping found. Run create_company_mapping() first."
                self.logger.error(error_msg)
                raise MigrationError(error_msg)

        analysis: dict[str, str | int | list[dict[str, str]]] = {
            "total_companies": len(self.company_mapping),
            "matched_companies": sum(1 for company in self.company_mapping.values() if company["matched_by"] != "none"),
            "matched_by_name": sum(1 for company in self.company_mapping.values() if company["matched_by"] == "name"),
            "matched_by_creation": sum(
                1 for company in self.company_mapping.values() if company["matched_by"] == "created"
            ),
            "matched_by_existing": sum(
                1 for company in self.company_mapping.values() if company["matched_by"] == "existing"
            ),
            "unmatched_companies": sum(
                1 for company in self.company_mapping.values() if company["matched_by"] == "none"
            ),
            "actually_created": self._created_companies,
            "unmatched_details": [
                {
                    "tempo_id": company["tempo_id"],
                    "tempo_key": company["tempo_key"],
                    "tempo_name": company["tempo_name"],
                }
                for company in self.company_mapping.values()
                if company["matched_by"] == "none"
            ],
        }

        total_companies = analysis.get("total_companies", 0)
        matched_companies = analysis.get("matched_companies", 0)
        if isinstance(total_companies, int) and isinstance(matched_companies, int) and total_companies > 0:
            analysis["match_percentage"] = int((matched_companies / total_companies) * 100)
        else:
            analysis["match_percentage"] = 0

        self._save_to_json(analysis, "company_mapping_analysis.json")

        self.logger.info("Company mapping analysis complete")
        self.logger.info("Total companies: %d", analysis["total_companies"])
        self.logger.info("Matched companies: %d (%.1f%%)", analysis["matched_companies"], analysis["match_percentage"])
        self.logger.info("- Matched by name: %d", analysis["matched_by_name"])
        self.logger.info("- Created in OpenProject: %d", analysis["actually_created"])
        self.logger.info("- Already existing in OpenProject: %d", analysis["matched_by_existing"])
        self.logger.info("Unmatched companies: %d", analysis["unmatched_companies"])

        return analysis

    def migrate_companies_bulk(self) -> dict[str, Any]:
        """Migrate companies from Tempo timesheet to OpenProject as top-level projects using bulk creation.

        This method uses a JSON-based approach similar to work package migration.

        Returns:
            Updated mapping with migration results

        Raises:
            MigrationError: If migration fails

        """
        self.logger.info("Starting bulk company migration...")

        if not self.tempo_companies:
            self._extract_tempo_companies()

        if not self.op_projects:
            self._extract_openproject_projects()

        if not self.company_mapping:
            self.create_company_mapping()

        companies_to_migrate = [
            company
            for company in self.tempo_companies.values()
            if self.company_mapping.get(company.get("id", company.get("tempo_id", "")), {}).get("matched_by") == "none"
        ]

        if not companies_to_migrate:
            self.logger.info("No companies found to migrate")
            return self.company_mapping

        self._created_companies = 0

        # Prepare company data for bulk creation
        companies_data = []
        identifier_to_tempo_id = {}  # Map identifiers to tempo IDs for later lookup

        for company in companies_to_migrate:
            # Get company ID from either field
            tempo_id = company.get("id")
            if not tempo_id and "tempo_id" in company:
                tempo_id = company.get("tempo_id")
                # Add 'id' if missing but 'tempo_id' exists
                company["id"] = tempo_id

            if not tempo_id:
                self.logger.warning("Skipping company without ID: %s", company)
                continue

            tempo_id = str(tempo_id)  # Ensure it's a string
            tempo_key = company.get("key", "")
            tempo_name = company.get("name", f"Unknown Company {tempo_id}")
            lead = company.get("lead", "")

            description = f"Migrated from Tempo company: {tempo_key}\n"
            if lead:
                description += f"Company Lead: {lead}\n"

            # Generate a valid identifier
            base_identifier = "customer_"
            if tempo_key:
                raw_id = tempo_key.lower()
                sanitized_id = re.sub(r"[^a-z0-9_-]", "_", raw_id)
                base_identifier += sanitized_id
            else:
                raw_id = tempo_name.lower()
                sanitized_id = re.sub(r"[^a-z0-9_-]", "_", raw_id)
                base_identifier += sanitized_id

            identifier = base_identifier[:100]
            identifier_to_tempo_id[identifier] = tempo_id

            # Generate the Jira URL for this Tempo company
            jira_base_url = config.jira_config.get("url", "").rstrip("/")
            jira_url = f"{jira_base_url}/rest/tempo-accounts/1/customer/{tempo_id}"

            # Add to the companies data for bulk creation
            companies_data.append(
                {
                    "tempo_id": tempo_id,
                    "tempo_key": tempo_key,
                    "tempo_name": tempo_name,
                    "name": tempo_name,
                    "identifier": identifier,
                    "description": description,
                    "status": company.get("status", "ACTIVE"),
                    "public": False,
                    "jira_url": jira_url,  # Add Jira URL for tracking
                },
            )

        if not companies_data:
            self.logger.info("No companies need to be created, all matched or already exist")
            self._save_to_json(self.company_mapping, Mappings.COMPANY_MAPPING_FILE)
            return self.company_mapping

        # Now do a bulk check for existing projects with these identifiers
        self.logger.info("Checking for existing projects with %d identifiers...", len(identifier_to_tempo_id))

        # Create a Ruby script to check all identifiers at once
        identifiers_list = list(identifier_to_tempo_id.keys())
        check_script = f"""
        identifiers = {json.dumps(identifiers_list)}
        existing_projects = Project.where(identifier: identifiers).pluck(:identifier, :id, :name)
        existing_map = {{}}
        existing_projects.each do |identifier, id, name|
          existing_map[identifier] = {{'id' => id, 'name' => name}}
        end
        existing_map
        """

        existing_projects_map = self.op_client.execute_json_query(check_script) or {}

        # Filter out companies that already exist
        companies_to_create = []
        for company_data in companies_data:
            identifier = company_data["identifier"]
            tempo_id = company_data["tempo_id"]

            if identifier in existing_projects_map:
                # Project already exists, update mapping
                existing = existing_projects_map[identifier]
                self.company_mapping[tempo_id] = {
                    "tempo_id": tempo_id,
                    "tempo_key": company_data["tempo_key"],
                    "tempo_name": company_data["tempo_name"],
                    "openproject_id": existing.get("id"),
                    "openproject_identifier": identifier,
                    "openproject_name": existing.get("name"),
                    "matched_by": "existing",
                }
            else:
                # Project doesn't exist, add to creation list
                companies_to_create.append(company_data)

        if not companies_to_create:
            self.logger.info("No companies need to be created, all already exist")
            self._save_to_json(self.company_mapping, Mappings.COMPANY_MAPPING_FILE)
            return self.company_mapping

        if config.migration_config.get("dry_run"):
            self.logger.info(
                "DRY RUN: Would create %d company projects",
                len(companies_to_create),
            )
            for company_data in companies_to_create:
                tempo_id = company_data["tempo_id"]
                self.company_mapping[tempo_id] = {
                    "tempo_id": tempo_id,
                    "tempo_key": company_data["tempo_key"],
                    "tempo_name": company_data["tempo_name"],
                    "openproject_id": None,
                    "openproject_identifier": company_data["identifier"],
                    "openproject_name": company_data["name"],
                    "matched_by": "would_create",
                }
            self._save_to_json(self.company_mapping, Mappings.COMPANY_MAPPING_FILE)
            return self.company_mapping

        # First, write the companies data to a JSON file that Rails can read
        temp_file_path = Path(self.data_dir).joinpath("tempo_companies_to_create.json")
        self.logger.info(
            "Writing %d companies to %s",
            len(companies_to_create),
            temp_file_path,
        )

        try:
            # Write the JSON file
            with temp_file_path.open("w") as f:
                json.dump(companies_to_create, f, indent=2)

            # Define the path for the file inside the container
            container_temp_path = Path("/tmp/tempo_companies.json")

            # Copy the file to the container
            self.op_client.transfer_file_to_container(temp_file_path, container_temp_path)

            # Process companies in smaller batches to avoid console crashes
            batch_size = 10  # Process companies in batches of 10 to avoid large script issues
            total_companies = len(companies_to_create)
            created_companies = []
            errors = []

            for i in range(0, total_companies, batch_size):
                batch_end = min(i + batch_size, total_companies)
                batch_companies = companies_to_create[i:batch_end]

                self.logger.info(
                    "Processing batch %d-%d of %d companies", i + 1, batch_end, total_companies
                )

                # Create a smaller JSON file for this batch
                batch_file_path = Path(self.data_dir).joinpath(f"tempo_companies_batch_{i}.json")
                with batch_file_path.open("w") as f:
                    json.dump(batch_companies, f, indent=2)

                # Copy batch file to container
                batch_container_path = Path(f"/tmp/tempo_companies_batch_{i}.json")
                self.op_client.transfer_file_to_container(batch_file_path, batch_container_path)

                # Prepare smaller Ruby script for this batch
                batch_result = self._create_companies_batch(batch_container_path, i)

                if batch_result:
                    batch_companies = batch_result.get("created", [])
                    created_companies.extend(batch_companies)
                    errors.extend(batch_result.get("errors", []))

                    # Update company mapping with both existing and newly created companies
                    for company in batch_companies:
                        tempo_id = company.get("tempo_id")
                        if tempo_id:
                            self.company_mapping[str(tempo_id)] = {
                                "tempo_id": tempo_id,
                                "tempo_key": company.get("tempo_key"),
                                "tempo_name": company.get("tempo_name"),
                                "openproject_id": company.get("openproject_id"),
                                "openproject_identifier": company.get("openproject_identifier"),
                                "openproject_name": company.get("openproject_name"),
                                "matched_by": company.get("status", "created"),  # "existing" or "created"
                            }

                # Clean up batch file
                try:
                    batch_file_path.unlink()
                except OSError:
                    pass

            # Prepare summary result
            output = {
                "status": "success",
                "created": created_companies,
                "errors": errors,
                "created_count": len(created_companies),
                "error_count": len(errors),
                "total": total_companies
            }



            self.logger.info(
                "Created %d company projects (errors: %d)", output.get("created_count", 0), output.get("error_count", 0),
            )

            # Refresh cached projects from OpenProject
            self.op_client.get_projects()

            # Save the updated mapping
            self._save_to_json(self.company_mapping, Mappings.COMPANY_MAPPING_FILE)

            return self.company_mapping

        except Exception as e:
            error_msg = f"Failed to migrate companies: {e}"
            self.logger.exception(error_msg)
            raise MigrationError(error_msg) from e

    def _create_companies_batch(self, batch_file_path: Path, batch_index: int) -> dict[str, Any] | None:
        """Create a batch of companies using a smaller Ruby script to avoid console crashes.

        Args:
            batch_file_path: Path to the JSON file containing the batch of companies
            batch_index: Index of this batch for logging

        Returns:
            Dictionary with created companies and errors, or None if execution failed
        """
        try:
            # Create a compact Ruby script for this batch
            ruby_script = f"""
            require 'json'

            # Load batch data
            companies_data = JSON.parse(File.read('{batch_file_path}'))
            puts "Processing batch with #{{companies_data.size}} companies"

            created_companies = []
            errors = []

            # Get custom fields
            jira_url_cf = CustomField.find_by(type: 'ProjectCustomField', name: 'Jira URL')
            tempo_id_cf = CustomField.find_by(type: 'ProjectCustomField', name: 'Tempo ID')

            companies_data.each do |company|
              begin
                tempo_id = company['tempo_id']
                tempo_name = company['tempo_name']
                jira_url = company['jira_url']

                # Check for existing project by URL first
                existing_project = nil
                if jira_url && jira_url_cf
                  existing_project = Project.joins(:custom_values)
                    .where(custom_values: {{ custom_field_id: jira_url_cf.id, value: jira_url }})
                    .first
                end

                # Check by Tempo ID if not found by URL
                if !existing_project && tempo_id && tempo_id_cf
                  existing_project = Project.joins(:custom_values)
                    .where(custom_values: {{ custom_field_id: tempo_id_cf.id, value: tempo_id.to_s }})
                    .first
                end

                # Check by identifier as last resort
                if !existing_project
                  existing_project = Project.find_by(identifier: company['identifier'])
                end

                if existing_project
                  created_companies << {{
                    'tempo_id' => tempo_id,
                    'tempo_key' => company['tempo_key'],
                    'tempo_name' => tempo_name,
                    'openproject_id' => existing_project.id,
                    'openproject_identifier' => existing_project.identifier,
                    'openproject_name' => existing_project.name,
                    'status' => 'existing'
                  }}
                  next
                end

                # Create new project
                project = Project.new(
                  name: company['name'],
                  identifier: company['identifier'],
                  description: company['description'],
                  public: false
                )

                if project.save
                  # Set custom fields
                  if tempo_id_cf && tempo_id
                    cv = project.custom_values.find_or_initialize_by(custom_field_id: tempo_id_cf.id)
                    cv.value = tempo_id.to_s
                    cv.save
                  end

                  if jira_url_cf && jira_url
                    cv = project.custom_values.find_or_initialize_by(custom_field_id: jira_url_cf.id)
                    cv.value = jira_url
                    cv.save
                  end

                  created_companies << {{
                    'tempo_id' => tempo_id,
                    'tempo_key' => company['tempo_key'],
                    'tempo_name' => tempo_name,
                    'openproject_id' => project.id,
                    'openproject_identifier' => project.identifier,
                    'openproject_name' => project.name,
                    'status' => 'created'
                  }}
                else
                  errors << {{
                    'tempo_id' => tempo_id,
                    'tempo_key' => company['tempo_key'],
                    'tempo_name' => tempo_name,
                    'identifier' => company['identifier'],
                    'errors' => project.errors.full_messages
                  }}
                end
              rescue => e
                errors << {{
                  'tempo_id' => company['tempo_id'],
                  'tempo_key' => company['tempo_key'],
                  'tempo_name' => company['tempo_name'],
                  'identifier' => company['identifier'],
                  'errors' => [e.message]
                }}
              end
            end

            result = {{
              'created' => created_companies,
              'errors' => errors,
              'created_count' => created_companies.size,
              'error_count' => errors.size
            }}

            File.write('/tmp/batch_result_{batch_index}.json', result.to_json)
            result
            """

            # Execute the batch script with shorter timeout
            result = self.op_client.execute_json_query(ruby_script, timeout=30)

            if isinstance(result, dict):
                return result
            else:
                self.logger.error("Batch %d: Expected dict result, got %s: %s", batch_index, type(result), str(result)[:200])
                raise ValueError(f"Batch {batch_index}: Invalid result format - expected dict, got {type(result)}")

        except (ValueError, Exception) as e:
            self.logger.error("Failed to create companies batch %d: %s", batch_index, e)
            # For batch processing, we want to stop on error to avoid data corruption
            if self.config.migration_config.get("stop_on_error", False):
                raise
            return None

    def migrate_customer_metadata(self) -> ComponentResult:
        """Migrate customer metadata (address, contact info, etc.) to OpenProject projects.

        This enhances company projects with additional metadata from Tempo.

        Returns:
            Dict with results stats

        """
        self.logger.info("Starting migration of customer metadata...")

        results: ComponentResult = ComponentResult(
            updated=0,
            failed=0,
            errors=[],
            warnings=[],
        )

        # Check for valid mappings
        if not hasattr(self, "company_mapping") or not self.company_mapping:
            self.logger.warning(
                "No company mapping available, run create_company_mapping first",
            )
            results["warnings"].append("No company mapping available")
            return results

        # Find all Tempo companies with OpenProject mapping
        companies_to_update = []

        for tempo_id, mapping in self.company_mapping.items():
            # Only process companies that were matched or created in OpenProject
            op_project_id = mapping.get("openproject_id")
            if not op_project_id:
                continue

            # Get the Tempo company data
            tempo_company = self.tempo_companies.get(tempo_id)
            if not tempo_company:
                continue

            raw_data = tempo_company.get("_raw", {})

            # Base metadata that's always included
            metadata = {
                "tempo_id": tempo_id,
                "openproject_id": op_project_id,
                "key": tempo_company.get("key", ""),
                "name": tempo_company.get("name", ""),
                "status": tempo_company.get("status", "ACTIVE"),
            }

            # Contact information if available
            if "lead" in tempo_company:
                lead = tempo_company.get("lead")
                if isinstance(lead, dict):
                    metadata["lead_key"] = lead.get("key", "")
                    metadata["lead_display_name"] = lead.get("displayName", "")
                elif isinstance(lead, str):
                    metadata["lead_key"] = lead

            # Additional information from raw data if available
            for field in [
                "addressLine1",
                "addressLine2",
                "city",
                "state",
                "zipCode",
                "country",
                "phoneNumber",
                "faxNumber",
                "email",
                "website",
            ]:
                if raw_data and field in raw_data:
                    metadata[field] = raw_data.get(field, "")

            companies_to_update.append(metadata)

        if not companies_to_update:
            self.logger.warning("No companies with metadata to update")
            return results

        # Write data to a temp file
        temp_file_path = Path(self.data_dir).joinpath("company_metadata.json")
        with temp_file_path.open("w") as f:
            json.dump(companies_to_update, f, indent=2)

        # Transfer file to container
        container_temp_path = Path("/tmp/company_metadata.json")
        result_file_local = Path(self.data_dir).joinpath("company_metadata_result.json")

        self.op_client.transfer_file_to_container(temp_file_path, container_temp_path)

        # Prepare Ruby script to update metadata
        self.logger.info(
            "Updating metadata for %d companies...",
            len(companies_to_update),
        )

        result_file_container = Path("/tmp/company_metadata_result.json")

        # Ruby script to execute
        ruby_script = """
        metadata_file_path = '/tmp/company_metadata.json'
        result_file_path = '/tmp/company_metadata_result.json'

        require 'json'

        # Load the metadata from the JSON file
        companies = JSON.parse(File.read(metadata_file_path))
        puts "Loaded metadata for #{companies.size} companies"

        updated_companies = []
        errors = []

        # Check if we have project custom fields available
        begin
          has_custom_fields = defined?(CustomField) && CustomField.where(type: 'ProjectCustomField').exists?
          puts "Project custom fields available: #{has_custom_fields}"

          # Create custom fields if needed
          if has_custom_fields
            # Create contact custom fields if they don't exist
            contact_cf = CustomField.where(type: 'ProjectCustomField', name: 'Customer Contact')
              .first_or_create do |cf|
              cf.field_format = 'text'
              cf.is_required = false
              puts "Created 'Customer Contact' custom field"
            end

            address_cf = CustomField.where(type: 'ProjectCustomField', name: 'Customer Address')
              .first_or_create do |cf|
              cf.field_format = 'text'
              cf.is_required = false
              puts "Created 'Customer Address' custom field"
            end

            website_cf = CustomField.where(type: 'ProjectCustomField', name: 'Website').first_or_create do |cf|
              cf.field_format = 'text'
              cf.is_required = false
              puts "Created 'Website' custom field"
            end

            tempo_id_cf = CustomField.where(type: 'ProjectCustomField', name: 'Tempo ID').first_or_create do |cf|
              cf.field_format = 'string'
              cf.is_required = false
              puts "Created 'Tempo ID' custom field"
            end

            jira_url_cf = CustomField.where(type: 'ProjectCustomField', name: 'Jira URL').first_or_create do |cf|
              cf.field_format = 'text'
              cf.is_required = false
              puts "Created 'Jira URL' custom field"
            end
          end
        rescue => cf_error
          puts "Error with custom fields: #{cf_error.message}"
          has_custom_fields = false
        end

        # Update each company project
        companies.each do |metadata|
          begin
            op_project_id = metadata['openproject_id']
            tempo_id = metadata['tempo_id']

            # Find the project
            project = Project.find_by(id: op_project_id)

            if project.nil?
              errors << {
                'tempo_id' => tempo_id,
                'openproject_id' => op_project_id,
                'error' => "Project not found"
              }
              puts "Error: Project with ID #{op_project_id} not found"
              next
            end

            # Update description to include additional metadata
            description = project.description || ""

            # Add contact information
            contact_info = []
            if metadata['lead_display_name']
              contact_info << "Lead: #{metadata['lead_display_name']} (#{metadata['lead_key']})"
            end
            contact_info << "Email: #{metadata['email']}" if metadata['email']
            contact_info << "Phone: #{metadata['phoneNumber']}" if metadata['phoneNumber']
            contact_info << "Fax: #{metadata['faxNumber']}" if metadata['faxNumber']

            # Add address information
            address_info = []
            address_info << metadata['addressLine1'] if metadata['addressLine1']
            address_info << metadata['addressLine2'] if metadata['addressLine2']
            if metadata['city'] || metadata['state'] || metadata['zipCode']
              address_info << "#{metadata['city']}, #{metadata['state']} #{metadata['zipCode']}"
            end
            address_info << metadata['country'] if metadata['country']

            # Build enhanced description
            new_description = description

            # Only add metadata section if it doesn't exist already
            unless description.include?("## Customer Metadata")
              new_description += "\n\n## Customer Metadata\n"
              new_description += "\nTempo ID: #{tempo_id}" if tempo_id

              if metadata['status']
                status_text = metadata['status'].capitalize
                new_description += "\nStatus: #{status_text}"
              end

              if contact_info.any?
                new_description += "\n\n### Contact Information\n"
                contact_info.each do |line|
                  new_description += "\n#{line}"
                end
              end

              if address_info.any?
                new_description += "\n\n### Address\n"
                address_info.each do |line|
                  new_description += "\n#{line}"
                end
              end

              if metadata['website']
                new_description += "\n\n### Website\n"
                new_description += "\n#{metadata['website']}"
              end
            end

            # Update project description
            project.description = new_description

            # Set status if applicable
            begin
              if metadata['status'] && ['CLOSED', 'ARCHIVED'].include?(metadata['status'])
                if defined?(ProjectStatus)
                  status = ProjectStatus.find_or_create_by(project_id: project.id)
                  status.code = 'finished'
                  status.save
                  puts "Set project status to 'finished'"
                end
              end
            rescue => status_error
              puts "Error setting status: #{status_error.message}"
            end

            # Set custom fields if available
            if has_custom_fields
              begin
                # Set contact information
                if contact_cf && contact_info.any?
                  contact_value = contact_info.join("\n")
                  custom_value = project.custom_values.find_or_initialize_by(custom_field_id: contact_cf.id)
                  custom_value.value = contact_value
                  custom_value.save
                end

                # Set address
                if address_cf && address_info.any?
                  address_value = address_info.join("\n")
                  custom_value = project.custom_values.find_or_initialize_by(custom_field_id: address_cf.id)
                  custom_value.value = address_value
                  custom_value.save
                end

                # Set website
                if website_cf && metadata['website']
                  custom_value = project.custom_values.find_or_initialize_by(custom_field_id: website_cf.id)
                  custom_value.value = metadata['website']
                  custom_value.save
                end

                # Set Tempo ID
                if tempo_id_cf && tempo_id
                  custom_value = project.custom_values.find_or_initialize_by(custom_field_id: tempo_id_cf.id)
                  custom_value.value = tempo_id
                  custom_value.save
                end
              rescue => cf_error
                puts "Error setting custom field values: #{cf_error.message}"
              end
            end

            # Save the project
            if project.save
              updated_companies << {
                'tempo_id' => tempo_id,
                'openproject_id' => op_project_id,
                'name' => project.name,
                'updated_description' => description != new_description,
                'updated_custom_fields' => has_custom_fields
              }
              puts "Updated metadata for project #{op_project_id}: #{project.name}"
            else
              errors << {
                'tempo_id' => tempo_id,
                'openproject_id' => op_project_id,
                'error' => project.errors.full_messages.join(', ')
              }
              puts "Error updating project: #{project.errors.full_messages.join(', ')}"
            end
          rescue => e
            errors << {
              'tempo_id' => metadata['tempo_id'],
              'openproject_id' => metadata['openproject_id'],
              'error' => e.message
            }
            puts "Exception: #{e.message}"
          end
        end

        # Write results to file
        result = {
          'status' => 'success',
          'updated' => updated_companies,
          'errors' => errors,
          'updated_count' => updated_companies.size,
          'error_count' => errors.size,
          'total' => companies.size
        }

        File.write(result_file_path, result.to_json)
        """

        # Execute the Ruby script using file-based method to avoid IOError
        self.op_client.execute_json_query(ruby_script)

        # Get the result data
        updated_companies = []
        errors = []

        self.op_client.transfer_file_from_container(
            result_file_container,
            result_file_local,
        )

        with result_file_local.open("r") as f:
            result_data = json.load(f)
            if result_data.get("status") == "success":
                updated_companies = result_data.get("updated", [])
                errors = result_data.get("errors", [])

        # Update results
        results["updated"] = len(updated_companies)
        results["failed"] = len(errors)
        if errors:
            results["errors"] = [
                f"{e.get('tempo_id', 'Unknown')}: {e.get('error', 'Unknown error')}" for e in errors[:10]
            ]
            if len(errors) > 10:
                results["errors"].append(f"...and {len(errors) - 10} more errors")

        # Log results
        if results["updated"] > 0:
            self.logger.success(
                "Updated metadata for %d companies",
                results["updated"],
            )
        if results["failed"] > 0:
            self.logger.warning(
                "Failed to update metadata for %d companies",
                results["failed"],
            )

        return results

    def run(self) -> ComponentResult:
        """Run the company migration process.

        Returns:
            Dictionary with migration results

        """
        self.logger.info("Starting company migration")

        try:
            # Extract data once and cache it
            self._extract_tempo_companies()
            self._extract_openproject_projects()

            # Create mapping
            self.create_company_mapping()

            # Migrate companies
            if not config.migration_config.get("dry_run", False):
                self.migrate_companies_bulk()
            else:
                self.logger.warning("Dry run mode - not creating companies")

            # Analyze results
            analysis = self.analyze_company_mapping()

            # Update mappings in global configuration
            if hasattr(config, 'mappings') and config.mappings:
                config.mappings.set_mapping("companies", self.company_mapping)

            return ComponentResult(
                success=True,
                data=analysis,
                success_count=analysis["matched_companies"],
                failed_count=analysis["unmatched_companies"],
                total_count=analysis["total_companies"],
            )
        except Exception as e:
            self.logger.exception("Error during company migration: %s", e)
            return ComponentResult(
                success=False,
                errors=[f"Error during company migration: {e}"],
                success_count=0,
                failed_count=len(self.tempo_companies) if self.tempo_companies else 0,
                total_count=len(self.tempo_companies) if self.tempo_companies else 0,
            )
