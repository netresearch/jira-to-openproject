#!/usr/bin/env python3
"""Issue type migration module for Jira to OpenProject migration.
Handles the migration of issue types from Jira to OpenProject work package types.
"""

import json
import re
import subprocess
import time
from pathlib import Path
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
        self.jira_issue_types = self._load_from_json(Path("jira_issue_types.json"), [])
        self.op_work_package_types = self._load_from_json(Path("op_work_package_types.json"), [])
        self.issue_type_mapping = self._load_from_json(Path("issue_type_mapping.json"), {})
        self.issue_type_id_mapping = self._load_from_json(Path("issue_type_id_mapping.json"), {})
        self.logger.info("Loaded %s Jira issue types", len(self.jira_issue_types))
        self.logger.info("Loaded %s OpenProject work package types", len(self.op_work_package_types))
        self.logger.info("Loaded %s issue type mappings", len(self.issue_type_mapping))
        self.logger.info("Loaded %s issue type ID mappings", len(self.issue_type_id_mapping))

    def extract_jira_issue_types(self) -> list[dict[str, Any]]:
        """Extract issue types from Jira.

        Returns:
            List of Jira issue type dictionaries

        """
        issue_types_file = self.data_dir / "jira_issue_types.json"

        if issue_types_file.exists() and not config.migration_config.get("force", False):
            self.logger.info("Jira issue types data already exists, skipping extraction (use --force to override)")
            with issue_types_file.open() as f:
                self.jira_issue_types = json.load(f)
            return self.jira_issue_types

        self.logger.info("Extracting issue types from Jira...")

        try:
            issue_types = self.jira_client.get_issue_types()

            self.logger.info("Extracted %s issue types from Jira", len(issue_types))

            self.jira_issue_types = issue_types
            self._save_to_json(issue_types, Path("jira_issue_types.json"))

            return issue_types
        except Exception as e:
            self.logger.exception("Failed to get issue types from Jira: %s", e)
            return []

    def extract_openproject_work_package_types(self) -> list[dict[str, Any]]:
        """Extract work package types from OpenProject.

        Returns:
            List of OpenProject work package type dictionaries

        """
        work_package_types_file = self.data_dir / "openproject_work_package_types.json"

        if work_package_types_file.exists() and not config.migration_config.get("force", False):
            self.logger.info(
                "OpenProject work package types data already exists, skipping extraction (use --force to override)",
            )
            with work_package_types_file.open() as f:
                self.op_work_package_types = json.load(f)
            return self.op_work_package_types

        self.logger.info("Extracting work package types from OpenProject...")

        try:
            self.op_work_package_types = self.op_client.get_work_package_types()
        except Exception as e:
            self.logger.warning("Failed to get work package types from OpenProject: %s", e)
            self.logger.warning("Using an empty list of work package types for OpenProject")
            self.op_work_package_types = []

        self.logger.info("Extracted %s work package types from OpenProject", len(self.op_work_package_types))

        self._save_to_json(self.op_work_package_types, Path("openproject_work_package_types.json"))

        return self.op_work_package_types

    def create_issue_type_mapping(self) -> dict[str, Any]:
        """Create a mapping between Jira issue types and OpenProject work package types.

        Returns:
            Dictionary mapping Jira issue types to OpenProject work package types

        """
        mapping_file = self.data_dir / "issue_type_mapping_template.json"
        if mapping_file.exists() and not config.migration_config.get("force", False):
            self.logger.info("Issue type mapping already exists, loading from file (use --force to recreate)")
            with mapping_file.open() as f:
                self.issue_type_mapping = json.load(f)
            return self.issue_type_mapping

        self.logger.info("Creating issue type mapping...")

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
        self._save_to_json(mapping, Path("issue_type_mapping_template.json"))

        total_types = len(mapping)
        matched_types = sum(1 for type_data in mapping.values() if type_data["openproject_id"] is not None)
        to_create_types = sum(1 for type_data in mapping.values() if type_data["openproject_id"] is None)
        match_percentage = (matched_types / total_types) * 100 if total_types > 0 else 0

        self.logger.info("Issue type mapping created for %s types", total_types)
        self.logger.info("Successfully matched %s types (%.1f%%)", matched_types, match_percentage)
        self.logger.info("Need to create %s new work package types in OpenProject", to_create_types)

        return mapping

    def normalize_issue_types(self) -> dict[str, Any]:
        """Normalize issue types by mapping sub-types to their corresponding normal types.

        This method identifies issue types starting with "Sub:" or "Sub-" and maps them
        to their corresponding "normal" issue types. This reduces the number of work
        package types to create in OpenProject and simplifies the mapping.

        Returns:
            Updated issue type mapping dictionary

        """
        self.logger.info("Normalizing issue types...")

        if not self.issue_type_mapping:
            self.create_issue_type_mapping()

        normalized_mapping = self.issue_type_mapping.copy()
        normalizations_applied = 0

        # Find all possible "normal" types (non-subtypes)
        normal_types = {}
        for jira_type_name, type_data in self.issue_type_mapping.items():
            # Skip if this is a sub-type
            if jira_type_name.startswith(("Sub:", "Sub-")):
                continue

            # Store the normal type data by name
            normal_types[jira_type_name] = type_data

        # Process all sub-types and map them to normal types
        for jira_type_name, type_data in list(normalized_mapping.items()):
            if not (jira_type_name.startswith(("Sub:", "Sub-"))):
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
                self.logger.info("Normalized '%s' to '%s'", jira_type_name, base_type_name)
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
                self.logger.info("Could not find matching normal type for '%s', mapping to 'Task'", jira_type_name)

        # Save the normalized mapping
        self.issue_type_mapping = normalized_mapping
        self._save_to_json(normalized_mapping, Path("issue_type_mapping_normalized.json"))

        self.logger.info("Issue type normalization complete: %s types normalized", normalizations_applied)

        # Update stats after normalization
        total_types = len(normalized_mapping)
        matched_types = sum(1 for type_data in normalized_mapping.values() if type_data["openproject_id"] is not None)
        to_create_types = sum(1 for type_data in normalized_mapping.values() if type_data["openproject_id"] is None)
        match_percentage = (matched_types / total_types) * 100 if total_types > 0 else 0

        self.logger.info("After normalization: %s types total", total_types)
        self.logger.info("Successfully matched %s types (%.1f%%)", matched_types, match_percentage)
        self.logger.info("Need to create %s new work package types in OpenProject", to_create_types)

        return normalized_mapping

    def check_existing_work_package_types(self) -> list[dict[str, Any]]:
        """Check existing work package types in OpenProject.

        This method checks existing work package types in OpenProject by directly parsing
        the JSON output from a file created via Rails console.

        Returns:
            List of existing work package types

        """
        self.logger.info("Checking existing work package types in OpenProject via Rails...")

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
            self.logger.info("Executing Rails command to write work package types to %s...", temp_file_path)
            write_result = self.op_client.execute_query(command)
            self.logger.debug(f"Result from work package type write: type={type(write_result)}, value={write_result}")
            if not isinstance(write_result, dict):
                msg = (
                    "Failed to run work package type write: Invalid response from OpenProject "
                    f"(type={type(write_result)}, value={write_result})"
                )
                self.logger.error(msg)
                return []

            if (
                write_result.get("status") == "success"
                and write_result.get("output")
                and "RAILS_EXEC_ERROR:" in write_result["output"]
            ):
                self.logger.error("Rails command reported an error during execution: %s", write_result["output"])
                return []
            if write_result.get("status") != "success":
                error_msg = write_result.get("error", "Unknown error executing Rails command for file write")
                self.logger.error("Failed to execute Rails command to write JSON file: %s", error_msg)
                return []

            self.logger.info("Rails command executed successfully. Checking existence of %s...", temp_file_path)

            time.sleep(0.5)

            container_name: str = config.openproject_config.get("container", None)
            op_server: str = config.openproject_config.get("server", None)

            if not op_server:
                self.logger.error(
                    "OpenProject server hostname is not configured "
                    "(J2O_OPENPROJECT_SERVER). Cannot run remote docker commands.",
                )
                return []

            ssh_base_cmd = ["ssh", op_server, "--"]
            docker_base_cmd = ["docker", "exec", container_name]

            ls_command = ssh_base_cmd + docker_base_cmd + ["ls", temp_file_path]
            self.logger.debug("Executing command: %s", " ".join(ls_command))
            try:
                ls_result = subprocess.run(ls_command, capture_output=True, text=True, check=False)
                if ls_result.returncode != 0:
                    error_details = ls_result.stderr.strip()
                    self.logger.error(
                        f"File {temp_file_path} check failed (exit code {ls_result.returncode}). "
                        f"ls stderr: {error_details}; stdout: {ls_result.stdout}",
                    )
                    return []
                self.logger.info("File %s confirmed to exist in container.", temp_file_path)
            except subprocess.SubprocessError as e:
                self.logger.exception("Error running docker exec ls command: %s", e)
                return []

            self.logger.info("Reading %s using ssh + docker exec...", temp_file_path)
            cat_command = ssh_base_cmd + docker_base_cmd + ["cat", temp_file_path]
            self.logger.debug("Executing command: %s", " ".join(cat_command))
            read_result = subprocess.run(cat_command, capture_output=True, text=True, check=False)

            if read_result.returncode != 0:
                self.logger.error("Failed to read %s via docker exec: %s", temp_file_path, read_result.stderr)
                return []

            json_content = read_result.stdout.strip()
            self.logger.debug("Content read from %s:\\n%s", temp_file_path, json_content)

            try:
                types: list[dict[str, Any]] = json.loads(json_content)
                self.logger.info("Successfully parsed %s work package types from file", len(types))
                return types
            except json.JSONDecodeError as e:
                self.logger.exception("Could not parse work package types JSON read from file: %s", e)
                self.logger.debug("Invalid JSON content: %s", json_content)
                return []

        except subprocess.SubprocessError as e:
            self.logger.exception("Error running docker exec command: %s", e)
            return []
        except Exception as e:
            self.logger.exception("Unexpected error during work package type retrieval: %s", e)
            return []
        finally:
            try:
                if "container_name" in locals() and container_name:
                    self.logger.debug("Attempting final removal of remote temporary file %s...", temp_file_path)
                    ssh_base_cmd = ["ssh", op_server, "--"] if "op_server" in locals() and op_server else []
                    docker_base_cmd = ["docker", "exec", container_name] if container_name else []
                    if ssh_base_cmd and docker_base_cmd:
                        rm_command = ssh_base_cmd + docker_base_cmd + ["rm", "-f", temp_file_path]
                        self.logger.debug("Executing final rm command: %s", " ".join(rm_command))
                        subprocess.run(rm_command, check=False, capture_output=True, timeout=10)
                    else:
                        self.logger.warning(
                            f"Skipping final removal of temporary file {temp_file_path} "
                            "due to missing server or container config.",
                        )

                else:
                    self.logger.debug(
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
                self.logger.warning(
                    "Failed during final removal of %s (Type: %s): %s",
                    temp_file_path,
                    error_type,
                    error_message,
                )

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

        self.logger.info("Creating work package type '%s' via Rails console...", type_name)

        # Check if the type already exists to avoid duplicates
        check_command = f'existing_type = Type.where("name ilike ?", "{type_name}").first'
        check_result = self.op_client.execute_query(check_command)
        self.logger.debug(f"check_result type: {type(check_result)}, value: {check_result}")
        if not isinstance(check_result, dict):
            self.logger.error(f"Expected dict from execute_query, got {type(check_result)}: {check_result}")
            return {"status": "error", "error": "Invalid result type from execute_query (check_result)"}

        exists_command = "existing_type.present?"
        exists_result = self.op_client.execute_query(exists_command)
        self.logger.debug(f"exists_result type: {type(exists_result)}, value: {exists_result}")
        if not isinstance(exists_result, dict):
            self.logger.error(f"Expected dict from execute_query, got {type(exists_result)}: {exists_result}")
            return {"status": "error", "error": "Invalid result type from execute_query (exists_result)"}

        if exists_result["status"] == "success" and "true" in exists_result["output"]:
            self.logger.info("Work package type '%s' already exists, retrieving ID", type_name)
            id_command = "existing_type.id"
            id_result = self.op_client.execute_query(id_command)
            self.logger.debug(f"id_result type: {type(id_result)}, value: {id_result}")
            if not isinstance(id_result, dict):
                self.logger.error(f"Expected dict from execute_query, got {type(id_result)}: {id_result}")
                return {"status": "error", "error": "Invalid result type from execute_query (id_result)"}

            if id_result["status"] == "success" and id_result["output"].strip().isdigit():
                type_id = int(id_result["output"].strip())
                return {
                    "status": "success",
                    "message": "Work package type already exists",
                    "id": type_id,
                }
            self.logger.warning("Failed to retrieve ID for existing type '%s'", type_name)

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

        result = self.op_client.execute_query(command)
        self.logger.debug(f"result type: {type(result)}, value: {result}")
        if not isinstance(result, dict):
            self.logger.error(f"Expected dict from execute_query, got {type(result)}: {result}")
            return {"status": "error", "error": "Invalid result type from execute_query (result)"}

        # Parse the result
        output = result.get("output", "")

        if "status: :error" in output:
            error_msg = "Unknown error"
            error_match = re.search(r'message: "([^"]+)"', output)
            if error_match:
                error_msg = error_match.group(1)

            self.logger.error("Error creating work package type '%s': %s", type_name, error_msg)
            return {"status": "error", "error": error_msg}

        # Extract ID if successful
        id_match = re.search(r"id: (\d+)", output)
        if id_match:
            type_id = int(id_match.group(1))
            self.logger.info("Created work package type '%s' with ID %s", type_name, type_id)

            # Verify the type exists by querying it
            verify_command = f"Type.find({type_id}).present? rescue false"
            verify_result = self.op_client.execute_query(verify_command)
            self.logger.debug(f"verify_result type: {type(verify_result)}, value: {verify_result}")
            if not isinstance(verify_result, dict):
                self.logger.error(f"Expected dict from execute_query, got {type(verify_result)}: {verify_result}")
                return {"status": "error", "error": "Invalid result type from execute_query (verify_result)"}

            if verify_result["status"] == "success" and "true" in verify_result["output"]:
                self.logger.info("Verified work package type '%s' with ID %s exists", type_name, type_id)
                return {"status": "success", "id": type_id, "name": type_name}
            self.logger.warning(
                "Created type but failed verification: %s",
                verify_result.get("output", "Unknown error"),
            )

        self.logger.warning("Failed to parse type ID from Rails response: %s", output)

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
                        self.logger.info("Work package type '%s' already exists, updating mapping", op_type_name)
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
            self.logger.info("No new work package types to create")
            return True

        self.logger.info("Preparing to create %s work package types in bulk", len(types_to_create))

        # Create a temporary JSON file to store the types
        temp_file = self.data_dir / "work_package_types_to_create.json"
        with temp_file.open("w") as f:
            json.dump(types_to_create, f, indent=2)

        self.logger.info("Saved %s work package types to create to %s", len(types_to_create), temp_file)

        # Define container paths
        container_temp_path = "/tmp/work_package_types_to_create.json"
        container_results_path = "/tmp/work_package_types_created.json"

        # Create a temporary file to store the results
        results_file = self.data_dir / "work_package_types_created.json"

        # Transfer the file to the container
        if not self.op_client.transfer_file_to_container(temp_file, container_temp_path):
            self.logger.error(
                "Failed to transfer types file to container from %s to %s",
                temp_file,
                container_temp_path,
            )
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
        self.logger.info("Executing bulk creation of work package types via Rails console...")
        result = self.op_client.execute_query(bulk_create_script)
        self.logger.debug(f"bulk_create_script result type: {type(result)}, value: {result}")
        if not isinstance(result, dict):
            self.logger.error(f"Expected dict from execute_query, got {type(result)}: {result}")
            return False

        if result["status"] != "success":
            self.logger.error("Failed to execute bulk creation script: %s", result.get("error", "Unknown error"))
            return False

        # Check for specific error markers in the output
        output = result.get("output", "")
        if output is None:
            self.logger.error("No output returned from Rails console")
            return False

        if "BULK_CREATE_FAILED" in output:
            error_match = re.search(r"BULK_CREATE_FAILED: (.*)", output)
            error_message = error_match.group(1) if error_match else "Unknown error"
            self.logger.error("Bulk creation failed: %s", error_message)
            return False

        # Parse results
        try:
            # Try to get the result file from the container
            if not self.op_client.transfer_file_from_container(container_results_path, results_file):
                self.logger.error("Failed to retrieve results file from container: %s", container_results_path)
                # Dump the entire output for debugging
                self.logger.debug("Rails console output: %s", output)
                return False

            if not results_file.exists():
                self.logger.error("Results file %s was not created", results_file)
                # Dump the entire output for debugging
                self.logger.debug("Rails console output: %s", output)
                return False

            with results_file.open() as f:
                result_content = f.read()
                if not result_content.strip():
                    self.logger.error("Results file %s is empty", results_file)
                    self.logger.debug("Rails console output: %s", output)
                    return False

                try:
                    creation_results = json.loads(result_content)
                except json.JSONDecodeError as e:
                    self.logger.exception("Failed to parse results file content: %s", e)
                    self.logger.debug("Results file content: %s", result_content)
                    self.logger.debug("Rails console output: %s", output)
                    return False

            created_count = len(creation_results.get("created", []))
            error_count = len(creation_results.get("errors", []))

            self.logger.info("Bulk creation completed: %s types created, %s errors", created_count, error_count)

            # Update mapping with created types
            for created_type in creation_results.get("created", []):
                jira_type_name = created_type.get("jira_type_name")
                if jira_type_name and jira_type_name in self.issue_type_mapping:
                    self.issue_type_mapping[jira_type_name]["openproject_id"] = created_type.get("id")
                    self.issue_type_mapping[jira_type_name]["matched_by"] = "created"
                    self.logger.info("Updated mapping for '%s' with ID %s", jira_type_name, created_type.get("id"))

            # Save updated mapping
            self._save_to_json(self.issue_type_mapping, Path("issue_type_mapping.json"))

            # Update ID mapping
            final_mapping = {}
            for type_name, mapping in self.issue_type_mapping.items():
                jira_id = mapping["jira_id"]
                op_id = mapping["openproject_id"]

                if op_id:
                    final_mapping[jira_id] = op_id

            self._save_to_json(final_mapping, Path("issue_type_id_mapping.json"))

            # Report errors
            if error_count > 0:
                self.logger.warning("Some work package types failed to create:")
                for error in creation_results.get("errors", []):
                    self.logger.warning("  - %s: %s", error.get("name"), error.get("error"))

            return error_count == 0

        except Exception as e:
            self.logger.exception("Error processing creation results: %s", e)
            import traceback

            self.logger.debug(traceback.format_exc())
            return False

    def migrate_issue_types(self) -> dict[str, Any]:
        """Prepare for migrating issue types from Jira to OpenProject.

        Returns:
            Updated mapping with analysis information

        """
        self.logger.info("Starting issue type migration preparation...")

        if not self.jira_issue_types:
            self.extract_jira_issue_types()

        if not self.op_work_package_types:
            self.extract_openproject_work_package_types()

        if not self.issue_type_mapping:
            self.create_issue_type_mapping()

        self.normalize_issue_types()

        op_types_to_create = {}
        for mapping in self.issue_type_mapping.values():
            if mapping["openproject_id"] is None:
                op_type_name = mapping["openproject_name"]
                if op_type_name not in op_types_to_create:
                    op_types_to_create[op_type_name] = mapping

        total_types_needing_creation = sum(
            1 for mapping in self.issue_type_mapping.values() if mapping["openproject_id"] is None
        )

        if op_types_to_create:
            self.logger.info(
                f"Need to create {len(op_types_to_create)} unique work package types in OpenProject via Rails console",
            )
            self.logger.info(
                f"(This will map to {total_types_needing_creation} total Jira issue types after deduplication)",
            )
        else:
            self.logger.info("No new work package types need to be created in OpenProject")

        final_mapping = {}
        for mapping in self.issue_type_mapping.values():
            jira_id = mapping["jira_id"]
            op_id = mapping["openproject_id"]

            if op_id:
                final_mapping[jira_id] = op_id

        self._save_to_json(final_mapping, Path("issue_type_id_mapping.json"))

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

        self._save_to_json(analysis, Path("issue_type_analysis.json"))

        self.logger.info("\nIssue Type Mapping Analysis:")
        self.logger.info("Total Jira issue types: %s", total)
        self.logger.info(
            f"- Matched to OpenProject types: {analysis['matched_op_types']} ({analysis['match_percentage']:.1f}%)",
        )
        self.logger.info(
            f"- Need creation in OpenProject: {analysis['types_to_create']} ({analysis['create_percentage']:.1f}%)",
        )

        if analysis["types_to_create"] > 0:
            self.logger.info(
                f"Action required: {analysis['types_to_create']} work package types "
                "need creation via Rails console (direct or script). "
                "Details in issue_type_analysis.json",
            )

        return analysis

    def _load_from_json(self, filename: Path, default: Any = None) -> Any:
        """Load data from a JSON file in the data directory.

        Args:
            filename: Name of the JSON file
            default: Default value to return if file doesn't exist

        Returns:
            Loaded JSON data or default value

        """
        filepath = self.data_dir / filename
        if filepath.exists():
            try:
                with filepath.open() as f:
                    return json.load(f)
            except Exception as e:
                self.logger.warning("Failed to load %s: %s", filepath, e)
                return default
        return default

    def update_mapping_file(self) -> bool:
        """Update the issue type mapping file with IDs from OpenProject.

        This method is useful when work package types were created manually via a Ruby script
        execution and the mapping file needs to be updated with the created type IDs.

        Returns:
            True if mapping was updated successfully, False otherwise

        """
        self.logger.info("Updating issue type mapping file with IDs from OpenProject...")

        # Get all work package types from OpenProject
        op_types = self.op_client.get_work_package_types()
        if not op_types:
            self.logger.error("Failed to retrieve work package types from OpenProject")
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
                self.logger.info("Found work package type '%s' with ID %s", op_name, op_id)
                self.issue_type_mapping[jira_type_name]["openproject_id"] = op_id
                self.issue_type_mapping[jira_type_name]["matched_by"] = (
                    "created" if type_data.get("matched_by") == "default_mapping_to_create" else "exact_match"
                )
                updated_count += 1
            else:
                self.logger.warning("Work package type '%s' not found in OpenProject", op_name)
                missing_count += 1

        # Save the updated mapping
        if updated_count > 0:
            self._save_to_json(self.issue_type_mapping, Path("issue_type_mapping.json"))
            self.logger.info("Updated mapping for %s work package types", updated_count)
        else:
            self.logger.info("No mapping updates needed")

        # Print summary
        self.logger.info(
            "Summary: Updated %s, Already mapped %s, Missing %s",
            updated_count,
            already_mapped_count,
            missing_count,
        )

        # Return success only if we have more matches than misses
        if missing_count > updated_count and missing_count > 0:
            self.logger.warning("Migration partially failed: %s types were not found in OpenProject", missing_count)
            return False

        return updated_count > 0 or already_mapped_count > 0

    def run(self) -> ComponentResult:
        """Run the issue type migration.

        Returns:
            Dictionary with migration results

        """
        self.logger.info("Starting issue type migration...")

        # 1. Extract and process issue types
        self.extract_jira_issue_types()
        self.extract_openproject_work_package_types()
        self.create_issue_type_mapping()

        # 2. Normalize issue types (map sub-types to normal types)
        self.normalize_issue_types()

        # 3. Migrate issue types (create if needed)
        if config.migration_config.get("dry_run", False):
            self.logger.info("DRY RUN: Skipping actual issue type migration")
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
                self.logger.info("Attempting to create %s work package types via Rails console", types_to_create)
                rails_migration_success = self.migrate_issue_types_via_rails()
                if rails_migration_success:
                    self.logger.info("Successfully created work package types via Rails console")
                else:
                    self.logger.error("Failed to create some work package types via Rails console")

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
                    self.logger.info("Successfully updated issue type mappings with newly created types")
                else:
                    self.logger.warning(
                        "Updated issue type mappings but %s types still need creation",
                        final_types_to_create,
                    )

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
