"""
Company migration module for Jira to OpenProject migration.
Handles the migration of Tempo timesheet companies from Jira to OpenProject as top-level projects.
"""

import os
import sys
import json
import re
from typing import Dict, List, Any, Optional
from pathlib import Path

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src import config
from src.display import process_with_progress
from src.utils import load_json_file
from src.migrations.base_migration import BaseMigration
from src.mappings.mappings import Mappings

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
        data_dir: str = None,
    ):
        """
        Initialize the company migration.

        Args:
            jira_client: JiraClient instance.
            op_client: OpenProjectClient instance.
            data_dir: Path to data directory for storing mappings.
        """
        super().__init__(jira_client, op_client, None)

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
        self.logger.debug(f"CompanyMigration initialized with data dir: {self.data_dir}")

        # Load existing data if available
        self.tempo_companies = self._load_from_json(Mappings.TEMPO_COMPANIES_FILE) or {}
        self.op_projects = self._load_from_json(Mappings.OP_PROJECTS_FILE) or {}
        self.company_mapping = self._load_from_json(Mappings.COMPANY_MAPPING_FILE) or {}

    def extract_tempo_companies(self) -> Dict[str, Any]:
        """
        Extract companies from Tempo API.

        Returns:
            Dictionary of Tempo companies.
        """
        if load_json_file(self.tempo_companies_file):
            loaded_data = load_json_file(self.tempo_companies_file)

            # If loaded data is a dictionary, use it directly
            if isinstance(loaded_data, dict):
                self.tempo_companies = loaded_data
            # If loaded data is a list (after JSON serialization/deserialization), convert back to dict
            elif isinstance(loaded_data, list):
                # Convert list to dictionary with id as key
                self.tempo_companies = {}
                for company in loaded_data:
                    if not isinstance(company, dict):
                        self.logger.warning(f"Skipping non-dictionary company: {company}")
                        continue

                    # Get ID from either 'id' or 'tempo_id' field
                    company_id = None
                    if 'id' in company:
                        company_id = str(company['id'])
                    elif 'tempo_id' in company:
                        company_id = str(company['tempo_id'])
                        # Add 'id' field if missing but 'tempo_id' exists
                        company['id'] = company_id

                    if company_id:
                        self.tempo_companies[company_id] = company
                    else:
                        self.logger.warning(f"Skipping company without id or tempo_id: {company}")

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
                "lead": company.get("lead", {}).get("key") if company.get("lead") else None,
                "status": company.get("status", "ACTIVE"),
                "_raw": company
            }

        # Save to file
        self._save_to_json(self.tempo_companies, Mappings.TEMPO_COMPANIES_FILE)
        self.logger.info(f"Saved {len(self.tempo_companies)} companies to {self.tempo_companies_file}")

        return self.tempo_companies

    def extract_openproject_projects(self, force: bool = False) -> List[Dict[str, Any]]:
        """
        Extract projects from OpenProject.

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

    def create_company_mapping(self) -> Dict[str, Any]:
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
        tempo_companies_list = self.tempo_companies.values() if isinstance(self.tempo_companies, dict) else self.tempo_companies

        for tempo_company in tempo_companies_list:
            if not isinstance(tempo_company, dict):
                self.logger.warning(f"Skipping non-dictionary tempo_company: {tempo_company}")
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

    def create_company_project_in_openproject(
        self, tempo_company: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Create a top-level project in OpenProject based on a Tempo company.

        Args:
            tempo_company: The Tempo company data

        Returns:
            The created OpenProject project or None if creation failed
        """
        name = tempo_company.get("name")
        key = tempo_company.get("key", "")
        lead = tempo_company.get("lead", "")
        status = tempo_company.get("status", "ACTIVE")

        # Map Tempo status to OpenProject status
        op_status = "ON_TRACK"  # Default status
        if status in ["CLOSED", "ARCHIVED"]:
            op_status = "FINISHED"

        description = f"Migrated from Tempo company: {key}\n"
        if lead:
            description += f"Company Lead: {lead}\n"

        base_identifier = "customer_"

        if key:
            raw_id = key.lower()
            sanitized_id = re.sub(r'[^a-z0-9_-]', '_', raw_id)
            base_identifier += sanitized_id
        else:
            raw_id = name.lower()
            sanitized_id = re.sub(r'[^a-z0-9_-]', '_', raw_id)
            base_identifier += sanitized_id

        identifier = base_identifier[:100]

        if config.migration_config.get('dry_run'):
            return {
                "id": None,
                "name": name,
                "identifier": identifier,
                "description": {"raw": description},
                "_links": {"parent": {"href": None}},
                "public": False,
                "status": op_status
            }

        try:
            project, was_created = self.op_client.create_project(
                name=name,
                identifier=identifier,
                description=description,
                public=False,
                status=op_status
            )

            if project:
                if was_created:
                    self._created_companies += 1
                return project
            else:
                return {
                    "id": None,
                    "name": name,
                    "identifier": identifier,
                    "description": {"raw": description},
                    "_links": {"parent": {"href": None}},
                    "_placeholder": True,
                    "public": False,
                    "status": op_status
                }
        except Exception as e:
            error_msg = str(e)
            if "422" in error_msg and "taken" in error_msg:
                return {
                    "id": None,
                    "name": name,
                    "identifier": identifier,
                    "description": {"raw": description},
                    "_links": {"parent": {"href": None}},
                    "_placeholder": True,
                    "public": False,
                    "status": op_status
                }
            else:
                self.logger.error(f"Error creating company project {name}: {str(e)}")
                return None

    def migrate_companies(self) -> Dict[str, Any]:
        """
        Migrate companies from Tempo timesheet to OpenProject as top-level projects.

        Returns:
            Updated mapping with migration results
        """
        self.logger.info("Starting company migration...")

        if not self.tempo_companies:
            self.extract_tempo_companies()

        if not self.op_projects:
            self.extract_openproject_projects()

        if not self.company_mapping:
            self.create_company_mapping()

        companies_to_migrate = list(self.tempo_companies.values())

        if not companies_to_migrate:
            self.logger.warning(f"No companies found to migrate")
            return self.company_mapping

        self._created_companies = 0

        def process_company(company, context):
            # Get company ID, preferring 'id' but accepting 'tempo_id' as fallback
            tempo_id = company.get("id")
            if not tempo_id and "tempo_id" in company:
                tempo_id = company.get("tempo_id")
                # Add 'id' if missing but 'tempo_id' exists
                company["id"] = tempo_id

            if not tempo_id:
                self.logger.warning(f"Skipping company without ID: {company}")
                return "Unknown company (missing ID)"

            tempo_id = str(tempo_id)  # Ensure it's a string
            tempo_name = company.get("name", f"Unknown Company {tempo_id}")

            identifier = None
            if company.get("key"):
                base_identifier = "customer_" + re.sub(r'[^a-z0-9_-]', '_', company["key"].lower())
                identifier = base_identifier[:100]

            if identifier:
                existing = self.op_client.get_project_by_identifier(identifier)
                if existing:
                    self.company_mapping[tempo_id] = {
                        "tempo_id": tempo_id,
                        "tempo_key": company.get("key", ""),
                        "tempo_name": tempo_name,
                        "openproject_id": existing.get("id"),
                        "openproject_identifier": existing.get("identifier"),
                        "openproject_name": existing.get("name"),
                        "matched_by": "existing",
                    }
                    return tempo_name

            op_project = self.create_company_project_in_openproject(company)

            if op_project:
                self.company_mapping[tempo_id] = {
                    "tempo_id": tempo_id,
                    "tempo_key": company.get("key", ""),
                    "tempo_name": tempo_name,
                    "openproject_id": op_project.get("id"),
                    "openproject_identifier": op_project.get("identifier"),
                    "openproject_name": op_project.get("name"),
                    "matched_by": "created",
                }

            return tempo_name

        self.logger.info(f"Migrating {len(companies_to_migrate)} companies to OpenProject")
        process_with_progress(
            items=companies_to_migrate,
            process_func=process_company,
            description="Migrating companies",
            log_title="Companies Being Migrated",
            item_name_func=lambda company: company.get("name", "Unknown")
        )

        self._save_to_json(self.company_mapping, Mappings.COMPANY_MAPPING_FILE)

        if config.migration_config.get('dry_run'):
            self.logger.info(
                "DRY RUN: No company projects were actually created in OpenProject"
            )

        return self.company_mapping

    def analyze_company_mapping(self) -> Dict[str, Any]:
        """
        Analyze the company mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        if not self.company_mapping:
            mapping_path = os.path.join(self.data_dir, Mappings.COMPANY_MAPPING_FILE)
            if os.path.exists(mapping_path):
                with open(mapping_path, "r") as f:
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

        self.logger.info(f"Company mapping analysis complete")
        self.logger.info(f"Total companies: {analysis['total_companies']}")
        self.logger.info(
            f"Matched companies: {analysis['matched_companies']} ({analysis['match_percentage']:.1f}%)"
        )
        self.logger.info(f"- Matched by name: {analysis['matched_by_name']}")
        self.logger.info(f"- Created in OpenProject: {analysis['actually_created']}")
        self.logger.info(f"- Already existing in OpenProject: {analysis['matched_by_existing']}")
        self.logger.info(f"Unmatched companies: {analysis['unmatched_companies']}")

        return analysis

    def migrate_companies_bulk(self) -> Dict[str, Any]:
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
            company for company in self.tempo_companies.values()
            if self.company_mapping.get(company.get("id", company.get("tempo_id", "")), {}).get("matched_by") == "none"
        ]

        if not companies_to_migrate:
            self.logger.warning("No companies found to migrate")
            return self.company_mapping

        self._created_companies = 0

        # Check if Rails client is available - we need it for bulk creation
        if not self.op_client.rails_client:
            self.logger.warning("Rails client is not available, falling back to API-based creation")
            return self.migrate_companies()

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
                sanitized_id = re.sub(r'[^a-z0-9_-]', '_', raw_id)
                base_identifier += sanitized_id
            else:
                raw_id = tempo_name.lower()
                sanitized_id = re.sub(r'[^a-z0-9_-]', '_', raw_id)
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
            companies_data.append({
                "tempo_id": tempo_id,
                "tempo_key": tempo_key,
                "tempo_name": tempo_name,
                "name": tempo_name,
                "identifier": identifier,
                "description": description,
                "status": company.get("status", "ACTIVE"),
                "public": False
            })

        if not companies_data:
            self.logger.info("No companies need to be created, all matched or already exist")
            self._save_to_json(self.company_mapping, Mappings.COMPANY_MAPPING_FILE)
            return self.company_mapping

        if config.migration_config.get('dry_run'):
            self.logger.info(f"DRY RUN: Would create {len(companies_data)} company projects")
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

        # Get container and server info
        container_name = self.op_client.op_config.get("container")
        op_server = self.op_client.op_config.get("server")

        # Define the path for the file inside the container
        container_temp_path = "/tmp/tempo_companies.json"
        result_file_container = "/tmp/company_creation_result.json"
        result_file_local = os.path.join(self.data_dir, "company_creation_result.json")

        # Copy the file to the container
        if not self.op_client.rails_client.transfer_file_to_container(temp_file_path, container_temp_path):
            self.logger.error(f"Failed to transfer companies data to container")
            return self.company_mapping

        # Prepare and execute the Ruby script
        self.logger.info(f"Executing Rails script to create {len(companies_data)} companies")

        # Head section with f-string for Python variable interpolation
        head_script = f"""
        container_temp_path = '{container_temp_path}'
        result_file_container = '{result_file_container}'
        """

        # Main section without f-string for Ruby logic
        main_script = """
        begin
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
        rescue => e
          error_result = {
            'status' => 'error',
            'message' => e.message,
            'backtrace' => e.backtrace[0..5]
          }

          # Try to save error to file
          begin
            File.write(result_file_container, error_result.to_json)
          rescue => write_error
            puts "Failed to write error to file: #{write_error.message}"
          end

          # Return error result
          error_result
        end
        """

        # Combine the head and main sections
        ruby_script = head_script + main_script

        # Execute the Ruby script
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
                                "openproject_identifier": company.get("openproject_identifier"),
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
                                "error": ', '.join(error.get("errors", [])),
                                "error_type": error.get("error_type")
                            }
            else:
                # If direct output doesn't work, try to get the result file
                if self.op_client.rails_client.transfer_file_from_container(result_file_container, result_file_local):
                    try:
                        with open(result_file_local, 'r') as f:
                            result_data = json.load(f)

                            if result_data.get('status') == 'success':
                                created_companies = result_data.get('created', [])
                                created_count = len(created_companies)
                                errors = result_data.get('errors', [])

                                # Update the mapping
                                for company in created_companies:
                                    tempo_id = company.get("tempo_id")
                                    if tempo_id:
                                        self.company_mapping[tempo_id] = {
                                            "tempo_id": tempo_id,
                                            "tempo_key": company.get("tempo_key"),
                                            "tempo_name": company.get("tempo_name"),
                                            "openproject_id": company.get("openproject_id"),
                                            "openproject_identifier": company.get("openproject_identifier"),
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
                                            "error": ', '.join(error.get("errors", [])),
                                            "error_type": error.get("error_type")
                                        }
                    except Exception as e:
                        self.logger.error(f"Error processing result file: {str(e)}")
                else:
                    # Last resort - try to parse the console output
                    self.logger.warning(f"Could not get result file - parsing console output")
                    if isinstance(output, str):
                        created_matches = re.findall(r"Created project #(\d+): (.+?)$", output, re.MULTILINE)
                        created_count = len(created_matches)
                        self.logger.info(f"Found {created_count} created projects in console output")

                        # We can't reliably match these back to tempo IDs, so just log success
                        self._created_companies += created_count

        self.logger.info(f"Created {created_count} company projects (errors: {len(errors)})")

        # Save the updated mapping
        self._save_to_json(self.company_mapping, Mappings.COMPANY_MAPPING_FILE)

        return self.company_mapping

    def run(self, dry_run: bool = False, force: bool = False, mappings=None) -> Dict[str, Any]:
        """
        Run the company migration process.

        Args:
            dry_run: If True, don't actually create companies in OpenProject
            force: If True, force extraction of data even if it already exists
            mappings: Optional mappings object (not used in this migration)

        Returns:
            Dictionary with migration results
        """
        self.logger.info("Starting company migration", extra={"markup": True})

        try:
            # Extract data
            tempo_companies = self.extract_tempo_companies()
            op_projects = self.extract_openproject_projects(force=force)

            # Create mapping
            mapping = self.create_company_mapping()

            # Migrate companies if not in dry run mode
            if not dry_run:
                # Use bulk migration if Rails client is available
                if self.op_client.rails_client:
                    self.logger.info("Using bulk creation for company projects")
                    result = self.migrate_companies_bulk()
                else:
                    self.logger.info("Rails client not available, using individual API calls")
                    result = self.migrate_companies()
            else:
                self.logger.warning("Dry run mode - not creating company projects", extra={"markup": True})
                result = {
                    "status": "success",
                    "created_count": 0,
                    "matched_count": sum(1 for company in mapping.values() if company["matched_by"] != "none"),
                    "skipped_count": 0,
                    "failed_count": 0,
                    "total_count": len(tempo_companies)
                }

            # Analyze results
            analysis = self.analyze_company_mapping()

            return {
                "status": result.get("status", "success"),
                "success_count": result.get("created_count", 0) + result.get("matched_count", 0),
                "failed_count": result.get("failed_count", 0),
                "total_count": len(tempo_companies),
                "tempo_companies_count": len(tempo_companies),
                "op_projects_count": len(op_projects),
                "mapped_companies_count": len(mapping),
                "analysis": analysis
            }
        except Exception as e:
            self.logger.error(f"Error during company migration: {str(e)}", extra={"markup": True, "traceback": True})
            self.logger.exception(e)
            return {
                "status": "failed",
                "error": str(e),
                "success_count": 0,
                "failed_count": len(self.tempo_companies) if self.tempo_companies else 0,
                "total_count": len(self.tempo_companies) if self.tempo_companies else 0
            }
