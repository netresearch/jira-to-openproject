"""
Custom field migration module for Jira to OpenProject migration.
Handles the migration of custom fields from Jira to OpenProject.
"""

import json
import os
import pathlib
import time
from typing import Any, Optional

from src.models import ComponentResult
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient

# Import RailsConsolePexpect to handle direct Rails console execution
from src.clients.openproject_rails_client import OpenProjectRailsClient
from src.display import ProgressTracker, console
from src.migrations.base_migration import BaseMigration
from src import config

# Create rich console instance
console = console


class CustomFieldMigration(BaseMigration):
    """
    Handles the migration of custom fields from Jira to OpenProject.

    This class supports two approaches:
    1. Generate a Ruby script for manual execution via Rails console (traditional approach)
    2. Execute commands directly on the Rails console using pexpect (direct approach)
    """

    def __init__(
        self,
        jira_client: JiraClient | None = None,
        op_client: OpenProjectClient | None = None,
        rails_console: Optional["OpenProjectRailsClient"] = None,
    ) -> None:
        """
        Initialize the custom field migration process.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client
            rails_console: Initialized OpenProjectRailsClient instance (optional)
        """
        super().__init__(jira_client, op_client)
        self.jira_custom_fields: list[dict] = []
        self.op_custom_fields: list[dict] = []
        self.mapping: dict[str, dict] = {}
        self.analysis: dict = {}
        self.rails_console = rails_console

        self._load_data()

    def _load_data(self) -> None:
        """Load existing data from JSON files."""
        self.jira_custom_fields = self._load_from_json("jira_custom_fields.json", [])
        self.op_custom_fields = self._load_from_json("op_custom_fields.json", [])
        self.mapping = self._load_from_json("custom_field_mapping.json", {})

        analysis_data = self._load_from_json("custom_field_analysis.json", {})
        self.analysis = {} if analysis_data is None else analysis_data

        self.logger.info(f"Loaded {len(self.jira_custom_fields)} Jira custom fields")
        self.logger.info(
            f"Loaded {len(self.op_custom_fields)} OpenProject custom fields"
        )
        self.logger.info(f"Loaded {len(self.mapping)} custom field mappings")
        self.logger.info(
            f"Loaded analysis with {len(self.analysis)} keys: {list(self.analysis.keys())}"
        )

    def extract_jira_custom_fields(self) -> list[dict[str, Any]]:
        """
        Extract custom field information from Jira.

        Returns:
            List of Jira custom fields
        """
        custom_fields_file = os.path.join(self.data_dir, "jira_custom_fields.json")

        if os.path.exists(custom_fields_file) and not config.migration_config.get("force", False):
            self.logger.info(
                "Jira custom fields data already exists, skipping extraction (use --force to override)"
            )
            with open(custom_fields_file) as f:
                self.jira_custom_fields = json.load(f)
            return self.jira_custom_fields

        self.logger.info("Extracting custom fields from Jira...")

        try:
            self.jira_custom_fields = self.jira_client.jira.fields()

            self.jira_custom_fields = [
                field for field in self.jira_custom_fields if field.get("custom", False)
            ]

            self.logger.info(
                f"Extracted {len(self.jira_custom_fields)} custom fields from Jira"
            )

            self.logger.info("Retrieving options for select list custom fields...")

            enhanced_fields = []

            for field in self.jira_custom_fields:
                enhanced_field = field.copy()
                field_id = field.get("id")
                field_type = field.get("schema", {}).get("type", "")
                field_custom_type = field.get("schema", {}).get("custom", "")

                is_select_list = (
                    field_type == "option"
                    or "select" in field_custom_type.lower()
                    or "option" in field_custom_type.lower()
                    or "radio" in field_custom_type.lower()
                )

                if is_select_list:
                    try:
                        self.logger.debug(
                            f"Retrieving options for field: {field.get('name')}"
                        )
                        meta_data = self.jira_client.get_field_metadata(field_id)

                        allowed_values = []

                        if "allowedValues" in meta_data:
                            for value in meta_data["allowedValues"]:
                                if "value" in value:
                                    allowed_values.append(value["value"])
                                elif "name" in value:
                                    allowed_values.append(value["name"])

                        if allowed_values:
                            self.logger.debug(
                                f"Found {len(allowed_values)} options for field '{field.get('name')}' (ID: {field_id})"
                            )
                            enhanced_field["allowed_values"] = allowed_values
                    except Exception as e:
                        self.logger.warning(
                            f"Could not retrieve options for field '{field.get('name')}': {str(e)}"
                        )

                enhanced_fields.append(enhanced_field)

            self.jira_custom_fields = enhanced_fields

            with open(custom_fields_file, "w") as f:
                json.dump(self.jira_custom_fields, f, indent=2)
            self.logger.info(f"Saved data to {custom_fields_file}")

            return self.jira_custom_fields
        except Exception as e:
            self.logger.error(f"Failed to extract custom fields from Jira: {str(e)}")
            return []

    def extract_openproject_custom_fields(self) -> list[dict[str, Any]]:
        """
        Extract custom field information from OpenProject and save to a JSON file.

        Returns:
            List of custom field dictionaries retrieved from OpenProject
        """
        self.logger.info("Starting OpenProject custom field extraction...")

        # Path to the output JSON file
        output_file = pathlib.Path(self.output_dir) / "openproject_custom_fields.json"

        # Check if the data already exists
        if output_file.exists() and not config.migration_config.get("force", False):
            self.logger.info(
                f"Using existing OpenProject custom field data from {output_file}"
            )
            try:
                with open(output_file, encoding="utf-8") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                self.logger.warning(
                    f"Existing file {output_file} is invalid. Re-extracting data."
                )
            except Exception as e:
                self.logger.error(
                    f"Error reading existing data: {str(e)}. Re-extracting data."
                )

        self.logger.info(
            "Retrieving custom fields from OpenProject via Rails console..."
        )

        try:
            # Use the op_client's get_custom_fields method which uses Rails console
            all_fields = self.op_client.get_custom_fields(force_refresh=True)

            if not all_fields:
                self.logger.info(
                    "No custom fields found in OpenProject - this is normal for a new installation"
                )
                return []

            self.logger.info(
                f"Retrieved {len(all_fields)} custom fields from OpenProject"
            )

            # Save the extracted data
            self.logger.info(
                f"Saving {len(all_fields)} OpenProject custom fields to {output_file}"
            )
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(all_fields, f, indent=2)

            return all_fields

        except Exception as e:
            self.logger.error(f"Error extracting OpenProject custom fields: {str(e)}")
            self.logger.debug("Error details:", exc_info=True)
            return []

    def map_jira_field_to_openproject_format(self, jira_field: dict[str, Any]) -> str:
        """
        Map a Jira custom field type to the closest OpenProject field format.

        Args:
            jira_field: Jira custom field information

        Returns:
            OpenProject custom field format
        """
        schema = jira_field.get("schema", {})
        jira_type = schema.get("type", "")
        jira_custom_type = schema.get("custom", "")

        op_format_map = {
            "string": "text",
            "text": "text",
            "textarea": "text",
            "url": "text",
            "readonlyfield": "text",
            "gh-epic-link": "text",
            "gl-epic-link": "text",
            "option": "list",
            "select": "list",
            "radiobuttons": "list",
            "array": "list",
            "multiselect": "list",
            "checkboxes": "list",
            "date": "date",
            "datetime": "date",
            "number": "int",
            "integer": "int",
            "float": "float",
            "user": "user",
            "users": "user",
            "project": "list",
            "boolean": "bool",
            "cascadingselect": "list",
            "multiversion": "list",
            "version": "list",
            "multiuserpicker": "user",
            "userpicker": "user",
            "labels": "list",
            "priority": "list",
            "status": "list",
            "default": "text",
        }

        if jira_custom_type:
            if (
                "multiselect" in jira_custom_type.lower()
                or "checkbox" in jira_custom_type.lower()
            ):
                return "list"
            elif (
                "select" in jira_custom_type.lower()
                or "radio" in jira_custom_type.lower()
            ):
                return "list"
            elif "date" in jira_custom_type.lower():
                return "date"
            elif (
                "number" in jira_custom_type.lower()
                or "float" in jira_custom_type.lower()
            ):
                return "float"
            elif "integer" in jira_custom_type.lower():
                return "int"
            elif "user" in jira_custom_type.lower():
                return "user"
            elif (
                "text" in jira_custom_type.lower()
                or "string" in jira_custom_type.lower()
            ):
                return "text"
            elif "url" in jira_custom_type.lower():
                return "text"
            elif "boolean" in jira_custom_type.lower():
                return "bool"

        if jira_type == "array":
            items_type = schema.get("items", "")
            if items_type == "string":
                return "list"
            elif items_type == "user":
                return "user"
            elif items_type == "date":
                return "date"
            else:
                return "list"

        return op_format_map.get(jira_type, op_format_map.get("default", "text"))

    def create_custom_field_mapping(self) -> dict[str, Any]:
        """
        Create a mapping between Jira and OpenProject custom fields.

        Returns:
            Dictionary mapping Jira custom field IDs to OpenProject field information
        """
        self.logger.info("Creating custom field mapping...")

        self.extract_jira_custom_fields()

        self.extract_openproject_custom_fields()

        mapping = {}

        op_fields_by_name = {
            field.get("name", "").lower(): field for field in self.op_custom_fields
        }

        def process_field(jira_field: dict[str, Any], context: dict[str, Any]) -> str | None:
            jira_id = jira_field.get("id")
            jira_name = jira_field.get("name", "")
            jira_name_lower = jira_name.lower()

            op_field = op_fields_by_name.get(jira_name_lower, None)

            if op_field:
                mapping[jira_id] = {
                    "jira_id": jira_id,
                    "jira_name": jira_name,
                    "jira_type": jira_field.get("schema", {}).get("type", ""),
                    "jira_custom_type": jira_field.get("schema", {}).get("custom", ""),
                    "openproject_id": op_field.get("id"),
                    "openproject_name": op_field.get("name"),
                    "openproject_type": op_field.get("field_format", "text"),
                    "matched_by": "name",
                }
                return None
            else:
                op_format = self.map_jira_field_to_openproject_format(jira_field)

                mapping_entry = {
                    "jira_id": jira_id,
                    "jira_name": jira_name,
                    "jira_type": jira_field.get("schema", {}).get("type", ""),
                    "jira_custom_type": jira_field.get("schema", {}).get("custom", ""),
                    "openproject_id": None,
                    "openproject_name": jira_name,
                    "openproject_type": op_format,
                    "matched_by": "create",
                }

                if op_format == "list" and "allowed_values" in jira_field:
                    allowed_values = jira_field.get("allowed_values", [])
                    if allowed_values:
                        mapping_entry["possible_values"] = allowed_values
                        field_info = f"{jira_name} (Format: {op_format}, Options: {len(allowed_values)})"
                    else:
                        field_info = f"{jira_name} (Format: {op_format}, No options)"
                else:
                    field_info = f"{jira_name} (Format: {op_format})"

                mapping[jira_id] = mapping_entry

                return field_info

        with ProgressTracker(
            description="Creating custom field mapping",
            total=len(self.jira_custom_fields),
            log_title="Custom Fields to Create",
        ) as tracker:
            for jira_field in self.jira_custom_fields:
                jira_name = jira_field.get("name", "")
                tracker.update_description(f"Mapping custom field: {jira_name[:20]}")

                field_info = process_field(jira_field, {})

                if field_info:
                    tracker.add_log_item(field_info)

                tracker.increment()

        mapping_file = os.path.join(self.data_dir, "custom_field_mapping.json")
        with open(mapping_file, "w") as f:
            json.dump(mapping, f, indent=2)
        self.logger.info(f"Saved custom field mapping to {mapping_file}")

        total_fields = len(mapping)
        matched_fields = sum(
            1 for field in mapping.values() if field["matched_by"] == "name"
        )
        created_fields = sum(
            1 for field in mapping.values() if field["matched_by"] == "created"
        )

        self.logger.info(f"Custom field mapping created for {total_fields} fields")
        self.logger.info(f"- Matched by name: {matched_fields}")
        self.logger.info(f"- Need to create: {created_fields}")

        self.mapping = mapping
        return mapping

    def create_custom_field_via_rails(self, field_data: dict) -> dict[str, Any]:
        """
        Create a custom field in OpenProject via Rails console.

        Args:
            field_data: Custom field definition

        Returns:
            dict: Result of the operation
        """
        field_type = field_data.get("openproject_type", "string")
        field_name = field_data.get(
            "openproject_name", field_data.get("jira_name", "Unnamed Field")
        )

        field_name = field_name.replace('"', '\\"')

        possible_values_ruby = "[]"
        if field_type == "list":
            if (
                "possible_values" in field_data
                and field_data["possible_values"]
                and isinstance(field_data["possible_values"], list)
            ):
                values = field_data["possible_values"]
                escaped_values = [
                    v.replace('"', '\\"') if isinstance(v, str) else v for v in values
                ]
                values_str = ", ".join([f'"{v}"' for v in escaped_values])
                possible_values_ruby = f"[{values_str}]"
            else:
                possible_values_ruby = '["Default option"]'
                self.logger.warning(
                    f"Custom field '{field_name}' is a list type but has no options. Adding a default option."
                )

        # Header section with Python variable interpolation
        header = f"""
        begin
          cf = CustomField.find_by(name: '{field_name}')
          if cf
            puts "Custom field '{field_name}' already exists. Skipping..."
          else
            puts "Creating custom field: '{field_name}'..."
            cf = CustomField.new(
              name: '{field_name}',
              field_format: '{field_type}',
              is_required: {'true' if field_data.get('is_required', False) else 'false'},
              is_for_all: {'true' if field_data.get('is_for_all', True) else 'false'},
              type: "{field_data.get('openproject_field_type', 'WorkPackageCustomField')}",
            )

            # Set possible values for list type fields
            if cf.field_format == 'list'
              cf.possible_values = {possible_values_ruby}
        """

        # Main section without Python interpolation
        main_section = """
              if cf.possible_values.nil? || cf.possible_values.empty?
                cf.possible_values = ['Default option'] # Ensure default option exists
              end
            end

            if cf.save
              puts "  Successfully created custom field '#{cf.name}' with ID: #{cf.id}"

              # Make it available for all work package types
              if cf.is_for_all?
                puts "  Activating for all work package types..."
                Type.all.each do |type|
                  type.custom_fields << cf unless type.custom_fields.include?(cf)
                  type.save!
                end
              end
            else
              puts "  Error creating custom field '#{cf.name}': #{cf.errors.full_messages.join(', ')}"
            end
          end
        rescue => e
          puts "  An unexpected error occurred while processing '#{e.message}'"
        end
        """

        # Combine the sections
        command = header + main_section

        result = self.rails_console.execute(command)

        if result["status"] == "error":
            error_info = result.get("error", "Unknown error")
            if "errors" in result:
                error_info = f"Validation errors: {result['errors']}"
            elif "validation_errors" in result:
                error_info = f"Field validation failed: {result['validation_errors']}"

            self.logger.error(
                f"Error creating custom field '{field_name}': {error_info}"
            )

            if isinstance(result.get("errors"), list) and any(
                "Possible values" in err for err in result["errors"]
            ):
                self.logger.error(
                    f"Field '{field_name}' requires possible values for list type fields"
                )
        elif result["status"] == "success":
            self.logger.info(f"Created custom field '{field_name}' successfully")

        return result

    def migrate_custom_fields_via_json(self, fields_to_migrate: list[dict[str, Any]]) -> bool:
        """Migrate custom fields by creating a JSON file and processing it in a Ruby script.

        This is a more efficient approach than creating fields one by one via API calls.

        Args:
            fields_to_migrate: List of custom field definitions to migrate

        Returns:
            Boolean indicating success or failure
        """
        self.logger.info(
            f"Migrating {len(fields_to_migrate)} custom fields using batch migration"
        )

        # Convert the fields to the format expected by the Ruby script
        custom_fields_data = []
        for field in fields_to_migrate:
            if not field.get("jira_name"):
                self.logger.warning(f"Skipping field without name: {field}")
                continue

            field_data = {
                "name": field.get("jira_name"),
                "field_format": field.get("openproject_type", "text"),
                "is_required": field.get("is_required", False),
                "is_for_all": field.get("is_for_all", True),
                "type": field.get("openproject_field_type", "WorkPackageCustomField"),
                "jira_id": field.get("jira_id"),  # For mapping back
            }

            # Add possible values for list fields
            if field_data["field_format"] == "list":
                possible_values = field.get("possible_values", [])
                # Ensure there's at least one default option if none exist
                if not possible_values or not isinstance(possible_values, list):
                    possible_values = ["Default option"]
                field_data["possible_values"] = possible_values

            custom_fields_data.append(field_data)

        # Get the Rails client
        rails_client = None
        if self.rails_console:
            rails_client = self.rails_console
        elif hasattr(self.op_client, "rails_client") and self.op_client.rails_client:
            rails_client = self.op_client.rails_client
            self.logger.info("Using Rails client from OpenProject client")

        if not rails_client:
            self.logger.error("No Rails client available for execution")
            return False

        # First, write the custom fields data to a JSON file

        # Generate unique filename
        timestamp = int(time.time())
        temp_file_path = os.path.join(
            self.data_dir, f"custom_fields_batch_{timestamp}.json"
        )
        self.logger.info(
            f"Writing {len(custom_fields_data)} custom fields to {temp_file_path}"
        )

        try:
            # Write the data to the JSON file
            with open(temp_file_path, "w", encoding="utf-8") as f:
                json.dump(custom_fields_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.logger.error(f"Error creating JSON file: {e}")
            return False

        # Define the path for the file inside the container
        container_temp_path = f"/tmp/custom_fields_batch_{timestamp}.json"

        # Copy the file to the container
        if not rails_client.transfer_file_to_container(
            temp_file_path, container_temp_path
        ):
            self.logger.error("Failed to transfer custom fields file to container")
            return False

        # Define result file paths
        result_file_container = f"/tmp/cf_result_{timestamp}.json"
        result_file_local = os.path.join(self.data_dir, f"cf_result_{timestamp}.json")

        # Create the Ruby script (separating Python variables and Ruby code)

        # Python variables
        data_file_path = container_temp_path
        result_file_path = result_file_container

        # Main Ruby script (no Python interpolation)
        main_script = """
        begin
          require 'json'

          # Load the data from the JSON file
          begin
            file_content = File.read(data_file_path)
            custom_fields_data = JSON.parse(file_content)
            puts "Loaded #{custom_fields_data.length} custom fields from JSON file"
          rescue => e
            puts "Error loading custom fields data: #{e.message}"
            return { "status" => "error", "message" => "Failed to load JSON data: #{e.message}" }
          end

          # Process each custom field
          results = []
          success_count = 0
          existing_count = 0
          error_count = 0

          custom_fields_data.each_with_index do |field_data, index|
            begin
              field_name = field_data['name']

              # Show progress indicator
              if (index + 1) % 50 == 0 || index + 1 == custom_fields_data.count
                puts "  Progress: #{index + 1}/#{custom_fields_data.count} fields"
              end

              # Check if the field already exists
              cf = CustomField.find_by(name: field_name)
              if cf
                results << {
                  'name' => field_name,
                  'status' => 'existing',
                  'id' => cf.id,
                  'jira_id' => field_data['jira_id']
                }
                existing_count += 1
                next
              end

              # Create a new custom field
              cf = CustomField.new(
                name: field_name,
                field_format: field_data['field_format'],
                is_required: field_data['is_required'],
                is_for_all: field_data['is_for_all'],
                type: field_data['type']
              )

              # Set possible values for list fields
              if cf.field_format == 'list' && field_data['possible_values']
                # Handle possible values properly - it could be an array or a hash
                values = field_data['possible_values']
                if values.is_a?(Hash)
                  # Convert hash to array of values if needed
                  cf.possible_values = values.values.map { |value| value.to_s.strip }
                elsif values.is_a?(Array)
                  # Ensure all values are strings to prevent 'strip' errors on non-string objects
                  cf.possible_values = values.map { |value| value.to_s.strip }
                else
                  # Fallback to single value as string
                  cf.possible_values = [values.to_s.strip]
                end

                # Ensure we have at least one value
                if cf.possible_values.nil? || cf.possible_values.empty?
                  cf.possible_values = ['Default option']
                end
              end

              # Save the custom field
              if cf.save
                # Make it available for all work package types if is_for_all is true
                if cf.is_for_all?
                  Type.all.each do |type|
                    type.custom_fields << cf unless type.custom_fields.include?(cf)
                    type.save!
                  end
                end

                results << {
                  'name' => field_name,
                  'status' => 'created',
                  'id' => cf.id,
                  'jira_id' => field_data['jira_id']
                }
                success_count += 1
              else
                error_message = cf.errors.full_messages.join(', ')
                results << {
                  'name' => field_name,
                  'status' => 'error',
                  'message' => error_message,
                  'jira_id' => field_data['jira_id']
                }
                error_count += 1
              end
            rescue => e
              error_field = field_data['name'] || 'Unknown field'
              results << {
                'name' => error_field,
                'status' => 'error',
                'message' => e.message,
                'jira_id' => field_data['jira_id']
              }
              error_count += 1
            end
          end

          # Print summary to console
          puts "\nMigration complete: Created: #{success_count}, Existing: #{existing_count}, Errors: #{error_count}"

          # Write results to file
          result = {
            'status' => 'success',
            'created' => results.select { |r| r['status'] == 'created' },
            'existing' => results.select { |r| r['status'] == 'existing' },
            'errors' => results.select { |r| r['status'] == 'error' },
            'created_count' => success_count,
            'existing_count' => existing_count,
            'error_count' => error_count,
            'total' => custom_fields_data.length
          }

          # Save results to file
          File.write(result_file_path, result.to_json)
          puts "Results written to #{result_file_path}"

          # Return results
          result
        rescue Exception => e
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

        # Execute the script
        self.logger.info("Executing Ruby script with custom field data")

        # Combine header and main script, define variables
        complete_script = f"""
        # Ruby variables from Python
        data_file_path = '{data_file_path}'
        result_file_path = '{result_file_path}'

        {main_script}
        """

        # Execute the script
        result = rails_client.execute(complete_script)

        if result.get("status") != "success":
            self.logger.error(
                f"Error executing Ruby script: {result.get('error', 'Unknown error')}"
            )
            return False

        # Initialize variables
        created_count = 0
        error_count = 0
        existing_count = 0
        created_fields = []
        results = []

        # Try to get results from direct output first
        output = result.get("output")
        if isinstance(output, dict) and output.get("status") == "success":
            results = []
            results.extend(output.get("created", []))
            results.extend(output.get("existing", []))
            results.extend(output.get("errors", []))

            created_count = output.get("created_count", 0)
            existing_count = output.get("existing_count", 0)
            error_count = output.get("error_count", 0)

            self.logger.info(f"Retrieved results with {len(results)} entries")
        else:
            # If direct output doesn't work, try to get the result file
            if rails_client.transfer_file_from_container(
                result_file_container, result_file_local
            ):
                try:
                    with open(result_file_local) as f:
                        result_data = json.load(f)

                        if result_data.get("status") == "success":
                            results = []
                            results.extend(result_data.get("created", []))
                            results.extend(result_data.get("existing", []))
                            results.extend(result_data.get("errors", []))

                            created_count = result_data.get("created_count", 0)
                            existing_count = result_data.get("existing_count", 0)
                            error_count = result_data.get("error_count", 0)

                            self.logger.info(
                                f"Retrieved results with {len(results)} entries"
                            )
                        else:
                            self.logger.error(
                                f"Error in result data: {result_data.get('message', 'Unknown error')}"
                            )
                            return False
                except Exception as e:
                    self.logger.error(f"Error processing result file: {str(e)}")
                    return False
            else:
                self.logger.error("Failed to retrieve result file from container")
                return False

        # Process the results
        for result_entry in results:
            status = result_entry.get("status")
            jira_id = result_entry.get("jira_id")
            name = result_entry.get("name")

            if status == "created":
                if jira_id and jira_id in self.mapping:
                    self.mapping[jira_id]["matched_by"] = "created"
                    self.mapping[jira_id]["openproject_id"] = result_entry.get("id")
                    created_fields.append(name)
            elif status == "existing":
                if jira_id and jira_id in self.mapping:
                    self.mapping[jira_id]["matched_by"] = "name"
                    self.mapping[jira_id]["openproject_id"] = result_entry.get("id")
            elif status == "error":
                self.logger.error(
                    f"Error creating field '{name}': {result_entry.get('message')}"
                )

        # Save the updated mapping
        self._save_to_json(self.mapping, "custom_field_mapping.json")

        # Print summary
        self.logger.info("\nCustom Fields JSON Migration Summary:")
        self.logger.info(f"Total fields processed: {len(results)}")
        self.logger.info(f"Successfully created: {created_count}")
        self.logger.info(f"Already existing: {existing_count}")
        self.logger.info(f"Failed: {error_count}")

        if error_count > 0:
            self.logger.warning(f"{error_count} custom fields failed to create")

        if created_count > 0:
            self.logger.success(f"Successfully created {created_count} custom fields")
            return True
        elif error_count == 0:
            self.logger.info("No new custom fields created, but no errors either")
            return True
        else:
            self.logger.error("Failed to create any custom fields")
            return False

    def migrate_custom_fields(self) -> bool:
        """Migrate custom fields from Jira to OpenProject

        Returns:
            Boolean indicating success or failure
        """
        # Use the mapping analysis to decide whether to migrate
        analysis = self.analyze_custom_field_mapping()

        if not analysis:
            self.logger.error(
                "Analysis of custom field mapping failed. Cannot migrate."
            )
            return False

        self.logger.info(
            f"Starting custom field migration with {len(self.mapping)} fields in mapping"
        )

        self.extract_openproject_custom_fields()
        self.create_custom_field_mapping()

        # Check if we have fields that need to be created
        fields_to_create = [
            f for f in self.mapping.values() if f["matched_by"] == "create"
        ]

        if not fields_to_create:
            self.logger.info(
                "No custom fields need to be created. All mapped or ignored."
            )
            return True

        self.logger.info(f"Found {len(fields_to_create)} custom fields to create")

        # Use JSON-based approach for all migrations
        return self.migrate_custom_fields_via_json(fields_to_create)

    def analyze_custom_field_mapping(self) -> dict[str, Any]:
        """
        Analyze the custom field mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        # Check if analysis was recently performed to avoid duplicate logging
        if (
            hasattr(self, "_last_analysis_time")
            and time.time() - self._last_analysis_time < 5
        ):
            self.logger.debug("Skipping duplicate analysis - was just performed")
            # Make sure the analysis has a status field
            if hasattr(self, "analysis") and self.analysis:
                if "status" not in self.analysis:
                    self.analysis["status"] = "success"
                return self.analysis
            return {"status": "error", "message": "No analysis data available"}

        # Track when analysis was last performed
        self._last_analysis_time = time.time()

        if not self.mapping:
            mapping_path = os.path.join(self.data_dir, "custom_field_mapping.json")
            if os.path.exists(mapping_path):
                with open(mapping_path) as f:
                    self.mapping = json.load(f)
            else:
                self.logger.error(
                    "No custom field mapping found. Run create_custom_field_mapping() first."
                )
                return {"status": "error", "message": "No custom field mapping found"}

        # Analyze the mapping
        total_fields = len(self.mapping)
        matched_fields = sum(
            1 for field in self.mapping.values() if field["matched_by"] == "name"
        )
        created_fields = sum(
            1 for field in self.mapping.values() if field["matched_by"] == "created"
        )
        to_create_fields = sum(
            1 for field in self.mapping.values() if field["matched_by"] == "create"
        )

        analysis = {
            "status": "success",
            "total_jira_fields": total_fields,
            "matched_by_name": matched_fields,
            "created_directly": created_fields,
            "needs_manual_creation_or_script": to_create_fields,
            "unmatched_details": [
                {
                    "jira_id": field["jira_id"],
                    "jira_name": field["jira_name"],
                    "proposed_op_name": field["openproject_name"],
                    "proposed_op_type": field["openproject_type"],
                    "possible_values": field.get("possible_values", None),
                }
                for field in self.mapping.values()
                if field["matched_by"] == "create"
            ],
        }

        # Calculate percentages
        if total_fields > 0:
            analysis["match_percentage"] = (matched_fields / total_fields) * 100
            analysis["created_percentage"] = (created_fields / total_fields) * 100
            analysis["needs_creation_percentage"] = (
                to_create_fields / total_fields
            ) * 100
        else:
            analysis["match_percentage"] = 0
            analysis["created_percentage"] = 0
            analysis["needs_creation_percentage"] = 0

        # Save analysis to file
        self._save_to_json(analysis, "custom_field_analysis.json")
        self.analysis = analysis  # Update internal state

        # Print analysis summary
        self.logger.info("Custom Field Mapping Analysis:")
        self.logger.info(f"Total Jira custom fields processed: {total_fields}")
        self.logger.info(
            f"- Matched by name: {matched_fields} ({analysis['match_percentage']:.1f}%)"
        )
        self.logger.info(
            f"- Created directly via Rails: {created_fields} ({analysis['created_percentage']:.1f}%)"
        )
        self.logger.info(
            f"- Still need creation: {to_create_fields} ({analysis['needs_creation_percentage']:.1f}%)"
        )

        if to_create_fields > 0:
            self.logger.warning(
                f"Action required: {to_create_fields} custom fields need manual creation or script execution."
            )
            self.logger.warning("Details saved to custom_field_analysis.json")

        return analysis

    def update_mapping_file(self) -> bool:
        """
        Update the mapping file after manual creation of custom fields.

        Returns:
            True if mapping was updated, False otherwise
        """
        self.extract_openproject_custom_fields()
        self.extract_jira_custom_fields()
        return self.create_custom_field_mapping() is not None

    def run(self) -> ComponentResult:
        """
        Run the custom field migration process.

        Returns:
            Dictionary with migration results
        """
        self.logger.info("Starting custom field migration", extra={"markup": True})

        try:
            # Extract data once and cache it
            jira_fields = self.extract_jira_custom_fields()
            op_fields = self.extract_openproject_custom_fields()

            mapping = self.create_custom_field_mapping()

            # Migrate custom fields
            if not config.migration_config.get("dry_run", False) and self.rails_console:
                self.logger.info(
                    "Migrating custom fields via Rails console", extra={"markup": True}
                )
                success = self.migrate_custom_fields()
            elif not config.migration_config.get("dry_run", False):
                self.logger.info(
                    "Generating JSON migration script (no Rails console available)",
                    extra={"markup": True},
                )
                success = self.migrate_custom_fields()
            else:
                self.logger.warning(
                    "Dry run mode - not creating custom fields", extra={"markup": True}
                )
                success = True

            # Analyze results
            analysis = self.analyze_custom_field_mapping()

            return ComponentResult(
                success=success,
                jira_fields_count=len(jira_fields),
                op_fields_count=len(op_fields),
                mapped_fields_count=len(mapping),
                success_count=analysis.get("matched_by_name", 0)
                + analysis.get("created_directly", 0),
                failed_count=analysis.get("needs_manual_creation_or_script", 0),
                total_count=len(jira_fields),
                analysis=analysis,
            )
        except Exception as e:
            self.logger.exception(
                f"Error during custom field migration: {str(e)}",
                extra={"markup": True, "traceback": True},
            )
            return ComponentResult(
                success=False,
                error=str(e),
                success_count=0,
                failed_count=(
                    len(self.jira_custom_fields) if self.jira_custom_fields else 0
                ),
                total_count=(
                    len(self.jira_custom_fields) if self.jira_custom_fields else 0
                ),
            )

