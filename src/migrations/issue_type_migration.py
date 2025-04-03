#!/usr/bin/env python3
"""
Issue type migration module for Jira to OpenProject migration.
Handles the migration of issue types from Jira to OpenProject work package types.
"""

import os
import sys
import json
import re
import argparse
import time
import subprocess
from datetime import datetime
from typing import Dict, List, Any, Optional, Union, TYPE_CHECKING

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.models.mapping import JiraToOPMapping
from src import config
from src.display import ProgressTracker, process_with_progress, console

from src.clients.openproject_rails_client import OpenProjectRailsClient

# Get logger from config
logger = config.logger


class IssueTypeMigration:
    """
    Handles the migration of issue types from Jira to work package types in OpenProject.

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
        Initialize the issue type migration process.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client
            rails_console: Initialized OpenProjectRailsClient instance (optional)
            data_dir: Directory for storing migration data
            dry_run: If True, simulate migration without making changes
            force: If True, force re-extraction of data
        """
        self.jira_client = jira_client or JiraClient()
        self.op_client = op_client or OpenProjectClient()
        self.jira_issue_types: List[Dict] = []
        self.op_work_package_types: List[Dict] = []
        self.issue_type_mapping: Dict[str, Dict] = {}
        self.issue_type_id_mapping: Dict[str, int] = {}
        self.dry_run = dry_run
        self.rails_console = rails_console

        # Use the centralized config for var directories
        self.data_dir = data_dir or config.get_path("data")
        self.output_dir = config.get_path("output")

        # Define default mappings for common issue types
        self.default_mappings = {
            "Bug": {"name": "Bug", "color": "#D35400"},
            "Task": {"name": "Task", "color": "#1A67A3"},
            "User Story": {"name": "User Story", "color": "#27AE60"},
            "Epic": {"name": "Epic", "color": "#9B59B6"},
            "Feature": {"name": "Feature", "color": "#2788C8"},
            "Story": {"name": "User Story", "color": "#27AE60"},
            "Improvement": {"name": "Feature", "color": "#2788C8"},
            "New Feature": {"name": "Feature", "color": "#2788C8"},
            "Change Request": {"name": "Task", "color": "#1A67A3"},
            "Sub-task": {"name": "Task", "color": "#1A67A3"},
            "Technical Task": {"name": "Task", "color": "#1A67A3"},
            "Support Request": {"name": "Support", "color": "#4A148C"},
            "Documentation": {"name": "Documentation", "color": "#1F618D"},
            "Milestone": {"name": "Milestone", "color": "#E73E97"},
            "Phase": {"name": "Phase", "color": "#5C3566"},
            "Requirement": {"name": "Requirement", "color": "#8F4B2D"},
        }

        # Console instance for rich output
        self.console = console

        # Load existing data if available
        self._load_data()

    def _load_data(self) -> None:
        """Load existing data from JSON files."""
        self.jira_issue_types = self._load_from_json("jira_issue_types.json", [])
        self.op_work_package_types = self._load_from_json("op_work_package_types.json", [])
        self.issue_type_mapping = self._load_from_json("issue_type_mapping.json", {})
        self.issue_type_id_mapping = self._load_from_json("issue_type_id_mapping.json", {})
        logger.info(f"Loaded {len(self.jira_issue_types)=} Jira issue types")
        logger.info(f"Loaded {len(self.op_work_package_types)=} OpenProject work package types")
        logger.info(f"Loaded {len(self.issue_type_mapping)=} issue type mappings")
        logger.info(f"Loaded {len(self.issue_type_id_mapping)=} issue type ID mappings")

    def extract_jira_issue_types(self, force=False) -> List[Dict[str, Any]]:
        """
        Extract issue types from Jira.

        Args:
            force: If True, extract again even if data already exists

        Returns:
            List of Jira issue type dictionaries
        """
        # Check if we already have the data
        issue_types_file = os.path.join(self.data_dir, "jira_issue_types.json")

        if os.path.exists(issue_types_file) and not force:
            logger.info(
                "Jira issue types data already exists, skipping extraction (use --force to override)"
            )
            with open(issue_types_file, "r") as f:
                self.jira_issue_types = json.load(f)
            return self.jira_issue_types

        logger.info("Extracting issue types from Jira...")

        # Connect to Jira
        if not self.jira_client.connect():
            logger.error("Failed to connect to Jira")
            return []

        try:
            # Get all issue types
            issue_types = self.jira_client.get_issue_types()

            # Log the number of issue types found
            logger.info(f"Extracted {len(issue_types)} issue types from Jira")

            # Save issue types to file for later reference
            self.jira_issue_types = issue_types
            self._save_to_json(issue_types, "jira_issue_types.json")

            return issue_types
        except Exception as e:
            logger.error(f"Failed to get issue types from Jira: {str(e)}")
            return []

    def extract_openproject_work_package_types(self, force=False) -> List[Dict[str, Any]]:
        """
        Extract work package types from OpenProject.

        Args:
            force: If True, extract again even if data already exists

        Returns:
            List of OpenProject work package type dictionaries
        """
        # Check if we already have the data
        work_package_types_file = os.path.join(
            self.data_dir, "openproject_work_package_types.json"
        )

        if os.path.exists(work_package_types_file) and not force:
            logger.info(
                "OpenProject work package types data already exists, skipping extraction (use --force to override)"
            )
            with open(work_package_types_file, "r") as f:
                self.op_work_package_types = json.load(f)
            return self.op_work_package_types

        logger.info("Extracting work package types from OpenProject...")

        # Get work package types from OpenProject
        try:
            self.op_work_package_types = self.op_client.get_work_package_types()
        except Exception as e:
            logger.warning(
                f"Failed to get work package types from OpenProject: {str(e)}"
            )
            logger.warning("Using an empty list of work package types for OpenProject")
            self.op_work_package_types = []

        # Log the number of work package types found
        logger.info(
            f"Extracted {len(self.op_work_package_types)} work package types from OpenProject"
        )

        # Save work package types to file for later reference
        self._save_to_json(self.op_work_package_types, "openproject_work_package_types.json")

        return self.op_work_package_types

    def create_issue_type_mapping(self, force=False) -> Dict[str, Any]:
        """
        Create a mapping between Jira issue types and OpenProject work package types.

        This method creates a mapping based on their names and using default mappings
        for common issue types.

        Args:
            force: If True, create the mapping again even if it already exists

        Returns:
            Dictionary mapping Jira issue types to OpenProject work package types
        """
        # Check if we already have the mapping
        mapping_file = os.path.join(self.data_dir, "issue_type_mapping_template.json")
        if os.path.exists(mapping_file) and not force:
            logger.info(
                "Issue type mapping already exists, loading from file (use --force to recreate)"
            )
            with open(mapping_file, "r") as f:
                self.issue_type_mapping = json.load(f)
            return self.issue_type_mapping

        logger.info("Creating issue type mapping...")

        # Make sure we have issue types from both systems
        if not self.jira_issue_types:
            self.extract_jira_issue_types()

        if not self.op_work_package_types:
            self.extract_openproject_work_package_types()

        # Create lookup dictionary for OpenProject work package types
        op_types_by_name = {
            type_data.get("name", "").lower(): type_data
            for type_data in self.op_work_package_types
        }

        mapping = {}
        for jira_type in self.jira_issue_types:
            jira_type_id = jira_type.get("id")
            jira_type_name = jira_type.get("name", "")
            jira_type_description = jira_type.get("description", "")

            # Initialize mapping entry
            mapping[jira_type_name] = {
                "jira_id": jira_type_id,
                "jira_name": jira_type_name,
                "jira_description": jira_type_description,
                "openproject_id": None,
                "openproject_name": None,
                "color": None,
                "is_milestone": False,
                "matched_by": "none",
            }

            # First, try to find an exact match by name
            op_type = op_types_by_name.get(jira_type_name.lower())
            if op_type:
                mapping[jira_type_name].update({
                    "openproject_id": op_type.get("id"),
                    "openproject_name": op_type.get("name"),
                    "color": op_type.get("color", "#1A67A3"),
                    "is_milestone": jira_type_name.lower() == "milestone",
                    "matched_by": "exact_match",
                })
                continue

            # If no exact match, try using the default mappings
            default_mapping = self.default_mappings.get(jira_type_name)
            if default_mapping:
                default_name = default_mapping.get("name")
                op_type = op_types_by_name.get(default_name.lower())

                if op_type:
                    # Default mapping exists in OpenProject
                    mapping[jira_type_name].update({
                        "openproject_id": op_type.get("id"),
                        "openproject_name": op_type.get("name"),
                        "color": default_mapping.get("color", "#1A67A3"),
                        "is_milestone": default_name.lower() == "milestone",
                        "matched_by": "default_mapping",
                    })
                else:
                    # Default mapping doesn't exist in OpenProject yet
                    mapping[jira_type_name].update({
                        "openproject_id": None,
                        "openproject_name": default_name,
                        "color": default_mapping.get("color", "#1A67A3"),
                        "is_milestone": default_name.lower() == "milestone",
                        "matched_by": "default_mapping_to_create",
                    })
                continue

            # If we still don't have a mapping, use the same name as Jira
            mapping[jira_type_name].update({
                "openproject_id": None,
                "openproject_name": jira_type_name,
                "color": "#1A67A3",  # Default blue color
                "is_milestone": jira_type_name.lower() == "milestone",
                "matched_by": "same_name",
            })

        # Save mapping to file
        self.issue_type_mapping = mapping
        self._save_to_json(mapping, "issue_type_mapping_template.json")

        # Log statistics
        total_types = len(mapping)
        matched_types = sum(
            1 for type_data in mapping.values() if type_data["openproject_id"] is not None
        )
        to_create_types = sum(
            1 for type_data in mapping.values() if type_data["openproject_id"] is None
        )
        match_percentage = (matched_types / total_types) * 100 if total_types > 0 else 0

        logger.info(f"Issue type mapping created for {total_types} types")
        logger.info(
            f"Successfully matched {matched_types} types ({match_percentage:.1f}%)"
        )
        logger.info(f"Need to create {to_create_types} new work package types in OpenProject")

        return mapping

    def prepare_work_package_type_for_ruby(
        self, type_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Prepare a work package type for Ruby script generation.

        Args:
            type_data: Dictionary with work package type data

        Returns:
            Dictionary with prepared type data for Ruby script
        """
        name = type_data.get("openproject_name")
        color = type_data.get("color")
        is_milestone = type_data.get("is_milestone", False)
        is_default = name.lower() == "task"  # Make Task the default type

        logger.info(f"Preparing work package type for Ruby script: {name}")

        return {
            "name": name,
            "color": color,
            "is_milestone": is_milestone,
            "is_default": is_default
        }

    def connect_to_rails_console(self, window: int = 0, pane: int = 0) -> bool:
        """
        Connect to the Rails console via tmux.

        Args:
            window: tmux window number (default: 0)
            pane: tmux pane number (default: 0)

        Returns:
            bool: True if connection was successful, False otherwise
        """

        if self.rails_console:
            logger.info("Already connected to Rails console.")
            return True

        logger.info("Connecting to Rails console...")

        try:
            with ProgressTracker(description=f"Connecting to tmux session", total=1) as tracker:
                self.rails_console = OpenProjectRailsClient(
                    window=window,
                    pane=pane,
                    debug=False  # Set to True for debugging
                )

                # Test the connection by running a simple command
                result = self.rails_console.execute("Rails.env")

                if result['status'] == 'success':
                    logger.info(f"Connected to Rails console successfully (Rails environment: {result['output']})")
                    tracker.increment()
                    return True
                else:
                    logger.error(f"Failed to connect to Rails console: {result.get('error', 'Unknown error')}")
                    return False
        except Exception as e:
            logger.error(f"Error connecting to Rails console: {str(e)}")
            import traceback
            logger.debug(traceback.format_exc())
            return False

    def check_existing_work_package_types(self) -> List[Dict]:
        """
        Check existing work package types in OpenProject by directly parsing
        the JSON output from a file created via Rails console, similar to custom_field_migration.

        Returns:
            List of existing work package types
        """
        logger.info("Checking existing work package types in OpenProject via Rails...")


        # Define the path for the temporary file inside the container
        temp_file_path = "/tmp/op_work_package_types.json"

        # Use a command that outputs JSON to a file
        command = f"""
        begin
          # Get the types as a JSON string
          types = Type.all.map do |t|
            {{
              id: t.id,
              name: t.name,
              color: t.color&.hexcode,
              position: t.position,
              is_default: t.is_default,
              is_milestone: t.is_milestone
            }}
          end

          # Write the types as JSON to a temporary file
          File.write("{temp_file_path}", types.to_json)
          puts "===JSON_WRITE_SUCCESS===" # Marker for success
          nil # Ensure last expression is nil to avoid unwanted output
        rescue => e
          # Ensure error message is printed clearly for capture
          puts "RAILS_EXEC_ERROR: #{{e.message}} \\n #{{e.backtrace.join("\n")}}"
          nil # Ensure last expression is nil
        end
        """

        try:
            # Execute the command to write the file using the existing rails console client
            logger.info(f"Executing Rails command to write work package types to {temp_file_path}...")
            write_result = self.rails_console.execute(command)

            # Check for explicit error marker in the output
            if write_result.get('status') == 'success' and write_result.get('output') and "RAILS_EXEC_ERROR:" in write_result['output']:
                logger.error(f"Rails command reported an error during execution: {write_result['output']}")
                return []
            # Check if the command execution itself reported failure (e.g., timeout)
            elif write_result.get('status') != 'success':
                error_msg = write_result.get('error', 'Unknown error executing Rails command for file write')
                logger.error(f"Failed to execute Rails command to write JSON file: {error_msg}")
                return []

            # If command execution was successful, assume file was written (or ls will fail)
            logger.info(f"Rails command executed successfully. Checking existence of {temp_file_path}...")

            # Add a small delay just in case of filesystem lag
            time.sleep(0.5)

            # Use the specific openproject config section and rely on config for defaults/errors
            container_name = config.openproject_config.get('container')
            op_server = config.openproject_config.get('server') # Get the OpenProject server hostname

            if not op_server:
                logger.error("OpenProject server hostname is not configured (J2O_OPENPROJECT_SERVER). Cannot run remote docker commands.")
                return []

            # Define base command parts for SSH execution
            ssh_base_cmd = ["ssh", op_server, "--"]
            docker_base_cmd = ["docker", "exec", container_name]

            # Check if the file exists on the remote host via ssh + docker exec
            ls_command = ssh_base_cmd + docker_base_cmd + ["ls", temp_file_path]
            logger.debug(f"Executing command: {' '.join(ls_command)}")
            try:
                ls_result = subprocess.run(ls_command, capture_output=True, text=True, check=False)
                if ls_result.returncode != 0:
                    # Log the specific stderr output from the failed ls command
                    error_details = ls_result.stderr.strip()
                    logger.error(f"File {temp_file_path} check failed (exit code {ls_result.returncode}). ls stderr: {error_details}; stdout: {ls_result.stdout}")
                    return []
                logger.info(f"File {temp_file_path} confirmed to exist in container.")
            except subprocess.SubprocessError as e:
                logger.error(f"Error running docker exec ls command: {str(e)}")
                if 'console' in globals():
                    console.print_exception(show_locals=True)
                return []

            # Read the file content on the remote host via ssh + docker exec
            logger.info(f"Reading {temp_file_path} using ssh + docker exec...")
            cat_command = ssh_base_cmd + docker_base_cmd + ["cat", temp_file_path]
            logger.debug(f"Executing command: {' '.join(cat_command)}") # Log the cat command
            read_result = subprocess.run(cat_command, capture_output=True, text=True, check=False)

            if read_result.returncode != 0:
                logger.error(f"Failed to read {temp_file_path} via docker exec: {read_result.stderr}")
                return []

            json_content = read_result.stdout.strip()
            logger.debug(f"Content read from {temp_file_path}:\\n{json_content}")

            # Parse the JSON
            try:
                types = json.loads(json_content)
                logger.info(f"Successfully parsed {len(types)} work package types from file")
                return types
            except json.JSONDecodeError as e:
                logger.error(f"Could not parse work package types JSON read from file: {e}")
                logger.debug(f"Invalid JSON content: {json_content}")
                return []

        except subprocess.SubprocessError as e:
            logger.error(f"Error running docker exec command: {str(e)}")
            if 'console' in globals():
                console.print_exception(show_locals=True)
            return []
        except Exception as e:
            logger.error(f"Unexpected error during work package type retrieval: {str(e)}")
            if 'console' in globals():
                console.print_exception(show_locals=True)
            return []
        finally:
            # Attempt to remove the temporary file on the remote host via ssh + docker exec
            try:
                # Ensure container_name exists before trying to use it (already checked op_server)
                if 'container_name' in locals() and container_name:
                    logger.debug(f"Attempting final removal of remote temporary file {temp_file_path}...")
                    rm_command = ssh_base_cmd + docker_base_cmd + ["rm", "-f", temp_file_path]
                    logger.debug(f"Executing final rm command: {' '.join(rm_command)}")
                    subprocess.run(rm_command, check=False, capture_output=True, timeout=10) # Added timeout
                else:
                    logger.debug(f"Skipping final removal of temporary file {temp_file_path} as container_name was not defined.")
            except Exception as final_e: # Use a different variable name!
                try:
                    error_type = type(final_e)
                    error_message = str(final_e)
                except Exception as str_err:
                    error_type = "Unknown"
                    error_message = f"Failed to convert finally exception to string: {str_err}"
                logger.warning(f"Failed during final removal of {temp_file_path} (Type: {error_type}): {error_message}")

    def create_work_package_type_via_rails(self, type_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a work package type in OpenProject via Rails console.

        Args:
            type_data: Work package type definition

        Returns:
            dict: Result of the operation
        """
        # Build the Ruby command to create the work package type
        type_name = type_data.get('openproject_name', type_data.get('jira_name', 'Unnamed Type'))
        type_color = type_data.get('color', '#0086B2')  # Default to blue
        position = 1  # Default position

        # Properly escape type name for Ruby
        type_name = type_name.replace('"', '\\"')

        # Handle optional attributes
        is_default = str(type_data.get('is_default', False)).lower()
        is_milestone = str(type_data.get('is_milestone', False)).lower()

        command = f"""
        begin
          type = Type.new(
            name: "{type_name}",
            color: Color.new(hexcode: "{type_color}"),
            position: {position},
            is_default: {is_default},
            is_milestone: {is_milestone}
          )

          if type.save
            {{id: type.id, name: type.name, status: "success"}}
          else
            {{status: "error", errors: type.errors.full_messages}}
          end
        rescue => e
          {{status: "error", message: e.message}}
        end
        """

        result = self.rails_console.execute(command)

        if result['status'] == 'error':
            logger.error(f"Error creating work package type '{type_name}': {result.get('error', 'Unknown error')}")
        elif result['status'] == 'success':
            logger.info(f"Created work package type '{type_name}' successfully")

        return result

    def migrate_issue_types_via_rails(self, window: int = 0, pane: int = 0) -> bool:
        """
        Migrate issue types directly via the Rails console.

        Args:
            window: tmux window number (default: 0)
            pane: tmux pane number (default: 0)

        Returns:
            bool: True if migration was successful, False otherwise
        """
        # Make sure we have the mapping
        if not self.issue_type_mapping:
            self.create_issue_type_mapping()

        # Connect to Rails console
        if not self.connect_to_rails_console(window, pane):
            return False

        # Check existing work package types
        existing_types = self.check_existing_work_package_types()
        existing_names = [type.get('name') for type in existing_types]

        # Prepare to track results
        results = []
        success_count = 0
        error_count = 0
        skipped_count = 0

        # Migrate types that need to be created
        types_to_create = {
            name: type_info for name, type_info in self.issue_type_mapping.items()
            if type_info.get("openproject_id") is None
        }

        logger.info(f"Found {len(types_to_create)} work package types to create via Rails")

        with ProgressTracker(
            description="Migrating work package types via Rails",
            total=len(types_to_create),
            log_title="Work Package Types Rails Migration Results"
        ) as tracker:
            for type_name, type_data in types_to_create.items():
                op_type_name = type_data.get('openproject_name', type_name)
                tracker.update_description(f"Migrating type: {op_type_name[:20]}...")

                # Skip if the type already exists (based on names loaded earlier)
                if op_type_name in existing_names:
                    tracker.add_log_item(f"{op_type_name}: Skipped (already exists)")
                    skipped_count += 1
                    tracker.increment()
                    results.append({
                        'name': op_type_name,
                        'status': 'skipped',
                        'message': 'Already exists'
                    })
                    # Attempt to find the existing ID and update the mapping
                    existing_id = next((t['id'] for t in existing_types if t.get('name') == op_type_name), None)
                    if existing_id:
                         # Find the original Jira ID associated with this op_type_name
                         jira_id = type_data.get('jira_id')
                         if jira_id:
                             self.issue_type_mapping[jira_id]['openproject_id'] = existing_id
                             logger.debug(f"Updated mapping for existing type '{op_type_name}' with ID {existing_id}")
                         else:
                             # This case might happen if the mapping structure changes, log a warning
                             logger.warning(f"Could not find Jira ID for existing type '{op_type_name}' to update mapping.")
                    continue

                # Create the type
                result = self.create_work_package_type_via_rails(type_data)

                if result['status'] == 'success':
                    tracker.add_log_item(f"{op_type_name}: Created successfully")
                    success_count += 1
                    results.append({
                        'name': op_type_name,
                        'status': 'success',
                        'message': 'Created successfully'
                    })

                    # Update the mapping with the new ID
                    if 'id' in result:
                        type_data['openproject_id'] = result['id']
                else:
                    error_message = result.get('error', 'Unknown error')
                    tracker.add_log_item(f"{op_type_name}: Error - {error_message}")
                    error_count += 1
                    results.append({
                        'name': op_type_name,
                        'status': 'error',
                        'message': error_message
                    })

                tracker.increment()

        # Save the updated mapping to file
        self._save_to_json(self.issue_type_mapping, "issue_type_mapping.json")

        # Create the final issue type mapping format needed by the work package migration
        final_mapping = {}
        for type_name, mapping in self.issue_type_mapping.items():
            jira_id = mapping["jira_id"]
            op_id = mapping["openproject_id"]

            if op_id:
                final_mapping[jira_id] = op_id

        # Save the final mapping to a JSON file
        self._save_to_json(final_mapping, "issue_type_id_mapping.json")

        # Show summary
        logger.info("\nWork Package Types Migration Summary:")
        logger.info(f"Total types processed: {len(types_to_create)}")
        logger.info(f"Successfully created: {success_count}")
        logger.info(f"Skipped (already exists): {skipped_count}")
        logger.info(f"Failed: {error_count}")

        # Save results to file
        results_file = os.path.join(self.data_dir, "issue_type_migration_results.json")
        with open(results_file, "w") as f:
            json.dump(results, f, indent=2)
        logger.info(f"Saved migration results to {results_file}")

        return error_count == 0

    def migrate_issue_types(self) -> Dict[str, Any]:
        """
        Prepare for migrating issue types from Jira to OpenProject.

        This method focuses on saving mapping information and analyzing the mapping.
        Ruby scripts are only generated when explicitly requested with --generate-ruby.

        Returns:
            Updated mapping with analysis information
        """
        logger.info("Starting issue type migration preparation...")

        # Make sure we have issue types from both systems
        if not self.jira_issue_types:
            self.extract_jira_issue_types()

        if not self.op_work_package_types:
            self.extract_openproject_work_package_types()

        # Create an initial mapping
        if not self.issue_type_mapping:
            self.create_issue_type_mapping()

        # Get unique OpenProject type names to create
        op_types_to_create = {}
        for type_name, mapping in self.issue_type_mapping.items():
            if mapping["openproject_id"] is None:
                op_type_name = mapping["openproject_name"]
                if op_type_name not in op_types_to_create:
                    op_types_to_create[op_type_name] = mapping

        # Just log how many types need to be created
        if op_types_to_create:
            logger.info(f"Need to create {len(op_types_to_create)} work package types in OpenProject via Rails console")
        else:
            logger.info("No new work package types need to be created in OpenProject")

        # Create the final issue type mapping format needed by the work package migration
        final_mapping = {}
        for type_name, mapping in self.issue_type_mapping.items():
            jira_id = mapping["jira_id"]
            op_id = mapping["openproject_id"]

            if op_id:
                final_mapping[jira_id] = op_id

        # Save the final mapping to a JSON file
        self._save_to_json(final_mapping, "issue_type_id_mapping.json")

        # Also save a more detailed mapping file
        self._save_to_json(self.issue_type_mapping, "issue_type_mapping.json")

        # Log statistics
        total_types = len(self.issue_type_mapping)
        mapped_types = sum(
            1 for mapping in self.issue_type_mapping.values() if mapping["openproject_id"] is not None
        )
        types_to_create = total_types - mapped_types

        logger.info(f"Issue type migration preparation completed")
        logger.info(f"Total Jira issue types: {total_types}")
        logger.info(f"Already mapped to OpenProject: {mapped_types}")
        logger.info(f"Need to create via Rails console: {types_to_create}")

        return self.issue_type_mapping

    def analyze_issue_type_mapping(self) -> Dict[str, Any]:
        """
        Analyze the issue type mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        logger.info("Analyzing issue type mapping...")

        if not self.issue_type_mapping:
            try:
                with open(os.path.join(self.data_dir, "issue_type_mapping.json"), "r") as f:
                    self.issue_type_mapping = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load issue type mapping: {str(e)}")
                return {"status": "error", "message": str(e)}

        total_types = len(self.issue_type_mapping)
        if total_types == 0:
            return {
                "status": "warning",
                "message": "No issue types have been mapped yet",
                "issue_types_count": 0,
                "potential_issues": [],
            }

        # Count mappings by match type
        match_types = {}
        for type_name, mapping in self.issue_type_mapping.items():
            match_type = mapping.get("matched_by", "none")
            match_types[match_type] = match_types.get(match_type, 0) + 1

        # Look for potential issues
        potential_issues = []

        # Check for unmapped types
        unmapped_types = []
        for type_name, mapping in self.issue_type_mapping.items():
            if mapping.get("openproject_id") is None:
                unmapped_types.append(type_name)

        if unmapped_types:
            potential_issues.append(
                {
                    "issue": "unmapped_types",
                    "description": f"{len(unmapped_types)} issue types need to be created in OpenProject via Rails console",
                    "affected_items": unmapped_types,
                    "count": len(unmapped_types),
                }
            )

        # Prepare analysis results
        return {
            "status": "success",
            "issue_types_count": total_types,
            "mappings_by_match_type": match_types,
            "potential_issues": potential_issues,
        }

    def generate_ruby_script(self, types_to_create: Dict[str, Dict[str, Any]]) -> str:
        """
        Generate a Ruby script for manually importing work package types via Rails console.

        This function is only called when explicitly requested with --generate-ruby.

        Args:
            types_to_create: Dictionary of work package types to create

        Returns:
            Path to the generated Ruby script
        """
        # Create the Ruby script
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        script_file = os.path.join(
            self.output_dir, f"work_package_types_import_{timestamp}.rb"
        )

        with open(script_file, "w") as f:
            f.write("# OpenProject Work Package Types Import Script\n")
            f.write("# Generated by Jira to OpenProject Migration Tool\n")
            f.write(f"# Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

            f.write("# Usage:\n")
            f.write("# 1. Copy this file to the OpenProject server\n")
            f.write("# 2. Run the Rails console: bundle exec rails console\n")
            f.write("# 3. Load this script: load '/path/to/this/file.rb'\n\n")

            # Initialize configurations
            f.write("# Initialize configurations\n")
            f.write("puts \"Starting work package types import...\"\n\n")

            # First, define colors with their hexcodes for reference
            f.write("# Get or create colors\n")
            f.write("def find_or_create_color(hexcode)\n")
            f.write("  # Remove # if present and normalize to lowercase\n")
            f.write("  hexcode = hexcode.gsub('#', '').downcase\n")
            f.write("  # Try to find existing color\n")
            f.write("  color = Color.find_by(hexcode: hexcode)\n")
            f.write("  return color if color\n\n")
            f.write("  # Create a new color with a generated name if it doesn't exist\n")
            f.write("  name = \"Color #{hexcode}\"\n")
            f.write("  color = Color.new(name: name, hexcode: hexcode)\n")
            f.write("  if color.save\n")
            f.write("    puts \"Created new color: #{name} (##{hexcode})\"\n")
            f.write("    return color\n")
            f.write("  else\n")
            f.write("    puts \"Failed to create color ##{hexcode}: #{color.errors.full_messages.join(', ')}\"\n")
            f.write("    return nil\n")
            f.write("  end\n")
            f.write("end\n\n")

            # Prepare color objects for each type
            f.write("# Prepare colors for each type\n")
            f.write("colors = {}\n")

            # Add each unique color
            unique_colors = set()
            for type_data in types_to_create.values():
                color_hex = type_data["color"]
                unique_colors.add(color_hex)

            for color_hex in unique_colors:
                # Remove # and convert to lowercase for consistency
                normalized_hex = color_hex.replace('#', '').lower()
                f.write(f"colors['{normalized_hex}'] = find_or_create_color('{color_hex}')\n")

            f.write("\n# Define work package types to create\n")
            f.write("types_to_create = [\n")

            # Add each work package type that needs to be created
            for type_name, type_data in types_to_create.items():
                name = type_data["openproject_name"].replace("'", "\\'")  # Escape single quotes
                color_hex = type_data["color"].replace('#', '').lower()  # Normalize color hex
                is_milestone = "true" if type_data["is_milestone"] else "false"
                is_default = "true" if type_data["openproject_name"].lower() == "task" else "false"

                f.write(f"  {{ name: '{name}', color: colors['{color_hex}'], is_milestone: {is_milestone}, is_default: {is_default} }},\n")

            f.write("]\n\n")

            f.write("# Create the work package types\n")
            f.write("types_created = []\n")
            f.write("types_to_create.each do |type_config|\n")
            f.write("  begin\n")
            f.write("    # Skip if color is nil\n")
            f.write("    if type_config[:color].nil?\n")
            f.write("      puts \"Skipping '#{type_config[:name]}': Color not found or could not be created\"\n")
            f.write("      next\n")
            f.write("    end\n\n")
            f.write("    # Check if the type already exists by name\n")
            f.write("    existing_type = Type.find_by(name: type_config[:name])\n")
            f.write("    if existing_type\n")
            f.write("      puts \"Type '#{type_config[:name]}' already exists with ID #{existing_type.id}\"\n")
            f.write("      types_created << existing_type\n")
            f.write("    else\n")
            f.write("      # Create a new work package type\n")
            f.write("      type = Type.new(\n")
            f.write("        name: type_config[:name],\n")
            f.write("        color: type_config[:color],\n")
            f.write("        is_milestone: type_config[:is_milestone],\n")
            f.write("        is_default: type_config[:is_default]\n")
            f.write("      )\n")
            f.write("      # Save the type\n")
            f.write("      if type.save\n")
            f.write("        puts \"Created work package type '#{type_config[:name]}' with ID #{type.id}\"\n")
            f.write("        types_created << type\n")
            f.write("      else\n")
            f.write("        puts \"Failed to create work package type '#{type_config[:name]}': #{type.errors.full_messages.join(', ')}\"\n")
            f.write("      end\n")
            f.write("    end\n")
            f.write("  rescue => e\n")
            f.write("    puts \"Error creating work package type '#{type_config[:name]}': #{e.message}\"\n")
            f.write("  end\n")
            f.write("end\n\n")

            f.write("# Summary\n")
            f.write('puts "\\nWork Package Types Import Summary"\n')
            f.write('puts "--------------------------------"\n')
            f.write('puts "Total types processed: #{types_to_create.count}"\n')
            f.write('puts "Successfully created/found: #{types_created.count}"\n')
            f.write('puts "\\nWork package types import complete.\\n"\n')

            # Add instructions for manually updating the mapping file
            f.write("\n# IMPORTANT: After running this script, note down the IDs of created types\n")
            f.write("# and update the issue_type_mapping.json file with these IDs\n")
            f.write("# You can find the IDs in the output above or by running:\n")
            f.write("# types_created.each { |t| puts \"#{t.name}: #{t.id}\" }\n\n")
            f.write("# Run this command to see the IDs\n")
            f.write('puts "\\nWork Package Type IDs (for updating mapping file):"\n')
            f.write("types_created.each { |t| puts \"#{t.name}: #{t.id}\" }\n")

        logger.info(f"Generated Ruby script for work package types import: {script_file}")
        logger.info(f"IMPORTANT: You must run this script in the OpenProject Rails console to create the types")
        logger.info(f"Then update the issue_type_mapping.json file with the IDs of the created types")

        return script_file

    def _save_to_json(self, data: Any, filename: str):
        """
        Save data to a JSON file in the data directory.

        Args:
            data: The data to save
            filename: The name of the file to save to
        """
        filepath = os.path.join(self.data_dir, filename)
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        logger.debug(f"Saved data to {filepath}")

def run_issue_type_migration(
    dry_run: bool = False,
    generate_ruby: bool = False,
    direct_migration: bool = False,
    window: int = 0,
    pane: int = 0,
    force: bool = False
):
    """
    Run the issue type migration process.

    Args:
        dry_run: If True, don't make any changes to OpenProject
        generate_ruby: If True, generate a Ruby script for manual execution
        direct_migration: If True, execute the migration directly via Rails console
        window: tmux window number
        pane: tmux pane number
        force: If True, force extraction and mapping creation even if files exist
    """
    logger.info("Starting issue type migration")

    # Create migration instance
    migration = IssueTypeMigration(dry_run=dry_run)

    # First extract data from both sides
    migration.extract_jira_issue_types(force=force)
    migration.extract_openproject_work_package_types(force=force)

    if generate_ruby:
        # Just create the mapping and the Ruby script
        mapping = migration.create_issue_type_mapping(force=force)
        work_package_types = {
            name: migration.prepare_work_package_type_for_ruby(type_data)
            for name, type_data in mapping.items()
            if type_data.get("openproject_id") is None
        }
        migration.generate_ruby_script(work_package_types)
        logger.info("Ruby script generation complete")
    elif direct_migration:
        # Extract data if forced
        if force:
            migration.extract_jira_issue_types(force=True)
            migration.extract_openproject_work_package_types(force=True)

        # Create mapping and migrate directly
        migration.create_issue_type_mapping(force=force)
        success = migration.migrate_issue_types_via_rails(window, pane)
        if success:
            logger.info("Direct issue type migration completed successfully")
        else:
            logger.warning("Direct issue type migration completed with errors")
    else:
        # Extract data if forced or if running the full migration
        if force:
            migration.extract_jira_issue_types(force=True)
            migration.extract_openproject_work_package_types(force=True)

        # Run the full migration process
        mapping = migration.migrate_issue_types()
        analysis = migration.analyze_issue_type_mapping()

        logger.info(f"Issue type migration completed")

        # Remind user about the manual step if types need to be created
        if not dry_run:
            types_to_create = sum(1 for m in mapping.values() if m["openproject_id"] is None)
            if types_to_create > 0:
                logger.warning(f"IMPORTANT: {types_to_create} work package types need to be created")
                logger.warning("To create them directly via Rails console, run:")
                logger.warning(f"  python -m src.migrations.issue_type_migration --direct-migration")
                logger.warning("Or to generate a Ruby script for manual execution, run:")
                logger.warning(f"  python -m src.migrations.issue_type_migration --generate-ruby")

        return analysis


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run issue type migration")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode (no changes to OpenProject)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force extraction of data even if it already exists",
    )
    parser.add_argument(
        "--generate-ruby",
        action="store_true",
        help="Generate Ruby script for import via Rails console",
    )
    parser.add_argument(
        "--direct-migration",
        action="store_true",
        help="Directly migrate types using Rails console via pexpect",
    )
    parser.add_argument(
        "--session-name",
        help="tmux session name containing the Rails console",
    )
    parser.add_argument(
        "--window",
        type=int,
        help="tmux window number",
    )
    parser.add_argument(
        "--pane",
        type=int,
        help="tmux pane number",
    )
    args = parser.parse_args()

    run_issue_type_migration(
        dry_run=args.dry_run,
        generate_ruby=args.generate_ruby,
        direct_migration=args.direct_migration,
        window=args.window,
        pane=args.pane,
        force=args.force
    )
