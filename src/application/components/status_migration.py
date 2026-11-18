# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Status migration module for Jira to OpenProject migration.
Handles the migration of Jira statuses to OpenProject statuses.

Implementation is now complete, including:
- Status extraction from Jira and OpenProject
- Status mapping creation
- Status creation in OpenProject via Rails
- Automated and manual processes documented in docs/status_migration.md
- Test validation in tests/test_status_migration.py

Phase 7f notes
--------------
This migration does **not** consume the ``work_package`` mapping —
there is no ``wp_map`` ``dict | int`` ladder of the kind phase 7
retires. Its inputs are flat REST dicts from ``status``/``status
category`` endpoints, and its output is the orthogonal ``status``
mapping of shape ``{jira_status_id: {"openproject_id": ...,
"openproject_name": ...}}`` which is already uniformly dict-shaped at
every write site (``create_status_mapping``, ``migrate_statuses``,
``restore_mapping_from_openproject``). The status mapping is its own
world (similar to the workflow migration's ``status_mapping`` handled
in phase 7d) and ``WorkPackageMappingEntry.from_legacy`` does not
apply. Modelling Jira status payloads as :class:`JiraStatusRef` would
be cosmetic at this site — the payloads are flat ``{id, name,
statusCategory}`` dicts already — and is intentionally deferred.
Behaviour preserved.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.config import logger
from src.display import ProgressTracker
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.mappings.mappings import Mappings
from src.models import ComponentResult
from src.models.migration_error import MigrationError

_JIRA_STATUS_CATEGORIES_FILE = "jira_status_categories.json"
_JIRA_STATUSES_FILE = "jira_statuses.json"
_OP_STATUSES_FILE = "op_statuses.json"


_CATEGORY_COLORS = {
    "blue-gray": "#4A6785",
    "yellow": "#F6C342",
    "green": "#14892C",
    "red": "#D04437",
    "medium-gray": "#8993A4",
}

_IMPORT_STATUSES = """
results = {}
input_data.each do |row|
  key = row.fetch('jira_id').to_s
  name = row.fetch('name')
  begin
    status = Status.find_by('lower(name) = ?', name.downcase)
    reused = !status.nil?
    unless reused
      status = Status.create!(name: name, is_closed: row['is_closed'], is_default: !!row['is_default'],
                              color: Color.find_by(hexcode: row['color']))
    end
    results[key] = {id: status.id, name: status.name, already_existed: reused}
  rescue ActiveRecord::RecordInvalid => e
    results[key] = {error: e.record.errors.full_messages.join(', ')}
  end
end
puts $j2o_start_marker
puts JSON.generate(results)
puts $j2o_end_marker
"""


@register_entity_types("statuses", "status_types")
class StatusMigration(BaseMigration):
    """Handles the migration of statuses from Jira to OpenProject.

    This class is responsible for:
    1. Extracting statuses from Jira
    2. Creating corresponding statuses in OpenProject
    3. Creating and maintaining the status mapping
    """

    # Honours ``config.migration_config["dry_run"]`` by simulating
    # mapping creation instead of running Rails-side Status.create
    # (PR D, issue #260).
    DRY_RUN_SAFE: ClassVar[bool] = True

    def __init__(
        self,
        jira_client: JiraClient | None = None,
        op_client: OpenProjectClient | None = None,
        mappings: Mappings | None = None,
        tracker: ProgressTracker[Any] | None = None,
    ) -> None:
        """Initialize the status migration tools.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client
            mappings: Initialized Mappings instance
            tracker: Optional progress tracker instance

        """
        super().__init__(jira_client, op_client)

        if mappings is not None:
            self.mappings = mappings
        self.tracker = tracker

        # Initialize empty lists
        self.jira_statuses: list[dict[str, Any]] = []
        self.jira_status_categories: list[dict[str, Any]] = []
        self.op_statuses: list[dict[str, Any]] = []

        # Load existing status mappings
        self.status_mapping: dict[str, Any] = {}
        if self.mappings is not None and hasattr(self.mappings, "status_mapping"):
            self.status_mapping = self.mappings.status_mapping.copy()

        # Load existing data if available
        self._load_data()

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities from Jira for a specific type.

        This method enables idempotent workflow caching by providing a standard
        interface for entity retrieval. Called by run_with_change_detection() to fetch data
        with automatic thread-safe caching.

        Args:
            entity_type: The type of entities to retrieve (e.g., "statuses", "status_types")

        Returns:
            List of entity dictionaries from Jira API

        Raises:
            ValueError: If entity_type is not supported by this migration

        """
        # Check if this is the entity type we handle
        if entity_type in ("statuses", "status_types"):
            # Use the same API call pattern as extract_jira_statuses()
            if self.jira_client is None or not hasattr(self.jira_client, "jira") or self.jira_client.jira is None:
                msg = "Jira client is not properly initialized"
                logger.error(msg)
                raise ValueError(msg)

            # Use the REST API endpoint for status retrieval
            response = self.jira_client.jira._get_json("status")
            return response or []

        # Raise error for unsupported types
        msg = (
            f"StatusMigration does not support entity type: {entity_type}. "
            f"Supported types: ['statuses', 'status_types']"
        )
        raise ValueError(msg)

    def _load_data(self) -> None:
        """Load existing data from JSON files."""
        self.jira_statuses = self._load_from_json(Path(_JIRA_STATUSES_FILE), [])
        self.jira_status_categories = self._load_from_json(
            Path(_JIRA_STATUS_CATEGORIES_FILE),
            [],
        )
        self.op_statuses = self._load_from_json(Path(_OP_STATUSES_FILE), [])

        logger.info("Loaded %s Jira statuses", len(self.jira_statuses))
        logger.info(
            "Loaded %s Jira status categories",
            len(self.jira_status_categories),
        )
        logger.info("Loaded %s OpenProject statuses", len(self.op_statuses))

    def extract_jira_statuses(self) -> list[dict[str, Any]]:
        """Extract all statuses from Jira using the Jira statuses API endpoint.

        Returns:
            List of Jira status dictionaries

        """
        statuses_file = self.data_dir / _JIRA_STATUSES_FILE

        if statuses_file.exists() and not config.migration_config.get("force", False):
            logger.info(
                "Jira statuses data already exists, skipping extraction (use --force to override)",
            )
            with statuses_file.open() as f:
                self.jira_statuses = json.load(f)
            return self.jira_statuses

        logger.info("Extracting statuses from Jira...")

        try:
            # Check if jira_client and jira attribute exist
            if self.jira_client is None or not hasattr(self.jira_client, "jira") or self.jira_client.jira is None:
                msg = "Jira client is not properly initialized"
                logger.error(msg)
                raise MigrationError(msg)

            # Use the REST API endpoint for status retrieval
            response = self.jira_client.jira._get_json("status")
            statuses = response or []

            if not statuses:
                logger.warning("No statuses found in Jira")
                return []

            logger.info("Extracted %s statuses from Jira", len(statuses))

            self.jira_statuses = statuses
            self._save_to_json(statuses, _JIRA_STATUSES_FILE)

            return statuses
        except Exception as e:
            msg = f"Failed to extract statuses from Jira: {e}"
            logger.exception(msg)
            raise MigrationError(msg) from e

    def extract_status_categories(self) -> list[dict[str, Any]]:
        """Extract status categories from Jira.

        Returns:
            List of Jira status category dictionaries

        """
        categories_file = self.data_dir / _JIRA_STATUS_CATEGORIES_FILE

        if categories_file.exists() and not config.migration_config.get("force", False):
            logger.info(
                "Jira status categories data already exists, skipping extraction (use --force to override)",
            )
            with categories_file.open() as f:
                self.jira_status_categories = json.load(f)
            return self.jira_status_categories

        logger.info("Extracting status categories from Jira...")

        try:
            categories = self.jira_client.get_status_categories()
            if not categories:
                logger.warning("No status categories found in Jira")
                return []

            logger.info("Extracted %s status categories from Jira", len(categories))

            self.jira_status_categories = categories
            self._save_to_json(categories, _JIRA_STATUS_CATEGORIES_FILE)

            return categories
        except Exception as e:
            msg = f"Failed to extract status categories from Jira: {e}"
            logger.exception(msg)
            raise MigrationError(msg) from e

    def get_openproject_statuses(self) -> list[dict[str, Any]]:
        """Get all statuses from OpenProject.

        Returns:
            List of OpenProject status dictionaries

        """
        statuses_file = self.data_dir / _OP_STATUSES_FILE

        if statuses_file.exists() and not config.migration_config.get("force", False):
            logger.info(
                "OpenProject statuses data already exists, skipping extraction (use --force to override)",
            )
            with statuses_file.open() as f:
                self.op_statuses = json.load(f)
            return self.op_statuses

        logger.info("Getting statuses from OpenProject...")

        try:
            if self.op_client is None:
                msg = "OpenProject client is not initialized"
                raise MigrationError(msg)
            statuses = self.op_client.get_statuses()
            if not isinstance(statuses, list):
                msg = "OpenProject did not return a status catalog"
                raise MigrationError(msg)
            result_statuses = [dict(row) for row in statuses]
            self.op_statuses = result_statuses
            self._save_to_json(result_statuses, _OP_STATUSES_FILE)

            return result_statuses
        except Exception as e:
            msg = f"Failed to get statuses from OpenProject: {e}"
            logger.exception(msg)
            raise MigrationError(msg) from e

    def create_statuses_bulk_via_rails(
        self,
        statuses_to_create: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Create multiple statuses in OpenProject using a single Rails console command.

        Args:
            statuses_to_create: List of status dictionaries with keys:
                - jira_id: Jira status ID
                - name: Status name
                - is_closed: Whether the status is considered 'closed'
                - is_default: Whether the status is the default status
                - color: Optional hex color code

        Returns:
            Dictionary mapping Jira status IDs to created OpenProject status info

        """
        if not statuses_to_create:
            return {}
        envelope = self.op_client.execute_script_with_data(_IMPORT_STATUSES, statuses_to_create, timeout=90)
        results = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(results, dict):
            msg = "OpenProject did not confirm the status batch"
            raise MigrationError(msg)
        return results

    def create_status_mapping(self) -> dict[str, Any]:
        """Create a mapping between Jira statuses and OpenProject statuses.

        Returns:
            Dictionary mapping Jira status names to OpenProject status IDs

        """
        mapping_file = self.data_dir / "status_mapping.json"

        if mapping_file.exists() and not config.migration_config.get("force", False):
            logger.info(
                "Status mapping already exists, loading from file (use --force to recreate)",
            )
            with mapping_file.open() as f:
                self.status_mapping = json.load(f)
            return self.status_mapping

        logger.info("Creating status mapping...")

        if not self.jira_statuses:
            self.jira_statuses = self.extract_jira_statuses()

        if not self.op_statuses:
            self.op_statuses = self.get_openproject_statuses()

        # Create mapping based on name
        mapping: dict[str, dict[str, Any]] = {}
        op_statuses_by_name = {s.get("name", "").lower(): s for s in self.op_statuses}

        for jira_status in self.jira_statuses:
            jira_id = jira_status.get("id")
            jira_name = jira_status.get("name", "")

            # Skip if jira_id is None
            if jira_id is None:
                logger.warning("Skipping Jira status with no ID: %s", jira_name)
                continue

            # Ensure jira_id is a string
            jira_id = str(jira_id)

            # Check if a similar status exists in OpenProject
            op_status = None
            jira_name_lower = jira_name.lower()

            # Try exact match first
            if jira_name_lower in op_statuses_by_name:
                op_status = op_statuses_by_name[jira_name_lower]

            if op_status:
                mapping[jira_id] = {
                    "openproject_id": op_status.get("id"),
                    "openproject_name": op_status.get("name"),
                }
            else:
                # No match found
                mapping[jira_id] = {
                    "openproject_id": None,
                    "openproject_name": jira_name,
                }

        self.status_mapping = mapping
        config.mappings.set_mapping("status", mapping)

        return mapping

    def migrate_statuses(self) -> dict[str, Any]:
        """Migrate statuses from Jira to OpenProject.

        Returns:
            Dictionary with migration summary and status mapping

        """
        if not self.jira_statuses:
            self.jira_statuses = self.extract_jira_statuses()
        if not self.jira_statuses:
            msg = "No Jira statuses to migrate"
            raise MigrationError(msg)
        if not self.op_statuses:
            self.op_statuses = self.get_openproject_statuses()
        existing = {row["name"].casefold(): row for row in self.op_statuses}
        by_id = {str(row["id"]): row for row in self.op_statuses}
        mapping, pending, invalid = self._plan_statuses(existing, by_id)
        reused = len(mapping)
        dry_run = config.migration_config.get("dry_run", False)
        results = self._status_results(pending, dry_run)
        for row in pending:
            key = row["jira_id"]
            result = results.get(key) or {}
            if result.get("id"):
                mapping[key] = {"openproject_id": result["id"], "openproject_name": result["name"]}
                reused += int(bool(result.get("already_existed")))
            else:
                invalid += 1
                logger.error("No OpenProject status confirmed for Jira ID %s", key)
        if not dry_run:
            self._save_status_ids(mapping)
        return {
            "status": "failed" if invalid else "success",
            "total_processed": len(self.jira_statuses),
            "already_exists_count": reused,
            "created_count": len(mapping) - reused,
            "error_count": invalid,
            "mapping": mapping,
        }

    def _plan_statuses(self, existing, by_id):
        mapping = {}
        pending = []
        invalid = 0
        for source in self.jira_statuses:
            key, name = source.get("id"), source.get("name")
            if not key or not name:
                invalid += 1
                continue
            key = str(key)
            prior = self.status_mapping.get(key) or {}
            prior_id = prior.get("openproject_id") if isinstance(prior, dict) else prior
            target = by_id.get(str(prior_id)) or existing.get(name.casefold())
            if target:
                mapping[key] = {"openproject_id": target["id"], "openproject_name": target["name"]}
            else:
                category = source.get("statusCategory") or {}
                pending.append(
                    {
                        "jira_id": key,
                        "name": name,
                        "is_closed": (category.get("key") or "").lower() == "done",
                        "color": _CATEGORY_COLORS.get((category.get("colorName") or "").lower()),
                    }
                )
        return mapping, pending, invalid

    def _status_results(self, pending, dry_run):
        if dry_run:
            return {s["jira_id"]: {"id": f"dry_run_{s['jira_id']}", "name": s["name"]} for s in pending}
        return self.create_statuses_bulk_via_rails(pending) if pending else {}

    def _save_status_ids(self, mapping):
        self.status_mapping.update(mapping)
        config.mappings.set_mapping("status", self.status_mapping)
        if self.mappings is not None:
            self.mappings.status_mapping = self.status_mapping
        provenance = [
            {"jira_key": key, "jira_name": row["openproject_name"], "op_entity_id": row["openproject_id"]}
            for key, row in mapping.items()
        ]
        if provenance:
            try:
                self.op_client.bulk_record_entity_provenance("status", provenance)
            except Exception:
                logger.exception("Could not record status provenance in OpenProject")

    def analyze_status_mapping(self) -> dict[str, Any]:
        """Analyze the status mapping to identify potential issues.

        Returns:
            Dictionary with analysis results

        """
        logger.info("Analyzing status mapping...")

        if not self.status_mapping:
            self.status_mapping = config.mappings.get_mapping("status") or {}

        if not self.status_mapping:
            return {
                "status": "warning",
                "message": "No status mapping found",
                "statuses_count": 0,
            }

        # Count statuses
        total_statuses = len(self.status_mapping)
        mapped_statuses = sum(1 for s in self.status_mapping.values() if s.get("openproject_id") is not None)
        unmapped_statuses = total_statuses - mapped_statuses

        # List unmapped statuses
        unmapped = [
            {"jira_id": jira_id, "name": status.get("openproject_name")}
            for jira_id, status in self.status_mapping.items()
            if status.get("openproject_id") is None
        ]

        return {
            "status": "success",
            "statuses_count": total_statuses,
            "mapped_count": mapped_statuses,
            "unmapped_count": unmapped_statuses,
            "unmapped_statuses": unmapped[:10],  # Limit to first 10
            "message": f"Found {mapped_statuses}/{total_statuses} mapped statuses",
        }

    def restore_mapping_from_openproject(self) -> dict[str, Any]:
        """Restore status mapping from OpenProject provenance data alone.

        This method rebuilds the status mapping by querying the J2O Migration
        provenance project for status mapping work packages. It does NOT require
        Jira data, making it suitable for recovery scenarios where local mapping
        files are missing but OP contains provenance data from previous migrations.

        Returns:
            Dictionary keyed by Jira status ID with OpenProject mapping data

        """
        logger.info("Restoring status mapping from OpenProject provenance data...")

        # Query provenance registry for status mappings
        provenance_mappings = self.op_client.restore_entity_mappings_from_provenance("status")

        if not provenance_mappings:
            logger.info("No status provenance data found in OpenProject")
            return {}

        # Convert provenance format to standard mapping format
        mapping: dict[str, Any] = {}
        for jira_status_id, prov_data in provenance_mappings.items():
            mapping[jira_status_id] = {
                "jira_id": int(jira_status_id) if jira_status_id.isdigit() else jira_status_id,
                "jira_name": prov_data.get("jira_name"),
                "openproject_id": prov_data.get("openproject_id"),
                "matched_by": "j2o_provenance",
                "restored_from_op": True,
                "provenance_wp_id": prov_data.get("provenance_wp_id"),
            }

        # Persist mapping
        config.mappings.set_mapping("status", mapping)
        self.status_mapping = mapping

        # Update the mappings instance if available
        if self.mappings is not None:
            self.mappings.status_mapping = mapping

        logger.info("Restored %d status mappings from OpenProject provenance", len(mapping))
        return mapping

    def run(self) -> ComponentResult:
        """Run the status migration process.

        Returns:
            ComponentResult with migration results

        """
        logger.info("Running status migration...")

        self.status_mapping = {}

        # Check if mappings exists and has status_mapping attribute
        if hasattr(config, "mappings") and config.mappings is not None and hasattr(config.mappings, "status_mapping"):
            self.status_mapping = config.mappings.status_mapping.copy()

        try:
            # Step 1: Extract Jira statuses
            self.jira_statuses = self.extract_jira_statuses()
            if not self.jira_statuses:
                msg = "No Jira statuses found"
                logger.error(msg)
                raise MigrationError(msg)

            # Step 2: Extract Jira status categories
            self.jira_status_categories = self.extract_status_categories()

            # Step 3: Get OpenProject statuses
            self.op_statuses = self.get_openproject_statuses()

            migration_result = self.migrate_statuses()

            return ComponentResult(
                success=migration_result.get("status", "success") == "success",
                message=migration_result.get("message", "Status migration completed"),
                success_count=migration_result.get("created_count", 0)
                + migration_result.get("already_exists_count", 0),
                failed_count=migration_result.get("error_count", 0),
                total_count=migration_result.get("total_processed", 0),
                details=migration_result,
            )

        except Exception as e:
            logger.exception("Error during status migration: %s", e)
            return ComponentResult(
                success=False,
                errors=[f"Error during status migration: {e}"],
                success_count=0,
                failed_count=0,
                total_count=0,
            )
