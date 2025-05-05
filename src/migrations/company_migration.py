"""
Company migration module for Jira to OpenProject migration.
Handles the migration of Tempo timesheet companies from Jira to OpenProject as top-level projects.
"""

import json
import os
import re
from pathlib import Path
from typing import Any

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.mappings.mappings import Mappings
from src.migrations.base_migration import BaseMigration
from src.models import ComponentResult
from src.utils import data_handler


class CompanyMigration(BaseMigration):
    """
    Handles the migration of companies from Tempo timesheet to OpenProject.

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
        """
        Initialize the company migration.

        Args:
            jira_client: JiraClient instance.
            op_client: OpenProjectClient instance.
            data_dir: Path to data directory for storing mappings.
        """
        super().__init__(jira_client, op_client)

        # Configure paths
        self.data_dir = Path(data_dir or config.get_path("data"))
        os.makedirs(self.data_dir, exist_ok=True)

        # Setup file paths
        self.tempo_companies_file = self.data_dir / Mappings.TEMPO_COMPANIES_FILE
        self.op_projects_file = self.data_dir / Mappings.OP_PROJECTS_FILE

        # Data storage
        self.tempo_companies = {}
        self.op_projects = {}
        self.company_mapping = {}
        self._created_companies = 0  # Initialize counter for created companies

        # Logging
        self.logger.debug(
            f"CompanyMigration initialized with data dir: {self.data_dir}"
        )

        # Load existing data if available
        self.tempo_companies = self._load_from_json(Mappings.TEMPO_COMPANIES_FILE) or {}
        self.op_projects = self._load_from_json(Mappings.OP_PROJECTS_FILE) or {}
        self.company_mapping = self._load_from_json(Mappings.COMPANY_MAPPING_FILE) or {}

    def extract_tempo_companies(self) -> dict[str, Any]:
        """
        Extract companies from Tempo API.

        Returns:
            Dictionary of Tempo companies.
        """
        if data_handler.load_dict(self.tempo_companies_file):
            loaded_data = data_handler.load_dict(self.tempo_companies_file)

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
                            f"Skipping non-dictionary company: {company}"
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
                            f"Skipping company without id or tempo_id: {company}"
                        )

            self.logger.info(f"Loaded {len(self.tempo_companies)} companies from cache")
            return self.tempo_companies

        self.logger.info("Extracting Tempo companies...")

        # Get companies from Tempo API
        companies = self.jira_client.get_tempo_customers()

        if not companies:
            self.logger.warning("No companies found in Tempo")
            self.tempo_companies = {}
            return self.tempo_companies

        self.logger.info(f"Found {len(companies)} companies in Tempo")

        # Process companies
        for company in companies:
            company_id = str(company.get("id"))
            self.tempo_companies[company_id] = {
                "id": company_id,
                "key": company.get("key", "").strip(),
                "name": company.get("name", "").strip(),
                "lead": (
                    company.get("lead", {}).get("key") if company.get("lead") else None
                ),
                "status": company.get("status", "ACTIVE"),
                "_raw": company,
            }

        # Save to file
        self._save_to_json(self.tempo_companies, Mappings.TEMPO_COMPANIES_FILE)
        self.logger.info(
            f"Saved {len(self.tempo_companies)} companies to {self.tempo_companies_file}"
        )

        return self.tempo_companies

    def extract_openproject_projects(self, force: bool = False) -> list[dict[str, Any]]:
        """
        Extract projects from OpenProject.

        Args:
            force: If True, forces re-extraction even if cached data exists.

        Returns:
            List of OpenProject project dictionaries
        """
        self.logger.info("Extracting projects from OpenProject...")

        try:
            self.op_projects = self.op_client.get_projects()
        except Exception as e:
            self.logger.warning(f"Failed to get projects from OpenProject: {str(e)}")
            self.logger.warning("Using an empty list of projects for OpenProject")
            self.op_projects = []

        self.logger.info(f"Extracted {len(self.op_projects)} projects from OpenProject")

        self._save_to_json(self.op_projects, Mappings.OP_PROJECTS_FILE)

        return self.op_projects

    def create_company_mapping(self) -> dict[str, Any]:
        """
        Create a mapping between Tempo companies and OpenProject top-level projects.

        Returns:
            Dictionary mapping Tempo company IDs to OpenProject project IDs
        """
        self.logger.info("Creating company mapping...")

        if not self.tempo_companies:
            self.extract_tempo_companies()

        if not self.op_projects:
            self.extract_openproject_projects()

        top_level_projects = [
            project
            for project in self.op_projects
            if project.get("_links", {}).get("parent", {}).get("href") is None
        ]

        op_projects_by_name = {
            project.get("name", "").lower(): project for project in top_level_projects
        }

        mapping = {}
        # Handle both dictionary and list types for tempo_companies
        tempo_companies_list = (
            self.tempo_companies.values()
            if isinstance(self.tempo_companies, dict)
            else self.tempo_companies
        )

        for tempo_company in tempo_companies_list:
            if not isinstance(tempo_company, dict):
                self.logger.warning(
                    f"Skipping non-dictionary tempo_company: {tempo_company}"
                )
                continue

            # Get ID from either 'id' or 'tempo_id' field
            tempo_id = tempo_company.get("id")
            if not tempo_id and "tempo_id" in tempo_company:
                tempo_id = tempo_company.get("tempo_id")
                # Add 'id' if missing but 'tempo_id' exists
                tempo_company["id"] = tempo_id

            if not tempo_id:
                self.logger.warning(f"Skipping company without ID: {tempo_company}")
                continue

            tempo_key = tempo_company.get("key", "")
            tempo_name = tempo_company.get("name", f"Unknown Company {tempo_id}")
            tempo_name_lower = tempo_name.lower()

            op_project = op_projects_by_name.get(tempo_name_lower, None)

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
        matched_companies = sum(
            1 for company in mapping.values() if company["matched_by"] != "none"
        )
        match_percentage = (
            (matched_companies / total_companies) * 100 if total_companies > 0 else 0
        )

        self.logger.info(f"Company mapping created for {total_companies} companies")
        self.logger.info(
            f"Successfully matched {matched_companies} companies ({match_percentage:.1f}%)"
        )

        return mapping

    def analyze_company_mapping(self) -> dict[str, Any]:
        """
        Analyze the company mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        if not self.company_mapping:
            mapping_path = os.path.join(self.data_dir, Mappings.COMPANY_MAPPING_FILE)
            if os.path.exists(mapping_path):
                with open(mapping_path) as f:
                    self.company_mapping = json.load(f)
            else:
                self.logger.error(
                    "No company mapping found. Run create_company_mapping() first."
                )
                return {}

        analysis = {
            "total_companies": len(self.company_mapping),
            "matched_companies": sum(
                1
                for company in self.company_mapping.values()
                if company["matched_by"] != "none"
            ),
            "matched_by_name": sum(
                1
                for company in self.company_mapping.values()
                if company["matched_by"] == "name"
            ),
            "matched_by_creation": sum(
                1
                for company in self.company_mapping.values()
                if company["matched_by"] == "created"
            ),
            "matched_by_existing": sum(
                1
                for company in self.company_mapping.values()
                if company["matched_by"] == "existing"
            ),
            "unmatched_companies": sum(
                1
                for company in self.company_mapping.values()
                if company["matched_by"] == "none"
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

        total = analysis["total_companies"]
        if total > 0:
            analysis["match_percentage"] = (analysis["matched_companies"] / total) * 100
        else:
            analysis["match_percentage"] = 0

        self._save_to_json(analysis, "company_mapping_analysis.json")

        self.logger.info("Company mapping analysis complete")
        self.logger.info(f"Total companies: {analysis['total_companies']}")
        self.logger.info(
            f"Matched companies: {analysis['matched_companies']} ({analysis['match_percentage']:.1f}%)"
        )
        self.logger.info(f"- Matched by name: {analysis['matched_by_name']}")
        self.logger.info(f"- Created in OpenProject: {analysis['actually_created']}")
        self.logger.info(
            f"- Already existing in OpenProject: {analysis['matched_by_existing']}"
        )
        self.logger.info(f"Unmatched companies: {analysis['unmatched_companies']}")

        return analysis

    def migrate_companies_bulk(self) -> dict[str, Any]:
        """
        Migrate companies from Tempo timesheet to OpenProject as top-level projects using bulk creation.
        This method uses a JSON-based approach similar to work package migration.

        Returns:
            Updated mapping with migration results
        """
        self.logger.info("Starting bulk company migration...")

        if not self.tempo_companies:
            self.extract_tempo_companies()

        if not self.op_projects:
            self.extract_openproject_projects()

        if not self.company_mapping:
            self.create_company_mapping()

        companies_to_migrate = [
            company
            for company in self.tempo_companies.values()
            if self.company_mapping.get(
                company.get("id", company.get("tempo_id", "")), {}
            ).get("matched_by")
            == "none"
        ]

        if not companies_to_migrate:
            self.logger.warning("No companies found to migrate")
            return self.company_mapping

        self._created_companies = 0

        # Prepare company data for bulk creation
        companies_data = []
        for company in companies_to_migrate:
            # Get company ID from either field
            tempo_id = company.get("id")
            if not tempo_id and "tempo_id" in company:
                tempo_id = company.get("tempo_id")
                # Add 'id' if missing but 'tempo_id' exists
                company["id"] = tempo_id

            if not tempo_id:
                self.logger.warning(f"Skipping company without ID: {company}")
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

            # Check if project with this identifier already exists
            existing = self.op_client.get_project_by_identifier(identifier)
            if existing:
                self.company_mapping[tempo_id] = {
                    "tempo_id": tempo_id,
                    "tempo_key": tempo_key,
                    "tempo_name": tempo_name,
                    "openproject_id": existing.get("id"),
                    "openproject_identifier": existing.get("identifier"),
                    "openproject_name": existing.get("name"),
                    "matched_by": "existing",
                }
                continue

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
                }
            )

        if not companies_data:
            self.logger.info(
                "No companies need to be created, all matched or already exist"
            )
            self._save_to_json(self.company_mapping, Mappings.COMPANY_MAPPING_FILE)
            return self.company_mapping

        if config.migration_config.get("dry_run"):
            self.logger.info(
                f"DRY RUN: Would create {len(companies_data)} company projects"
            )
            for company_data in companies_data:
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
        temp_file_path = os.path.join(self.data_dir, Mappings.TEMPO_COMPANIES_FILE)
        self.logger.info(f"Writing {len(companies_data)} companies to {temp_file_path}")

        # Write the JSON file
        with open(temp_file_path, "w") as f:
            json.dump(companies_data, f, indent=2)

        # Define the path for the file inside the container
        container_temp_path = "/tmp/tempo_companies.json"
        result_file_container = "/tmp/company_creation_result.json"
        result_file_local = os.path.join(self.data_dir, "company_creation_result.json")

        # Copy the file to the container
        if not self.op_client.rails_client.transfer_file_to_container(
            temp_file_path, container_temp_path
        ):
            self.logger.error("Failed to transfer companies data to container")
            return self.company_mapping

        # Prepare and execute the Ruby script
        self.logger.info(
            f"Executing Rails script to create {len(companies_data)} companies"
        )

        # Ruby script to create companies
        ruby_script = """
        container_temp_path = '/tmp/tempo_companies.json'
        result_file_container = '/tmp/company_creation_result.json'

        require 'json'

        # Load the data from the JSON file
        companies_data = JSON.parse(File.read(container_temp_path))
        companies_count = companies_data.size
        puts "Loaded #{companies_count} companies from JSON file"

        created_companies = []
        errors = []

        # Create each company project
        companies_data.each do |company|
          begin
            # Store Tempo data for mapping
            tempo_id = company['tempo_id']
            tempo_key = company['tempo_key']
            tempo_name = company['tempo_name']
            tempo_status = company['status'] || 'ACTIVE'

            # Map Tempo status to OpenProject status code
            status_code = 'on_track'  # Default status
            if ['CLOSED', 'ARCHIVED'].include?(tempo_status)
              status_code = 'finished'
            end

            # Create project object with only the needed attributes
            project = Project.new(
              name: company['name'],
              identifier: company['identifier'],
              description: company['description'],
              public: false
            )

            # Save the project
            if project.save
              # Set the project status using appropriate model
              begin
                if defined?(Status)  # Try Status model first (older OpenProject)
                  begin
                    # Map status code to name
                    status_name = 'On track'
                    if status_code == 'finished'
                      status_name = 'Finished'
                    end

                    status = Status.find_or_create_by(name: status_name)
                    if project.respond_to?(:status=)
                      project.status = status
                      project.save
                      puts "Set project status to #{status_name} using Status model"
                    else
                      puts "Project does not respond to status="
                    end
                  rescue => e1
                    puts "Status model error: #{e1.message}"

                    # Try ProjectStatus instead
                    if defined?(ProjectStatus)
                      begin
                        ps = ProjectStatus.find_or_create_by(project_id: project.id)
                        ps.code = status_code
                        ps.save
                        puts "Set project status to #{status_code} using ProjectStatus model"
                      rescue => e2
                        puts "ProjectStatus error: #{e2.message}"
                      end
                    else
                      puts "Neither Status nor ProjectStatus models available"
                    end
                  end
                elsif defined?(ProjectStatus)  # Try ProjectStatus model directly
                  begin
                    ps = ProjectStatus.find_or_create_by(project_id: project.id)
                    ps.code = status_code
                    ps.save
                    puts "Set project status to #{status_code} using ProjectStatus model"
                  rescue => e
                    puts "ProjectStatus error: #{e.message}"
                  end
                else
                  puts "No status models available"
                end
              rescue => status_error
                puts "Error setting project status for #{project.name}: #{status_error.message}"
              end

              created_companies << {
                'tempo_id' => tempo_id,
                'tempo_key' => tempo_key,
                'tempo_name' => tempo_name,
                'openproject_id' => project.id,
                'openproject_identifier' => project.identifier,
                'openproject_name' => project.name,
                'status' => status_code
              }
              puts "Created project #{project.id}: #{project.name} with status #{status_code}"
            else
              errors << {
                'tempo_id' => tempo_id,
                'tempo_key' => tempo_key,
                'tempo_name' => tempo_name,
                'identifier' => company['identifier'],
                'errors' => project.errors.full_messages,
                'error_type' => 'validation_error'
              }
              puts "Error creating project: #{project.errors.full_messages.join(', ')}"
            end
          rescue => e
            errors << {
              'tempo_id' => company['tempo_id'],
              'tempo_key' => company['tempo_key'],
              'tempo_name' => company['tempo_name'],
              'identifier' => company['identifier'],
              'errors' => [e.message],
              'error_type' => 'exception'
            }
            puts "Exception: #{e.message}"
          end
        end

        # Write results to result file
        result = {
          'status' => 'success',
          'created' => created_companies,
          'errors' => errors,
          'created_count' => created_companies.size,
          'error_count' => errors.size,
          'total' => companies_count
        }

        File.write(result_file_container, result.to_json)
        puts "Results written to #{result_file_container}"

        # Also return the result for direct capture
        result
        """

        # Execute the Ruby script using file-based approach
        result = self.op_client.rails_client.execute(ruby_script)
        output = result.get("output", "")

        created_count = 0
        errors = []

        # Process the result
        if result.get("status") == "success":
            # If direct output contains the result
            if isinstance(output, dict):
                if output.get("status") == "success":
                    created_companies = output.get("created", [])
                    created_count = len(created_companies)
                    errors = output.get("errors", [])

                    # Update the mapping
                    for company in created_companies:
                        tempo_id = company.get("tempo_id")
                        if tempo_id:
                            self.company_mapping[tempo_id] = {
                                "tempo_id": tempo_id,
                                "tempo_key": company.get("tempo_key"),
                                "tempo_name": company.get("tempo_name"),
                                "openproject_id": company.get("openproject_id"),
                                "openproject_identifier": company.get(
                                    "openproject_identifier"
                                ),
                                "openproject_name": company.get("openproject_name"),
                                "matched_by": "created",
                            }
                            self._created_companies += 1

                    # Handle errors
                    for error in errors:
                        tempo_id = error.get("tempo_id")
                        if tempo_id:
                            self.company_mapping[tempo_id] = {
                                "tempo_id": tempo_id,
                                "tempo_key": error.get("tempo_key"),
                                "tempo_name": error.get("tempo_name"),
                                "openproject_id": None,
                                "openproject_identifier": error.get("identifier"),
                                "openproject_name": None,
                                "matched_by": "error",
                                "error": ", ".join(error.get("errors", [])),
                                "error_type": error.get("error_type"),
                            }
            else:
                # If direct output doesn't work, try to get the result file
                if self.op_client.rails_client.transfer_file_from_container(
                    result_file_container, result_file_local
                ):
                    try:
                        with open(result_file_local) as f:
                            result_data = json.load(f)

                            if result_data.get("status") == "success":
                                created_companies = result_data.get("created", [])
                                created_count = len(created_companies)
                                errors = result_data.get("errors", [])

                                # Update the mapping
                                for company in created_companies:
                                    tempo_id = company.get("tempo_id")
                                    if tempo_id:
                                        self.company_mapping[tempo_id] = {
                                            "tempo_id": tempo_id,
                                            "tempo_key": company.get("tempo_key"),
                                            "tempo_name": company.get("tempo_name"),
                                            "openproject_id": company.get(
                                                "openproject_id"
                                            ),
                                            "openproject_identifier": company.get(
                                                "openproject_identifier"
                                            ),
                                            "openproject_name": company.get(
                                                "openproject_name"
                                            ),
                                            "matched_by": "created",
                                        }
                                        self._created_companies += 1

                                # Handle errors
                                for error in errors:
                                    tempo_id = error.get("tempo_id")
                                    if tempo_id:
                                        self.company_mapping[tempo_id] = {
                                            "tempo_id": tempo_id,
                                            "tempo_key": error.get("tempo_key"),
                                            "tempo_name": error.get("tempo_name"),
                                            "openproject_id": None,
                                            "openproject_identifier": error.get(
                                                "identifier"
                                            ),
                                            "openproject_name": None,
                                            "matched_by": "error",
                                            "error": ", ".join(error.get("errors", [])),
                                            "error_type": error.get("error_type"),
                                        }
                    except Exception as e:
                        self.logger.error(f"Error processing result file: {str(e)}")
                else:
                    # Last resort - try to parse the console output
                    self.logger.warning(
                        "Could not get result file - parsing console output"
                    )
                    if isinstance(output, str):
                        created_matches = re.findall(
                            r"Created project #(\d+): (.+?)$", output, re.MULTILINE
                        )
                        created_count = len(created_matches)
                        self.logger.info(
                            f"Found {created_count} created projects in console output"
                        )

                        # We can't reliably match these back to tempo IDs, so just log success
                        self._created_companies += created_count

        self.logger.info(
            f"Created {created_count} company projects (errors: {len(errors)})"
        )

        # Refresh cached projects from OpenProject
        self.op_client.get_projects(force_refresh=True)

        # Save the updated mapping
        self._save_to_json(self.company_mapping, Mappings.COMPANY_MAPPING_FILE)

        return self.company_mapping

    def migrate_customer_metadata(self) -> dict[str, Any]:
        """
        Migrate customer metadata (address, contact info, etc.) to OpenProject projects.
        This enhances company projects with additional metadata from Tempo.

        Returns:
            Dict with results stats
        """
        self.logger.info("Starting migration of customer metadata...")

        results = {"updated": 0, "failed": 0, "errors": [], "warnings": []}

        # Check for valid mappings
        if not hasattr(self, "company_mapping") or not self.company_mapping:
            self.logger.warning(
                "No company mapping available, run create_company_mapping first"
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
        temp_file_path = os.path.join(self.data_dir, "company_metadata.json")
        with open(temp_file_path, "w") as f:
            json.dump(companies_to_update, f, indent=2)

        # Transfer file to container
        container_temp_path = "/tmp/company_metadata.json"
        result_file_container = "/tmp/company_metadata_result.json"
        result_file_local = os.path.join(self.data_dir, "company_metadata_result.json")

        if not self.op_client.rails_client.transfer_file_to_container(
            temp_file_path, container_temp_path
        ):
            self.logger.error("Failed to transfer company metadata to container")
            results["failed"] = len(companies_to_update)
            return results

        # Prepare Ruby script to update metadata
        self.logger.info(
            f"Updating metadata for {len(companies_to_update)} companies..."
        )

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
            contact_cf = CustomField.where(type: 'ProjectCustomField', name: 'Customer Contact').first_or_create do |cf|
              cf.field_format = 'text'
              cf.is_required = false
              puts "Created 'Customer Contact' custom field"
            end

            address_cf = CustomField.where(type: 'ProjectCustomField', name: 'Customer Address').first_or_create do |cf|
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
            contact_info << "Lead: #{metadata['lead_display_name']} (#{metadata['lead_key']})" if metadata['lead_display_name']
            contact_info << "Email: #{metadata['email']}" if metadata['email']
            contact_info << "Phone: #{metadata['phoneNumber']}" if metadata['phoneNumber']
            contact_info << "Fax: #{metadata['faxNumber']}" if metadata['faxNumber']

            # Add address information
            address_info = []
            address_info << metadata['addressLine1'] if metadata['addressLine1']
            address_info << metadata['addressLine2'] if metadata['addressLine2']
            address_info << "#{metadata['city']}, #{metadata['state']} #{metadata['zipCode']}" if metadata['city'] || metadata['state'] || metadata['zipCode']
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
        puts "Results written to #{result_file_path}"

        # Return the result
        result
        """

        # Execute the Ruby script using file-based method to avoid IOError
        result = self.op_client.rails_client.execute(ruby_script)

        # Process the result
        if result.get("status") != "success":
            self.logger.error(
                f"Error executing Ruby script: {result.get('error', 'Unknown error')}"
            )
            results["failed"] = len(companies_to_update)
            results["errors"].append(
                result.get("error", "Failed to execute Ruby script")
            )
            return results

        # Get the result data
        updated_companies = []
        errors = []

        output = result.get("output", {})
        if isinstance(output, dict) and output.get("status") == "success":
            updated_companies = output.get("updated", [])
            errors = output.get("errors", [])
        elif isinstance(output, dict):
            updated_companies = output.get("updated", [])
            errors = output.get("errors", [])
        else:
            # Try to get result file from container
            if self.op_client.rails_client.transfer_file_from_container(
                result_file_container, result_file_local
            ):
                try:
                    with open(result_file_local) as f:
                        result_data = json.load(f)
                        if result_data.get("status") == "success":
                            updated_companies = result_data.get("updated", [])
                            errors = result_data.get("errors", [])
                except Exception as e:
                    self.logger.error(f"Error reading result file: {str(e)}")
                    results["failed"] = len(companies_to_update)
                    results["errors"].append(str(e))
                    return results

        # Update results
        results["updated"] = len(updated_companies)
        results["failed"] = len(errors)
        if errors:
            results["errors"] = [
                f"{e.get('tempo_id', 'Unknown')}: {e.get('error', 'Unknown error')}"
                for e in errors[:10]
            ]
            if len(errors) > 10:
                results["errors"].append(f"...and {len(errors) - 10} more errors")

        # Log results
        if results["updated"] > 0:
            self.logger.success(f"Updated metadata for {results['updated']} companies")
        if results["failed"] > 0:
            self.logger.warning(
                f"Failed to update metadata for {results['failed']} companies"
            )

        return results

    def run(self) -> ComponentResult:
        """
        Run the company migration process.

        Returns:
            Dictionary with migration results
        """
        self.logger.info("Starting company migration", extra={"markup": True})

        try:
            # Extract data
            tempo_companies = self.extract_tempo_companies()
            op_projects = self.extract_openproject_projects()

            # Create mapping
            mapping = self.create_company_mapping()

            # Migrate companies if not in dry run mode
            if not config.migration_config.get("dry_run", False):
                self.logger.info("Using bulk creation for company projects")
                result = self.migrate_companies_bulk()

                # Migrate additional metadata
                self.logger.info("Migrating customer metadata...")
                metadata_result = self.migrate_customer_metadata()
            else:
                self.logger.warning(
                    "Dry run mode - not creating company projects",
                    extra={"markup": True},
                )
                result = {
                    "status": "success",
                    "created_count": 0,
                    "matched_count": sum(
                        1
                        for company in mapping.values()
                        if company["matched_by"] != "none"
                    ),
                    "skipped_count": 0,
                    "failed_count": 0,
                    "total_count": len(tempo_companies),
                }
                metadata_result = {
                    "total": len(mapping),
                    "updated": 0,
                    "skipped": len(mapping),
                    "failed": 0,
                    "errors": [],
                }

            # Analyze results
            analysis = self.analyze_company_mapping()

            return ComponentResult(
                success=True if "success" == result.get("status", "success") else False,
                success_count=result.get("created_count", 0)
                + result.get("matched_count", 0),
                failed_count=result.get("failed_count", 0),
                total_count=len(tempo_companies),
                tempo_companies_count=len(tempo_companies),
                op_projects_count=len(op_projects),
                mapped_companies_count=len(mapping),
                metadata_updated=metadata_result.get("updated", 0),
                metadata_failed=metadata_result.get("failed", 0),
                analysis=analysis,
            )
        except Exception as e:
            self.logger.error(
                f"Error during company migration: {str(e)}",
                extra={"markup": True, "traceback": True},
            )
            self.logger.exception(e)
            return ComponentResult(
                success=False,
                error=str(e),
                success_count=0,
                failed_count=(
                    len(self.tempo_companies) if self.tempo_companies else 0
                ),
                total_count=(
                    len(self.tempo_companies) if self.tempo_companies else 0
                ),
            )
