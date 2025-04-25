#!/usr/bin/env python3
"""
Issue type migration module for Jira to OpenProject migration.
Handles the migration of issue types from Jira to OpenProject work package types.
"""

import json
import os
import subprocess
import time
from typing import Any, Optional

from src.models import ComponentResult
from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.clients.openproject_rails_client import OpenProjectRailsClient
from src.display import ProgressTracker, console
from src.migrations.base_migration import BaseMigration

# Get logger from config
logger = config.logger


class IssueTypeMigration(BaseMigration):
    """
    Handles the migration of issue types from Jira to work package types in OpenProject.

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
        Initialize the issue type migration process.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client
            rails_console: Initialized OpenProjectRailsClient instance (optional)
        """
        super().__init__(jira_client, op_client)

        self.jira_issue_types: list[dict] = []
        self.op_work_package_types: list[dict] = []
        self.issue_type_mapping: dict[str, dict] = {}
        self.issue_type_id_mapping: dict[str, int] = {}
        self.rails_console = rails_console

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

        self.console = console

        self._load_data()

    def _load_data(self) -> None:
        """Load existing data from JSON files."""
        self.jira_issue_types = self._load_from_json("jira_issue_types.json", [])
        self.op_work_package_types = self._load_from_json(
            "op_work_package_types.json", []
        )
        self.issue_type_mapping = self._load_from_json("issue_type_mapping.json", {})
        self.issue_type_id_mapping = self._load_from_json(
            "issue_type_id_mapping.json", {}
        )
        logger.info(f"Loaded {len(self.jira_issue_types)=} Jira issue types")
        logger.info(
            f"Loaded {len(self.op_work_package_types)=} OpenProject work package types"
        )
        logger.info(f"Loaded {len(self.issue_type_mapping)=} issue type mappings")
        logger.info(f"Loaded {len(self.issue_type_id_mapping)=} issue type ID mappings")

    def extract_jira_issue_types(self, force=False) -> list[dict[str, Any]]:
        """
        Extract issue types from Jira.

        Args:
            force: If True, extract again even if data already exists

        Returns:
            List of Jira issue type dictionaries
        """
        issue_types_file = os.path.join(self.data_dir, "jira_issue_types.json")

        if os.path.exists(issue_types_file) and not force:
            logger.info(
                "Jira issue types data already exists, skipping extraction (use --force to override)"
            )
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
            logger.error(f"Failed to get issue types from Jira: {str(e)}")
            return []

    def extract_openproject_work_package_types(
        self, force=False
    ) -> list[dict[str, Any]]:
        """
        Extract work package types from OpenProject.

        Args:
            force: If True, extract again even if data already exists

        Returns:
            List of OpenProject work package type dictionaries
        """
        work_package_types_file = os.path.join(
            self.data_dir, "openproject_work_package_types.json"
        )

        if os.path.exists(work_package_types_file) and not force:
            logger.info(
                "OpenProject work package types data already exists, skipping extraction (use --force to override)"
            )
            with open(work_package_types_file) as f:
                self.op_work_package_types = json.load(f)
            return self.op_work_package_types

        logger.info("Extracting work package types from OpenProject...")

        try:
            self.op_work_package_types = self.op_client.get_work_package_types()
        except Exception as e:
            logger.warning(
                f"Failed to get work package types from OpenProject: {str(e)}"
            )
            logger.warning("Using an empty list of work package types for OpenProject")
            self.op_work_package_types = []

        logger.info(
            f"Extracted {len(self.op_work_package_types)} work package types from OpenProject"
        )

        self._save_to_json(
            self.op_work_package_types, "openproject_work_package_types.json"
        )

        return self.op_work_package_types

    def create_issue_type_mapping(self, force=False) -> dict[str, Any]:
        """
        Create a mapping between Jira issue types and OpenProject work package types.

        Args:
            force: If True, create the mapping again even if it already exists

        Returns:
            Dictionary mapping Jira issue types to OpenProject work package types
        """
        mapping_file = os.path.join(self.data_dir, "issue_type_mapping_template.json")
        if os.path.exists(mapping_file) and not force:
            logger.info(
                "Issue type mapping already exists, loading from file (use --force to recreate)"
            )
            with open(mapping_file) as f:
                self.issue_type_mapping = json.load(f)
            return self.issue_type_mapping

        logger.info("Creating issue type mapping...")

        if not self.jira_issue_types:
            self.extract_jira_issue_types()

        if not self.op_work_package_types:
            self.extract_openproject_work_package_types()

        op_types_by_name = {
            type_data.get("name", "").lower(): type_data
            for type_data in self.op_work_package_types
        }

        mapping = {}
        for jira_type in self.jira_issue_types:
            jira_type_id = jira_type.get("id")
            jira_type_name = jira_type.get("name", "")
            jira_type_description = jira_type.get("description", "")

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
                    }
                )
                continue

            default_mapping = self.default_mappings.get(jira_type_name)
            if default_mapping:
                default_name = default_mapping.get("name")
                op_type = op_types_by_name.get(default_name.lower())

                if op_type:
                    mapping[jira_type_name].update(
                        {
                            "openproject_id": op_type.get("id"),
                            "openproject_name": op_type.get("name"),
                            "color": default_mapping.get("color", "#1A67A3"),
                            "is_milestone": default_name.lower() == "milestone",
                            "matched_by": "default_mapping",
                        }
                    )
                else:
                    mapping[jira_type_name].update(
                        {
                            "openproject_id": None,
                            "openproject_name": default_name,
                            "color": default_mapping.get("color", "#1A67A3"),
                            "is_milestone": default_name.lower() == "milestone",
                            "matched_by": "default_mapping_to_create",
                        }
                    )
                continue

            mapping[jira_type_name].update(
                {
                    "openproject_id": None,
                    "openproject_name": jira_type_name,
                    "color": "#1A67A3",
                    "is_milestone": jira_type_name.lower() == "milestone",
                    "matched_by": "same_name",
                }
            )

        self.issue_type_mapping = mapping
        self._save_to_json(mapping, "issue_type_mapping_template.json")

        total_types = len(mapping)
        matched_types = sum(
            1
            for type_data in mapping.values()
            if type_data["openproject_id"] is not None
        )
        to_create_types = sum(
            1 for type_data in mapping.values() if type_data["openproject_id"] is None
        )
        match_percentage = (matched_types / total_types) * 100 if total_types > 0 else 0

        logger.info(f"Issue type mapping created for {total_types} types")
        logger.info(
            f"Successfully matched {matched_types} types ({match_percentage:.1f}%)"
        )
        logger.info(
            f"Need to create {to_create_types} new work package types in OpenProject"
        )

        return mapping

    def normalize_issue_types(self) -> dict[str, Any]:
        """
        Normalize issue types by mapping sub-types to their corresponding normal types.

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
            if not (
                jira_type_name.startswith("Sub:") or jira_type_name.startswith("Sub-")
            ):
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
                    }
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
                    }
                )
                normalizations_applied += 1
                logger.info(
                    f"Could not find matching normal type for '{jira_type_name}', mapping to 'Task'"
                )

        # Save the normalized mapping
        self.issue_type_mapping = normalized_mapping
        self._save_to_json(normalized_mapping, "issue_type_mapping_normalized.json")

        logger.info(
            f"Issue type normalization complete: {normalizations_applied} types normalized"
        )

        # Update stats after normalization
        total_types = len(normalized_mapping)
        matched_types = sum(
            1
            for type_data in normalized_mapping.values()
            if type_data["openproject_id"] is not None
        )
        to_create_types = sum(
            1
            for type_data in normalized_mapping.values()
            if type_data["openproject_id"] is None
        )
        match_percentage = (matched_types / total_types) * 100 if total_types > 0 else 0

        logger.info(f"After normalization: {total_types} types total")
        logger.info(
            f"Successfully matched {matched_types} types ({match_percentage:.1f}%)"
        )
        logger.info(
            f"Need to create {to_create_types} new work package types in OpenProject"
        )

        return normalized_mapping

    def prepare_work_package_type_for_ruby(
        self, type_data: dict[str, Any]
    ) -> dict[str, Any]:
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
        is_default = name.lower() == "task"

        logger.info(f"Preparing work package type for Ruby script: {name}")

        return {
            "name": name,
            "color": color,
            "is_milestone": is_milestone,
            "is_default": is_default,
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
            with ProgressTracker(
                description="Connecting to tmux session", total=1
            ) as tracker:
                self.rails_console = OpenProjectRailsClient(
                    window=window, pane=pane, debug=False
                )

                result = self.rails_console.execute("Rails.env")

                if result["status"] == "success":
                    logger.info(
                        f"Connected to Rails console successfully (Rails environment: {result['output']})"
                    )
                    tracker.increment()
                    return True
                else:
                    logger.error(
                        f"Failed to connect to Rails console: {result.get('error', 'Unknown error')}"
                    )
                    return False
        except Exception as e:
            logger.error(f"Error connecting to Rails console: {str(e)}")
            import traceback

            logger.debug(traceback.format_exc())
            return False

    def check_existing_work_package_types(self) -> list[dict]:
        """
        Check existing work package types in OpenProject by directly parsing
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
            logger.info(
                f"Executing Rails command to write work package types to {temp_file_path}..."
            )
            write_result = self.rails_console.execute(command)

            if (
                write_result.get("status") == "success"
                and write_result.get("output")
                and "RAILS_EXEC_ERROR:" in write_result["output"]
            ):
                logger.error(
                    f"Rails command reported an error during execution: {write_result['output']}"
                )
                return []
            elif write_result.get("status") != "success":
                error_msg = write_result.get(
                    "error", "Unknown error executing Rails command for file write"
                )
                logger.error(
                    f"Failed to execute Rails command to write JSON file: {error_msg}"
                )
                return []

            logger.info(
                f"Rails command executed successfully. Checking existence of {temp_file_path}..."
            )

            time.sleep(0.5)

            container_name = config.openproject_config.get("container")
            op_server = config.openproject_config.get("server")

            if not op_server:
                logger.error(
                    "OpenProject server hostname is not configured "
                    "(J2O_OPENPROJECT_SERVER). Cannot run remote docker commands."
                )
                return []

            ssh_base_cmd = ["ssh", op_server, "--"]
            docker_base_cmd = ["docker", "exec", container_name]

            ls_command = ssh_base_cmd + docker_base_cmd + ["ls", temp_file_path]
            logger.debug(f"Executing command: {' '.join(ls_command)}")
            try:
                ls_result = subprocess.run(
                    ls_command, capture_output=True, text=True, check=False
                )
                if ls_result.returncode != 0:
                    error_details = ls_result.stderr.strip()
                    logger.error(
                        f"File {temp_file_path} check failed (exit code {ls_result.returncode}). "
                        f"ls stderr: {error_details}; stdout: {ls_result.stdout}"
                    )
                    return []
                logger.info(f"File {temp_file_path} confirmed to exist in container.")
            except subprocess.SubprocessError as e:
                logger.error(f"Error running docker exec ls command: {str(e)}")
                if "console" in globals():
                    console.print_exception(show_locals=True)
                return []

            logger.info(f"Reading {temp_file_path} using ssh + docker exec...")
            cat_command = ssh_base_cmd + docker_base_cmd + ["cat", temp_file_path]
            logger.debug(f"Executing command: {' '.join(cat_command)}")
            read_result = subprocess.run(
                cat_command, capture_output=True, text=True, check=False
            )

            if read_result.returncode != 0:
                logger.error(
                    f"Failed to read {temp_file_path} via docker exec: {read_result.stderr}"
                )
                return []

            json_content = read_result.stdout.strip()
            logger.debug(f"Content read from {temp_file_path}:\\n{json_content}")

            try:
                types = json.loads(json_content)
                logger.info(
                    f"Successfully parsed {len(types)} work package types from file"
                )
                return types
            except json.JSONDecodeError as e:
                logger.error(
                    f"Could not parse work package types JSON read from file: {e}"
                )
                logger.debug(f"Invalid JSON content: {json_content}")
                return []

        except subprocess.SubprocessError as e:
            logger.error(f"Error running docker exec command: {str(e)}")
            if "console" in globals():
                console.print_exception(show_locals=True)
            return []
        except Exception as e:
            logger.error(
                f"Unexpected error during work package type retrieval: {str(e)}"
            )
            if "console" in globals():
                console.print_exception(show_locals=True)
            return []
        finally:
            try:
                if "container_name" in locals() and container_name:
                    logger.debug(
                        f"Attempting final removal of remote temporary file {temp_file_path}..."
                    )
                    ssh_base_cmd = (
                        ["ssh", op_server, "--"]
                        if "op_server" in locals() and op_server
                        else []
                    )
                    docker_base_cmd = (
                        ["docker", "exec", container_name] if container_name else []
                    )
                    if ssh_base_cmd and docker_base_cmd:
                        rm_command = (
                            ssh_base_cmd
                            + docker_base_cmd
                            + ["rm", "-f", temp_file_path]
                        )
                        logger.debug(
                            f"Executing final rm command: {' '.join(rm_command)}"
                        )
                        subprocess.run(
                            rm_command, check=False, capture_output=True, timeout=10
                        )
                    else:
                        logger.warning(
                            f"Skipping final removal of temporary file {temp_file_path} "
                            "due to missing server or container config."
                        )

                else:
                    logger.debug(
                        f"Skipping final removal of temporary file {temp_file_path} "
                        "as container_name was not defined."
                    )
            except Exception as final_e:
                try:
                    error_type = type(final_e)
                    error_message = str(final_e)
                except Exception as str_err:
                    error_type = "Unknown"
                    error_message = (
                        f"Failed to convert finally exception to string: {str_err}"
                    )
                logger.warning(
                    f"Failed during final removal of {temp_file_path} (Type: {error_type}): {error_message}"
                )

    def create_work_package_type_via_rails(
        self, type_data: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Create a work package type in OpenProject via Rails console.

        Args:
            type_data: Work package type definition

        Returns:
            dict: Result of the operation
        """
        type_name = type_data.get(
            "openproject_name", type_data.get("jira_name", "Unnamed Type")
        )
        type_color = type_data.get("color", "#0086B2")
        position = 1

        type_name = type_name.replace('"', '\\"')

        is_default = str(type_data.get("is_default", False)).lower()
        is_milestone = str(type_data.get("is_milestone", False)).lower()

        # Header section with Python f-string variables
        header_script = f"""
        # Type configuration variables
        type_name = "{type_name}"
        type_color = "{type_color}"
        type_position = {position}
        is_default_flag = {is_default}
        is_milestone_flag = {is_milestone}
        """

        # Main Ruby section without f-strings
        main_script = """
        begin
          type = Type.new(
            name: type_name,
            color: Color.new(hexcode: type_color),
            position: type_position,
            is_default: is_default_flag,
            is_milestone: is_milestone_flag
          )

          if type.save
            {id: type.id, name: type.name, status: "success"}
          else
            {status: "error", errors: type.errors.full_messages}
          end
        rescue => e
          {status: "error", message: e.message}
        end
        """

        # Combine the scripts
        command = header_script + main_script

        result = self.rails_console.execute(command)

        if result["status"] == "error":
            logger.error(
                f"Error creating work package type '{type_name}': {result.get('error', 'Unknown error')}"
            )
        elif result["status"] == "success":
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
        if not self.issue_type_mapping:
            self.create_issue_type_mapping()

        if not self.connect_to_rails_console(window, pane):
            return False

        existing_types = self.check_existing_work_package_types()
        existing_names = [type.get("name") for type in existing_types]

        results = []
        success_count = 0
        error_count = 0
        skipped_count = 0

        types_to_create = {
            name: type_info
            for name, type_info in self.issue_type_mapping.items()
            if type_info.get("openproject_id") is None
        }

        logger.info(
            f"Found {len(types_to_create)} work package types to create via Rails"
        )

        with ProgressTracker(
            description="Migrating work package types via Rails",
            total=len(types_to_create),
            log_title="Work Package Types Rails Migration Results",
        ) as tracker:
            for type_name, type_data in types_to_create.items():
                op_type_name = type_data.get("openproject_name", type_name)
                tracker.update_description(f"Migrating type: {op_type_name[:20]}...")

                if op_type_name in existing_names:
                    tracker.add_log_item(f"{op_type_name}: Skipped (already exists)")
                    skipped_count += 1
                    tracker.increment()
                    results.append(
                        {
                            "name": op_type_name,
                            "status": "skipped",
                            "message": "Already exists",
                        }
                    )
                    existing_id = next(
                        (
                            t["id"]
                            for t in existing_types
                            if t.get("name") == op_type_name
                        ),
                        None,
                    )
                    if existing_id:
                        jira_id = type_data.get("jira_id")
                        if jira_id:
                            original_jira_name = next(
                                (
                                    name
                                    for name, data in self.issue_type_mapping.items()
                                    if data.get("jira_id") == jira_id
                                ),
                                None,
                            )
                            if (
                                original_jira_name
                                and original_jira_name in self.issue_type_mapping
                            ):
                                self.issue_type_mapping[original_jira_name][
                                    "openproject_id"
                                ] = existing_id
                                logger.debug(
                                    f"Updated mapping for existing type '{op_type_name}' with ID {existing_id}"
                                )
                            else:
                                logger.warning(
                                    f"Could not find mapping entry for Jira ID {jira_id} "
                                    f"to update existing type '{op_type_name}'."
                                )
                        else:
                            logger.warning(
                                f"Could not find Jira ID for existing type '{op_type_name}' "
                                "to update mapping."
                            )
                    continue

                result = self.create_work_package_type_via_rails(type_data)

                if result["status"] == "success":
                    tracker.add_log_item(f"{op_type_name}: Created successfully")
                    success_count += 1
                    results.append(
                        {
                            "name": op_type_name,
                            "status": "success",
                            "message": "Created successfully",
                        }
                    )

                    if "id" in result:
                        jira_id = type_data.get("jira_id")
                        original_jira_name = next(
                            (
                                name
                                for name, data in self.issue_type_mapping.items()
                                if data.get("jira_id") == jira_id
                            ),
                            None,
                        )
                        if (
                            original_jira_name
                            and original_jira_name in self.issue_type_mapping
                        ):
                            self.issue_type_mapping[original_jira_name][
                                "openproject_id"
                            ] = result["id"]
                        else:
                            logger.warning(
                                f"Could not update mapping for newly created type {op_type_name} "
                                f"(Jira ID: {jira_id}). Mapping key missing."
                            )
                else:
                    error_message = result.get("error", "Unknown error")
                    tracker.add_log_item(f"{op_type_name}: Error - {error_message}")
                    error_count += 1
                    results.append(
                        {
                            "name": op_type_name,
                            "status": "error",
                            "message": error_message,
                        }
                    )

                tracker.increment()

        self._save_to_json(self.issue_type_mapping, "issue_type_mapping.json")

        final_mapping = {}
        for type_name, mapping in self.issue_type_mapping.items():
            jira_id = mapping["jira_id"]
            op_id = mapping["openproject_id"]

            if op_id:
                final_mapping[jira_id] = op_id

        self._save_to_json(final_mapping, "issue_type_id_mapping.json")

        logger.info("\nWork Package Types Migration Summary:")
        logger.info(f"Total types processed: {len(types_to_create)}")
        logger.info(f"Successfully created: {success_count}")
        logger.info(f"Skipped (already exists): {skipped_count}")
        logger.info(f"Failed: {error_count}")

        results_file = os.path.join(self.data_dir, "issue_type_migration_results.json")
        with open(results_file, "w") as f:
            json.dump(results, f, indent=2)
        logger.info(f"Saved migration results to {results_file}")

        return error_count == 0

    def migrate_issue_types(self) -> dict[str, Any]:
        """
        Prepare for migrating issue types from Jira to OpenProject.

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

        if op_types_to_create:
            logger.info(
                f"Need to create {len(op_types_to_create)} work package types in OpenProject via Rails console"
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
        """
        Analyze the issue type mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        if not self.issue_type_mapping:
            self.create_issue_type_mapping()

        analysis = {
            "total_jira_types": len(self.issue_type_mapping),
            "matched_op_types": sum(
                1
                for mapping in self.issue_type_mapping.values()
                if mapping.get("openproject_id") is not None
            ),
            "types_to_create": sum(
                1
                for mapping in self.issue_type_mapping.values()
                if mapping.get("openproject_id") is None
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
            f"- Matched to OpenProject types: {analysis['matched_op_types']} ({analysis['match_percentage']:.1f}%)"
        )
        logger.info(
            f"- Need creation in OpenProject: {analysis['types_to_create']} ({analysis['create_percentage']:.1f}%)"
        )

        if analysis["types_to_create"] > 0:
            logger.warning(
                f"Action required: {analysis['types_to_create']} work package types "
                "need creation via Rails console (direct or script). "
                "Details in issue_type_analysis.json"
            )

        return analysis

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
                with open(filepath) as f:
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

    def update_mapping_file(self, force: bool = False) -> bool:
        """
        Update the issue type mapping file with IDs from OpenProject.

        This method is useful when work package types were created manually via a Ruby script
        execution and the mapping file needs to be updated with the created type IDs.

        Args:
            force: If True, force refresh of OpenProject work package types

        Returns:
            True if mapping was updated successfully, False otherwise
        """
        logger.info("Updating issue type mapping file with IDs from OpenProject...")

        # Get all work package types from OpenProject
        op_types = self.op_client.get_work_package_types(force_refresh=force)
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
            if (
                type_data.get("openproject_id")
                and type_data.get("matched_by") != "default_mapping_to_create"
            ):
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
                    "created"
                    if type_data.get("matched_by") == "default_mapping_to_create"
                    else "exact_match"
                )
                updated_count += 1
            else:
                logger.warning(
                    f"Work package type '{op_name}' not found in OpenProject"
                )
                missing_count += 1

        # Save the updated mapping
        if updated_count > 0:
            self._save_to_json(self.issue_type_mapping, "issue_type_mapping.json")
            logger.success(f"Updated mapping for {updated_count} work package types")
        else:
            logger.info("No mapping updates needed")

        # Print summary
        logger.info(
            f"Summary: Updated {updated_count}, Already mapped {already_mapped_count}, Missing {missing_count}"
        )

        return updated_count > 0 or already_mapped_count > 0

    def run(
        self, dry_run: bool = False, force: bool = False, mappings=None
    ) -> dict[str, Any]:
        """
        Run the issue type migration.

        Args:
            dry_run: If True, don't actually create or update anything
            force: If True, force re-extraction of data
            mappings: Optional mappings object (not used in this migration)

        Returns:
            Dictionary with migration results
        """
        logger.info("Starting issue type migration...")

        # 1. Extract and process issue types
        self.extract_jira_issue_types(force=force)
        self.extract_openproject_work_package_types(force=force)
        self.create_issue_type_mapping(force=force)

        # 2. Normalize issue types (map sub-types to normal types)
        self.normalize_issue_types()

        # 3. Migrate issue types (create if needed)
        if dry_run:
            logger.info("DRY RUN: Skipping actual issue type migration")
            results = ComponentResult(
                success=True,
                total_types=len(self.issue_type_mapping),
                matched_types=sum(
                    1
                    for t in self.issue_type_mapping.values()
                    if t.get("openproject_id") is not None
                ),
                normalized_types=sum(
                    1
                    for t in self.issue_type_mapping.values()
                    if t.get("matched_by", "").startswith("normalized_to_")
                    or t.get("matched_by") == "fallback_to_task"
                ),
                created_types=0,
                dry_run=True,
            )
        else:
            # Migrate issue types
            migration_results = self.migrate_issue_types()

            results = ComponentResult(
                success=self.update_mapping_file(force=force),
                total_types=migration_results.get("total", 0),
                matched_types=migration_results.get("matched", 0),
                normalized_types=migration_results.get("normalized", 0),
                created_types=migration_results.get("created", 0),
                existing_types=migration_results.get("unchanged", 0),
                failed_types=migration_results.get("failed", 0),
                message=migration_results.get("message", ""),
            )

        return results
