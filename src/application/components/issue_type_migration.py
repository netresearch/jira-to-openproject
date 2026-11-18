#!/usr/bin/env python3
# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Issue type migration module for Jira to OpenProject migration.
Handles mapping and creation of issue types from Jira to work package types in OpenProject.

Phase 7h notes
--------------
This migration is **orthogonal to the Phase 7 typed wp_map pipeline**.
It stores type mappings by Jira name and by Jira ID. Target IDs are
confirmed by the OpenProject type catalog or by the structured Rails
batch response before the mapping is persisted.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, ClassVar

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.display import console
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult, MigrationError

_USER_STORY_TYPE = "User Story"
_JIRA_ISSUE_TYPES_FILE = "jira_issue_types.json"


_TYPE_LOOKUP = """
def j2o_work_package_type(row)
  name = row.fetch('name')
  existing = Type.find_by('lower(name) = ?', name.downcase)
  return existing if existing

  color = Color.find_by(hexcode: row['color']) || Color.first
  Type.create!(name: name, color: color, is_milestone: row.fetch('is_milestone', false))
end
"""

_FIND_OR_CREATE_TYPE = (
    _TYPE_LOOKUP
    + """
type = j2o_work_package_type(input_data)
puts $j2o_start_marker
puts JSON.generate({id: type.id, name: type.name})
puts $j2o_end_marker
"""
)

_CREATE_TYPES = (
    _TYPE_LOOKUP
    + """
created = []
errors = []
input_data.each_with_index do |row, index|
  begin
    type = j2o_work_package_type(row)
    created << {index: index, id: type.id}
  rescue ActiveRecord::RecordInvalid => e
    errors << {index: index, errors: e.record.errors.full_messages}
  end
end
puts $j2o_start_marker
puts JSON.generate({created: created, errors: errors})
puts $j2o_end_marker
"""
)


@register_entity_types("issue_types", "work_package_types")
class IssueTypeMigration(BaseMigration):
    """Handles the migration of issue types from Jira to work package types in OpenProject.

    This class supports two approaches:
    1. Generate a Ruby script for manual execution via Rails console (traditional approach)
    2. Execute commands directly on the Rails console using pexpect (direct approach)
    """

    # Honours ``config.migration_config["dry_run"]`` by passing
    # ``dry_run=True`` to the Rails type-create path (PR D, issue #260).
    DRY_RUN_SAFE: ClassVar[bool] = True

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
            _USER_STORY_TYPE: {"name": _USER_STORY_TYPE, "color": "#27AE60"},
            "Epic": {"name": "Epic", "color": "#9B59B6"},
            "Feature": {"name": "Feature", "color": "#2788C8"},
            "Story": {"name": _USER_STORY_TYPE, "color": "#27AE60"},
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
        self.jira_issue_types = self._load_from_json(Path(_JIRA_ISSUE_TYPES_FILE), [])
        self.op_work_package_types = self._load_from_json(
            Path("op_work_package_types.json"),
            [],
        )
        # Load via mapping controller to keep cache authoritative
        self.issue_type_mapping = config.mappings.get_mapping("issue_type") or {}
        self.issue_type_id_mapping = config.mappings.get_mapping("issue_type_id") or {}
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
        issue_types_file = self.data_dir / _JIRA_ISSUE_TYPES_FILE

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
            self._save_to_json(issue_types, Path(_JIRA_ISSUE_TYPES_FILE))

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
        matched_types = sum(1 for type_data in mapping.values() if type_data["openproject_id"] is not None)
        to_create_types = sum(1 for type_data in mapping.values() if type_data["openproject_id"] is None)
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
        for jira_type_name in normalized_mapping:
            if not (jira_type_name.startswith(("Sub:", "Sub-"))):
                continue

            # Get the base type name by removing the "Sub:" or "Sub-" prefix
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
        matched_types = sum(1 for type_data in normalized_mapping.values() if type_data["openproject_id"] is not None)
        to_create_types = sum(1 for type_data in normalized_mapping.values() if type_data["openproject_id"] is None)
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
        name = type_data.get("openproject_name", "")
        result = self.op_client.execute_script_with_data(
            _FIND_OR_CREATE_TYPE,
            {
                "name": name,
                "color": type_data.get("color", "#1A67A3"),
                "is_milestone": bool(type_data.get("is_milestone")),
            },
        )
        row = result.get("data") if isinstance(result, dict) else None
        if not isinstance(row, dict) or not row.get("id"):
            return {"status": "error", "error": f"Could not confirm OpenProject type {name!r}"}
        return {"status": "success", "id": row["id"], "name": row["name"]}

    def _resolve_name_taken_errors(
        self,
        errors: list[dict[str, Any]],
        records: list[dict[str, Any]],
        existing_types: list[dict[str, Any]],
    ) -> tuple[int, list[dict[str, Any]]]:
        """Reclaim 'Name has already been taken' bulk-create errors.

        For each error matching that exact Rails validation, look up the
        existing OP type by case-insensitive name and update every
        ``self.issue_type_mapping`` entry whose ``openproject_name`` matches
        — including sub-types that normalized to the same base name —
        pointing them at the found id.

        Returns ``(resolved_count, unresolved_errors)``. The caller can keep
        treating ``unresolved_errors`` as failures.
        """
        if not errors:
            return 0, []

        existing_by_name = {
            (t.get("name") or "").lower(): t.get("id") for t in existing_types if t.get("name") and t.get("id")
        }

        resolved = 0
        unresolved: list[dict[str, Any]] = []
        for err in errors:
            err_msgs = err.get("errors") or []
            is_taken = any("Name has already been taken" in str(m) for m in err_msgs)
            idx = err.get("index")
            if not is_taken or not isinstance(idx, int) or not (0 <= idx < len(records)):
                unresolved.append(err)
                continue

            taken_name = (records[idx].get("name") or "").strip()
            existing_id = existing_by_name.get(taken_name.lower())
            if not existing_id:
                unresolved.append(err)
                continue

            self._assign_new_type_id(taken_name, existing_id, "found_existing_after_create")

            self.logger.info(
                "Resolved 'Name has already been taken' for type '%s' -> existing OP id %s",
                taken_name,
                existing_id,
            )
            resolved += 1

        return resolved, unresolved

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
        records = self._prepare_type_records(self.check_existing_work_package_types())
        if not records:
            self._persist_type_ids()
            self._record_type_provenance()
            return

        try:
            result = self.op_client.execute_script_with_data(_CREATE_TYPES, records, timeout=120)
            body = result.get("data") if isinstance(result, dict) else None
            if not isinstance(body, dict):
                msg = "OpenProject did not return the type batch result"
                raise MigrationError(msg)
            errors = body["errors"]
            self._apply_type_results(records, body["created"], errors)

            # Recover from "Name has already been taken." validation errors by
            # looking up the existing OP type id. Done before persistence so
            # the resolved mappings get saved + included in provenance below.
            if errors:
                errors = self._recover_type_errors(errors, records)

            self._persist_type_ids()

            self._record_type_provenance()

            if errors:
                self.logger.warning("Some work package types failed to create: %s", len(errors))
                msg = f"Failed to create {len(errors)} work package types"
                raise MigrationError(msg)

        except Exception as e:
            msg = f"Error during Type bulk creation: {e}"
            self.logger.exception(msg)
            raise MigrationError(msg) from e

    def _apply_type_results(self, records, created, errors):
        expected = set(range(len(records)))
        received = [item["index"] for item in [*created, *errors]]
        if len(received) != len(expected) or set(received) != expected:
            msg = "OpenProject returned incomplete or duplicate type results"
            raise MigrationError(msg)
        for item in created:
            if not item.get("id"):
                msg = "OpenProject returned a type without an ID"
                raise MigrationError(msg)
            self._assign_new_type_id(records[item["index"]]["name"], item["id"], "created")

    def _assign_new_type_id(self, name, target_id, matched_by):
        for mapping in self.issue_type_mapping.values():
            if mapping.get("openproject_name") == name and mapping.get("openproject_id") is None:
                mapping.update(openproject_id=target_id, matched_by=matched_by)

    def _persist_type_ids(self):
        config.mappings.set_mapping("issue_type", self.issue_type_mapping)
        ids = {
            row["jira_id"]: row["openproject_id"]
            for row in self.issue_type_mapping.values()
            if row.get("jira_id") is not None and row.get("openproject_id")
        }
        config.mappings.set_mapping("issue_type_id", ids)

    def _prepare_type_records(self, existing_types):
        existing = {row["name"].casefold(): row.get("id") for row in existing_types if row.get("name")}
        records = {}
        for mapping in self.issue_type_mapping.values():
            if mapping.get("openproject_id") is not None:
                continue
            name = mapping.get("openproject_name", "")
            target_id = existing.get(name.casefold())
            if target_id:
                mapping.update(openproject_id=target_id, matched_by="found_existing")
                continue
            records.setdefault(
                name,
                {
                    "name": name,
                    "is_milestone": bool(mapping.get("is_milestone")),
                    "color": mapping.get("color"),
                },
            )
        return list(records.values())

    def _recover_type_errors(self, errors, records):
        try:
            refreshed_existing = self.check_existing_work_package_types()
        except Exception as fetch_err:
            self.logger.warning(
                "Could not refresh existing types for taken-name recovery: %s",
                fetch_err,
            )
            refreshed_existing = []
        resolved, errors = self._resolve_name_taken_errors(
            errors,
            records,
            refreshed_existing,
        )
        if resolved:
            self.logger.info(
                "Recovered %d 'Name has already been taken' errors via lookup",
                resolved,
            )
        return errors

    def _record_type_provenance(self):
        # Record provenance for successfully migrated issue types
        # This enables restoration of mappings from OP alone without local files
        provenance_mappings = [
            {
                "jira_key": str(m.get("jira_id", "")),  # Use Jira type ID as key
                "jira_name": m.get("jira_name"),
                "op_entity_id": m["openproject_id"],
            }
            for m in self.issue_type_mapping.values()
            if m.get("openproject_id")
        ]
        if provenance_mappings:
            try:
                result = self.op_client.bulk_record_entity_provenance("type", provenance_mappings)
                self.logger.info(
                    "Recorded type provenance: %d success, %d failed",
                    result.get("success", 0),
                    result.get("failed", 0),
                )
            except Exception as prov_err:
                self.logger.warning("Failed to record type provenance: %s", prov_err)

    def _types_for_mapping_update(self):
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
        return op_types

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
            self.logger.info(
                "No new work package types need to be created in OpenProject",
            )

        final_mapping = {}
        for mapping in self.issue_type_mapping.values():
            jira_id = mapping["jira_id"]
            op_id = mapping["openproject_id"]

            if op_id:
                final_mapping[jira_id] = op_id

        config.mappings.set_mapping("issue_type_id", final_mapping)

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
            True if any mappings were updated, False otherwise.

        Raises:
            MigrationError: If mapping update fails

        """
        self.logger.info(
            "Updating issue type mapping file with IDs from OpenProject...",
        )

        # Check if we're in mock mode
        op_types = self._types_for_mapping_update()

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
                self.logger.info(
                    "Found work package type '%s' with ID %s",
                    op_name,
                    op_id,
                )
                self.issue_type_mapping[jira_type_name]["openproject_id"] = op_id
                self.issue_type_mapping[jira_type_name]["matched_by"] = (
                    "created" if type_data.get("matched_by") == "default_mapping_to_create" else "exact_match"
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
            config.mappings.set_mapping("issue_type", self.issue_type_mapping)
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

        return updated_count > 0

    def restore_mapping_from_openproject(self) -> dict[str, Any]:
        """Restore issue type mapping from OpenProject provenance data alone.

        This method rebuilds the type mapping by querying the J2O Migration
        provenance project for type mapping work packages. It does NOT require
        Jira data, making it suitable for recovery scenarios where local mapping
        files are missing but OP contains provenance data from previous migrations.

        Returns:
            Dictionary keyed by Jira type ID with OpenProject mapping data

        """
        self.logger.info("Restoring issue type mapping from OpenProject provenance data...")

        # Query provenance registry for type mappings
        provenance_mappings = self.op_client.restore_entity_mappings_from_provenance("type")

        if not provenance_mappings:
            self.logger.info("No type provenance data found in OpenProject")
            return {}

        # Convert provenance format to standard mapping format
        mapping: dict[str, Any] = {}
        id_mapping: dict[int, int] = {}

        for jira_type_id, prov_data in provenance_mappings.items():
            mapping[jira_type_id] = {
                "jira_id": int(jira_type_id) if jira_type_id.isdigit() else jira_type_id,
                "jira_name": prov_data.get("jira_name"),
                "openproject_id": prov_data.get("openproject_id"),
                "matched_by": "j2o_provenance",
                "restored_from_op": True,
                "provenance_wp_id": prov_data.get("provenance_wp_id"),
            }
            if prov_data.get("openproject_id") and jira_type_id.isdigit():
                id_mapping[int(jira_type_id)] = prov_data["openproject_id"]

        # Persist mappings
        config.mappings.set_mapping("issue_type", mapping)
        config.mappings.set_mapping("issue_type_id", id_mapping)
        self.issue_type_mapping = mapping

        self.logger.info("Restored %d type mappings from OpenProject provenance", len(mapping))
        return mapping

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
