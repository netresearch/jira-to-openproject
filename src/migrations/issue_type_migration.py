#!/usr/bin/env python3
"""Issue type migration module for Jira to OpenProject migration.
Handles mapping and creation of issue types from Jira to work package types in OpenProject.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src import config
from src.display import configure_logging, console
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult, MigrationError

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient

# Prefer shared logger; fall back if unavailable
try:
    from src.config import logger  # type: ignore
except Exception:
    logger = configure_logging("INFO", None)


@register_entity_types("issue_types", "work_package_types")
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

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities from Jira for a specific type.

        This method enables idempotent workflow caching by providing a standard
        interface for entity retrieval. Called by run_with_change_detection() to fetch data
        with automatic thread-safe caching.

        Args:
            entity_type: The type of entities to retrieve (e.g., "issue_types", "work_package_types")

        Returns:
            List of entity dictionaries from Jira API

        Raises:
            ValueError: If entity_type is not supported by this migration

        """
        # Check if this is the entity type we handle
        if entity_type in ("issue_types", "work_package_types"):
            return self.jira_client.get_issue_types()

        # Raise error for unsupported types
        msg = (
            f"IssueTypeMigration does not support entity type: {entity_type}. "
            f"Supported types: ['issue_types', 'work_package_types']"
        )
        raise ValueError(msg)

    def _load_data(self) -> None:
        """Load existing data from JSON files."""
        self.jira_issue_types = self._load_from_json(Path("jira_issue_types.json"), [])
        self.op_work_package_types = self._load_from_json(
            Path("op_work_package_types.json"),
            [],
        )
        # Load via mapping controller to keep cache authoritative
        from src import config as _cfg
        self.issue_type_mapping = _cfg.mappings.get_mapping("issue_type") or {}
        from src import config as _cfg
        self.issue_type_id_mapping = _cfg.mappings.get_mapping("issue_type_id") or {}
        self.logger.info("Loaded %s Jira issue types", len(self.jira_issue_types))
        self.logger.info(
            "Loaded %s OpenProject work package types",
            len(self.op_work_package_types),
        )
        self.logger.info("Loaded %s issue type mappings", len(self.issue_type_mapping))
        self.logger.info(
            "Loaded %s issue type ID mappings",
            len(self.issue_type_id_mapping),
        )

    def extract_jira_issue_types(self) -> list[dict[str, Any]]:
        """Extract issue types from Jira.

        Returns:
            List of Jira issue type dictionaries

        """
        issue_types_file = self.data_dir / "jira_issue_types.json"

        if issue_types_file.exists() and not config.migration_config.get(
            "force",
            False,
        ):
            self.logger.info(
                "Jira issue types data already exists, skipping extraction (use --force to override)",
            )
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
            msg = f"Failed to get issue types from Jira: {e}"
            self.logger.exception(msg)
            raise MigrationError(msg) from e

    def extract_openproject_work_package_types(self) -> list[dict[str, Any]]:
        """Extract work package types from OpenProject.

        Returns:
            List of OpenProject work package type dictionaries

        """
        work_package_types_file = self.data_dir / "openproject_work_package_types.json"

        if work_package_types_file.exists() and not config.migration_config.get(
            "force",
            False,
        ):
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
            # Honor stop-on-error: fail hard so orchestrator halts and we can fix root cause
            msg = f"Failed to get work package types from OpenProject: {e}"
            self.logger.error(msg)
            if config.migration_config.get("stop_on_error", False):
                raise MigrationError(msg) from e
            # Otherwise fallback to empty list (legacy behavior)
            self.logger.warning("Using an empty list of work package types for OpenProject")
            self.op_work_package_types = []

        self.logger.info(
            "Extracted %s work package types from OpenProject",
            len(self.op_work_package_types),
        )

        self._save_to_json(
            self.op_work_package_types,
            Path("openproject_work_package_types.json"),
        )

        return self.op_work_package_types

    def create_issue_type_mapping(self) -> dict[str, Any]:
        """Create a mapping between Jira issue types and OpenProject work package types.

        Returns:
            Dictionary mapping Jira issue types to OpenProject work package types

        """
        mapping_file = self.data_dir / "issue_type_mapping_template.json"
        if mapping_file.exists() and not config.migration_config.get("force", False):
            self.logger.info(
                "Issue type mapping already exists, loading from file (use --force to recreate)",
            )
            with mapping_file.open() as f:
                self.issue_type_mapping = json.load(f)
            return self.issue_type_mapping

        self.logger.info("Creating issue type mapping...")

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

            default_mapping: dict[str, str] | None = self.default_mappings.get(
                jira_type_name,
            )
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
        matched_types = sum(
            1
            for type_data in mapping.values()
            if type_data["openproject_id"] is not None
        )
        to_create_types = sum(
            1 for type_data in mapping.values() if type_data["openproject_id"] is None
        )
        match_percentage = (matched_types / total_types) * 100 if total_types > 0 else 0

        self.logger.info("Issue type mapping created for %s types", total_types)
        self.logger.info(
            "Successfully matched %s types (%.1f%%)",
            matched_types,
            match_percentage,
        )
        self.logger.info(
            "Need to create %s new work package types in OpenProject",
            to_create_types,
        )

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
                self.logger.info(
                    "Normalized '%s' to '%s'",
                    jira_type_name,
                    base_type_name,
                )
            else:
                # If no corresponding normal type is found, propose creating a new
                # work package type using the base type name instead of falling back to Task
                normalized_mapping[jira_type_name].update(
                    {
                        "openproject_id": None,
                        "openproject_name": base_type_name,
                        "color": type_data.get("color", "#1A67A3"),
                        "is_milestone": False,
                        # Keep the prefix so dry-run summaries still count this as a normalization
                        "matched_by": f"normalized_to_{base_type_name}",
                        "normalized_from": jira_type_name,
                        "normalized_to": base_type_name,
                    },
                )
                normalizations_applied += 1
                self.logger.info(
                    "Could not find matching normal type for '%s', proposing new type '%s'",
                    jira_type_name,
                    base_type_name,
                )

        # Save the normalized mapping
        self.issue_type_mapping = normalized_mapping
        self._save_to_json(
            normalized_mapping,
            Path("issue_type_mapping_normalized.json"),
        )

        self.logger.info(
            "Issue type normalization complete: %s types normalized",
            normalizations_applied,
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

        self.logger.info("After normalization: %s types total", total_types)
        self.logger.info(
            "Successfully matched %s types (%.1f%%)",
            matched_types,
            match_percentage,
        )
        self.logger.info(
            "Need to create %s new work package types in OpenProject",
            to_create_types,
        )

        return normalized_mapping

    def check_existing_work_package_types(self) -> list[dict[str, Any]]:
        """Check existing work package types in OpenProject.

        This method checks existing work package types in OpenProject by directly parsing
        the JSON output from a file created via Rails console.

        Returns:
            List of existing work package types

        """
        self.logger.info(
            "Checking existing work package types in OpenProject via Rails...",
        )

        try:
            types = self.op_client.get_work_package_types()
            self.logger.info("Found %s work package types", len(types))
            return types
        except Exception as e:
            msg = f"Error checking existing work package types: {e}"
            self.logger.exception(msg)
            raise MigrationError(msg) from e

    def create_work_package_type_via_rails(
        self,
        type_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a work package type in OpenProject via Rails console.

        Args:
            type_data: Dictionary with type data from Jira issue type mapping

        Returns:
            Dictionary with operation result status

        """
        type_name = type_data.get("openproject_name", "")
        type_color = type_data.get("color", "#1A67A3")
        is_milestone = type_data.get("is_milestone", False)

        self.logger.info(
            "Creating work package type '%s' via Rails console...",
            type_name,
        )

        # Check if the type already exists to avoid duplicates
        # Use parameterized query to prevent Rails injection
        safe_type_name = self.ruby_escape(type_name)
        check_command = (
            f"existing_type = Type.where(\"name ilike ?\", '{safe_type_name}').first"
        )
        check_result = self.op_client.execute_query(check_command)
        self.logger.debug(
            f"check_result type: {type(check_result)}, value: {check_result}",
        )
        if not isinstance(check_result, dict):
            self.logger.error(
                f"Expected dict from execute_query, got {type(check_result)}: {check_result}",
            )
            return {
                "status": "error",
                "error": "Invalid result type from execute_query (check_result)",
            }

        exists_command = "existing_type.present?"
        exists_result = self.op_client.execute_query(exists_command)
        self.logger.debug(
            f"exists_result type: {type(exists_result)}, value: {exists_result}",
        )
        if not isinstance(exists_result, dict):
            self.logger.error(
                f"Expected dict from execute_query, got {type(exists_result)}: {exists_result}",
            )
            return {
                "status": "error",
                "error": "Invalid result type from execute_query (exists_result)",
            }

        if exists_result["status"] == "success" and "true" in exists_result["output"]:
            self.logger.info(
                "Work package type '%s' already exists, retrieving ID",
                type_name,
            )
            id_command = "existing_type.id"
            id_result = self.op_client.execute_query(id_command)
            self.logger.debug(f"id_result type: {type(id_result)}, value: {id_result}")
            if not isinstance(id_result, dict):
                self.logger.error(
                    f"Expected dict from execute_query, got {type(id_result)}: {id_result}",
                )
                return {
                    "status": "error",
                    "error": "Invalid result type from execute_query (id_result)",
                }

            if (
                id_result["status"] == "success"
                and id_result["output"].strip().isdigit()
            ):
                type_id = int(id_result["output"].strip())
                return {
                    "status": "success",
                    "message": "Work package type already exists",
                    "id": type_id,
                }
            self.logger.warning(
                "Failed to retrieve ID for existing type '%s'",
                type_name,
            )

        # Create the type with proper escaping to prevent Rails injection
        milestone_flag = "true" if is_milestone else "false"
        safe_type_color = self.ruby_escape(type_color)

        command = f"""
        begin
          type = Type.create!(
            name: '{safe_type_name}',
            color: '{safe_type_color}',
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
            self.logger.error(
                f"Expected dict from execute_query, got {type(result)}: {result}",
            )
            return {
                "status": "error",
                "error": "Invalid result type from execute_query (result)",
            }

        # Parse the result
        output = result.get("output", "")

        if "status: :error" in output:
            error_msg = "Unknown error"
            error_match = re.search(r'message: "([^"]+)"', output)
            if error_match:
                error_msg = error_match.group(1)

            self.logger.error(
                "Error creating work package type '%s': %s",
                type_name,
                error_msg,
            )
            return {"status": "error", "error": error_msg}

        # Extract ID if successful
        id_match = re.search(r"id: (\d+)", output)
        if id_match:
            type_id = int(id_match.group(1))
            self.logger.info(
                "Created work package type '%s' with ID %s",
                type_name,
                type_id,
            )

            # Verify the type exists by querying it (type_id is safe as it's an integer)
            verify_command = f"Type.find({type_id}).present? rescue false"
            verify_result = self.op_client.execute_query(verify_command)
            self.logger.debug(
                f"verify_result type: {type(verify_result)}, value: {verify_result}",
            )
            if not isinstance(verify_result, dict):
                self.logger.error(
                    f"Expected dict from execute_query, got {type(verify_result)}: {verify_result}",
                )
                return {
                    "status": "error",
                    "error": "Invalid result type from execute_query (verify_result)",
                }

            if (
                verify_result["status"] == "success"
                and "true" in verify_result["output"]
            ):
                self.logger.info(
                    "Verified work package type '%s' with ID %s exists",
                    type_name,
                    type_id,
                )
                return {"status": "success", "id": type_id, "name": type_name}
            self.logger.warning(
                "Created type but failed verification: %s",
                verify_result.get("output", "Unknown error"),
            )

        self.logger.warning("Failed to parse type ID from Rails response: %s", output)

        # If we reached here without returning, assume success but couldn't get ID
        return {"status": "success", "name": type_name}

    def migrate_issue_types_via_rails(self, window: int = 0, pane: int = 0) -> None:
        """Migrate issue types directly via the Rails console using a bulk operation.

        This method creates all required work package types at once via a single Rails script,
        following the pattern used in other migration components.

        Args:
            window: tmux window number (default: 0)
            pane: tmux pane number (default: 0)

        Raises:
            MigrationError: If migration fails

        """
        if not self.issue_type_mapping:
            self.create_issue_type_mapping()

        # Get existing types to avoid creating duplicates
        existing_types = self.check_existing_work_package_types()
        existing_names = [
            type_data.get("name", "").lower() for type_data in existing_types
        ]

        # Create lookup dictionary for O(1) access instead of O(n) linear search
        # This optimizes the algorithm from O(n²) to O(n)
        existing_types_by_name = {}
        for type_data in existing_types:
            type_name_lower = type_data.get("name", "").lower()
            if type_name_lower:
                existing_types_by_name[type_name_lower] = type_data

        # Collect types to create
        types_to_create: list[dict[str, str | bool]] = []
        for type_name, type_data in self.issue_type_mapping.items():
            if type_data.get("openproject_id") is None:
                op_type_name = type_data.get("openproject_name", "")
                op_type_name_lower = op_type_name.lower()

                # Skip if already exists (case insensitive check)
                if op_type_name_lower in existing_names:
                    # Try to update mapping with existing ID using O(1) lookup
                    existing_type_data = existing_types_by_name.get(op_type_name_lower)
                    if existing_type_data:
                        existing_id = existing_type_data.get("id")
                        if existing_id:
                            self.logger.info(
                                "Work package type '%s' already exists, updating mapping",
                                op_type_name,
                            )
                            self.issue_type_mapping[type_name][
                                "openproject_id"
                            ] = existing_id
                            self.issue_type_mapping[type_name][
                                "matched_by"
                            ] = "found_existing"
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
            # Persist the current in-memory mapping so orchestrator can see it
            from src import config as _cfg
            _cfg.mappings.set_mapping("issue_type", self.issue_type_mapping)
            # Also persist the ID mapping for downstream components
            final_mapping: dict[int, int] = {}
            for type_name, mapping in self.issue_type_mapping.items():
                jira_id = mapping.get("jira_id")
                op_id = mapping.get("openproject_id")
                if jira_id is not None and op_id:
                    final_mapping[jira_id] = op_id
            _cfg.mappings.set_mapping("issue_type_id", final_mapping)
            return

        self.logger.info(
            "Preparing to create %s work package types in bulk",
            len(types_to_create),
        )

        # Use generic bulk create helper for Type
        try:
            # Build records/meta from mapping that need creation
            records: list[dict[str, Any]] = []
            meta: list[dict[str, Any]] = []
            for mapping in self.issue_type_mapping.values():
                if mapping.get("openproject_id") is None:
                    meta.append({
                        "jira_type_name": mapping.get("jira_name"),
                        "proposed_name": mapping.get("openproject_name"),
                    })
                    records.append({
                        "name": mapping.get("openproject_name"),
                        "is_milestone": bool(mapping.get("is_milestone", False)),
                        "is_default": bool(mapping.get("is_default", False)),
                        # Color creation/linking moved to Python pre-processing later if needed
                    })

            result = self.op_client.bulk_create_records(
                model="Type",
                records=records,
                timeout=120,
                result_basename="j2o_type_bulk_result.json",
            )

            if not isinstance(result, dict) or result.get("status") != "success":
                raise MigrationError(result.get("message", "Bulk type creation failed"))

            created = result.get("created", []) or []
            errors = result.get("errors", []) or []

            for item in created:
                idx = item.get("index")
                if isinstance(idx, int) and 0 <= idx < len(meta):
                    proposed_name = meta[idx].get("proposed_name")
                    # Update issue_type_mapping entries matching proposed_name
                    for mapping in self.issue_type_mapping.values():
                        if mapping.get("openproject_name") == proposed_name and mapping.get("openproject_id") is None:
                            mapping["openproject_id"] = item.get("id")
                            mapping["matched_by"] = "created"

            # Persist mappings
            from src import config as _cfg
            _cfg.mappings.set_mapping("issue_type", self.issue_type_mapping)
            final_mapping = {}
            for type_name, m in self.issue_type_mapping.items():
                jira_id = m["jira_id"]
                op_id = m.get("openproject_id")
                if op_id:
                    final_mapping[jira_id] = op_id
            _cfg.mappings.set_mapping("issue_type_id", final_mapping)

            if errors:
                self.logger.warning("Some work package types failed to create: %s", len(errors))
                raise MigrationError(f"Failed to create {len(errors)} work package types")

        except Exception as e:
            msg = f"Error during Type bulk creation: {e}"
            self.logger.exception(msg)
            raise MigrationError(msg) from e

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
            1
            for mapping in self.issue_type_mapping.values()
            if mapping["openproject_id"] is None
        )

        if op_types_to_create:
            self.logger.info(
                f"Need to create {len(op_types_to_create)} unique work package types in OpenProject via Rails console",
            )
            self.logger.info(
                f"(This will map to {total_types_needing_creation} total Jira issue types after deduplication)",
            )
        else:
            self.logger.info(
                "No new work package types need to be created in OpenProject",
            )

        final_mapping = {}
        for mapping in self.issue_type_mapping.values():
            jira_id = mapping["jira_id"]
            op_id = mapping["openproject_id"]

            if op_id:
                final_mapping[jira_id] = op_id

        from src import config as _cfg
        _cfg.mappings.set_mapping("issue_type_id", final_mapping)

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

    def update_mapping_file(self) -> None:
        """Update the issue type mapping file with IDs from OpenProject.

        This method is useful when work package types were created manually via a Ruby script
        execution and the mapping file needs to be updated with the created type IDs.

        Raises:
            MigrationError: If mapping update fails

        """
        self.logger.info(
            "Updating issue type mapping file with IDs from OpenProject...",
        )

        # Check if we're in mock mode
        mock_mode = os.environ.get("J2O_USE_MOCK_APIS", "false").lower() == "true"
        self.logger.debug(
            "Mock mode: Using mock work package types for mapping update",
        )

        if mock_mode:
            # In mock mode, use the mock work package types
            op_types = [
                {
                    "id": 1,
                    "name": "Task",
                    "color": "#0000FF",
                    "position": 1,
                    "is_default": True,
                    "is_milestone": False,
                },
                {
                    "id": 2,
                    "name": "Bug",
                    "color": "#FF0000",
                    "position": 2,
                    "is_default": False,
                    "is_milestone": False,
                },
                {
                    "id": 3,
                    "name": "Feature",
                    "color": "#00FF00",
                    "position": 3,
                    "is_default": False,
                    "is_milestone": False,
                },
            ]
            self.logger.info(
                "Mock mode: Using mock work package types for mapping update",
            )
        else:
            # Use the robust file-based retrieval only (single consistent path)
            op_types = self.check_existing_work_package_types()
            if not op_types:
                msg = "Failed to retrieve work package types from OpenProject"
                self.logger.error(msg)
                raise MigrationError(msg)

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
                self.logger.info(
                    "Found work package type '%s' with ID %s",
                    op_name,
                    op_id,
                )
                self.issue_type_mapping[jira_type_name]["openproject_id"] = op_id
                self.issue_type_mapping[jira_type_name]["matched_by"] = (
                    "created"
                    if type_data.get("matched_by") == "default_mapping_to_create"
                    else "exact_match"
                )
                updated_count += 1
            else:
                self.logger.warning(
                    "Work package type '%s' not found in OpenProject",
                    op_name,
                )
                missing_count += 1

        # Save the updated mapping through controller
        if updated_count > 0:
            from src import config as _cfg
            _cfg.mappings.set_mapping("issue_type", self.issue_type_mapping)
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

        # Check if too many types are missing
        if missing_count > updated_count and missing_count > 0:
            msg = f"Migration partially failed: {missing_count} types were not found in OpenProject"
            self.logger.warning(msg)
            raise MigrationError(msg)

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
            self.logger.info("Starting migration via Rails...")
            self.migrate_issue_types_via_rails()

            # Update mapping file with created types
            self.logger.info("Updating mapping file...")
            self.update_mapping_file()

            self.logger.info("Issue type migration completed successfully")
            results = ComponentResult(
                success=True,
                message="Issue types migrated successfully",
                modified_files=["issue_type_mapping.json"],
            )

        return results
