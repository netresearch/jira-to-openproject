"""Custom field migration module for Jira to OpenProject migration.

Handles the migration of custom fields from Jira to OpenProject.
"""

import json
import pathlib
import time
from pathlib import Path
from typing import Any

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient

# Import RailsConsolePexpect to handle direct Rails console execution
from src.display import ProgressTracker, console
from src.migrations.base_migration import BaseMigration
from src.models import ComponentResult, MigrationError

# Create rich console instance
console = console


class CustomFieldMigration(BaseMigration):
    """Handles the migration of custom fields from Jira to OpenProject.

    This class supports two approaches:
    1. Generate a Ruby script for manual execution via Rails console (traditional approach)
    2. Execute commands directly on the Rails console using pexpect (direct approach)
    """

    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
        rails_console = None,
    ) -> None:
        """Initialize the custom field migration process.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client
            rails_console: Optional rails console client (for backward compatibility)

        """
        super().__init__(jira_client, op_client)
        self.jira_custom_fields: list[dict[str, Any]] = []
        self.op_custom_fields: list[dict[str, Any]] = []
        self.mapping: dict[str, Any] = {}
        self.analysis: dict[str, Any] = {}

        # For backward compatibility with tests
        self.rails_client = rails_console or op_client

        self._load_data()

    def _load_data(self) -> None:
        """Load existing data from JSON files."""
        self.jira_custom_fields = self._load_from_json("jira_custom_fields.json", [])
        self.op_custom_fields = self._load_from_json("op_custom_fields.json", [])
        self.mapping = self._load_from_json("custom_field_mapping.json", {})

        analysis_data = self._load_from_json("custom_field_analysis.json", {})
        self.analysis = {} if analysis_data is None else analysis_data

        self.logger.info(
            "Loaded %d Jira custom fields",
            len(self.jira_custom_fields),
        )
        self.logger.info(
            "Loaded %d OpenProject custom fields",
            len(self.op_custom_fields),
        )
        self.logger.info(
            "Loaded %d custom field mappings",
            len(self.mapping),
        )
        self.logger.info(
            "Loaded analysis with %d keys: %s",
            len(self.analysis),
            list(self.analysis.keys()),
        )

    def extract_jira_custom_fields(self) -> list[dict[str, Any]]:
        """Extract custom field information from Jira.

        Returns:
            List of Jira custom fields

        Raises:
            MigrationError: If extraction fails

        """
        custom_fields_file = self.data_dir / "jira_custom_fields.json"

        if custom_fields_file.exists() and not config.migration_config.get("force", False):
            self.logger.info("Jira custom fields data already exists, skipping extraction (use --force to override)")
            with custom_fields_file.open() as f:
                self.jira_custom_fields = json.load(f)
            return self.jira_custom_fields

        self.logger.info("Extracting Jira custom fields...")

        try:
            # Get all fields from Jira
            jira_fields = self.jira_client.jira.fields()

            # Filter to only include custom fields
            custom_fields = []
            for field in jira_fields:
                if field.get("custom", False):
                    custom_field_data = {
                        "id": field.get("id"),
                        "name": field.get("name"),
                        "type": field.get("schema", {}).get("type", "unknown"),
                        "custom_type": field.get("schema", {}).get("custom", "unknown"),
                    }

                    # Special handling for fields with allowedValues (select fields)
                    if "schema" in field and field["schema"].get("type") in ["option", "array"]:
                        try:
                            # Note: This makes an additional API call for each select field
                            # For large Jira instances this could be optimized
                            field_metadata = self.jira_client.get_field_metadata(field.get("id"))
                            if "allowedValues" in field_metadata:
                                allowed_values = []
                                for value in field_metadata["allowedValues"]:
                                    # Different fields might structure their values differently
                                    if "value" in value:
                                        allowed_values.append(value["value"])
                                    elif "name" in value:
                                        allowed_values.append(value["name"])
                                    else:
                                        allowed_values.append(str(value))
                                custom_field_data["allowed_values"] = allowed_values
                        except Exception as e:
                            self.logger.warning(
                                "Failed to get metadata for field %s: %s",
                                field.get("name"),
                                str(e),
                            )

                    custom_fields.append(custom_field_data)

            # Save to file
            if not custom_fields_file.parent.exists():
                custom_fields_file.parent.mkdir(parents=True, exist_ok=True)

            with custom_fields_file.open("w") as f:
                json.dump(custom_fields, f, indent=2, ensure_ascii=False)

            self.logger.info("Saved Jira custom fields data (%d fields)", len(custom_fields))
            self.jira_custom_fields = custom_fields
            return custom_fields

        except Exception as e:
            self.logger.exception("Failed to extract Jira custom fields")
            error = f"Failed to extract Jira custom fields: {str(e)}"
            raise MigrationError(error) from e

    def extract_openproject_custom_fields(self) -> list[dict[str, Any]]:
        """Extract custom field information from OpenProject and save to a JSON file.

        Returns:
            List of custom field dictionaries retrieved from OpenProject

        Raises:
            MigrationError: If extraction fails

        """
        self.logger.info("Starting OpenProject custom field extraction...")

        # Path to the output JSON file
        output_file = pathlib.Path(self.output_dir) / "openproject_custom_fields.json"

        # Check if the data already exists
        if output_file.exists() and not config.migration_config.get("force", False):
            self.logger.info("Using existing OpenProject custom field data from %s", output_file)
            try:
                with output_file.open(encoding="utf-8") as f:
                    data = json.load(f)
                    self.op_custom_fields = data
                    return data
            except json.JSONDecodeError:
                self.logger.warning("Existing file %s is invalid. Re-extracting data.", output_file)
            except Exception as e:
                self.logger.exception("Error reading existing data: %s. Re-extracting data.", e)

        self.logger.info("Retrieving custom fields from OpenProject via Rails console...")

        try:
            # Use the op_client's get_custom_fields method which uses Rails console
            all_fields = self.op_client.get_custom_fields(force_refresh=True)

            if not all_fields:
                msg = "Failed to retrieve custom fields from OpenProject"
                raise MigrationError(msg)

            # Process and save the fields
            self.op_custom_fields = all_fields
            self._save_to_json(all_fields, "op_custom_fields.json")

            return all_fields
        except Exception as e:
            error_msg = f"Failed to extract custom fields from OpenProject: {e!s}"
            self.logger.exception(error_msg)
            raise MigrationError(error_msg) from e

    def map_jira_field_to_openproject_format(self, jira_field: dict[str, Any]) -> str:
        """Map a Jira custom field type to the closest OpenProject field format.

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
                or "select" in jira_custom_type.lower()
                or "radio" in jira_custom_type.lower()
            ):
                return "list"
            if "date" in jira_custom_type.lower():
                return "date"
            if "number" in jira_custom_type.lower() or "float" in jira_custom_type.lower():
                return "float"
            if "integer" in jira_custom_type.lower():
                return "int"
            if "user" in jira_custom_type.lower():
                return "user"
            if (
                "text" in jira_custom_type.lower()
                or "string" in jira_custom_type.lower()
                or "url" in jira_custom_type.lower()
            ):
                return "text"
            if "boolean" in jira_custom_type.lower():
                return "bool"

        if jira_type == "array":
            items_type = schema.get("items", "")
            if items_type == "string":
                return "list"
            if items_type == "user":
                return "user"
            if items_type == "date":
                return "date"
            return "list"

        return op_format_map.get(jira_type, op_format_map.get("default", "text"))

    def create_custom_field_mapping(self) -> dict[str, Any]:
        """Create a mapping between Jira and OpenProject custom fields.

        Returns:
            Dictionary mapping Jira custom field IDs to OpenProject field information

        """
        self.logger.info("Creating custom field mapping...")

        self.extract_jira_custom_fields()

        self.extract_openproject_custom_fields()

        mapping = {}

        op_fields_by_name = {field.get("name", "").lower(): field for field in self.op_custom_fields}

        def process_field(jira_field: dict[str, Any], context: dict[str, Any]) -> str | None:
            jira_id = jira_field.get("id")
            jira_name = jira_field.get("name", "")
            jira_name_lower = jira_name.lower()

            op_field = op_fields_by_name.get(jira_name_lower)

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

        mapping_file = self.data_dir / "custom_field_mapping.json"
        with mapping_file.open("w") as f:
            json.dump(mapping, f, indent=2)
        self.logger.info("Saved custom field mapping to %s", mapping_file)

        total_fields = len(mapping)
        matched_fields = sum(1 for field in mapping.values() if field["matched_by"] == "name")
        created_fields = sum(1 for field in mapping.values() if field["matched_by"] == "created")

        self.logger.info(
            "Custom field mapping created for %d fields",
            total_fields,
        )
        self.logger.info(
            "- Matched by name: %d",
            matched_fields,
        )
        self.logger.info("- Need to create: %d", created_fields)

        self.mapping = mapping
        return mapping

    def create_custom_field_via_rails(self, field_data: dict[str, Any]) -> dict[str, Any]:
        """Create a custom field in OpenProject via Rails console.

        Args:
            field_data: Custom field definition

        Returns:
            dict: Result of the operation

        """
        field_type = field_data.get("openproject_type", "string")
        field_name = field_data.get("openproject_name", field_data.get("jira_name", "Unnamed Field"))

        field_name = field_name.replace('"', '\\"')

        possible_values_ruby = "[]"
        if field_type == "list":
            if (
                "possible_values" in field_data
                and field_data["possible_values"]
                and isinstance(field_data["possible_values"], list)
            ):
                values = field_data["possible_values"]
                escaped_values = [v.replace('"', '\\"') if isinstance(v, str) else v for v in values]
                values_str = ", ".join([f'"{v}"' for v in escaped_values])
                possible_values_ruby = f"[{values_str}]"
            else:
                possible_values_ruby = '["Default option"]'
                self.logger.warning(
                    "Custom field '%s' is a list type but has no options. Adding a default option.",
                    field_name,
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

        result = self.op_client.execute(command)

        if result.get("status") == "error":
            error_info = result.get("error", "Unknown error")
            if "errors" in result:
                error_info = f"Validation errors: {result['errors']}"
            elif "validation_errors" in result:
                error_info = f"Field validation failed: {result['validation_errors']}"

            self.logger.error("Error creating custom field '%s': %s", field_name, error_info)

            if isinstance(result.get("errors"), list) and any("Possible values" in err for err in result["errors"]):
                self.logger.error("Field '%s' requires possible values for list type fields", field_name)
        elif result.get("status") == "success":
            self.logger.info("Created custom field '%s' successfully", field_name)

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
            "Migrating %d custom fields using batch migration",
            len(fields_to_migrate),
        )

        # Convert the fields to the format expected by the Ruby script
        custom_fields_data = []
        for field in fields_to_migrate:
            if not field.get("jira_name"):
                self.logger.warning("Skipping field without name: %s", field)
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

        # Generate a timestamp for uniqueness
        timestamp = int(time.time())

        # Write the custom fields data to a JSON file
        data_file_path = self.data_dir / f"custom_fields_batch_{timestamp}.json"
        with open(data_file_path, "w", encoding="utf-8") as f:
            json.dump(custom_fields_data, f, indent=2, ensure_ascii=False)

        self.logger.info("Writing %d custom fields to %s", len(custom_fields_data), data_file_path)

        # Transfer the file to the container
        container_data_path = f"/tmp/custom_fields_batch_{timestamp}.json"

        # Use rails_client for file transfers
        if hasattr(self.rails_client, "transfer_file_to_container"):
            success = self.rails_client.transfer_file_to_container(data_file_path, container_data_path)
            if not success:
                self.logger.error("Failed to transfer custom fields data to container")
                return False
        else:
            self.logger.error("Rails client does not support file transfers")
            return False

        # Ruby script to create custom fields from the JSON file
        ruby_script = f"""
        # Ruby variables from Python
        data_file_path = "{container_data_path}"

        begin
          # Load the JSON data
          require 'json'
          json_data = File.read(data_file_path)
          custom_fields_data = JSON.parse(json_data)

          puts "Loading #{{custom_fields_data.length}} custom fields from #{{data_file_path}}"

          # Initialize counters
          created_fields = []
          existing_fields = []
          error_fields = []

          # Process each custom field
          custom_fields_data.each do |field_data|
            begin
              field_name = field_data['name']
              field_format = field_data['field_format']
              jira_id = field_data['jira_id']

              puts "Processing field: #{{field_name}} (#{{field_format}})"

              # Check if field already exists
              existing_field = CustomField.find_by(name: field_name)

              if existing_field
                puts "  Custom field '#{{field_name}}' already exists with ID: #{{existing_field.id}}"
                existing_fields << {{
                  name: field_name,
                  id: existing_field.id,
                  jira_id: jira_id,
                  status: 'existing'
                }}
                next
              end

              # Create new custom field
              cf = CustomField.new(
                name: field_name,
                field_format: field_format,
                is_required: field_data['is_required'] || false,
                is_for_all: field_data['is_for_all'] || true,
                type: field_data['type'] || 'WorkPackageCustomField'
              )

              # Set possible values for list fields
              if field_format == 'list'
                if field_data['possible_values'] && !field_data['possible_values'].empty?
                  values = field_data['possible_values']
                  cf.possible_values = values.map {{ |value| value.to_s.strip }}
                else
                  cf.possible_values = ['Default option']
                end

                # Ensure field has at least one value
                if cf.possible_values.nil? || cf.possible_values.empty?
                  cf.possible_values = ['Default option']
                end
              end

              # Save the custom field
              if cf.save
                puts "  Created custom field '#{{field_name}}' with ID: #{{cf.id}}"

                # Make it available for all work package types if is_for_all
                if cf.is_for_all?
                  puts "  Activating for all work package types..."
                  Type.all.each do |type|
                    type.custom_fields << cf unless type.custom_fields.include?(cf)
                    type.save!
                  end
                end

                created_fields << {{
                  name: field_name,
                  id: cf.id,
                  jira_id: jira_id,
                  status: 'created'
                }}
              else
                puts "  Error creating custom field '#{{field_name}}': #{{cf.errors.full_messages.join(', ')}}"
                error_fields << {{
                  name: field_name,
                  jira_id: jira_id,
                  status: 'error',
                  errors: cf.errors.full_messages
                }}
              end
            rescue => e
              puts "  Error processing field '#{{field_data['name']}}': #{{e.message}}"
              error_fields << {{
                name: field_data['name'],
                jira_id: field_data['jira_id'],
                status: 'error',
                errors: [e.message]
              }}
            end
          end

          # Output the results
          result = {{
            status: 'success',
            created: created_fields,
            existing: existing_fields,
            errors: error_fields,
            created_count: created_fields.length,
            existing_count: existing_fields.length,
            error_count: error_fields.length
          }}

          puts "Results: Created #{{created_fields.length}}, Already existed: #{{existing_fields.length}}, Errors: #{{error_fields.length}}"

          # Write results to file for later processing if needed
          result_file = "/tmp/custom_fields_result_#{{Time.now.to_i}}.json"
          File.write(result_file, JSON.pretty_generate(result))
          puts "Results written to #{{result_file}}"

          # Return the result object
          result

        rescue Exception => e
          error_result = {{
            status: 'error',
            message: e.message,
            backtrace: e.backtrace,
            data_file: data_file_path
          }}

          puts "Critical error: #{{e.message}}"
          error_result
        end
        """

        # Execute the Ruby script
        self.logger.info("Executing Ruby script with custom field data")
        result = self.rails_client.execute(ruby_script)

        if result.get("status") == "error":
            self.logger.error("Error executing Ruby script: %s", result.get("error", "Unknown error"))
            return False

        # Check the result status from the Ruby output
        output = result.get("output", {})
        created_count = output.get("created_count", 0)
        existing_count = output.get("existing_count", 0)
        error_count = output.get("error_count", 0)

        self.logger.info(
            "Custom field migration completed: Created %d, Existing %d, Errors %d",
            created_count,
            existing_count,
            error_count,
        )

        if created_count > 0:
            # Update the mapping file with the newly created fields
            self.logger.info("Updating mapping with newly created fields")
            self.update_mapping_file()

        return True

    def migrate_custom_fields(self) -> bool:
        """Migrate custom fields from Jira to OpenProject.

        Returns:
            Boolean indicating success or failure

        """
        # Use the mapping analysis to decide whether to migrate
        analysis = self.analyze_custom_field_mapping()

        if not analysis:
            self.logger.error("Analysis of custom field mapping failed. Cannot migrate.")
            return False

        self.logger.info(
            "Starting custom field migration with %d fields in mapping",
            len(self.mapping),
        )

        self.extract_openproject_custom_fields()
        self.create_custom_field_mapping()

        # Check if we have fields that need to be created
        fields_to_create = [f for f in self.mapping.values() if f.get("matched_by") == "create"]

        if not fields_to_create:
            # If no fields in the mapping need to be created, but we were explicitly provided with
            # fields to create in the mapping, use those directly
            # This is primarily for testing purposes
            if any(f.get("matched_by") == "create" for f in self.mapping.values()):
                fields_to_create = [f for f in self.mapping.values() if f.get("matched_by") == "create"]
            else:
                self.logger.info("No custom fields need to be created. All mapped or ignored.")
                return True

        self.logger.info("Found %d custom fields to create", len(fields_to_create))

        # Use JSON-based approach for all migrations
        return self.migrate_custom_fields_via_json(fields_to_create)

    def analyze_custom_field_mapping(self) -> dict[str, Any]:
        """Analyze the custom field mapping to identify potential issues.

        Returns:
            Dictionary with analysis results

        """
        # Check if analysis was recently performed to avoid duplicate logging
        if hasattr(self, "_last_analysis_time") and time.time() - self._last_analysis_time < 5:
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
            mapping_path = self.data_dir / "custom_field_mapping.json"
            if mapping_path.exists():
                with mapping_path.open() as f:
                    self.mapping = json.load(f)
            else:
                self.logger.error("No custom field mapping found. Run create_custom_field_mapping() first.")
                return {"status": "error", "message": "No custom field mapping found"}

        # Analyze the mapping
        total_fields = len(self.mapping)
        matched_fields = sum(1 for field in self.mapping.values() if field["matched_by"] == "name")
        created_fields = sum(1 for field in self.mapping.values() if field["matched_by"] == "created")
        to_create_fields = sum(1 for field in self.mapping.values() if field["matched_by"] == "create")

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
            analysis["needs_creation_percentage"] = (to_create_fields / total_fields) * 100
        else:
            analysis["match_percentage"] = 0
            analysis["created_percentage"] = 0
            analysis["needs_creation_percentage"] = 0

        # Save analysis to file
        self._save_to_json(analysis, "custom_field_analysis.json")
        self.analysis = analysis  # Update internal state

        # Print analysis summary
        self.logger.info("Custom Field Mapping Analysis:")
        self.logger.info("Total Jira custom fields processed: %d", total_fields)
        self.logger.info("- Matched by name: %d (%.1f%%)", matched_fields, analysis["match_percentage"])
        self.logger.info("- Created directly via Rails: %d (%.1f%%)", created_fields, analysis["created_percentage"])
        self.logger.info(
            "- Still need creation: %d (%.1f%%)",
            to_create_fields,
            analysis["needs_creation_percentage"],
        )

        if to_create_fields > 0:
            self.logger.warning(
                "Action required: %d custom fields need manual creation or script execution.",
                to_create_fields,
            )
            self.logger.warning("Details saved to custom_field_analysis.json")

        return analysis

    def update_mapping_file(self) -> bool:
        """Update the mapping file after manual creation of custom fields.

        Returns:
            True if mapping was updated, False otherwise

        """
        self.extract_openproject_custom_fields()
        self.extract_jira_custom_fields()
        return self.create_custom_field_mapping() is not None

    def run(self) -> ComponentResult:
        """Run the custom field migration process.

        Returns:
            Dictionary with migration results

        """
        self.logger.info("Starting custom field migration")

        try:
            # Extract data once and cache it
            jira_fields = self.extract_jira_custom_fields()
            op_fields = self.extract_openproject_custom_fields()

            mapping = self.create_custom_field_mapping()

            # Migrate custom fields
            self.logger.info(
                "Migrating custom fields via Rails console",
            )
            success = self.migrate_custom_fields()

            # Analyze results
            analysis = self.analyze_custom_field_mapping()

            return ComponentResult(
                success=success,
                jira_fields_count=len(jira_fields),
                op_fields_count=len(op_fields),
                mapped_fields_count=len(mapping),
                success_count=analysis.get("matched_by_name", 0) + analysis.get("created_directly", 0),
                failed_count=analysis.get("needs_manual_creation_or_script", 0),
                total_count=len(jira_fields),
                analysis=analysis,
            )
        except Exception as e:
            self.logger.exception("Error during custom field migration")
            return ComponentResult(
                success=False,
                error=str(e),
                success_count=0,
                failed_count=(len(jira_fields) if jira_fields else 0),
                total_count=(len(jira_fields) if jira_fields else 0),
            )
