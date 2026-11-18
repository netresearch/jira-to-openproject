#!/usr/bin/env python3
# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

from src.display import configure_logging

"""Change detection system for idempotent migration operations.

This module provides functionality to detect changes in Jira entities between
migration runs, enabling incremental and idempotent migration operations.
"""

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from src import config


# Type definitions for change detection
class EntitySnapshot(TypedDict):
    """Represents a snapshot of an entity at a specific point in time."""

    entity_id: str
    entity_type: str
    last_modified: str | None
    checksum: str
    data: dict[str, Any]
    snapshot_timestamp: str


class EntityChange(TypedDict):
    """Represents a detected change in an entity."""

    entity_id: str
    entity_type: str
    change_type: str  # 'created', 'updated', 'deleted'
    old_data: dict[str, Any] | None
    new_data: dict[str, Any] | None
    priority: int  # 1-10, higher means more important


class ChangeReport(TypedDict):
    """Complete change detection report."""

    detection_timestamp: str
    baseline_snapshot_timestamp: str | None
    total_changes: int
    changes_by_type: dict[str, int]
    changes: list[EntityChange]
    summary: dict[str, Any]


class ChangeDetector:
    """Detects changes in Jira entities for idempotent migration operations.

    This class provides functionality to:
    - Store snapshots of Jira entities after successful migrations
    - Compare current entity state with stored snapshots
    - Identify created, updated, and deleted entities
    - Generate prioritized change reports
    """

    def __init__(self, snapshot_dir: Path | None = None) -> None:
        """Initialize the change detector.

        Args:
            snapshot_dir: Directory to store entity snapshots.
                         Defaults to var/snapshots/

        """
        self.logger = configure_logging("INFO", None)
        self.snapshot_dir = snapshot_dir or config.get_path("data").parent / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

        # Ensure snapshots directory structure exists
        (self.snapshot_dir / "current").mkdir(exist_ok=True)
        (self.snapshot_dir / "archive").mkdir(exist_ok=True)

    def _calculate_entity_checksum(self, entity_data: dict[str, Any]) -> str:
        """Calculate a checksum for an entity to detect changes.

        Args:
            entity_data: The entity data to checksum

        Returns:
            SHA256 checksum of the entity data

        """
        # Create a normalized representation for checksum calculation
        # Remove fields that change frequently but don't indicate real changes
        normalized_data = entity_data.copy()

        # Remove timestamp fields that change on every API call
        fields_to_ignore = [
            "self",
            "lastViewed",
            "expand",
            "renderedFields",
            "transitions",
            "operations",
            "editmeta",
        ]

        for field in fields_to_ignore:
            normalized_data.pop(field, None)

        # Sort the dictionary to ensure consistent ordering
        normalized_json = json.dumps(
            normalized_data,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(normalized_json.encode("utf-8")).hexdigest()

    def _get_entity_last_modified(self, entity_data: dict[str, Any]) -> str | None:
        """Extract last modified timestamp from entity data.

        Args:
            entity_data: The entity data

        Returns:
            Last modified timestamp or None if not available

        """
        # Common timestamp fields in Jira entities
        timestamp_fields = ["updated", "lastModified", "created"]

        for field in timestamp_fields:
            if field in entity_data:
                return entity_data[field]

        return None

    def create_snapshot(
        self,
        entities: list[dict[str, Any]],
        entity_type: str,
        migration_component: str | None = None,
    ) -> Path:
        """Create a snapshot of entities after a successful migration.

        Args:
            entities: List of entities to snapshot
            entity_type: Type of entities (e.g., 'users', 'projects', 'issues')
            migration_component: Name of the migration component creating snapshot

        Returns:
            Path to the created snapshot file

        """
        timestamp = datetime.now(tz=UTC).isoformat()

        snapshots: list[EntitySnapshot] = []

        for entity in entities:
            entity_id = self._get_entity_id(entity, entity_type)
            if not entity_id:
                self.logger.warning("Skipping entity without ID in %s", entity_type)
                continue

            snapshot = EntitySnapshot(
                entity_id=entity_id,
                entity_type=entity_type,
                last_modified=self._get_entity_last_modified(entity),
                checksum=self._calculate_entity_checksum(entity),
                data=entity,
                snapshot_timestamp=timestamp,
            )
            snapshots.append(snapshot)

        # Save snapshot to file
        snapshot_filename = f"{entity_type}_{timestamp.replace(':', '-')}.json"
        snapshot_path = self.snapshot_dir / "archive" / snapshot_filename

        snapshot_data = {
            "timestamp": timestamp,
            "entity_type": entity_type,
            "migration_component": migration_component,
            "entity_count": len(snapshots),
            "snapshots": snapshots,
        }

        def json_serializer(obj: Any) -> str:
            """Handle non-serializable objects like Jira PropertyHolder."""
            if hasattr(obj, "__dict__"):
                return str(obj)
            return repr(obj)

        with snapshot_path.open("w") as f:
            json.dump(snapshot_data, f, indent=2, default=json_serializer)

        # Update current snapshot pointer
        current_snapshot_path = self.snapshot_dir / "current" / f"{entity_type}.json"
        with current_snapshot_path.open("w") as f:
            json.dump(
                {
                    "latest_snapshot": snapshot_filename,
                    "timestamp": timestamp,
                    "entity_count": len(snapshots),
                },
                f,
                indent=2,
            )

        self.logger.info(
            "Created snapshot for %d %s entities: %s",
            len(snapshots),
            entity_type,
            snapshot_path,
        )

        return snapshot_path

    def _get_entity_id(self, entity: dict[str, Any], entity_type: str) -> str | None:
        """Extract entity ID from entity data based on entity type.

        Args:
            entity: The entity data
            entity_type: Type of entity

        Returns:
            Entity ID or None if not found

        """
        # Common ID fields for different entity types
        # Note: For users, 'key' is more reliable than 'accountId' as many
        # Jira Server/DC instances have null accountId for all users
        id_field_mapping = {
            "users": "key",
            "projects": "key",
            "issues": "key",
            "worklogs": "id",
            "comments": "id",
            "attachments": "id",
            "statuses": "id",
            "issuetypes": "id",
            "priorities": "id",
            "resolutions": "id",
            "customfields": "id",
        }

        # Try specific field for entity type
        id_field = id_field_mapping.get(entity_type, "id")
        if id_field in entity and entity[id_field] is not None:
            return str(entity[id_field])

        # Fallback to common ID fields
        for field in ["id", "key", "accountId", "name"]:
            if field in entity and entity[field] is not None:
                return str(entity[field])

        return None

    def detect_changes(
        self,
        current_entities: list[dict[str, Any]],
        entity_type: str,
    ) -> ChangeReport:
        """Detect changes between current entities and stored snapshot.

        Args:
            current_entities: Current entities from Jira
            entity_type: Type of entities being compared

        Returns:
            Change detection report

        """
        return self._compare_snapshot(current_entities, entity_type, self._load_baseline_snapshot(entity_type))

    def _compare_snapshot(
        self,
        current_entities: list[dict[str, Any]],
        entity_type: str,
        baseline: dict[str, Any] | None,
    ) -> ChangeReport:
        detection_timestamp = datetime.now(tz=UTC).isoformat()
        baseline = baseline or {}
        baseline_timestamp = baseline.get("timestamp")
        baseline_entities = {snapshot["entity_id"]: snapshot for snapshot in baseline.get("snapshots", [])}

        # Build current entities lookup
        current_entities_lookup = {
            identifier: entity
            for entity in current_entities
            if (identifier := self._get_entity_id(entity, entity_type))
        }

        changes: list[EntityChange] = []

        # Find created and updated entities
        for entity_id, current_entity in current_entities_lookup.items():
            previous = baseline_entities.get(entity_id)
            if previous and self._calculate_entity_checksum(current_entity) == previous["checksum"]:
                continue
            change_type = "updated" if previous else "created"
            changes.append(
                EntityChange(
                    entity_id=entity_id,
                    entity_type=entity_type,
                    change_type=change_type,
                    old_data=previous["data"] if previous else None,
                    new_data=current_entity,
                    priority=self._calculate_change_priority(entity_type, change_type, current_entity),
                )
            )

        # Find deleted entities
        for entity_id, baseline_entity in baseline_entities.items():
            if entity_id not in current_entities_lookup:
                change = EntityChange(
                    entity_id=entity_id,
                    entity_type=entity_type,
                    change_type="deleted",
                    old_data=baseline_entity["data"],
                    new_data=None,
                    priority=self._calculate_change_priority(
                        entity_type,
                        "deleted",
                        None,
                    ),
                )
                changes.append(change)

        # Calculate summary statistics
        changes_by_type = dict(Counter(change["change_type"] for change in changes))

        # Sort changes by priority (highest first)
        changes.sort(key=lambda x: x["priority"], reverse=True)

        return ChangeReport(
            detection_timestamp=detection_timestamp,
            baseline_snapshot_timestamp=baseline_timestamp,
            total_changes=len(changes),
            changes_by_type=changes_by_type,
            changes=changes,
            summary={
                "baseline_entity_count": len(baseline_entities),
                "current_entity_count": len(current_entities_lookup),
                "entities_created": changes_by_type.get("created", 0),
                "entities_updated": changes_by_type.get("updated", 0),
                "entities_deleted": changes_by_type.get("deleted", 0),
            },
        )

    def _load_baseline_snapshot(self, entity_type: str) -> dict[str, Any] | None:
        """Load the baseline snapshot for an entity type.

        Args:
            entity_type: Type of entities

        Returns:
            Baseline snapshot data or None if not found

        """
        current_snapshot_path = self.snapshot_dir / "current" / f"{entity_type}.json"

        if not current_snapshot_path.exists():
            self.logger.debug("No baseline snapshot found for %s", entity_type)
            return None

        try:
            with current_snapshot_path.open("r") as f:
                current_info = json.load(f)

            snapshot_filename = current_info.get("latest_snapshot")
            if not snapshot_filename:
                return None

            snapshot_path = self.snapshot_dir / "archive" / snapshot_filename
            if not snapshot_path.exists():
                self.logger.warning("Snapshot file not found: %s", snapshot_path)
                return None

            with snapshot_path.open("r") as f:
                return json.load(f)

        except (json.JSONDecodeError, KeyError) as e:
            self.logger.warning(
                "Error loading baseline snapshot for %s: %s",
                entity_type,
                e,
            )
            return None

    def _calculate_change_priority(
        self,
        entity_type: str,
        change_type: str,
        new_data: dict[str, Any] | None,
    ) -> int:
        """Calculate priority for a detected change.

        Args:
            entity_type: Type of entity
            change_type: Type of change (created, updated, deleted)
            new_data: New entity data

        Returns:
            Priority score (1-10, higher is more important)

        """
        base_priority = {
            "projects": 9,  # High priority - affects everything else
            "users": 8,  # High priority - affects assignments
            "customfields": 7,  # Medium-high priority - affects data structure
            "issuetypes": 6,  # Medium priority - affects work packages
            "statuses": 6,  # Medium priority - affects workflows
            "issues": 5,  # Medium priority - core content
            "worklogs": 4,  # Medium-low priority - time tracking
            "comments": 3,  # Lower priority - additional content
            "attachments": 3,  # Lower priority - files
        }.get(entity_type, 5)

        # Adjust priority based on change type
        change_type_modifier = {
            "deleted": 2,  # Deletions are high priority
            "created": 1,  # Creations are medium priority
            "updated": 0,  # Updates are base priority
        }.get(change_type, 0)

        # Additional priority adjustments based on entity content
        content_modifier = 0
        if new_data:
            # Check for critical fields that indicate important changes
            if entity_type == "projects" and new_data.get("archived", False):
                content_modifier += 2  # Project archival is important
            elif entity_type == "users" and not new_data.get("active", True):
                content_modifier += 1  # User deactivation is notable

        return min(
            10,
            max(1, base_priority + change_type_modifier + content_modifier),
        )

    def get_changes_since_timestamp(
        self,
        entity_type: str,
        since_timestamp: str,
    ) -> ChangeReport | None:
        """Compare the latest captured state with the newest snapshot at or before a timestamp.

        Args:
            entity_type: Type of entities
            since_timestamp: ISO timestamp to compare against

        Returns:
            Change report or None if no baseline found

        """
        latest = self._load_baseline_snapshot(entity_type)
        if latest is None:
            return None
        cutoff = self._utc_timestamp(since_timestamp)
        baseline = None
        baseline_time = None
        for path in (self.snapshot_dir / "archive").glob("*.json"):
            if not path.name.startswith(f"{entity_type}_"):
                continue
            try:
                snapshot = json.loads(path.read_bytes())
                captured = self._utc_timestamp(snapshot["timestamp"])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.logger.warning("Could not read archived snapshot %s: %s", path, exc)
                continue
            if snapshot.get("entity_type") != entity_type or captured > cutoff:
                continue
            if baseline_time is None or captured > baseline_time:
                baseline, baseline_time = snapshot, captured
        if baseline is None:
            return None
        entities = [record["data"] for record in latest["snapshots"]]
        return self._compare_snapshot(entities, entity_type, baseline)

    @staticmethod
    def _utc_timestamp(value: str) -> datetime:
        timestamp = datetime.fromisoformat(value)
        return timestamp.astimezone(UTC) if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)

    def cleanup_old_snapshots(self, keep_days: int = 30) -> int:
        """Clean up old snapshot files to save disk space.

        Args:
            keep_days: Number of days of snapshots to keep

        Returns:
            Number of files deleted

        """
        cutoff_timestamp = datetime.now(tz=UTC).timestamp() - (keep_days * 24 * 60 * 60)
        deleted_count = 0

        archive_dir = self.snapshot_dir / "archive"
        if not archive_dir.exists():
            return 0

        for snapshot_file in archive_dir.glob("*.json"):
            if snapshot_file.stat().st_mtime < cutoff_timestamp:
                try:
                    snapshot_file.unlink()
                    deleted_count += 1
                    self.logger.debug("Deleted old snapshot: %s", snapshot_file.name)
                except OSError as e:
                    self.logger.warning(
                        "Failed to delete snapshot %s: %s",
                        snapshot_file.name,
                        e,
                    )

        if deleted_count > 0:
            self.logger.info("Cleaned up %d old snapshot files", deleted_count)

        return deleted_count
