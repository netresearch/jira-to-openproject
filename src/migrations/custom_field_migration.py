"""
Custom field migration module for Jira to OpenProject migration.
Handles the migration of custom fields from Jira to OpenProject.
"""

import os
import sys
import json
import time
import pathlib
from datetime import datetime
from typing import Dict, List, Any, Optional

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import ProgressTracker, console
from src.migrations.base_migration import BaseMigration

# Import RailsConsolePexpect to handle direct Rails console execution
from src.clients.openproject_rails_client import OpenProjectRailsClient

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
        jira_client: Optional[JiraClient] = None,
        op_client: Optional[OpenProjectClient] = None,
        rails_console: Optional['OpenProjectRailsClient'] = None,
    ) -> None:
        """
        Initialize the custom field migration process.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client
            rails_console: Initialized OpenProjectRailsClient instance (optional)
        """
        super().__init__(jira_client, op_client)
        self.jira_custom_fields: List[Dict] = []
        self.op_custom_fields: List[Dict] = []
        self.mapping: Dict[str, Dict] = {}
        self.analysis: Dict = {}
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
        self.logger.info(f"Loaded {len(self.op_custom_fields)} OpenProject custom fields")
        self.logger.info(f"Loaded {len(self.mapping)} custom field mappings")
        self.logger.info(f"Loaded analysis with {len(self.analysis)} keys: {list(self.analysis.keys())}")

    def extract_jira_custom_fields(self, force=False) -> List[Dict[str, Any]]:
        """
        Extract custom field information from Jira.

        Args:
            force: If True, extract again even if data already exists

        Returns:
            List of Jira custom fields
        """
        custom_fields_file = os.path.join(self.data_dir, "jira_custom_fields.json")

        if os.path.exists(custom_fields_file) and not force:
            self.logger.info(
                "Jira custom fields data already exists, skipping extraction (use --force to override)"
            )
            with open(custom_fields_file, "r") as f:
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
                    field_type == "option" or
                    "select" in field_custom_type.lower() or
                    "option" in field_custom_type.lower() or
                    "radio" in field_custom_type.lower()
                )

                if is_select_list:
                    try:
                        self.logger.notice(f"Retrieving options for field: {field.get('name')}")
                        meta_data = self.jira_client.get_field_metadata(field_id)

                        allowed_values = []

                        if "allowedValues" in meta_data:
                            for value in meta_data["allowedValues"]:
                                if "value" in value:
                                    allowed_values.append(value["value"])
                                elif "name" in value:
                                    allowed_values.append(value["name"])

                        if allowed_values:
                            self.logger.notice(f"Found {len(allowed_values)} options for field '{field.get('name')}' (ID: {field_id})")
                            enhanced_field["allowed_values"] = allowed_values
                    except Exception as e:
                        self.logger.warning(f"Could not retrieve options for field '{field.get('name')}': {str(e)}")

                enhanced_fields.append(enhanced_field)

            self.jira_custom_fields = enhanced_fields

            with open(custom_fields_file, "w") as f:
                json.dump(self.jira_custom_fields, f, indent=2)
            self.logger.info(f"Saved data to {custom_fields_file}")

            return self.jira_custom_fields
        except Exception as e:
            self.logger.error(f"Failed to extract custom fields from Jira: {str(e)}")
            return []

    def extract_openproject_custom_fields(self, force: bool = False) -> List[Dict[str, Any]]:
        """
        Extract custom field information from OpenProject and save to a JSON file.

        Args:
            force (bool): If True, forces extraction even if data exists.

        Returns:
            List of custom field dictionaries retrieved from OpenProject
        """
        self.logger.info("Starting OpenProject custom field extraction...")

        # Path to the output JSON file
        output_file = pathlib.Path(self.output_dir) / "openproject_custom_fields.json"

        # Check if the data already exists
        if output_file.exists() and not force:
            self.logger.info(f"Using existing OpenProject custom field data from {output_file}")
            try:
                with open(output_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                self.logger.warning(f"Existing file {output_file} is invalid. Re-extracting data.")
            except Exception as e:
                self.logger.error(f"Error reading existing data: {str(e)}. Re-extracting data.")

        self.logger.info("Retrieving custom fields from OpenProject via API...")

        try:
            # Use the op_client's get_custom_fields method which uses Rails console
            all_fields = self.op_client.get_custom_fields(force_refresh=True)

            if not all_fields:
                self.logger.error("Failed to retrieve custom fields from OpenProject")
                return []

            self.logger.info(f"Retrieved {len(all_fields)} custom fields from OpenProject")

            # Save the extracted data
            self.logger.info(f"Saving {len(all_fields)} OpenProject custom fields to {output_file}")
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(all_fields, f, indent=2)

            return all_fields

        except Exception as e:
            self.logger.error(f"Error extracting OpenProject custom fields: {str(e)}")
            self.logger.debug(f"Error details:", exc_info=True)
            return []

    def map_jira_field_to_openproject_format(self, jira_field: Dict[str, Any]) -> str:
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

    def create_custom_field_mapping(self, force: bool = False) -> Dict[str, Any]:
        """
        Create a mapping between Jira and OpenProject custom fields.

        Args:
            force: If True, force extraction of data even if it already exists

        Returns:
            Dictionary mapping Jira custom field IDs to OpenProject field information
        """
        self.logger.info("Creating custom field mapping...")

        self.logger.info("Extracting custom fields from Jira...")
        self.logger.debug(f"Force: {force}")
        self.extract_jira_custom_fields(force=force)

        self.logger.info("Extracting custom fields from OpenProject...")
        self.extract_openproject_custom_fields(force=force)

        mapping = {}

        op_fields_by_name = {
            field.get("name", "").lower(): field for field in self.op_custom_fields
        }

        fields_to_create = []

        def process_field(jira_field, context):
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
            log_title="Custom Fields to Create"
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

    def create_custom_field_via_rails(self, field_data: Dict) -> Dict[str, Any]:
        """
        Create a custom field in OpenProject via Rails console.

        Args:
            field_data: Custom field definition

        Returns:
            dict: Result of the operation
        """
        field_type = field_data.get('openproject_type', 'string')
        field_name = field_data.get('openproject_name', field_data.get('jira_name', 'Unnamed Field'))

        field_name = field_name.replace('"', '\\"')

        possible_values_ruby = "[]"
        if field_type == 'list':
            if 'possible_values' in field_data and field_data['possible_values'] and isinstance(field_data['possible_values'], list):
                values = field_data['possible_values']
                escaped_values = [v.replace('"', '\\"') if isinstance(v, str) else v for v in values]
                values_str = ', '.join([f'"{v}"' for v in escaped_values])
                possible_values_ruby = f"[{values_str}]"
            else:
                possible_values_ruby = '["Default option"]'
                self.logger.warning(f"Custom field '{field_name}' is a list type but has no options. Adding a default option.")

        default_value = "nil"
        if 'default_value' in field_data:
            value = field_data['default_value']
            if field_type == 'string' or field_type == 'text':
                default_value = f'"{value}"'
            elif field_type == 'bool':
                default_value = 'true' if value else 'false'
            elif field_type in ('int', 'float'):
                default_value = str(value)
            elif field_type == 'date':
                default_value = f'Date.parse("{value}")' if value else 'nil'

        command = f"""
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
              if cf.possible_values.nil? || cf.possible_values.empty?
                cf.possible_values = ['Default option'] # Ensure default option exists
              end
            end

            if cf.save
              puts "  Successfully created custom field '{field_name}' with ID: #{{cf.id}}"

              # Make it available for all work package types
              if cf.is_for_all?
                puts "  Activating for all work package types..."
                Type.all.each do |type|
                  type.custom_fields << cf unless type.custom_fields.include?(cf)
                  type.save!
                end
              end
            else
              puts "  Error creating custom field '{field_name}': #{{cf.errors.full_messages.join(', ')}}"
            end
          end
        rescue => e
          puts "  An unexpected error occurred while processing '{field_name}': #{{e.message}}"
        end
        """

        result = self.rails_console.execute(command)

        if result['status'] == 'error':
            error_info = result.get('error', 'Unknown error')
            if 'errors' in result:
                error_info = f"Validation errors: {result['errors']}"
            elif 'validation_errors' in result:
                error_info = f"Field validation failed: {result['validation_errors']}"

            self.logger.error(f"Error creating custom field '{field_name}': {error_info}")

            if isinstance(result.get('errors'), list) and any("Possible values" in err for err in result['errors']):
                self.logger.error(f"Field '{field_name}' requires possible values for list type fields")
        elif result['status'] == 'success':
            self.logger.info(f"Created custom field '{field_name}' successfully")

        return result

    def migrate_custom_fields_via_rails(self, window: int = 0, pane: int = 0) -> bool:
        """
        Migrate custom fields via direct Rails console execution.

        Args:
            window: tmux window number (default: 0)
            pane: tmux pane number (default: 0)

        Returns:
            True if migration was successful, False otherwise
        """
        self.logger.info("Starting direct custom fields migration via Rails console")

        existing_fields = self.op_client.get_custom_fields()
        if existing_fields is None:
            self.logger.error("Failed to retrieve existing custom fields from OpenProject. Defaulting to empty list.")
            existing_fields = []
        existing_names = [field.get('name') for field in existing_fields]
        existing_name_to_field = {field.get('name'): field for field in existing_fields}

        results = []
        success_count = 0
        error_count = 0
        skipped_count = 0
        fixed_count = 0

        previous_results_file = os.path.join(self.data_dir, "custom_field_migration_results.json")
        previous_errors = {}
        if os.path.exists(previous_results_file):
            try:
                with open(previous_results_file, "r") as f:
                    previous_results = json.load(f)
                    for result in previous_results:
                        if result.get('status') == 'error':
                            previous_errors[result.get('name')] = result

                    if previous_errors:
                        self.logger.info(f"Found {len(previous_errors)} previously failed custom fields")
            except Exception as e:
                self.logger.warning(f"Could not load previous migration results: {str(e)}")

        fields_to_create = [field for field in self.mapping.values() if field["matched_by"] == "create"]

        updated_count = 0
        for field in list(fields_to_create):
            field_name = field.get('openproject_name', field.get('jira_name', 'Unknown field'))
            if field_name in existing_names:
                field_id = field.get('jira_id')
                if field_id in self.mapping:
                    self.mapping[field_id]["matched_by"] = "name"
                    self.mapping[field_id]["openproject_id"] = existing_name_to_field.get(field_name, {}).get('id')
                    updated_count += 1

        if updated_count > 0:
            self.logger.info(f"Updated mapping for {updated_count} fields that already exist in OpenProject")
            fields_to_create = [field for field in self.mapping.values() if field["matched_by"] == "create"]

        self.logger.info(f"Found {len(fields_to_create)} custom fields to create")

        with ProgressTracker(
            description="Migrating custom fields",
            total=len(fields_to_create) + updated_count,
            log_title="Custom Fields Migration Results"
        ) as tracker:
            for i in range(updated_count):
                tracker.increment()

            for field in fields_to_create:
                field_name = field.get('openproject_name', field.get('jira_name', 'Unknown field'))
                tracker.update_description(f"Migrating field: {field_name[:20]}")

                if field_name in existing_names:
                    tracker.add_log_item(f"{field_name}: Skipped (already exists)")
                    skipped_count += 1

                    field_id = field.get('jira_id')
                    if field_id in self.mapping:
                        self.mapping[field_id]["matched_by"] = "name"
                        self.mapping[field_id]["openproject_id"] = existing_name_to_field.get(field_name, {}).get('id')

                    tracker.increment()
                    results.append({
                        'name': field_name,
                        'status': 'skipped',
                        'message': 'Already exists'
                    })
                    continue

                if field_name in previous_errors:
                    prev_error = previous_errors[field_name]
                    self.logger.info(f"Field '{field_name}' failed in previous run: {prev_error.get('message')}")

                    if field.get('openproject_type') == 'list' and (
                        'possible_values' not in field or
                        not field['possible_values'] or
                        not isinstance(field['possible_values'], list)
                    ):
                        self.logger.info(f"Adding default option to list field '{field_name}'")
                        field['possible_values'] = ["Default option"]
                        fixed_count += 1

                result = self.create_custom_field_via_rails(field)

                if result['status'] == 'success':
                    tracker.add_log_item(f"{field_name}: Created successfully")
                    success_count += 1

                    field_id = field.get('jira_id')
                    if field_id in self.mapping:
                        self.mapping[field_id]["matched_by"] = "created"
                        self.mapping[field_id]["openproject_id"] = result.get('id')

                    results.append({
                        'name': field_name,
                        'status': 'success',
                        'message': 'Created successfully',
                        'id': result.get('id')
                    })
                else:
                    if 'errors' in result and isinstance(result['errors'], list):
                        error_message = ', '.join(result['errors'])
                    elif 'validation_errors' in result:
                        error_message = str(result['validation_errors'])
                    else:
                        error_message = result.get('error', 'Unknown error')

                    tracker.add_log_item(f"{field_name}: Error - {error_message}")
                    error_count += 1
                    results.append({
                        'name': field_name,
                        'status': 'error',
                        'message': error_message
                    })

                tracker.increment()

        self._save_to_json(self.mapping, "custom_field_mapping.json")

        self.logger.info("\nCustom Fields Rails Migration Summary:")
        self.logger.info(f"Total fields processed: {len(fields_to_create) + updated_count}")
        self.logger.info(f"Successfully created: {success_count}")
        self.logger.info(f"Skipped (already exists): {skipped_count + updated_count}")
        self.logger.info(f"Fixed before migration: {fixed_count}")
        self.logger.info(f"Failed: {error_count}")

        results_file = os.path.join(self.data_dir, "custom_field_migration_results.json")
        with open(results_file, "w") as f:
            json.dump(results, f, indent=2)
        self.logger.info(f"Saved migration results to {results_file}")

        if error_count > 0:
            self.logger.error(f"{error_count} custom fields failed to migrate. Check {results_file} for details.")
            return False

        self.logger.success("Custom fields migration via Rails console completed successfully.")
        return True

    def migrate_custom_fields(self, direct_migration=False):
        """Migrate custom fields from Jira to OpenProject"""
        # Use the mapping analysis to decide whether to do direct migration or generate a Ruby script
        analysis = self.analyze_custom_field_mapping()

        if not analysis:
            self.logger.error("Analysis of custom field mapping failed. Cannot migrate.")
            return False

        self.logger.info(f"Starting custom field migration with {len(self.mapping)} fields in mapping")

        # Check if we have fields that need to be created
        fields_to_create = [f for f in self.mapping.values() if f["matched_by"] == "create"]

        if not fields_to_create:
            self.logger.info("No custom fields need to be created. All mapped or ignored.")
            return True

        self.logger.info(f"Found {len(fields_to_create)} custom fields to create")

        if direct_migration and self.rails_console:
            self.logger.info("Using direct Rails console migration method")
            return self.migrate_custom_fields_via_rails()

        # Use JSON-based approach
        return self.migrate_custom_fields_via_json(fields_to_create)

    def migrate_custom_fields_via_json(self, fields_to_migrate):
        """Migrate custom fields by creating a JSON file and processing it in a Ruby script.

        This is a more efficient approach than creating fields one by one via API calls.
        """
        self.logger.info(f"Migrating {len(fields_to_migrate)} custom fields using Ruby migration")

        # Convert the fields to the format expected by the Ruby script
        custom_fields_data = []
        for field in fields_to_migrate:
            if not field.get("jira_name"):
                self.logger.warning(f"Skipping field without name: {field}")
                continue

            field_data = {
                "name": field.get("jira_name"),
                "field_format": field.get("field_format", "string"),
                "is_required": field.get("is_required", False),
                "is_for_all": field.get("is_for_all", True),
                "type": field.get("type", "WorkPackageCustomField"),
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

        # Save the custom fields data to a JSON file
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(custom_fields_data, f, indent=2)
            json_file_path = f.name

        self.logger.info(f"Created JSON file with {len(custom_fields_data)} custom field definitions")

        # Get the Rails client
        rails_client = None
        if self.rails_console:
            rails_client = self.rails_console
        elif hasattr(self.op_client, 'rails_client') and self.op_client.rails_client:
            rails_client = self.op_client.rails_client
            self.logger.info("Using Rails client from OpenProject client")

        if not rails_client:
            self.logger.error("No Rails client available for execution")
            return False

        # First, transfer the JSON file to the container
        timestamp = int(time.time())
        container_json_path = f"/tmp/custom_fields_{timestamp}.json"
        if not rails_client.transfer_file_to_container(json_file_path, container_json_path):
            self.logger.error("Failed to transfer JSON file to container")
            return False

        # Define a results file path in the container
        results_file_path = f"/tmp/custom_fields_results_{timestamp}.json"
        log_file_path = f"/tmp/custom_fields_log_{timestamp}.txt"

        # Create the header script with Python f-string variables
        header_script = f"""
# Configuration variables
container_json_path = '{container_json_path}'
results_file_path = '{results_file_path}'
log_file_path = '{log_file_path}'
"""

        # Main Ruby script without f-strings
        main_script = """
begin
  # Open a log file for detailed output
  log_file = File.open(log_file_path, 'w')

  # Function to write to log file instead of console
  def log(message, log_file)
    log_file.puts(message)
    log_file.flush
  end

  # Read the JSON file
  require 'json'
  log("Reading custom field definitions from #{container_json_path}...", log_file)
  custom_fields_data = JSON.parse(File.read(container_json_path))
  puts "Processing #{custom_fields_data.count} custom fields..."

  # Process each custom field
  results = []
  success_count = 0
  existing_count = 0
  error_count = 0

  custom_fields_data.each_with_index do |field_data, index|
    begin
      field_name = field_data['name']
      log("Processing field: #{field_name}", log_file)

      # Show minimal progress indicator every 50 fields or at the end
      if (index + 1) % 50 == 0 || index + 1 == custom_fields_data.count
        puts "  Progress: #{index + 1}/#{custom_fields_data.count} fields"
      end

      # Check if the field already exists
      cf = CustomField.find_by(name: field_name)
      if cf
        log("  Custom field '#{field_name}' already exists with ID #{cf.id}", log_file)
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
      log("  Creating custom field '#{field_name}'...", log_file)
      cf = CustomField.new(
        name: field_name,
        field_format: field_data['field_format'],
        is_required: field_data['is_required'],
        is_for_all: field_data['is_for_all'],
        type: field_data['type']
      )

      # Set possible values for list fields
      if cf.field_format == 'list' && field_data['possible_values']
        cf.possible_values = field_data['possible_values']
        if cf.possible_values.nil? || cf.possible_values.empty?
          cf.possible_values = ['Default option']
        end
      end

      # Save the custom field
      if cf.save
        log("  Successfully created custom field '#{field_name}' with ID #{cf.id}", log_file)

        # Make it available for all work package types if is_for_all is true
        if cf.is_for_all?
          log("  Activating for all work package types...", log_file)
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
        log("  Error creating custom field '#{field_name}': #{error_message}", log_file)
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
      log("  Error processing field '#{error_field}': #{e.message}", log_file)
      log("  #{e.backtrace.join("\n")}", log_file)
      results << {
        'name' => error_field,
        'status' => 'error',
        'message' => e.message,
        'jira_id' => field_data['jira_id']
      }
      error_count += 1
    end
  end

  # Write the results to a JSON file
  File.write(results_file_path, results.to_json)
  log("Results written to #{results_file_path}", log_file)

  # Print just a summary to console
  puts "\nMigration complete: Created: #{success_count}, Existing: #{existing_count}, Errors: #{error_count}"
  puts "  Log: #{log_file_path}"

  # Close log file
  log_file.close
rescue => e
  puts "Error: #{e.message}"
  # Try to log the error to the file
  begin
    File.open(log_file_path, 'a') do |f|
      f.puts("FATAL ERROR: #{e.message}")
      f.puts(e.backtrace.join("\n"))
    end
  rescue
    # If logging fails, at least show the error in console
    puts e.backtrace.join("\n")
  end
end
"""

        # Combine the scripts
        ruby_command = header_script + main_script

        # Execute the Ruby command
        self.logger.info(f"Executing Ruby command to process fields from {container_json_path}")
        result = rails_client.execute(ruby_command)

        # Also retrieve the log file from the container
        local_log_path = os.path.join(self.output_dir, f"custom_fields_log_{timestamp}.txt")
        rails_client.transfer_file_from_container(log_file_path, local_log_path)
        self.logger.info(f"Detailed logs saved to {local_log_path}")

        # Clean up local temporary file
        try:
            os.unlink(json_file_path)
        except Exception as e:
            self.logger.debug(f"Error cleaning up temporary JSON file: {e}")

        # Check if execution was successful
        if result.get('status') != 'success':
            self.logger.error(f"Error executing Ruby command: {result.get('error', 'Unknown error')}")
            return False

        # Get the results file from the container
        local_results_path = os.path.join(self.output_dir, f"custom_fields_results_{timestamp}.json")

        if not rails_client.transfer_file_from_container(results_file_path, local_results_path):
            self.logger.warning("Could not retrieve results file from container")
            # Try to parse results from stdout
            output = result.get('output', '')
            # Check if we can find the summary line in the output
            if "Migration complete" in output:
                self.logger.info(f"Results detected in output: {output}")
                return True
            else:
                self.logger.error("Could not extract results from output")
                return False

        # Read the results file
        try:
            with open(local_results_path, 'r') as f:
                results = json.load(f)
            self.logger.info(f"Retrieved results file with {len(results)} entries")

            # Process the results
            success_count = 0
            error_count = 0
            created_fields = []

            for result_entry in results:
                status = result_entry.get('status')
                jira_id = result_entry.get('jira_id')
                name = result_entry.get('name')

                if status == 'created' or status == 'existing':
                    if jira_id and jira_id in self.mapping:
                        self.mapping[jira_id]["matched_by"] = status
                        self.mapping[jira_id]["openproject_id"] = result_entry.get('id')
                        if status == 'created':
                            success_count += 1
                            created_fields.append(name)
                elif status == 'error':
                    error_count += 1
                    self.logger.error(f"Error creating field '{name}': {result_entry.get('message')}")

            # Save the updated mapping
            self._save_to_json(self.mapping, "custom_field_mapping.json")

            # Print summary
            self.logger.info("\nCustom Fields JSON Migration Summary:")
            self.logger.info(f"Total fields processed: {len(results)}")
            self.logger.info(f"Successfully created: {success_count}")
            self.logger.info(f"Failed: {error_count}")

            if error_count > 0:
                self.logger.warning(f"{error_count} custom fields failed to create")

            if success_count > 0:
                self.logger.success(f"Successfully created {success_count} custom fields")
                return True
            elif error_count == 0:
                self.logger.info("No new custom fields created, but no errors either")
                return True
            else:
                self.logger.error("Failed to create any custom fields")
                return False

        except Exception as e:
            self.logger.error(f"Error processing results: {e}")
            return False

    def analyze_custom_field_mapping(self) -> Dict[str, Any]:
        """
        Analyze the custom field mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        # Check if analysis was recently performed to avoid duplicate logging
        if hasattr(self, '_last_analysis_time') and time.time() - self._last_analysis_time < 5:
            self.logger.debug("Skipping duplicate analysis - was just performed")
            # Make sure the analysis has a status field
            if hasattr(self, 'analysis') and self.analysis:
                if 'status' not in self.analysis:
                    self.analysis['status'] = 'success'
                return self.analysis
            return {'status': 'error', 'message': 'No analysis data available'}

        # Track when analysis was last performed
        self._last_analysis_time = time.time()

        if not self.mapping:
            mapping_path = os.path.join(self.data_dir, "custom_field_mapping.json")
            if os.path.exists(mapping_path):
                with open(mapping_path, "r") as f:
                    self.mapping = json.load(f)
            else:
                self.logger.error(
                    "No custom field mapping found. Run create_custom_field_mapping() first."
                )
                return {'status': 'error', 'message': 'No custom field mapping found'}

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
        self.logger.info(f"- Matched by name: {matched_fields} ({analysis['match_percentage']:.1f}%)")
        self.logger.info(f"- Created directly via Rails: {created_fields} ({analysis['created_percentage']:.1f}%)")
        self.logger.info(f"- Still need creation: {to_create_fields} ({analysis['needs_creation_percentage']:.1f}%)")

        if to_create_fields > 0:
            self.logger.warning(f"Action required: {to_create_fields} custom fields need manual creation or script execution.")
            self.logger.warning("Details saved to custom_field_analysis.json")

        return analysis

    # Helper methods
    def _load_from_json(self, filename: str, default: Any = None) -> Any:
        """
        Load data from a JSON file in the data directory.

        Args:
            filename: Name of the JSON file
            default: Default value to return if file doesn't exist

        Returns:
            Loaded JSON data or default value
        """
        filepath = os.path.join(self.data_dir, filename)
        if os.path.exists(filepath):
            try:
                with open(filepath, "r") as f:
                    return json.load(f)
            except Exception as e:
                self.logger.warning(f"Failed to load {filepath}: {e}")
                return default
        return default

    def _save_to_json(self, data: Any, filename: str):
        """
        Save data to a JSON file in the data directory.

        Args:
            data: Data to save
            filename: Name of the file to save to
        """
        filepath = os.path.join(self.data_dir, filename)
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        self.logger.debug(f"Saved data to {filepath}")

    def update_mapping_file(self, force: bool = False) -> bool:
        """
        Update the mapping file after manual creation of custom fields.

        Args:
            force: If True, force extraction even if data exists

        Returns:
            True if mapping was updated, False otherwise
        """
        self.extract_openproject_custom_fields(force=force)
        self.extract_jira_custom_fields(force=force)
        return self.create_custom_field_mapping(force=force) is not None

    def run(self, dry_run: bool = False, force: bool = False, mappings=None) -> Dict[str, Any]:
        """
        Run the custom field migration process.

        Args:
            dry_run: If True, don't actually create fields in OpenProject
            force: If True, force extraction of data even if it already exists
            mappings: Optional mappings object (not used in this migration)

        Returns:
            Dictionary with migration results
        """
        self.logger.info("Starting custom field migration", extra={"markup": True})

        try:
            # Extract data
            jira_fields = self.extract_jira_custom_fields(force=force)
            op_fields = self.extract_openproject_custom_fields(force=force)

            # Create mapping
            mapping = self.create_custom_field_mapping(force=force)

            # Migrate custom fields based on the direct migration flag
            if not dry_run and self.rails_console:
                self.logger.info("Migrating custom fields via Rails console", extra={"markup": True})
                success = self.migrate_custom_fields(direct_migration=True)
            elif not dry_run:
                self.logger.info("Generating JSON migration script (no Rails console available)", extra={"markup": True})
                success = self.migrate_custom_fields(direct_migration=False)
            else:
                self.logger.warning("Dry run mode - not creating custom fields", extra={"markup": True})
                success = True

            # Analyze results
            analysis = self.analyze_custom_field_mapping()

            status = "success" if success else "failed"

            return {
                "status": status,
                "jira_fields_count": len(jira_fields),
                "op_fields_count": len(op_fields),
                "mapped_fields_count": len(mapping),
                "success_count": analysis.get("matched_by_name", 0) + analysis.get("created_directly", 0),
                "failed_count": analysis.get("needs_manual_creation_or_script", 0),
                "total_count": len(jira_fields),
                "analysis": analysis
            }
        except Exception as e:
            self.logger.error(f"Error during custom field migration: {str(e)}", extra={"markup": True, "traceback": True})
            return {
                "status": "failed",
                "error": str(e),
                "success_count": 0,
                "failed_count": len(self.jira_custom_fields) if self.jira_custom_fields else 0,
                "total_count": len(self.jira_custom_fields) if self.jira_custom_fields else 0
            }
