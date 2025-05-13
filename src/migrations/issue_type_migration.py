#!/usr/bin/env python3
"""Issue type migration module for Jira to OpenProject migration.
Handles the migration of issue types from Jira to OpenProject work package types.
"""

import json
import os
import re
import subprocess
import time
from typing import Any

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import console
from src.migrations.base_migration import BaseMigration
from src.models import ComponentResult

# Get logger from config
logger = config.logger


class IssueTypeMigration(BaseMigration):
    """Handles the migration of issue types from Jira to work package types in OpenProject.

    This class supports two approaches:
    1. Generate a Ruby script for manual execution via Rails console (traditional approach)
    2. Execute commands directly on the Rails console using pexpect (direct approach)
    """

    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
    ) -> None:
        """Initialize the issue type migration process.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client

        """
        super().__init__(jira_client, op_client)

        self.jira_issue_types: list[dict[str, Any]] = []
        self.op_work_package_types: list[dict[str, Any]] = []
        self.issue_type_mapping: dict[str, dict[str, Any]] = {}
        self.issue_type_id_mapping: dict[str, int] = {}
        # Get rails_client from op_client instead of requiring it directly
        self.rails_console = op_client.rails_client

        # Default mappings for default jira issue types to default openproject work package types
        self.default_mappings: dict[str, dict[str, str]] = {
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

        self.console = console

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

    def extract_jira_issue_types(self) -> list[dict[str, Any]]:
        """Extract issue types from Jira.

        Returns:
            List of Jira issue type dictionaries

        """
        issue_types_file = os.path.join(self.data_dir, "jira_issue_types.json")

        if os.path.exists(issue_types_file) and not config.migration_config.get("force", False):
            logger.info("Jira issue types data already exists, skipping extraction (use --force to override)")
            with open(issue_types_file) as f:
                self.jira_issue_types = json.load(f)
            return self.jira_issue_types

        logger.info("Extracting issue types from Jira...")

        try:
            issue_types = self.jira_client.get_issue_types()

            logger.info(f"Extracted {len(issue_types)} issue types from Jira")

            self.jira_issue_types = issue_types
            self._save_to_json(issue_types, "jira_issue_types.json")

            return issue_types
        except Exception as e:
            logger.error(f"Failed to get issue types from Jira: {e!s}")
            return []

    def extract_openproject_work_package_types(self) -> list[dict[str, Any]]:
        """Extract work package types from OpenProject.

        Returns:
            List of OpenProject work package type dictionaries

        """
        work_package_types_file = os.path.join(self.data_dir, "openproject_work_package_types.json")

        if os.path.exists(work_package_types_file) and not config.migration_config.get("force", False):
            logger.info(
                "OpenProject work package types data already exists, skipping extraction (use --force to override)",
            )
            with open(work_package_types_file) as f:
                self.op_work_package_types = json.load(f)
            return self.op_work_package_types

        logger.info("Extracting work package types from OpenProject...")

        try:
            self.op_work_package_types = self.op_client.get_work_package_types()
        except Exception as e:
            logger.warning(f"Failed to get work package types from OpenProject: {e!s}")
            logger.warning("Using an empty list of work package types for OpenProject")
            self.op_work_package_types = []

        logger.info(f"Extracted {len(self.op_work_package_types)} work package types from OpenProject")

        self._save_to_json(self.op_work_package_types, "openproject_work_package_types.json")

        return self.op_work_package_types

    def create_issue_type_mapping(self) -> dict[str, Any]:
        """Create a mapping between Jira issue types and OpenProject work package types.

        Returns:
            Dictionary mapping Jira issue types to OpenProject work package types

        """
        mapping_file = os.path.join(self.data_dir, "issue_type_mapping_template.json")
        if os.path.exists(mapping_file) and not config.migration_config.get("force", False):
            logger.info("Issue type mapping already exists, loading from file (use --force to recreate)")
            with open(mapping_file) as f:
                self.issue_type_mapping = json.load(f)
            return self.issue_type_mapping

        logger.info("Creating issue type mapping...")

        if not self.jira_issue_types:
            self.extract_jira_issue_types()

        if not self.op_work_package_types:
            self.extract_openproject_work_package_types()

        op_types_by_name = {type_data.get("name", "").lower(): type_data for type_data in self.op_work_package_types}

        mapping = {}
        for jira_type in self.jira_issue_types:
            jira_type_id: int = jira_type.get("id", 0)
            jira_type_name: str = jira_type.get("name", "")
            jira_type_description: str = jira_type.get("description", "")

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

            op_type = op_types_by_name.get(jira_type_name.lower())
            if op_type:
                mapping[jira_type_name].update(
                    {
                        "openproject_id": op_type.get("id"),
                        "openproject_name": op_type.get("name"),
                        "color": op_type.get("color", "#1A67A3"),
                        "is_milestone": jira_type_name.lower() == "milestone",
                        "matched_by": "exact_match",
                    },
                )
                continue

            default_mapping: dict[str, str] | None = self.default_mappings.get(jira_type_name)
            if default_mapping:
                default_name: str = default_mapping.get("name", "")
                op_type = op_types_by_name.get(default_name.lower())

                if op_type:
                    mapping[jira_type_name].update(
                        {
                            "openproject_id": op_type.get("id"),
                            "openproject_name": op_type.get("name"),
                            "color": default_mapping.get("color", "#1A67A3"),
                            "is_milestone": default_name.lower() == "milestone",
                            "matched_by": "default_mapping",
                        },
                    )
                else:
                    mapping[jira_type_name].update(
                        {
                            "openproject_id": None,
                            "openproject_name": default_name,
                            "color": default_mapping.get("color", "#1A67A3"),
                            "is_milestone": default_name.lower() == "milestone",
                            "matched_by": "default_mapping_to_create",
                        },
                    )
                continue

            mapping[jira_type_name].update(
                {
                    "openproject_id": None,
                    "openproject_name": jira_type_name,
                    "color": "#1A67A3",
                    "is_milestone": jira_type_name.lower() == "milestone",
                    "matched_by": "same_name",
                },
            )

        self.issue_type_mapping = mapping
        self._save_to_json(mapping, "issue_type_mapping_template.json")

        total_types = len(mapping)
        matched_types = sum(1 for type_data in mapping.values() if type_data["openproject_id"] is not None)
        to_create_types = sum(1 for type_data in mapping.values() if type_data["openproject_id"] is None)
        match_percentage = (matched_types / total_types) * 100 if total_types > 0 else 0

        logger.info(f"Issue type mapping created for {total_types} types")
        logger.info(f"Successfully matched {matched_types} types ({match_percentage:.1f}%)")
        logger.info(f"Need to create {to_create_types} new work package types in OpenProject")

        return mapping

    def normalize_issue_types(self) -> dict[str, Any]:
        """Normalize issue types by mapping sub-types to their corresponding normal types.

        This method identifies issue types starting with "Sub:" or "Sub-" and maps them
        to their corresponding "normal" issue types. This reduces the number of work
        package types to create in OpenProject and simplifies the mapping.

        Returns:
            Updated issue type mapping dictionary

        """
        logger.info("Normalizing issue types...")

        if not self.issue_type_mapping:
            self.create_issue_type_mapping()

        normalized_mapping = self.issue_type_mapping.copy()
        normalizations_applied = 0

        # Find all possible "normal" types (non-subtypes)
        normal_types = {}
        for jira_type_name, type_data in self.issue_type_mapping.items():
            # Skip if this is a sub-type
            if jira_type_name.startswith("Sub:") or jira_type_name.startswith("Sub-"):
                continue

            # Store the normal type data by name
            normal_types[jira_type_name] = type_data

        # Process all sub-types and map them to normal types
        for jira_type_name, type_data in list(normalized_mapping.items()):
            if not (jira_type_name.startswith("Sub:") or jira_type_name.startswith("Sub-")):
                continue

            # Get the base type name by removing the "Sub:" or "Sub-" prefix
            if jira_type_name.startswith("Sub:"):
                base_type_name = jira_type_name[4:].strip()
            else:  # Sub-
                base_type_name = jira_type_name[4:].strip()

            # Find the corresponding normal type
            base_type_data = normal_types.get(base_type_name)

            if base_type_data:
                # Update the subtype to use the normal type's mapping
                normalized_mapping[jira_type_name].update(
                    {
                        "openproject_id": base_type_data.get("openproject_id"),
                        "openproject_name": base_type_data.get("openproject_name"),
                        "color": base_type_data.get("color"),
                        "is_milestone": base_type_data.get("is_milestone", False),
                        "matched_by": "normalized_to_" + base_type_name,
                        "normalized_from": jira_type_name,
                        "normalized_to": base_type_name,
                    },
                )
                normalizations_applied += 1
                logger.info(f"Normalized '{jira_type_name}' to '{base_type_name}'")
            else:
                # If no corresponding normal type found, map it to a generic Task
                normalized_mapping[jira_type_name].update(
                    {
                        "openproject_name": "Task",
                        "color": "#1A67A3",
                        "is_milestone": False,
                        "matched_by": "fallback_to_task",
                        "normalized_from": jira_type_name,
                        "normalized_to": "Task",
                    },
                )
                normalizations_applied += 1
                logger.info(f"Could not find matching normal type for '{jira_type_name}', mapping to 'Task'")

        # Save the normalized mapping
        self.issue_type_mapping = normalized_mapping
        self._save_to_json(normalized_mapping, "issue_type_mapping_normalized.json")

        logger.info(f"Issue type normalization complete: {normalizations_applied} types normalized")

        # Update stats after normalization
        total_types = len(normalized_mapping)
        matched_types = sum(1 for type_data in normalized_mapping.values() if type_data["openproject_id"] is not None)
        to_create_types = sum(1 for type_data in normalized_mapping.values() if type_data["openproject_id"] is None)
        match_percentage = (matched_types / total_types) * 100 if total_types > 0 else 0

        logger.info(f"After normalization: {total_types} types total")
        logger.info(f"Successfully matched {matched_types} types ({match_percentage:.1f}%)")
        logger.info(f"Need to create {to_create_types} new work package types in OpenProject")

        return normalized_mapping

    def check_existing_work_package_types(self) -> list[dict[str, Any]]:
        """Check existing work package types in OpenProject by directly parsing
        the JSON output from a file created via Rails console.

        Returns:
            List of existing work package types

        """
        logger.info("Checking existing work package types in OpenProject via Rails...")

        temp_file_path = "/tmp/op_work_package_types.json"

        command = f"""
        begin
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

          File.write("{temp_file_path}", types.to_json)
          puts "===JSON_WRITE_SUCCESS==="
          nil
        rescue => e
          puts "RAILS_EXEC_ERROR: #{{e.message}} \\n #{{e.backtrace.join("\\n")}}"
          nil
        end
        """

        try:
            logger.info(f"Executing Rails command to write work package types to {temp_file_path}...")
            write_result = self.rails_console.execute_query(command)

            if (
                write_result.get("status") == "success"
                and write_result.get("output")
                and "RAILS_EXEC_ERROR:" in write_result["output"]
            ):
                logger.error(f"Rails command reported an error during execution: {write_result['output']}")
                return []
            if write_result.get("status") != "success":
                error_msg = write_result.get("error", "Unknown error executing Rails command for file write")
                logger.error(f"Failed to execute Rails command to write JSON file: {error_msg}")
                return []

            logger.info(f"Rails command executed successfully. Checking existence of {temp_file_path}...")

            time.sleep(0.5)

            container_name: str = config.openproject_config.get("container", None)
            op_server: str = config.openproject_config.get("server", None)

            if not op_server:
                logger.error(
                    "OpenProject server hostname is not configured "
                    "(J2O_OPENPROJECT_SERVER). Cannot run remote docker commands.",
                )
                return []

            ssh_base_cmd = ["ssh", op_server, "--"]
            docker_base_cmd = ["docker", "exec", container_name]

            ls_command = ssh_base_cmd + docker_base_cmd + ["ls", temp_file_path]
            logger.debug(f"Executing command: {' '.join(ls_command)}")
            try:
                ls_result = subprocess.run(ls_command, capture_output=True, text=True, check=False)
                if ls_result.returncode != 0:
                    error_details = ls_result.stderr.strip()
                    logger.error(
                        f"File {temp_file_path} check failed (exit code {ls_result.returncode}). "
                        f"ls stderr: {error_details}; stdout: {ls_result.stdout}",
                    )
                    return []
                logger.info(f"File {temp_file_path} confirmed to exist in container.")
            except subprocess.SubprocessError as e:
                logger.exception(f"Error running docker exec ls command: {e!s}")
                return []

            logger.info(f"Reading {temp_file_path} using ssh + docker exec...")
            cat_command = ssh_base_cmd + docker_base_cmd + ["cat", temp_file_path]
            logger.debug(f"Executing command: {' '.join(cat_command)}")
            read_result = subprocess.run(cat_command, capture_output=True, text=True, check=False)

            if read_result.returncode != 0:
                logger.error(f"Failed to read {temp_file_path} via docker exec: {read_result.stderr}")
                return []

            json_content = read_result.stdout.strip()
            logger.debug(f"Content read from {temp_file_path}:\\n{json_content}")

            try:
                types: list[dict[str, Any]] = json.loads(json_content)
                logger.info(f"Successfully parsed {len(types)} work package types from file")
                return types
            except json.JSONDecodeError as e:
                logger.error(f"Could not parse work package types JSON read from file: {e}")
                logger.debug(f"Invalid JSON content: {json_content}")
                return []

        except subprocess.SubprocessError as e:
            logger.exception(f"Error running docker exec command: {e!s}")
            return []
        except Exception as e:
            logger.exception(f"Unexpected error during work package type retrieval: {e!s}")
            return []
        finally:
            try:
                if "container_name" in locals() and container_name:
                    logger.debug(f"Attempting final removal of remote temporary file {temp_file_path}...")
                    ssh_base_cmd = ["ssh", op_server, "--"] if "op_server" in locals() and op_server else []
                    docker_base_cmd = ["docker", "exec", container_name] if container_name else []
                    if ssh_base_cmd and docker_base_cmd:
                        rm_command = ssh_base_cmd + docker_base_cmd + ["rm", "-f", temp_file_path]
                        logger.debug(f"Executing final rm command: {' '.join(rm_command)}")
                        subprocess.run(rm_command, check=False, capture_output=True, timeout=10)
                    else:
                        logger.warning(
                            f"Skipping final removal of temporary file {temp_file_path} "
                            "due to missing server or container config.",
                        )

                else:
                    logger.debug(
                        f"Skipping final removal of temporary file {temp_file_path} "
                        "as container_name was not defined.",
                    )
            except Exception as final_e:
                try:
                    error_type = type(final_e)
                    error_message = str(final_e)
                except Exception as str_err:
                    error_type = "Unknown"
                    error_message = f"Failed to convert finally exception to string: {str_err}"
                logger.warning(f"Failed during final removal of {temp_file_path} (Type: {error_type}): {error_message}")

    def create_work_package_type_via_rails(self, type_data: dict[str, Any]) -> dict[str, Any]:
        """Create a work package type in OpenProject via Rails console.

        Args:
            type_data: Dictionary with type data from Jira issue type mapping

        Returns:
            Dictionary with operation result status

        """
        type_name = type_data.get("openproject_name", "")
        type_color = type_data.get("color", "#1A67A3")
        is_milestone = type_data.get("is_milestone", False)

        logger.info(f"Creating work package type '{type_name}' via Rails console...")

        # Check if the type already exists to avoid duplicates
        check_command = f'existing_type = Type.where("name ilike ?", "{type_name}").first'
        check_result = self.rails_console.execute_query(check_command)

        if check_result["status"] != "success":
            return {
                "status": "error",
                "error": f"Failed to check for existing type: {check_result.get('error', 'Unknown error')}",
            }

        exists_command = "existing_type.present?"
        exists_result = self.rails_console.execute_query(exists_command)

        if exists_result["status"] == "success" and "true" in exists_result["output"]:
            logger.info(f"Work package type '{type_name}' already exists, retrieving ID")
            id_command = "existing_type.id"
            id_result = self.rails_console.execute_query(id_command)

            if id_result["status"] == "success" and id_result["output"].strip().isdigit():
                type_id = int(id_result["output"].strip())
                return {
                    "status": "success",
                    "message": "Work package type already exists",
                    "id": type_id,
                }
            logger.warning(f"Failed to retrieve ID for existing type '{type_name}'")

        # Create the type
        milestone_flag = "true" if is_milestone else "false"

        command = f"""
        begin
          type = Type.create!(
            name: '{type_name}',
            color: '{type_color}',
            is_milestone: {milestone_flag},
            is_default: false,
            is_standard: true,
            position: Type.count + 1,
            attribute_groups: Type.new.default_attribute_groups
          )
          type_id = type.id
          {{id: type_id, name: type.name, status: :success}}
        rescue => e
          {{status: :error, message: e.message}}
        end
        """

        result = self.rails_console.execute_query(command)

        if result["status"] != "success":
            logger.error(
                f"Error executing Rails command to create type '{type_name}': {result.get('error', 'Unknown error')}",
            )
            return {
                "status": "error",
                "error": f"Rails execution error: {result.get('error', 'Unknown error')}",
            }

        # Parse the result
        output = result.get("output", "")

        if "status: :error" in output:
            error_msg = "Unknown error"
            error_match = re.search(r'message: "([^"]+)"', output)
            if error_match:
                error_msg = error_match.group(1)

            logger.error(f"Error creating work package type '{type_name}': {error_msg}")
            return {"status": "error", "error": error_msg}

        # Extract ID if successful
        id_match = re.search(r"id: (\d+)", output)
        if id_match:
            type_id = int(id_match.group(1))
            logger.info(f"Created work package type '{type_name}' with ID {type_id}")

            # Verify the type exists by querying it
            verify_command = f"Type.find({type_id}).present? rescue false"
            verify_result = self.rails_console.execute_query(verify_command)

            if verify_result["status"] == "success" and "true" in verify_result["output"]:
                logger.info(f"Verified work package type '{type_name}' with ID {type_id} exists")
                return {"status": "success", "id": type_id, "name": type_name}
            logger.warning(f"Created type but failed verification: {verify_result.get('output', 'Unknown error')}")

        logger.warning(f"Failed to parse type ID from Rails response: {output}")

        # If we reached here without returning, assume success but couldn't get ID
        return {"status": "success", "name": type_name}

    def migrate_issue_types_via_rails(self, window: int = 0, pane: int = 0) -> bool:
        """Migrate issue types directly via the Rails console using a bulk operation.

        This method creates all required work package types at once via a single Rails script,
        following the pattern used in other migration components.

        Args:
            window: tmux window number (default: 0)
            pane: tmux pane number (default: 0)

        Returns:
            bool: True if migration was successful, False otherwise

        """
        if not self.issue_type_mapping:
            self.create_issue_type_mapping()

        # Get existing types to avoid creating duplicates
        existing_types = self.check_existing_work_package_types()
        existing_names = [type_data.get("name", "").lower() for type_data in existing_types]

        # Collect types to create
        types_to_create: list[dict[str, str | bool]] = []
        for type_name, type_data in self.issue_type_mapping.items():
            if type_data.get("openproject_id") is None:
                op_type_name = type_data.get("openproject_name", "")

                # Skip if already exists (case insensitive check)
                if op_type_name.lower() in existing_names:
                    # Try to update mapping with existing ID
                    existing_id = next(
                        (t["id"] for t in existing_types if t.get("name", "").lower() == op_type_name.lower()),
                        None,
                    )
                    if existing_id:
                        logger.info(f"Work package type '{op_type_name}' already exists, updating mapping")
                        self.issue_type_mapping[type_name]["openproject_id"] = existing_id
                        self.issue_type_mapping[type_name]["matched_by"] = "found_existing"
                    continue

                # Only add unique type names to prevent duplicates
                if not any(t.get("name") == op_type_name for t in types_to_create):
                    types_to_create.append(
                        {
                            "name": op_type_name,
                            "color": type_data.get("color", "#1A67A3"),
                            "is_milestone": type_data.get("is_milestone", False),
                            "is_default": op_type_name.lower() == "task",
                            "jira_type_name": type_name,  # Store original Jira type name for mapping
                        },
                    )

        if not types_to_create:
            logger.info("No new work package types to create")
            return True

        logger.info(f"Preparing to create {len(types_to_create)} work package types in bulk")

        # Create a temporary JSON file to store the types
        temp_file = os.path.join(self.data_dir, "work_package_types_to_create.json")
        with open(temp_file, "w") as f:
            json.dump(types_to_create, f, indent=2)

        logger.info(f"Saved {len(types_to_create)} work package types to create to {temp_file}")

        # Define container paths
        container_temp_path = "/tmp/work_package_types_to_create.json"
        container_results_path = "/tmp/work_package_types_created.json"

        # Create a temporary file to store the results
        results_file = os.path.join(self.data_dir, "work_package_types_created.json")

        # Transfer the file to the container
        if not self.rails_console.transfer_file_to_container(temp_file, container_temp_path):
            logger.error(f"Failed to transfer types file to container from {temp_file} to {container_temp_path}")
            return False

        # Generate Rails script to create all types at once with proper separation
        # 1. Header section with Python variable interpolation
        header_script = f"""
        # Ruby variables setup - generated by Python
        types_input_file = '{container_temp_path}'
        results_output_file = '{container_results_path}'
        """

        # 2. Main Ruby script without Python interpolation
        main_script = """
        puts "Starting bulk work package type creation"
        puts "Types input file: #{types_input_file}"
        puts "Results output file: #{results_output_file}"
        begin
          # First verify we can read the input file
          if !File.exist?(types_input_file)
            puts "BULK_CREATE_FAILED: Input file not found: #{types_input_file}"
            exit 1
          end

          # Try to read the input file
          begin
            file_content = File.read(types_input_file)
            puts "Successfully read input file (#{file_content.size} bytes)"
          rescue => e
            puts "BULK_CREATE_FAILED: Error reading input file: #{e.message}"
            exit 1
          end

          # Try to parse the JSON
          begin
            types_data = JSON.parse(file_content)
            puts "Successfully parsed JSON with #{types_data.size} types to create"
          rescue => e
            puts "BULK_CREATE_FAILED: Error parsing JSON: #{e.message}"
            exit 1
          end

          created_types = []
          errors = []

          # Process each type
          types_data.each_with_index do |type_data, index|
            begin
              puts "Processing type #{index+1}/#{types_data.size}: #{type_data['name']}"

              # Check if type already exists
              existing = Type.where("LOWER(name) = ?", type_data["name"].downcase).first

              if existing
                puts "Type already exists: #{existing.name} (ID: #{existing.id})"
                created_types << {
                  original_name: type_data["name"],
                  id: existing.id,
                  name: existing.name,
                  status: "existing",
                  jira_type_name: type_data["jira_type_name"]
                }
                next
              end

              # Find or create color object
              color_hex = type_data["color"]
              color = Color.find_by(hexcode: color_hex)

              if color.nil?
                # If color doesn't exist, create it
                puts "Creating new color with hexcode: #{color_hex}"
                color = Color.create!(hexcode: color_hex, name: color_hex)
                puts "Created new color: #{color.name} (ID: #{color.id})"
              else
                puts "Using existing color: #{color.name} (ID: #{color.id})"
              end

              # Create new type
              puts "Creating new type: #{type_data['name']}"
              type = Type.create!(
                name: type_data["name"],
                color: color,
                is_milestone: type_data["is_milestone"],
                is_default: type_data["is_default"],
                is_standard: true,
                position: Type.maximum(:position).to_i + 1,
                attribute_groups: Type.new.default_attribute_groups
              )

              puts "Successfully created type: #{type.name} (ID: #{type.id})"
              created_types << {
                original_name: type_data["name"],
                id: type.id,
                name: type.name,
                status: "created",
                jira_type_name: type_data["jira_type_name"]
              }
            rescue => e
              puts "Error creating type #{type_data['name']}: #{e.message}"
              errors << {
                name: type_data["name"],
                error: e.message,
                jira_type_name: type_data["jira_type_name"]
              }
            end
          end

          # Save results to file
          results = {
            created: created_types,
            errors: errors
          }

          # Write results to file
          begin
            json_output = results.to_json
            puts "Writing results to #{results_output_file} (#{json_output.size} bytes)"
            File.write(results_output_file, json_output)
            puts "Results successfully written to file"
          rescue => e
            puts "BULK_CREATE_FAILED: Error writing results file: #{e.message}"
            # Don't exit, try to provide console output anyway
          end

          puts "BULK_CREATE_COMPLETED: Created #{created_types.length} types, Errors: #{errors.length}"
          created_types.each do |type|
            puts "CREATED_TYPE: #{type[:id]} - #{type[:name]} (#{type[:status]})"
          end

          errors.each do |error|
            puts "ERROR_TYPE: #{error[:name]} - #{error[:error]}"
          end
        rescue => e
          puts "BULK_CREATE_FAILED: #{e.message}"
          puts e.backtrace.join("\\n")
        end
        """

        # Combine the scripts
        bulk_create_script = header_script + main_script

        # Execute the bulk creation script
        logger.info("Executing bulk creation of work package types via Rails console...")
        result = self.rails_console.execute_query(bulk_create_script)

        if result["status"] != "success":
            logger.error(f"Failed to execute bulk creation script: {result.get('error', 'Unknown error')}")
            return False

        # Check for specific error markers in the output
        output = result.get("output", "")
        if output is None:
            logger.error("No output returned from Rails console")
            return False

        if "BULK_CREATE_FAILED" in output:
            error_match = re.search(r"BULK_CREATE_FAILED: (.*)", output)
            error_message = error_match.group(1) if error_match else "Unknown error"
            logger.error(f"Bulk creation failed: {error_message}")
            return False

        # Parse results
        try:
            # Try to get the result file from the container
            if not self.rails_console.transfer_file_from_container(container_results_path, results_file):
                logger.error(f"Failed to retrieve results file from container: {container_results_path}")
                # Dump the entire output for debugging
                logger.debug(f"Rails console output: {output}")
                return False

            if not os.path.exists(results_file):
                logger.error(f"Results file {results_file} was not created")
                # Dump the entire output for debugging
                logger.debug(f"Rails console output: {output}")
                return False

            with open(results_file) as f:
                result_content = f.read()
                if not result_content.strip():
                    logger.error(f"Results file {results_file} is empty")
                    logger.debug(f"Rails console output: {output}")
                    return False

                try:
                    creation_results = json.loads(result_content)
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse results file content: {e}")
                    logger.debug(f"Results file content: {result_content}")
                    logger.debug(f"Rails console output: {output}")
                    return False

            created_count = len(creation_results.get("created", []))
            error_count = len(creation_results.get("errors", []))

            logger.info(f"Bulk creation completed: {created_count} types created, {error_count} errors")

            # Update mapping with created types
            for created_type in creation_results.get("created", []):
                jira_type_name = created_type.get("jira_type_name")
                if jira_type_name and jira_type_name in self.issue_type_mapping:
                    self.issue_type_mapping[jira_type_name]["openproject_id"] = created_type.get("id")
                    self.issue_type_mapping[jira_type_name]["matched_by"] = "created"
                    logger.info(f"Updated mapping for '{jira_type_name}' with ID {created_type.get('id')}")

            # Save updated mapping
            self._save_to_json(self.issue_type_mapping, "issue_type_mapping.json")

            # Update ID mapping
            final_mapping = {}
            for type_name, mapping in self.issue_type_mapping.items():
                jira_id = mapping["jira_id"]
                op_id = mapping["openproject_id"]

                if op_id:
                    final_mapping[jira_id] = op_id

            self._save_to_json(final_mapping, "issue_type_id_mapping.json")

            # Report errors
            if error_count > 0:
                logger.warning("Some work package types failed to create:")
                for error in creation_results.get("errors", []):
                    logger.warning(f"  - {error.get('name')}: {error.get('error')}")

            return error_count == 0

        except Exception as e:
            logger.error(f"Error processing creation results: {e!s}")
            import traceback

            logger.debug(traceback.format_exc())
            return False

    def migrate_issue_types(self) -> dict[str, Any]:
        """Prepare for migrating issue types from Jira to OpenProject.

        Returns:
            Updated mapping with analysis information

        """
        logger.info("Starting issue type migration preparation...")

        if not self.jira_issue_types:
            self.extract_jira_issue_types()

        if not self.op_work_package_types:
            self.extract_openproject_work_package_types()

        if not self.issue_type_mapping:
            self.create_issue_type_mapping()

        self.normalize_issue_types()

        op_types_to_create = {}
        for type_name, mapping in self.issue_type_mapping.items():
            if mapping["openproject_id"] is None:
                op_type_name = mapping["openproject_name"]
                if op_type_name not in op_types_to_create:
                    op_types_to_create[op_type_name] = mapping

        total_types_needing_creation = sum(
            1 for mapping in self.issue_type_mapping.values() if mapping["openproject_id"] is None
        )

        if op_types_to_create:
            logger.info(
                f"Need to create {len(op_types_to_create)} unique work package types in OpenProject via Rails console",
            )
            logger.info(
                f"(This will map to {total_types_needing_creation} total Jira issue types after deduplication)",
            )
        else:
            logger.info("No new work package types need to be created in OpenProject")

        final_mapping = {}
        for type_name, mapping in self.issue_type_mapping.items():
            jira_id = mapping["jira_id"]
            op_id = mapping["openproject_id"]

            if op_id:
                final_mapping[jira_id] = op_id

        self._save_to_json(final_mapping, "issue_type_id_mapping.json")

        return self.analyze_issue_type_mapping()

    def analyze_issue_type_mapping(self) -> dict[str, Any]:
        """Analyze the issue type mapping to identify potential issues.

        Returns:
            Dictionary with analysis results

        """
        if not self.issue_type_mapping:
            self.create_issue_type_mapping()

        analysis: dict[str, Any] = {
            "total_jira_types": len(self.issue_type_mapping),
            "matched_op_types": sum(
                1 for mapping in self.issue_type_mapping.values() if mapping.get("openproject_id") is not None
            ),
            "types_to_create": sum(
                1 for mapping in self.issue_type_mapping.values() if mapping.get("openproject_id") is None
            ),
            "mapping_details": self.issue_type_mapping,
            "unmatched_details": [
                {
                    "jira_id": mapping["jira_id"],
                    "jira_name": mapping["jira_name"],
                    "proposed_op_name": mapping["openproject_name"],
                    "proposed_color": mapping["color"],
                    "is_milestone": mapping["is_milestone"],
                }
                for mapping in self.issue_type_mapping.values()
                if mapping.get("openproject_id") is None
            ],
        }

        total = analysis["total_jira_types"]
        if total > 0:
            analysis["match_percentage"] = (analysis["matched_op_types"] / total) * 100
            analysis["create_percentage"] = (analysis["types_to_create"] / total) * 100
        else:
            analysis["match_percentage"] = 0
            analysis["create_percentage"] = 0

        self._save_to_json(analysis, "issue_type_analysis.json")

        logger.info("\nIssue Type Mapping Analysis:")
        logger.info(f"Total Jira issue types: {total}")
        logger.info(
            f"- Matched to OpenProject types: {analysis['matched_op_types']} ({analysis['match_percentage']:.1f}%)",
        )
        logger.info(
            f"- Need creation in OpenProject: {analysis['types_to_create']} ({analysis['create_percentage']:.1f}%)",
        )

        if analysis["types_to_create"] > 0:
            logger.info(
                f"Action required: {analysis['types_to_create']} work package types "
                "need creation via Rails console (direct or script). "
                "Details in issue_type_analysis.json",
            )

        return analysis

    def _load_from_json(self, filename: str, default: Any = None) -> Any:
        """Load data from a JSON file in the data directory.

        Args:
            filename: Name of the JSON file
            default: Default value to return if file doesn't exist

        Returns:
            Loaded JSON data or default value

        """
        filepath = os.path.join(self.data_dir, filename)
        if os.path.exists(filepath):
            try:
                with open(filepath) as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load {filepath}: {e}")
                return default
        return default

    def update_mapping_file(self) -> bool:
        """Update the issue type mapping file with IDs from OpenProject.

        This method is useful when work package types were created manually via a Ruby script
        execution and the mapping file needs to be updated with the created type IDs.

        Returns:
            True if mapping was updated successfully, False otherwise

        """
        logger.info("Updating issue type mapping file with IDs from OpenProject...")

        # Get all work package types from OpenProject
        op_types = self.op_client.get_work_package_types()
        if not op_types:
            logger.error("Failed to retrieve work package types from OpenProject")
            return False

        # Create a dictionary of name to type mapping for easy lookup
        op_types_by_name = {type_data.get("name"): type_data for type_data in op_types}

        # Count the mapping updates
        updated_count = 0
        already_mapped_count = 0
        missing_count = 0

        # Update mapping for each type that was supposed to be created
        for jira_type_name, type_data in self.issue_type_mapping.items():
            op_name = type_data.get("openproject_name", "")

            # Skip if already mapped
            if type_data.get("openproject_id") and type_data.get("matched_by") != "default_mapping_to_create":
                already_mapped_count += 1
                continue

            # Find by name in OpenProject types
            if op_name in op_types_by_name:
                op_type = op_types_by_name[op_name]
                op_id = op_type.get("id")

                # Update the mapping
                logger.info(f"Found work package type '{op_name}' with ID {op_id}")
                self.issue_type_mapping[jira_type_name]["openproject_id"] = op_id
                self.issue_type_mapping[jira_type_name]["matched_by"] = (
                    "created" if type_data.get("matched_by") == "default_mapping_to_create" else "exact_match"
                )
                updated_count += 1
            else:
                logger.warning(f"Work package type '{op_name}' not found in OpenProject")
                missing_count += 1

        # Save the updated mapping
        if updated_count > 0:
            self._save_to_json(self.issue_type_mapping, "issue_type_mapping.json")
            logger.info(f"Updated mapping for {updated_count} work package types")
        else:
            logger.info("No mapping updates needed")

        # Print summary
        logger.info(f"Summary: Updated {updated_count}, Already mapped {already_mapped_count}, Missing {missing_count}")

        # Return success only if we have more matches than misses
        if missing_count > updated_count and missing_count > 0:
            logger.warning(f"Migration partially failed: {missing_count} types were not found in OpenProject")
            return False

        return updated_count > 0 or already_mapped_count > 0

    def run(self) -> ComponentResult:
        """Run the issue type migration.

        Returns:
            Dictionary with migration results

        """
        logger.info("Starting issue type migration...")

        # 1. Extract and process issue types
        self.extract_jira_issue_types()
        self.extract_openproject_work_package_types()
        self.create_issue_type_mapping()

        # 2. Normalize issue types (map sub-types to normal types)
        self.normalize_issue_types()

        # 3. Migrate issue types (create if needed)
        if config.migration_config.get("dry_run", False):
            logger.info("DRY RUN: Skipping actual issue type migration")
            results = ComponentResult(
                success=True,
                total_types=len(self.issue_type_mapping),
                matched_types=sum(1 for t in self.issue_type_mapping.values() if t.get("openproject_id") is not None),
                normalized_types=sum(
                    1
                    for t in self.issue_type_mapping.values()
                    if t.get("matched_by", "").startswith("normalized_to_") or t.get("matched_by") == "fallback_to_task"
                ),
                created_types=0,
                dry_run=True,
            )
        else:
            # Migrate issue types
            self.migrate_issue_types()

            # Attempt to create work package types via Rails console
            types_to_create = sum(
                1 for mapping in self.issue_type_mapping.values() if mapping.get("openproject_id") is None
            )

            rails_migration_success = False
            if types_to_create > 0:
                logger.info(f"Attempting to create {types_to_create} work package types via Rails console")
                rails_migration_success = self.migrate_issue_types_via_rails()
                if rails_migration_success:
                    logger.info("Successfully created work package types via Rails console")
                else:
                    logger.error("Failed to create some work package types via Rails console")

            # Update the mapping file with any newly created types
            mapping_updated = self.update_mapping_file()

            # Check if we're in a good state despite potential errors
            final_types_to_create = sum(
                1 for mapping in self.issue_type_mapping.values() if mapping.get("openproject_id") is None
            )

            # Success if mappings updated and most types were created
            migration_success = mapping_updated and (
                final_types_to_create == 0 or final_types_to_create < types_to_create * 0.2
            )

            if mapping_updated:
                if migration_success:
                    logger.info("Successfully updated issue type mappings with newly created types")
                else:
                    logger.warning(f"Updated issue type mappings but {final_types_to_create} types still need creation")

            # Get final counts after all operations
            created_types = sum(
                1 for mapping in self.issue_type_mapping.values() if mapping.get("matched_by") == "created"
            )

            results = ComponentResult(
                success=migration_success,
                total_types=len(self.issue_type_mapping),
                matched_types=sum(
                    1 for mapping in self.issue_type_mapping.values() if mapping.get("openproject_id") is not None
                ),
                normalized_types=sum(
                    1
                    for mapping in self.issue_type_mapping.values()
                    if mapping.get("matched_by", "").startswith("normalized_to_")
                    or mapping.get("matched_by") == "fallback_to_task"
                ),
                created_types=created_types,
                existing_types=sum(
                    1 for mapping in self.issue_type_mapping.values() if mapping.get("matched_by") == "exact_match"
                ),
                failed_types=final_types_to_create,
                message=(
                    f"Created {created_types} types, {final_types_to_create} failed"
                    if final_types_to_create > 0
                    else ""
                ),
            )

        return results
