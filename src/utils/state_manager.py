#!/usr/bin/env python3
"""State preservation system for idempotent migration operations.

This module provides functionality to track entity mappings between Jira and
OpenProject, maintain migration history, and enable rollback capabilities.
"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from src import config


# Type definitions for state management
class EntityMapping(TypedDict):
    """Represents a mapping between Jira and OpenProject entities."""
    mapping_id: str
    jira_entity_type: str
    jira_entity_id: str
    openproject_entity_type: str
    openproject_entity_id: str
    mapped_at: str
    mapped_by: str  # Migration component name
    mapping_version: str
    metadata: dict[str, Any]


class MigrationRecord(TypedDict):
    """Represents a single migration operation record."""
    record_id: str
    migration_component: str
    entity_type: str
    operation_type: str  # 'create', 'update', 'delete', 'migrate'
    started_at: str
    completed_at: str | None
    status: str  # 'started', 'completed', 'failed', 'rolled_back'
    entity_count: int
    success_count: int
    error_count: int
    errors: list[str]
    version: str
    user: str | None
    metadata: dict[str, Any]


class StateSnapshot(TypedDict):
    """Represents a complete state snapshot for rollback purposes."""
    snapshot_id: str
    created_at: str
    created_by: str
    description: str
    mapping_count: int
    record_count: int
    version: str
    metadata: dict[str, Any]


class StateManager:
    """Manages state preservation for idempotent migration operations.

    This class provides functionality to:
    - Track entity mappings between Jira and OpenProject
    - Maintain historical migration information
    - Support versioned state storage with rollback capability
    - Provide tools for state inspection and verification
    """

    def __init__(self, state_dir: Path | None = None) -> None:
        """Initialize the state manager.

        Args:
            state_dir: Directory to store state files.
                      Defaults to var/state/
        """
        self.logger = config.logger
        self.state_dir = state_dir or config.get_path("data").parent / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Ensure state directory structure exists
        (self.state_dir / "mappings").mkdir(exist_ok=True)
        (self.state_dir / "history").mkdir(exist_ok=True)
        (self.state_dir / "snapshots").mkdir(exist_ok=True)
        (self.state_dir / "current").mkdir(exist_ok=True)

        # State tracking
        self._current_mappings: dict[str, EntityMapping] = {}
        self._current_records: list[MigrationRecord] = []
        self._current_version = self._generate_version()

        # Load current state on initialization
        self._load_current_state()

    def _generate_version(self) -> str:
        """Generate a unique version identifier."""
        return f"v{datetime.now(tz=UTC).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    def _generate_id(self) -> str:
        """Generate a unique identifier."""
        return uuid.uuid4().hex

    def register_entity_mapping(
        self,
        jira_entity_type: str,
        jira_entity_id: str,
        openproject_entity_type: str,
        openproject_entity_id: str,
        migration_component: str,
        metadata: dict[str, Any] | None = None
    ) -> str:
        """Register a mapping between Jira and OpenProject entities.

        Args:
            jira_entity_type: Type of Jira entity (e.g., 'project', 'issue', 'user')
            jira_entity_id: Jira entity identifier
            openproject_entity_type: Type of OpenProject entity
            openproject_entity_id: OpenProject entity identifier
            migration_component: Name of the component that created this mapping
            metadata: Additional metadata about the mapping

        Returns:
            Mapping ID for future reference
        """
        mapping_id = self._generate_id()

        mapping = EntityMapping(
            mapping_id=mapping_id,
            jira_entity_type=jira_entity_type,
            jira_entity_id=str(jira_entity_id),
            openproject_entity_type=openproject_entity_type,
            openproject_entity_id=str(openproject_entity_id),
            mapped_at=datetime.now(tz=UTC).isoformat(),
            mapped_by=migration_component,
            mapping_version=self._current_version,
            metadata=metadata or {}
        )

        self._current_mappings[mapping_id] = mapping

        self.logger.debug(
            "Registered entity mapping: %s:%s -> %s:%s (%s)",
            jira_entity_type, jira_entity_id,
            openproject_entity_type, openproject_entity_id,
            mapping_id
        )

        return mapping_id

    def get_entity_mapping(
        self,
        jira_entity_type: str,
        jira_entity_id: str
    ) -> EntityMapping | None:
        """Get entity mapping by Jira entity information.

        Args:
            jira_entity_type: Type of Jira entity
            jira_entity_id: Jira entity identifier

        Returns:
            Entity mapping or None if not found
        """
        for mapping in self._current_mappings.values():
            if (mapping["jira_entity_type"] == jira_entity_type and
                mapping["jira_entity_id"] == str(jira_entity_id)):
                return mapping
        return None

    def get_reverse_mapping(
        self,
        openproject_entity_type: str,
        openproject_entity_id: str
    ) -> EntityMapping | None:
        """Get entity mapping by OpenProject entity information.

        Args:
            openproject_entity_type: Type of OpenProject entity
            openproject_entity_id: OpenProject entity identifier

        Returns:
            Entity mapping or None if not found
        """
        for mapping in self._current_mappings.values():
            if (mapping["openproject_entity_type"] == openproject_entity_type and
                mapping["openproject_entity_id"] == str(openproject_entity_id)):
                return mapping
        return None

    def start_migration_record(
        self,
        migration_component: str,
        entity_type: str,
        operation_type: str,
        entity_count: int = 0,
        user: str | None = None,
        metadata: dict[str, Any] | None = None
    ) -> str:
        """Start a new migration record.

        Args:
            migration_component: Name of the migration component
            entity_type: Type of entities being migrated
            operation_type: Type of operation being performed
            entity_count: Number of entities to be processed
            user: User performing the migration
            metadata: Additional metadata

        Returns:
            Record ID for future reference
        """
        record_id = self._generate_id()

        record = MigrationRecord(
            record_id=record_id,
            migration_component=migration_component,
            entity_type=entity_type,
            operation_type=operation_type,
            started_at=datetime.now(tz=UTC).isoformat(),
            completed_at=None,
            status='started',
            entity_count=entity_count,
            success_count=0,
            error_count=0,
            errors=[],
            version=self._current_version,
            user=user,
            metadata=metadata or {}
        )

        self._current_records.append(record)

        self.logger.info(
            "Started migration record: %s for %s (%s entities)",
            record_id, migration_component, entity_count
        )

        return record_id

    def complete_migration_record(
        self,
        record_id: str,
        success_count: int,
        error_count: int = 0,
        errors: list[str] | None = None
    ) -> None:
        """Complete a migration record.

        Args:
            record_id: Migration record ID
            success_count: Number of successfully processed entities
            error_count: Number of entities that failed
            errors: List of error messages
        """
        record = self._find_record(record_id)
        if not record:
            self.logger.warning("Migration record not found: %s", record_id)
            return

        record["completed_at"] = datetime.now(tz=UTC).isoformat()
        record["status"] = 'completed' if error_count == 0 else 'failed'
        record["success_count"] = success_count
        record["error_count"] = error_count
        record["errors"] = errors or []

        self.logger.info(
            "Completed migration record: %s (%d success, %d errors)",
            record_id, success_count, error_count
        )

    def _find_record(self, record_id: str) -> MigrationRecord | None:
        """Find a migration record by ID."""
        for record in self._current_records:
            if record["record_id"] == record_id:
                return record
        return None

    def create_state_snapshot(
        self,
        description: str,
        user: str | None = None,
        metadata: dict[str, Any] | None = None
    ) -> str:
        """Create a complete state snapshot for rollback purposes.

        Args:
            description: Description of the snapshot
            user: User creating the snapshot
            metadata: Additional metadata

        Returns:
            Snapshot ID
        """
        snapshot_id = self._generate_id()

        snapshot = StateSnapshot(
            snapshot_id=snapshot_id,
            created_at=datetime.now(tz=UTC).isoformat(),
            created_by=user or "system",
            description=description,
            mapping_count=len(self._current_mappings),
            record_count=len(self._current_records),
            version=self._current_version,
            metadata=metadata or {}
        )

        # Save snapshot to file
        snapshot_path = self.state_dir / "snapshots" / f"{snapshot_id}.json"
        snapshot_data = {
            "snapshot": snapshot,
            "mappings": self._current_mappings,
            "records": self._current_records
        }

        with snapshot_path.open('w') as f:
            json.dump(snapshot_data, f, indent=2)

        self.logger.info(
            "Created state snapshot: %s (%d mappings, %d records)",
            snapshot_id, len(self._current_mappings), len(self._current_records)
        )

        return snapshot_id

    def save_current_state(self) -> None:
        """Save current state to persistent storage."""
        try:
            # Save current mappings
            mappings_path = self.state_dir / "current" / "mappings.json"
            with mappings_path.open('w') as f:
                json.dump(self._current_mappings, f, indent=2)

            # Save current records
            records_path = self.state_dir / "current" / "records.json"
            with records_path.open('w') as f:
                json.dump(self._current_records, f, indent=2)

            # Save version info
            version_path = self.state_dir / "current" / "version.json"
            version_info = {
                "version": self._current_version,
                "last_updated": datetime.now(tz=UTC).isoformat(),
                "mapping_count": len(self._current_mappings),
                "record_count": len(self._current_records)
            }
            with version_path.open('w') as f:
                json.dump(version_info, f, indent=2)

            self.logger.debug("Saved current state to persistent storage")

        except Exception as e:
            self.logger.exception("Failed to save current state: %s", e)

    def _load_current_state(self) -> None:
        """Load current state from persistent storage."""
        try:
            # Load current mappings
            mappings_path = self.state_dir / "current" / "mappings.json"
            if mappings_path.exists():
                with mappings_path.open('r') as f:
                    self._current_mappings = json.load(f)

            # Load current records
            records_path = self.state_dir / "current" / "records.json"
            if records_path.exists():
                with records_path.open('r') as f:
                    self._current_records = json.load(f)

            # Load version info
            version_path = self.state_dir / "current" / "version.json"
            if version_path.exists():
                with version_path.open('r') as f:
                    version_info = json.load(f)
                    self._current_version = version_info.get("version", self._current_version)

            self.logger.debug(
                "Loaded current state: %d mappings, %d records, version %s",
                len(self._current_mappings), len(self._current_records), self._current_version
            )

        except Exception as e:
            self.logger.warning("Failed to load current state: %s", e)
            # Continue with empty state if loading fails

    def get_mapping_statistics(self) -> dict[str, Any]:
        """Get statistics about current entity mappings.

        Returns:
            Dictionary with mapping statistics
        """
        stats = {
            "total_mappings": len(self._current_mappings),
            "mappings_by_jira_type": {},
            "mappings_by_openproject_type": {},
            "mappings_by_component": {}
        }

        for mapping in self._current_mappings.values():
            # Count by Jira entity type
            jira_type = mapping["jira_entity_type"]
            stats["mappings_by_jira_type"][jira_type] = stats["mappings_by_jira_type"].get(jira_type, 0) + 1

            # Count by OpenProject entity type
            op_type = mapping["openproject_entity_type"]
            stats["mappings_by_openproject_type"][op_type] = stats["mappings_by_openproject_type"].get(op_type, 0) + 1

            # Count by migration component
            component = mapping["mapped_by"]
            stats["mappings_by_component"][component] = stats["mappings_by_component"].get(component, 0) + 1

        return stats

    def get_migration_history(self, limit: int = 100) -> list[MigrationRecord]:
        """Get recent migration history.

        Args:
            limit: Maximum number of records to return

        Returns:
            List of recent migration records
        """
        # Sort by started_at timestamp, most recent first
        sorted_records = sorted(
            self._current_records,
            key=lambda r: r["started_at"],
            reverse=True
        )
        return sorted_records[:limit]

    def cleanup_old_state(self, keep_days: int = 30) -> int:
        """Clean up old state files to save disk space.

        Args:
            keep_days: Number of days of state files to keep

        Returns:
            Number of files deleted
        """
        cutoff_timestamp = datetime.now(tz=UTC).timestamp() - (keep_days * 24 * 60 * 60)
        deleted_count = 0

        # Clean up old snapshots
        snapshots_dir = self.state_dir / "snapshots"
        if snapshots_dir.exists():
            for snapshot_file in snapshots_dir.glob("*.json"):
                if snapshot_file.stat().st_mtime < cutoff_timestamp:
                    try:
                        snapshot_file.unlink()
                        deleted_count += 1
                        self.logger.debug("Deleted old snapshot: %s", snapshot_file.name)
                    except OSError as e:
                        self.logger.warning("Failed to delete snapshot %s: %s", snapshot_file.name, e)

        if deleted_count > 0:
            self.logger.info("Cleaned up %d old state files", deleted_count)

        return deleted_count
