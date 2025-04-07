"""
Custom field migration module for Jira to OpenProject migration.
Handles the migration of custom fields from Jira to OpenProject.
"""

import os
import sys
import json
import re
import argparse
import time
from datetime import datetime
from typing import Dict, List, Any, Optional, TYPE_CHECKING
from collections import deque
import threading
import queue
import subprocess

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src import config
from src.display import ProgressTracker, process_with_progress, console
from src.migrations.base_migration import BaseMigration

# Import RailsConsolePexpect to handle direct Rails console execution
from src.clients.openproject_rails_client import OpenProjectRailsClient

# Get logger from config
logger = config.logger

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

        logger.info(f"Loaded {len(self.jira_custom_fields)} Jira custom fields")
        logger.info(f"Loaded {len(self.op_custom_fields)} OpenProject custom fields")
        logger.info(f"Loaded {len(self.mapping)} custom field mappings")
        logger.info(f"Loaded analysis with {len(self.analysis)} keys: {list(self.analysis.keys())}")

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
            logger.info(
                "Jira custom fields data already exists, skipping extraction (use --force to override)"
            )
            with open(custom_fields_file, "r") as f:
                self.jira_custom_fields = json.load(f)
            return self.jira_custom_fields

        logger.info("Extracting custom fields from Jira...")

        if not self.jira_client.connect():
            logger.error("Failed to connect to Jira")
            return []

        try:
            self.jira_custom_fields = self.jira_client.jira.fields()

            self.jira_custom_fields = [
                field for field in self.jira_custom_fields if field.get("custom", False)
            ]

            logger.info(
                f"Extracted {len(self.jira_custom_fields)} custom fields from Jira"
            )

            logger.info("Retrieving options for select list custom fields...")

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
                        meta_data = self.jira_client.get_field_metadata(field_id)

                        allowed_values = []

                        if "allowedValues" in meta_data:
                            for value in meta_data["allowedValues"]:
                                if "value" in value:
                                    allowed_values.append(value["value"])
                                elif "name" in value:
                                    allowed_values.append(value["name"])

                        if allowed_values:
                            logger.info(f"Found {len(allowed_values)} options for field '{field.get('name')}' (ID: {field_id})")
                            enhanced_field["allowed_values"] = allowed_values
                    except Exception as e:
                        logger.warning(f"Could not retrieve options for field '{field.get('name')}': {str(e)}")

                enhanced_fields.append(enhanced_field)

            self.jira_custom_fields = enhanced_fields

            with open(custom_fields_file, "w") as f:
                json.dump(self.jira_custom_fields, f, indent=2)
            logger.info(f"Saved data to {custom_fields_file}")

            return self.jira_custom_fields
        except Exception as e:
            logger.error(f"Failed to extract custom fields from Jira: {str(e)}")
            return []

    def extract_openproject_custom_fields(self, force=False) -> List[Dict[str, Any]]:
        """
        Extract custom field information from OpenProject.

        Args:
            force: If True, extract again even if data already exists

        Returns:
            List of OpenProject custom fields
        """
        custom_fields_file = os.path.join(
            self.data_dir, "openproject_custom_fields.json"
        )

        if os.path.exists(custom_fields_file) and not force:
            logger.info(
                "OpenProject custom fields data already exists, skipping extraction (use --force to override)"
            )
            with open(custom_fields_file, "r") as f:
                self.op_custom_fields = json.load(f)
            return self.op_custom_fields

        logger.info("Extracting custom fields from OpenProject...")

        try:
            fields = self.op_client.get_custom_fields()

            if fields and isinstance(fields, list):
                self.op_custom_fields = fields
                logger.success(f"Successfully retrieved {len(fields)} custom fields from OpenProject")
            else:
                logger.error("Could not retrieve custom fields or received unexpected data format.")
                raise RuntimeError("Failed to retrieve custom fields from OpenProject.")

            logger.info(
                f"Extracted {len(self.op_custom_fields)} custom fields from OpenProject"
            )

            with open(custom_fields_file, "w") as f:
                json.dump(self.op_custom_fields, f, indent=2)
            logger.info(f"Saved data to {custom_fields_file}")

            return self.op_custom_fields

        except Exception as e:
            logger.error(f"Failed to extract custom fields from OpenProject: {str(e)}")
            raise RuntimeError(f"Failed to extract custom fields: {str(e)}")

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
        logger.info("Creating custom field mapping...")

        logger.info("Extracting custom fields from Jira...")
        self.extract_jira_custom_fields(force=force)

        logger.info("Extracting custom fields from OpenProject...")
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
        logger.info(f"Saved custom field mapping to {mapping_file}")

        total_fields = len(mapping)
        matched_fields = sum(
            1 for field in mapping.values() if field["matched_by"] == "name"
        )
        created_fields = sum(
            1 for field in mapping.values() if field["matched_by"] == "create"
        )

        logger.info(f"Custom field mapping created for {total_fields} fields")
        logger.info(f"- Matched by name: {matched_fields}")
        logger.info(f"- Need to create: {created_fields}")

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
                logger.warning(f"Custom field '{field_name}' is a list type but has no options. Adding a default option.")

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
          cf = CustomField.new(
            name: "{field_name}",
            field_format: "{field_type}",
            is_required: {'true' if field_data.get('is_required', False) else 'false'},
            is_for_all: {'true' if field_data.get('is_for_all', True) else 'false'},
            type: "{field_data.get('openproject_field_type', 'WorkPackageCustomField')}",
            default_value: {default_value}
          )

          if cf.field_format == 'list'
            cf.possible_values = {possible_values_ruby}
            if cf.possible_values.nil? || cf.possible_values.empty?
              cf.possible_values = ["Default option"]
            end
          end

          if cf.field_format == 'bool'
            cf.default_value = {'true' if field_data.get('default_value', False) else 'false'}
          end

          if cf.is_for_all?
            cf.projects = Project.all
          end

          if cf.save
            {{id: cf.id, name: cf.name, status: "success"}}
          else
            {{status: "error", errors: cf.errors.full_messages, validation_errors: cf.errors.messages}}
          end
        rescue => e
          {{status: "error", message: e.message}}
        end
        """

        result = self.rails_console.execute(command)

        if result['status'] == 'error':
            error_info = result.get('error', 'Unknown error')
            if 'errors' in result:
                error_info = f"Validation errors: {result['errors']}"
            elif 'validation_errors' in result:
                error_info = f"Field validation failed: {result['validation_errors']}"

            logger.error(f"Error creating custom field '{field_name}': {error_info}")

            if isinstance(result.get('errors'), list) and any("Possible values" in err for err in result['errors']):
                logger.error(f"Field '{field_name}' requires possible values for list type fields")
        elif result['status'] == 'success':
            logger.info(f"Created custom field '{field_name}' successfully")

        return result

    def migrate_custom_fields_via_rails(self, window: int = 0, pane: int = 0) -> bool:
        """
        Migrate custom fields to OpenProject using the Rails console.

        Args:
            window: The tmux window to use (default: 0)
            pane: The tmux pane to use (default: 0)

        Returns:
            bool: True if migration was successful, False otherwise
        """
        logger.info("Starting direct custom fields migration via Rails console")

        existing_fields = self.op_client.get_custom_fields()
        if existing_fields is None:
            logger.error("Failed to retrieve existing custom fields from OpenProject. Defaulting to empty list.")
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
                        logger.info(f"Found {len(previous_errors)} previously failed custom fields")
            except Exception as e:
                logger.warning(f"Could not load previous migration results: {str(e)}")

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
            logger.info(f"Updated mapping for {updated_count} fields that already exist in OpenProject")
            fields_to_create = [field for field in self.mapping.values() if field["matched_by"] == "create"]

        logger.info(f"Found {len(fields_to_create)} custom fields to create")

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
                    logger.info(f"Field '{field_name}' failed in previous run: {prev_error.get('message')}")

                    if field.get('openproject_type') == 'list' and (
                        'possible_values' not in field or
                        not field['possible_values'] or
                        not isinstance(field['possible_values'], list)
                    ):
                        logger.info(f"Adding default option to list field '{field_name}'")
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

        logger.info("\nCustom Fields Rails Migration Summary:")
        logger.info(f"Total fields processed: {len(fields_to_create) + updated_count}")
        logger.info(f"Successfully created: {success_count}")
        logger.info(f"Skipped (already exists): {skipped_count + updated_count}")
        logger.info(f"Fixed before migration: {fixed_count}")
        logger.info(f"Failed: {error_count}")

        results_file = os.path.join(self.data_dir, "custom_field_migration_results.json")
        with open(results_file, "w") as f:
            json.dump(results, f, indent=2)
        logger.info(f"Saved migration results to {results_file}")

        if error_count > 0:
            logger.error(f"{error_count} custom fields failed to migrate. Check {results_file} for details.")
            return False

        logger.success("Custom fields migration via Rails console completed successfully.")
        return True

    def generate_ruby_script(self, fields_to_create: List[Dict[str, Any]]) -> str:
        """
        Generate a Ruby script to create custom fields in OpenProject.

        Args:
            fields_to_create: List of custom fields to generate script for

        Returns:
            Path to the generated Ruby script
        """
        logger.info(f"Generating Ruby script for {len(fields_to_create)} custom fields...")

        script_lines = [
            "#!/usr/bin/env ruby",
            "# Generated by Jira to OpenProject migration script",
            "require 'json'",
            "",
            "puts 'Starting custom field creation...'",
            "",
        ]

        # Generate commands for each custom field
        for field in fields_to_create:
            field_name = field["openproject_name"]
            field_type = field["openproject_type"]
            possible_values = field.get("possible_values", [])

            # Properly escape field name and values for Ruby
            field_name_escaped = field_name.replace("'", "\\'")
            possible_values_ruby = "[]"

            if field_type == "list":
                # Ensure there's at least one default option if none exist
                if not possible_values or not isinstance(possible_values, list):
                    possible_values = ["Default option"]

                escaped_values = [
                    v.replace("'", "\\'") if isinstance(v, str) else str(v)
                    for v in possible_values
                ]
                values_str = ", ".join([f"'{v}'" for v in escaped_values])
                possible_values_ruby = f"[{values_str}]"

            # Generate the Ruby code for creating the custom field
            command = f"""
            begin
              cf = CustomField.find_by(name: '{field_name_escaped}')
              if cf
                puts "Custom field '{field_name_escaped}' already exists. Skipping..."
              else
                puts "Creating custom field: '{field_name_escaped}'..."
                cf = CustomField.new(
                  name: '{field_name_escaped}',
                  field_format: '{field_type}',
                  is_required: {'true' if field.get('is_required', False) else 'false'},
                  is_for_all: {'true' if field.get('is_for_all', True) else 'false'},
                  type: "{field.get('openproject_field_type', 'WorkPackageCustomField')}",
                )

                # Set possible values for list type fields
                if cf.field_format == 'list'
                  cf.possible_values = {possible_values_ruby}
                  if cf.possible_values.nil? || cf.possible_values.empty?
                    cf.possible_values = ['Default option'] # Ensure default option exists
                  end
                end

                if cf.save
                  puts "  Successfully created custom field '{field_name_escaped}' with ID: #{cf.id}"

                  # Make it available for all work package types
                  if cf.is_for_all?
                    puts "  Activating for all work package types..."
                    Type.all.each do |type|
                      type.custom_fields << cf unless type.custom_fields.include?(cf)
                      type.save!
                    end
                  end
                else
                  puts "  Error creating custom field '{field_name_escaped}': #{cf.errors.full_messages.join(', ')}"
                end
              end
            rescue => e
              puts "  An unexpected error occurred while processing '{field_name_escaped}': #{e.message}"
            end
            """
            script_lines.append(command)
            script_lines.append("") # Add a blank line for readability

        script_lines.append("puts 'Custom field creation script finished.'")

        # Save the script to a file
        script_path = os.path.join(
            self.output_dir,
            f"create_custom_fields_{datetime.now().strftime('%Y%m%d_%H%M%S')}.rb",
        )
        os.makedirs(self.output_dir, exist_ok=True)
        with open(script_path, "w") as f:
            f.write("\n".join(script_lines))

        logger.info(f"Generated Ruby script: {script_path}")
        return script_path

    def migrate_custom_fields(self, direct_migration: bool = False) -> Dict[str, Any]:
        """
        Migrate custom fields from Jira to OpenProject.

        Depending on the `direct_migration` flag, this either generates a Ruby script
        or attempts to create fields directly via the Rails console.

        Args:
            direct_migration (bool): If True, attempt direct migration via Rails console.
                                     Otherwise, generate a Ruby script.

        Returns:
            Updated mapping with migration results or paths to generated scripts
        """
        logger.info("Starting custom field migration...")

        # Make sure we have the mapping
        if not self.mapping:
            self.create_custom_field_mapping()

        # Get fields that need to be created
        fields_to_create = [
            field for field in self.mapping.values() if field["matched_by"] == "create"
        ]

        if not fields_to_create:
            logger.info("No new custom fields need to be created.")
            return self.mapping

        if direct_migration:
            logger.info("Using direct migration via Rails console.")
            if self.rails_console:
                # Attempt direct migration
                success = self.migrate_custom_fields_via_rails()
                if success:
                    logger.success("Direct custom field migration completed.")
                else:
                    logger.error("Direct custom field migration encountered errors.")
                    logger.warning("Consider generating a Ruby script for manual review.")
            else:
                logger.error("Direct migration requested but Rails console client not initialized.")
                logger.warning("Falling back to generating Ruby script.")
                # Generate the script as a fallback
                script_path = self.generate_ruby_script(fields_to_create)
                logger.warning(f"Run this script in the OpenProject Rails console: {script_path}")

        else:
            logger.info("Generating Ruby script for manual migration.")
            # Generate the Ruby script
            script_path = self.generate_ruby_script(fields_to_create)
            logger.warning(f"Run this script in the OpenProject Rails console: {script_path}")

        # Update analysis after potential changes
        self.analyze_custom_field_mapping()
        return self.mapping

    def analyze_custom_field_mapping(self) -> Dict[str, Any]:
        """
        Analyze the custom field mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        if not self.mapping:
            mapping_path = os.path.join(self.data_dir, "custom_field_mapping.json")
            if os.path.exists(mapping_path):
                with open(mapping_path, "r") as f:
                    self.mapping = json.load(f)
            else:
                logger.error(
                    "No custom field mapping found. Run create_custom_field_mapping() first."
                )
                return {}

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
        logger.info("\nCustom Field Mapping Analysis:")
        logger.info(f"Total Jira custom fields processed: {total_fields}")
        logger.info(f"- Matched by name: {matched_fields} ({analysis['match_percentage']:.1f}%)")
        logger.info(f"- Created directly via Rails: {created_fields} ({analysis['created_percentage']:.1f}%)")
        logger.info(f"- Still need creation: {to_create_fields} ({analysis['needs_creation_percentage']:.1f}%)")

        if to_create_fields > 0:
            logger.warning(f"Action required: {to_create_fields} custom fields need manual creation or script execution.")
            logger.warning("Details saved to custom_field_analysis.json")

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
                logger.warning(f"Failed to load {filepath}: {e}")
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
        logger.debug(f"Saved data to {filepath}")

# Standalone execution for testing or isolated runs
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate custom fields from Jira to OpenProject.")
    parser.add_argument("--config", default="config.yaml", help="Path to the configuration file.")
    parser.add_argument("--direct", action="store_true", help="Attempt direct migration via Rails console (requires tmux, pexpect).")
    parser.add_argument("--force", action="store_true", help="Force re-extraction of data.")
    parser.add_argument("--log-level", default="INFO", help="Set logging level (DEBUG, INFO, WARNING, ERROR)")

    args = parser.parse_args()

    # Load configuration
    config.load_config(args.config)
    config.setup_logging(level=args.log_level)

    # Initialize clients
    jira_client = JiraClient()
    op_client = OpenProjectClient()
    rails_console = None
    if args.direct:
        # Initialize Rails console client only if needed
        rails_console = OpenProjectRailsClient()

    # Initialize migration class
    migration = CustomFieldMigration(
        jira_client=jira_client,
        op_client=op_client,
        rails_console=rails_console
    )

    # Execute the migration steps
    migration.create_custom_field_mapping(force=args.force)
    migration.migrate_custom_fields(direct_migration=args.direct)
    migration.analyze_custom_field_mapping()

    logger.info("Custom field migration process finished.")
