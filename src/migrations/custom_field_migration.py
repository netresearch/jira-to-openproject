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
from src.display import ProgressTracker, process_with_progress, console, print_info
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
        data_dir: str | None = None,
        dry_run: bool = True,
        force: bool = False,
    ) -> None:
        """
        Initialize the custom field migration process.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client
            rails_console: Initialized OpenProjectRailsClient instance (optional)
            data_dir: Directory for storing migration data
            dry_run: If True, simulate migration without making changes
            force: If True, force re-extraction of data
        """
        # Call the BaseMigration constructor
        super().__init__(jira_client, op_client, data_dir, dry_run, force)
        self.jira_custom_fields: List[Dict] = []
        self.op_custom_fields: List[Dict] = []
        self.mapping: Dict[str, Dict] = {}
        self.analysis: Dict = {}
        self.rails_console = rails_console  # Store the provided client

        # Load existing data
        self._load_data()

    def _load_data(self) -> None:
        """Load existing data from JSON files."""
        self.jira_custom_fields = self._load_from_json("jira_custom_fields.json", [])
        self.op_custom_fields = self._load_from_json("op_custom_fields.json", [])
        self.mapping = self._load_from_json("custom_field_mapping.json", {})

        # Make sure self.analysis is always a dictionary, never None
        analysis_data = self._load_from_json("custom_field_analysis.json", {})
        self.analysis = {} if analysis_data is None else analysis_data

        # Log the loaded data with safe length checks
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

        # Connect to Jira
        if not self.jira_client.connect():
            logger.error("Failed to connect to Jira")
            return []

        # Get custom fields from Jira
        try:
            self.jira_custom_fields = self.jira_client.jira.fields()

            # Filter out non-custom fields
            self.jira_custom_fields = [
                field for field in self.jira_custom_fields if field.get("custom", False)
            ]

            logger.info(
                f"Extracted {len(self.jira_custom_fields)} custom fields from Jira"
            )

            # Get options for select list fields
            logger.info("Retrieving options for select list custom fields...")

            # Enhanced fields with options
            enhanced_fields = []

            for field in self.jira_custom_fields:
                enhanced_field = field.copy()
                field_id = field.get("id")
                field_type = field.get("schema", {}).get("type", "")
                field_custom_type = field.get("schema", {}).get("custom", "")

                # Check if this is a select list field
                is_select_list = (
                    field_type == "option" or
                    "select" in field_custom_type.lower() or
                    "option" in field_custom_type.lower() or
                    "radio" in field_custom_type.lower()
                )

                if is_select_list:
                    try:
                        # Get allowed values for this field
                        # For custom fields, we need to query the meta data
                        # from the field configuration scheme
                        meta_data = self.jira_client.get_field_metadata(field_id)

                        # Extract allowed values from meta data
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

            # Replace original fields with enhanced ones
            self.jira_custom_fields = enhanced_fields

            # Save to file
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

        # Use the OpenProjectClient's shared implementation
        try:
            # Get custom fields using the shared implementation
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

            # Save to file
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

        # Direct mappings for common types
        op_format_map = {
            # Text field types
            "string": "text",
            "text": "text",
            "textarea": "text",
            "url": "text",
            "readonlyfield": "text",
            "gh-epic-link": "text",
            "gl-epic-link": "text",
            # Single select types
            "option": "list",
            "select": "list",
            "radiobuttons": "list",
            # Multi-select types
            "array": "list",  # May need special handling based on sub-type
            "multiselect": "list",
            "checkboxes": "list",
            # Date types
            "date": "date",
            "datetime": "date",
            # Number types
            "number": "int",
            "integer": "int",
            "float": "float",
            # User types
            "user": "user",
            "users": "user",
            # Project types
            "project": "list",
            # Boolean types
            "boolean": "bool",
            # Special types
            "cascadingselect": "list",  # Not ideal but closest
            "multiversion": "list",
            "version": "list",
            "multiuserpicker": "user",
            "userpicker": "user",
            "labels": "list",  # Not ideal but closest
            "priority": "list",
            "status": "list",
            # Default type if not matched
            "default": "text",
        }

        # Handle Jira custom types that need special mapping
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

        # Special case handling for array types
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

        # Default mapping based on Jira field type or fallback to default
        return op_format_map.get(jira_type, op_format_map.get("default", "text"))

    def create_custom_field_mapping(self, force: bool = False) -> Dict[str, Any]:
        """
        Create a mapping between Jira and OpenProject custom fields.

        Since creating custom fields in OpenProject is not fully supported through the API,
        this method now focuses on creating a mapping file for later use.

        Args:
            force: If True, force extraction of data even if it already exists

        Returns:
            Dictionary mapping Jira custom field IDs to OpenProject field information
        """
        logger.info("Creating custom field mapping...")

        # Extract custom fields from both systems
        logger.info("Extracting custom fields from Jira...")
        self.extract_jira_custom_fields(force=force)

        logger.info("Extracting custom fields from OpenProject...")
        self.extract_openproject_custom_fields(force=force)

        # Create a mapping between Jira and OpenProject custom fields
        mapping = {}

        # Create a lookup dictionary for OpenProject custom fields by name
        op_fields_by_name = {
            field.get("name", "").lower(): field for field in self.op_custom_fields
        }

        # Find fields that need to be created
        fields_to_create = []

        def process_field(jira_field, context):
            jira_id = jira_field.get("id")
            jira_name = jira_field.get("name", "")
            jira_name_lower = jira_name.lower()

            # Try to find a matching custom field in OpenProject by name
            op_field = op_fields_by_name.get(jira_name_lower, None)

            if op_field:
                # If a matching field is found, create a mapping with "matched" status
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
                return None  # No field info to return for the log
            else:
                # If no matching field is found, create a mapping with "create" status
                # and determine the appropriate OpenProject field format
                op_format = self.map_jira_field_to_openproject_format(jira_field)

                mapping_entry = {
                    "jira_id": jira_id,
                    "jira_name": jira_name,
                    "jira_type": jira_field.get("schema", {}).get("type", ""),
                    "jira_custom_type": jira_field.get("schema", {}).get("custom", ""),
                    "openproject_id": None,
                    "openproject_name": jira_name,  # Use the same name
                    "openproject_type": op_format,
                    "matched_by": "create",
                }

                # If this is a list type field, copy the allowed values from Jira if available
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

                # Return field info for the log
                return field_info

        # Process all Jira custom fields with progress tracking
        with ProgressTracker(
            description="Creating custom field mapping",
            total=len(self.jira_custom_fields),
            log_title="Custom Fields to Create"
        ) as tracker:
            for jira_field in self.jira_custom_fields:
                jira_name = jira_field.get("name", "")
                tracker.update_description(f"Mapping custom field: {jira_name[:20]}")

                # Process the field
                field_info = process_field(jira_field, {})

                # Add to log if field needs creation
                if field_info:
                    tracker.add_log_item(field_info)

                # Increment progress
                tracker.increment()

        # Save mapping to file
        mapping_file = os.path.join(self.data_dir, "custom_field_mapping.json")
        with open(mapping_file, "w") as f:
            json.dump(mapping, f, indent=2)
        logger.info(f"Saved custom field mapping to {mapping_file}")

        # Analyze the mapping
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
        # Build the Ruby command to create the custom field
        field_type = field_data.get('openproject_type', 'string')
        field_name = field_data.get('openproject_name', field_data.get('jira_name', 'Unnamed Field'))

        # Properly escape field name for Ruby
        field_name = field_name.replace('"', '\\"')

        # Handle possible values for list fields
        possible_values_ruby = "[]"
        if field_type == 'list':
            # For list type fields, we must have at least one option
            if 'possible_values' in field_data and field_data['possible_values'] and isinstance(field_data['possible_values'], list):
                values = field_data['possible_values']
                # Escape any quotes in values
                escaped_values = [v.replace('"', '\\"') if isinstance(v, str) else v for v in values]
                values_str = ', '.join([f'"{v}"' for v in escaped_values])
                possible_values_ruby = f"[{values_str}]"
            else:
                # Ensure there's at least one default option to avoid validation error
                possible_values_ruby = '["Default option"]'
                logger.warning(f"Custom field '{field_name}' is a list type but has no options. Adding a default option.")

        # Handle default values
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

          # Set possible values for list fields
          if cf.field_format == 'list'
            cf.possible_values = {possible_values_ruby}
            # Double check there's at least one option to avoid validation error
            if cf.possible_values.nil? || cf.possible_values.empty?
              cf.possible_values = ["Default option"]
            end
          end

          # For boolean fields, set the default
          if cf.field_format == 'bool'
            cf.default_value = {'true' if field_data.get('default_value', False) else 'false'}
          end

          # Associate with all projects
          if cf.is_for_all?
            cf.projects = Project.all
          end

          if cf.save
            {{id: cf.id, name: cf.name, status: "success"}}
          else
            # More detailed error reporting
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

            # Special handling for possible values error
            if isinstance(result.get('errors'), list) and any("Possible values" in err for err in result['errors']):
                logger.error(f"Field '{field_name}' requires possible values for list type fields")
        elif result['status'] == 'success':
            logger.info(f"Created custom field '{field_name}' successfully")

        return result

    def migrate_custom_fields_via_rails(self, window: int = 0, pane: int = 0) -> bool:
        """
        Migrate custom fields directly via the Rails console.

        Args:
            window: tmux window number (default: 0)
            pane: tmux pane number (default: 0)

        Returns:
            bool: True if migration was successful, False otherwise
        """
        logger.info("Starting direct custom fields migration via Rails console")

        # Check existing custom fields
        existing_fields = self.op_client.get_custom_fields()
        # Ensure existing_fields is never None to prevent TypeError when iterating
        if existing_fields is None:
            logger.error("Failed to retrieve existing custom fields from OpenProject. Defaulting to empty list.")
            existing_fields = []
        existing_names = [field.get('name') for field in existing_fields]

        # Prepare to track results
        results = []
        success_count = 0
        error_count = 0
        skipped_count = 0
        fixed_count = 0  # Track fields we had to fix

        # Load previous migration results if they exist
        previous_results_file = os.path.join(self.data_dir, "custom_field_migration_results.json")
        previous_errors = {}
        if os.path.exists(previous_results_file):
            try:
                with open(previous_results_file, "r") as f:
                    previous_results = json.load(f)
                    # Create a map of previously failed fields
                    for result in previous_results:
                        if result.get('status') == 'error':
                            previous_errors[result.get('name')] = result

                    if previous_errors:
                        logger.info(f"Found {len(previous_errors)} previously failed custom fields")
            except Exception as e:
                logger.warning(f"Could not load previous migration results: {str(e)}")

        # Migrate fields that need to be created
        fields_to_create = [field for field in self.mapping.values() if field["matched_by"] == "create"]

        logger.info(f"Found {len(fields_to_create)} custom fields to create")

        with ProgressTracker(
            description="Migrating custom fields",
            total=len(fields_to_create),
            log_title="Custom Fields Migration Results"
        ) as tracker:
            for field in fields_to_create:
                field_name = field.get('openproject_name', field.get('jira_name', 'Unknown field'))
                tracker.update_description(f"Migrating field: {field_name[:20]}")

                # Skip if the field already exists
                if field_name in existing_names:
                    tracker.add_log_item(f"{field_name}: Skipped (already exists)")
                    skipped_count += 1
                    tracker.increment()
                    results.append({
                        'name': field_name,
                        'status': 'skipped',
                        'message': 'Already exists'
                    })
                    continue

                # Check if this field failed before and needs fixing
                if field_name in previous_errors:
                    prev_error = previous_errors[field_name]
                    logger.info(f"Field '{field_name}' failed in previous run: {prev_error.get('message')}")

                    # Fix specific errors we know about
                    if field.get('openproject_type') == 'list' and (
                        'possible_values' not in field or
                        not field['possible_values'] or
                        not isinstance(field['possible_values'], list)
                    ):
                        logger.info(f"Adding default option to list field '{field_name}'")
                        field['possible_values'] = ["Default option"]
                        fixed_count += 1

                # Create the field
                result = self.create_custom_field_via_rails(field)

                if result['status'] == 'success':
                    tracker.add_log_item(f"{field_name}: Created successfully")
                    success_count += 1
                    results.append({
                        'name': field_name,
                        'status': 'success',
                        'message': 'Created successfully'
                    })
                else:
                    # Get detailed error information
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

        # Show summary
        logger.info("\nCustom Fields Rails Migration Summary:")
        logger.info(f"Total fields processed: {len(fields_to_create)}")
        logger.info(f"Successfully created: {success_count}")
        logger.info(f"Skipped (already exists): {skipped_count}")
        logger.info(f"Fixed before migration: {fixed_count}")
        logger.info(f"Failed: {error_count}")

        # Save results to file
        results_file = os.path.join(self.data_dir, "custom_field_migration_results.json")
        with open(results_file, "w") as f:
            json.dump(results, f, indent=2)
        logger.info(f"Saved migration results to {results_file}")

        # Review failed fields to provide additional guidance
        if error_count > 0:
            logger.info("\nReview of failed custom fields:")
            failed_fields = [r for r in results if r['status'] == 'error']
            list_field_errors = 0

            for failed in failed_fields:
                field_name = failed.get('name')
                error_msg = failed.get('message', '')

                if 'Possible values' in error_msg:
                    list_field_errors += 1

            if list_field_errors > 0:
                logger.warning(f"Found {list_field_errors} fields that failed due to missing possible values.")
                logger.warning("These fields need list options. Run with --force to retry with default options.")

        return error_count == 0

    def migrate_custom_fields(self, force: bool = False, direct_migration: bool = False) -> Dict[str, Any]:
        """
        Prepare for migrating custom fields from Jira to OpenProject.

        Since creating custom fields in OpenProject is not fully supported through the API,
        this method now focuses on saving mapping information and analyzing the mapping.
        Ruby scripts are only generated when explicitly requested with --generate-ruby.

        Args:
            force: If True, force extraction of data even if it already exists
            direct_migration: If True, execute the migration directly via Rails console

        Returns:
            Updated mapping with analysis information
        """
        logger.info("Starting custom field migration")

        if not self.mapping:
            self.create_custom_field_mapping(force=force)

        if direct_migration:
            self.migrate_custom_fields_via_rails()

        # Note: Ruby script generation is now handled separately and only when
        # explicitly requested with --generate-ruby flag

        # Save analysis information
        analysis = self.analyze_field_mapping(force=force)

        # Provide a summary of the custom field migration
        total_fields = len(self.mapping)
        needs_creation = sum(1 for field in self.mapping.values() if field["matched_by"] == "create")

        logger.info(f"Custom field migration complete - {total_fields} fields processed, {needs_creation} need creation")

        if self.dry_run:
            logger.info("DRY RUN: No custom fields were actually created in OpenProject")

        return analysis

    def analyze_field_mapping(self, force: bool = False) -> Dict[str, Any]:
        """
        Analyze the custom field mapping to identify potential issues.

        Args:
            force: If True, force extraction of data even if it already exists

        Returns:
            Dictionary with analysis results
        """
        if not self.mapping:
            self.create_custom_field_mapping(force=force)

        # Count fields by type
        jira_types = {}
        op_types = {}

        for field in self.mapping.values():
            jira_type = field.get("jira_type", "unknown")
            op_type = field.get("openproject_type", "unknown")

            jira_types[jira_type] = jira_types.get(jira_type, 0) + 1
            op_types[op_type] = op_types.get(op_type, 0) + 1

        # Create analysis dictionary
        analysis = {
            "total_fields": len(self.mapping),
            "matched_fields": sum(
                1
                for field in self.mapping.values()
                if field["matched_by"] == "name"
            ),
            "created_fields": sum(
                1
                for field in self.mapping.values()
                if field["matched_by"] == "create"
            ),
            "jira_types": jira_types,
            "openproject_types": op_types,
            "potential_issues": [],
        }

        # Save analysis to file
        analysis_file = os.path.join(
            self.data_dir, "custom_field_mapping_analysis.json"
        )
        with open(analysis_file, "w") as f:
            json.dump(analysis, f, indent=2)
        logger.info(f"Saved custom field mapping analysis to {analysis_file}")

        # Log analysis summary
        logger.info(f"Custom field mapping analysis complete")
        logger.info(f"Total custom fields: {analysis['total_fields']}")
        logger.info(
            f"Matched fields: {analysis['matched_fields']} ({analysis['matched_fields'] / analysis['total_fields'] * 100:.1f}% of total)"
        )
        logger.info(
            f"Fields to create: {analysis['created_fields']} ({analysis['created_fields'] / analysis['total_fields'] * 100:.1f}% of total)"
        )

        return analysis

    def generate_ruby_script(self, force: bool = False) -> str:
        """
        Generate a Ruby script for manually importing custom fields via Rails console.

        Args:
            force: If True, force extraction of data even if it already exists

        Returns:
            Path to the generated Ruby script
        """
        if not self.mapping:
            self.create_custom_field_mapping(force=force)

        # Create the Ruby script
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        script_file = os.path.join(
            self.output_dir, f"custom_fields_import_{timestamp}.rb"
        )

        with open(script_file, "w") as f:
            f.write("# OpenProject Custom Fields Import Script\n")
            f.write("# Generated by Jira to OpenProject Migration Tool\n")
            f.write(f"# Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

            f.write("# Usage:\n")
            f.write("# 1. Copy this file to the OpenProject server\n")
            f.write(
                "# 2. Run the Rails console: bundle exec rails console\n"
            )
            f.write("# 3. Load this script: load '/path/to/this/file.rb'\n\n")

            f.write(
                "# Start by getting all existing work package types (needed for associations)\n"
            )
            f.write("types = Type.all\n")
            f.write('puts "Found #{types.count} work package types"\n\n')

            f.write("# Create custom fields\n")
            f.write("custom_fields_to_create = [\n")

            # Add each custom field that needs to be created
            for field in self.mapping.values():
                if field["matched_by"] == "create":
                    jira_name = field["jira_name"].replace(
                        "'", "\\'"
                    )  # Escape single quotes
                    op_type = field["openproject_type"]

                    # Prepare field configuration based on type
                    config = "{"

                    if op_type == "list":
                        # For list types, we need to add options
                        config += "options: ['', 'Option 1', 'Option 2'], "

                    # Add name and field format
                    config += f"name: '{jira_name}', field_format: '{op_type}', is_required: false, is_filter: true"
                    config += "}"

                    f.write(f"  {config},\n")

            f.write("]\n\n")

            f.write("# Create the custom fields\n")
            f.write("custom_fields_created = []\n")
            f.write("custom_fields_to_create.each do |field_config|\n")
            f.write("  begin\n")
            f.write("    # Check if the field already exists by name\n")
            f.write(
                "    existing_field = WorkPackageCustomField.find_by(name: field_config[:name])\n"
            )
            f.write("    if existing_field\n")
            f.write(
                "      puts \"Field '#{field_config[:name]}' already exists with ID #{existing_field.id}\"\n"
            )
            f.write("      custom_fields_created << existing_field\n")
            f.write("    else\n")
            f.write("      # Create a new custom field\n")
            f.write("      field = WorkPackageCustomField.new(field_config)\n")
            f.write("      # Associate with all work package types\n")
            f.write("      field.types = types\n")
            f.write("      # Save the field\n")
            f.write("      if field.save\n")
            f.write(
                "        puts \"Created custom field '#{field_config[:name]}' with ID #{field.id}\"\n"
            )
            f.write("        custom_fields_created << field\n")
            f.write("      else\n")
            f.write(
                "        puts \"Failed to create custom field '#{field_config[:name]}': #{field.errors.full_messages.join(', ')}\"\n"
            )
            f.write("      end\n")
            f.write("    end\n")
            f.write("  rescue => e\n")
            f.write(
                "    puts \"Error creating custom field '#{field_config[:name]}': #{e.message}\"\n"
            )
            f.write("  end\n")
            f.write("end\n\n")

            f.write("# Summary\n")
            f.write('puts "\\nCustom Fields Import Summary"\n')
            f.write('puts "---------------------------"\n')
            f.write('puts "Total fields processed: #{custom_fields_to_create.count}"\n')
            f.write(
                'puts "Successfully created/found: #{custom_fields_created.count}"\n'
            )
            f.write('puts "\\nCustom fields import complete.\\n"\n')

        logger.info(f"Generated Ruby script for custom fields import: {script_file}")
        return script_file


def run_custom_field_migration(
    jira_client: Optional[JiraClient] = None,
    op_client: Optional[OpenProjectClient] = None,
    rails_console: Optional['OpenProjectRailsClient'] = None,
    dry_run: bool = False,
    force: bool = False,
    direct_migration: bool = False
) -> None:
    """
    Run the custom field migration process, injecting necessary clients.

    Args:
        jira_client: Initialized Jira client.
        op_client: Initialized OpenProject client.
        rails_console: Initialized OpenProject Rails client (optional).
        dry_run: Simulate migration without making changes.
        force: Force re-extraction of data.
        direct_migration: Use direct Rails console execution.
    """
    # Instantiate the migration class, passing all required arguments
    migration = CustomFieldMigration(
        jira_client=jira_client,
        op_client=op_client,
        rails_console=rails_console,
        dry_run=dry_run,
        force=force
    )
    # Call the migration method
    migration.migrate_custom_fields(force=force, direct_migration=direct_migration)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Jira Custom Field Migration")
    parser.add_argument("--dry-run", action="store_true", help="Simulate migration")
    parser.add_argument("--force", action="store_true", help="Force data re-extraction")
    parser.add_argument(
        "--direct-migration",
        action="store_true",
        help="Execute migration directly via Rails console",
    )
    args = parser.parse_args()

    # Initialize clients for standalone execution (won't have rails_console here easily)
    # In standalone mode, direct migration relies on the class internal handling if rails_console is None
    logger.info("Running custom field migration in standalone mode.")
    jira_client_standalone = JiraClient()
    op_client_standalone = OpenProjectClient() # Assumes OpenProjectClient can handle rails_client=None

    run_custom_field_migration(
        jira_client=jira_client_standalone,
        op_client=op_client_standalone,
        rails_console=None, # Standalone typically won't have pre-initialized rails
        dry_run=args.dry_run,
        force=args.force,
        direct_migration=args.direct_migration
    )
