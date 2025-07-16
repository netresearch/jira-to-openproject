#!/usr/bin/env python3
"""Data preservation system for protecting manually modified OpenProject data.

This module provides functionality to detect conflicts between Jira changes and
manual OpenProject modifications, implementing configurable resolution strategies
to preserve user data while maintaining migration synchronization.
"""

import hashlib
import json
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, TypedDict

from src import config


class ConflictResolution(Enum):
    """Strategies for resolving conflicts between Jira and OpenProject changes."""

    JIRA_WINS = "jira_wins"  # Jira data takes precedence
    OPENPROJECT_WINS = "openproject_wins"  # OpenProject data takes precedence
    MERGE = "merge"  # Attempt to merge changes
    PROMPT_USER = "prompt_user"  # Ask user for resolution
    SKIP = "skip"  # Skip update entirely


class MergeStrategy(Enum):
    """Strategies for merging conflicting field values."""

    LATEST_TIMESTAMP = "latest_timestamp"  # Use most recently modified value
    LONGEST_VALUE = "longest_value"  # Use longer text value
    CONCATENATE = "concatenate"  # Combine both values
    CUSTOM = "custom"  # Use custom merge logic


class EntityChangeType(Enum):
    """Types of entity changes detected."""

    CREATED = "created"  # Entity was created manually
    MODIFIED = "modified"  # Entity was modified manually
    DELETED = "deleted"  # Entity was deleted manually
    UNCHANGED = "unchanged"  # No manual changes detected


class PreservationPolicy(TypedDict):
    """Configuration for data preservation behavior per entity type."""

    entity_type: str
    conflict_resolution: ConflictResolution
    merge_strategy: MergeStrategy
    protected_fields: list[str]  # Fields that should never be overwritten
    merge_fields: list[str]  # Fields that can be merged
    track_changes: bool  # Whether to track manual changes
    backup_before_update: bool  # Create backup before any updates


class ConflictInfo(TypedDict):
    """Information about a detected conflict."""

    entity_id: str
    entity_type: str
    jira_changes: dict[str, Any]  # Changes detected from Jira
    openproject_changes: dict[str, Any]  # Manual changes in OpenProject
    conflicted_fields: list[str]  # Fields that have conflicts
    resolution_strategy: ConflictResolution
    timestamp: str


class ChangeSnapshot(TypedDict):
    """Snapshot of an entity's state at a specific time."""

    entity_id: str
    entity_type: str
    timestamp: str
    checksum: str
    data: dict[str, Any]
    source: str  # "migration" or "manual"


class ConflictReport(TypedDict):
    """Report of all conflicts detected during preservation analysis."""

    total_conflicts: int
    conflicts_by_type: dict[str, int]
    conflicts_by_resolution: dict[str, int]
    conflicts: list[ConflictInfo]
    timestamp: str


class DataPreservationManager:
    """Manages data preservation safeguards for idempotent migration operations.

    This class provides functionality to:
    - Detect manually added or modified data in OpenProject
    - Identify conflicts between Jira changes and OpenProject changes
    - Apply configurable resolution strategies
    - Preserve user data while maintaining migration synchronization
    """

    def __init__(
        self,
        preservation_dir: Path | None = None,
        jira_client: Any = None,
        openproject_client: Any = None,
    ) -> None:
        """Initialize the data preservation manager.

        Args:
            preservation_dir: Directory to store preservation data
            jira_client: Jira client for data comparison
            openproject_client: OpenProject client for data comparison
        """
        self.logger = config.logger
        self.preservation_dir = (
            preservation_dir or config.get_path("data").parent / "preservation"
        )
        self.preservation_dir.mkdir(parents=True, exist_ok=True)

        # Create directory structure
        (self.preservation_dir / "original_states").mkdir(exist_ok=True)
        (self.preservation_dir / "conflicts").mkdir(exist_ok=True)
        (self.preservation_dir / "policies").mkdir(exist_ok=True)
        (self.preservation_dir / "backups").mkdir(exist_ok=True)

        # Store client references for data comparison
        self.jira_client = jira_client
        self.openproject_client = openproject_client

        # Load preservation policies
        self._load_preservation_policies()

    def _load_preservation_policies(self) -> None:
        """Load preservation policies from configuration."""
        policies_file = (
            self.preservation_dir / "policies" / "preservation_policies.json"
        )

        # Default policies for common entity types
        default_policies = {
            "users": PreservationPolicy(
                entity_type="users",
                conflict_resolution=ConflictResolution.OPENPROJECT_WINS,
                merge_strategy=MergeStrategy.LATEST_TIMESTAMP,
                protected_fields=["password", "last_login", "admin_status"],
                merge_fields=["firstname", "lastname", "mail"],
                track_changes=True,
                backup_before_update=True,
            ),
            "projects": PreservationPolicy(
                entity_type="projects",
                conflict_resolution=ConflictResolution.MERGE,
                merge_strategy=MergeStrategy.CUSTOM,
                protected_fields=["created_on", "updated_on", "status"],
                merge_fields=["description", "homepage"],
                track_changes=True,
                backup_before_update=True,
            ),
            "work_packages": PreservationPolicy(
                entity_type="work_packages",
                conflict_resolution=ConflictResolution.MERGE,
                merge_strategy=MergeStrategy.LATEST_TIMESTAMP,
                protected_fields=["created_on", "updated_on"],
                merge_fields=["subject", "description", "estimated_hours"],
                track_changes=True,
                backup_before_update=True,
            ),
            "custom_fields": PreservationPolicy(
                entity_type="custom_fields",
                conflict_resolution=ConflictResolution.JIRA_WINS,
                merge_strategy=MergeStrategy.LATEST_TIMESTAMP,
                protected_fields=["created_on"],
                merge_fields=["name", "description"],
                track_changes=False,
                backup_before_update=False,
            ),
        }

        if policies_file.exists():
            try:
                with policies_file.open() as f:
                    loaded_policies = json.load(f)

                    # Convert string enum values back to enum objects
                    converted_policies = {}
                    for entity_type, policy in loaded_policies.items():
                        converted_policy = policy.copy()

                        # Convert conflict_resolution string to enum
                        if isinstance(policy.get("conflict_resolution"), str):
                            try:
                                converted_policy["conflict_resolution"] = (
                                    ConflictResolution(policy["conflict_resolution"])
                                )
                            except ValueError:
                                self.logger.warning(
                                    "Invalid conflict_resolution value '%s' for %s, using default",
                                    policy["conflict_resolution"],
                                    entity_type,
                                )
                                converted_policy["conflict_resolution"] = (
                                    ConflictResolution.PROMPT_USER
                                )

                        # Convert merge_strategy string to enum
                        if isinstance(policy.get("merge_strategy"), str):
                            try:
                                converted_policy["merge_strategy"] = MergeStrategy(
                                    policy["merge_strategy"]
                                )
                            except ValueError:
                                self.logger.warning(
                                    "Invalid merge_strategy value '%s' for %s, using default",
                                    policy["merge_strategy"],
                                    entity_type,
                                )
                                converted_policy["merge_strategy"] = (
                                    MergeStrategy.LATEST_TIMESTAMP
                                )

                        converted_policies[entity_type] = converted_policy

                    # Merge with defaults, allowing overrides
                    self.preservation_policies = {
                        **default_policies,
                        **converted_policies,
                    }
            except Exception as e:
                self.logger.warning("Failed to load preservation policies: %s", e)
                self.preservation_policies = default_policies
        else:
            self.preservation_policies = default_policies
            self._save_preservation_policies()

    def _save_preservation_policies(self) -> None:
        """Save current preservation policies to configuration."""
        policies_file = (
            self.preservation_dir / "policies" / "preservation_policies.json"
        )

        # Convert enum values to strings for JSON serialization
        serializable_policies = {}
        for entity_type, policy in self.preservation_policies.items():
            serializable_policies[entity_type] = {
                "entity_type": policy["entity_type"],
                "conflict_resolution": policy["conflict_resolution"].value,
                "merge_strategy": policy["merge_strategy"].value,
                "protected_fields": policy["protected_fields"],
                "merge_fields": policy["merge_fields"],
                "track_changes": policy["track_changes"],
                "backup_before_update": policy["backup_before_update"],
            }

        try:
            with policies_file.open("w") as f:
                json.dump(serializable_policies, f, indent=2)
        except Exception as e:
            self.logger.warning("Failed to save preservation policies: %s", e)

    def _calculate_entity_checksum(self, entity_data: dict[str, Any]) -> str:
        """Calculate checksum for entity data to detect changes.

        Args:
            entity_data: Entity data to checksum

        Returns:
            SHA256 checksum of normalized entity data
        """
        # Create normalized data excluding volatile fields
        normalized_data = entity_data.copy()

        # Remove fields that change frequently but don't indicate real changes
        volatile_fields = [
            "self",
            "lastViewed",
            "expand",
            "transitions",
            "operations",
            "editmeta",
            "renderedFields",
            "updated_on",
            "last_activity_at",
        ]

        for field in volatile_fields:
            normalized_data.pop(field, None)

        # Sort for consistent ordering
        normalized_json = json.dumps(
            normalized_data, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(normalized_json.encode("utf-8")).hexdigest()

    def store_original_state(
        self,
        entity_id: str,
        entity_type: str,
        entity_data: dict[str, Any],
        source: str = "migration",
    ) -> None:
        """Store the original state of an entity after migration.

        Args:
            entity_id: Unique identifier for the entity
            entity_type: Type of entity (users, projects, work_packages, etc.)
            entity_data: Current state of the entity
            source: Source of the data ("migration" or "manual")
        """
        timestamp = datetime.now(tz=UTC).isoformat()
        checksum = self._calculate_entity_checksum(entity_data)

        snapshot = ChangeSnapshot(
            entity_id=entity_id,
            entity_type=entity_type,
            timestamp=timestamp,
            checksum=checksum,
            data=entity_data,
            source=source,
        )

        # Store in entity-type specific directory
        entity_dir = self.preservation_dir / "original_states" / entity_type
        entity_dir.mkdir(exist_ok=True)

        snapshot_file = entity_dir / f"{entity_id}.json"

        try:
            with snapshot_file.open("w") as f:
                json.dump(snapshot, f, indent=2)

            self.logger.debug(
                "Stored original state for %s %s (checksum: %s)",
                entity_type,
                entity_id,
                checksum[:8],
            )
        except Exception as e:
            self.logger.warning(
                "Failed to store original state for %s %s: %s",
                entity_type,
                entity_id,
                e,
            )

    def detect_openproject_changes(
        self, entity_id: str, entity_type: str, current_data: dict[str, Any]
    ) -> EntityChangeType:
        """Detect if an entity has been manually modified in OpenProject.

        Args:
            entity_id: Entity identifier
            entity_type: Type of entity
            current_data: Current entity data from OpenProject

        Returns:
            Type of change detected
        """
        entity_dir = self.preservation_dir / "original_states" / entity_type
        snapshot_file = entity_dir / f"{entity_id}.json"

        if not snapshot_file.exists():
            # No original state stored - entity was created manually
            return EntityChangeType.CREATED

        try:
            with snapshot_file.open() as f:
                stored_snapshot = json.load(f)

            # Calculate checksum of current data
            current_checksum = self._calculate_entity_checksum(current_data)

            if current_checksum == stored_snapshot["checksum"]:
                return EntityChangeType.UNCHANGED
            else:
                return EntityChangeType.MODIFIED

        except Exception as e:
            self.logger.warning(
                "Failed to check changes for %s %s: %s", entity_type, entity_id, e
            )
            # Assume unchanged if we can't determine
            return EntityChangeType.UNCHANGED

    def detect_conflicts(
        self,
        jira_changes: dict[str, Any],
        entity_id: str,
        entity_type: str,
        current_openproject_data: dict[str, Any],
    ) -> ConflictInfo | None:
        """Detect conflicts between Jira changes and OpenProject modifications.

        Args:
            jira_changes: Changes detected in Jira
            entity_id: Entity identifier
            entity_type: Type of entity
            current_openproject_data: Current OpenProject entity data

        Returns:
            ConflictInfo if conflict detected, None otherwise
        """
        # Check if entity was manually modified
        op_change_type = self.detect_openproject_changes(
            entity_id, entity_type, current_openproject_data
        )

        if op_change_type == EntityChangeType.UNCHANGED:
            # No manual changes in OpenProject, no conflict
            return None

        # Get original state for comparison
        entity_dir = self.preservation_dir / "original_states" / entity_type
        snapshot_file = entity_dir / f"{entity_id}.json"

        if not snapshot_file.exists():
            # No original state - treat as manual creation
            openproject_changes = current_openproject_data
        else:
            try:
                with snapshot_file.open() as f:
                    stored_snapshot = json.load(f)

                # Calculate what changed in OpenProject
                original_data = stored_snapshot["data"]
                openproject_changes = self._calculate_field_changes(
                    original_data, current_openproject_data
                )
            except Exception as e:
                self.logger.warning("Failed to load original state: %s", e)
                openproject_changes = current_openproject_data

        # Find conflicted fields (changed in both systems)
        conflicted_fields = []
        for field in jira_changes.keys():
            if field in openproject_changes:
                conflicted_fields.append(field)

        if not conflicted_fields:
            # No actual field conflicts
            return None

        # Get resolution strategy for this entity type
        policy = self.preservation_policies.get(entity_type)
        resolution_strategy = (
            policy["conflict_resolution"] if policy else ConflictResolution.PROMPT_USER
        )

        return ConflictInfo(
            entity_id=entity_id,
            entity_type=entity_type,
            jira_changes=jira_changes,
            openproject_changes=openproject_changes,
            conflicted_fields=conflicted_fields,
            resolution_strategy=resolution_strategy,
            timestamp=datetime.now(tz=UTC).isoformat(),
        )

    def _calculate_field_changes(
        self, original_data: dict[str, Any], current_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Calculate field-level changes between two entity states.

        Args:
            original_data: Original entity data
            current_data: Current entity data

        Returns:
            Dictionary of changed fields with their new values
        """
        changes = {}

        for field, current_value in current_data.items():
            original_value = original_data.get(field)
            if original_value != current_value:
                changes[field] = current_value

        return changes

    def resolve_conflict(
        self,
        conflict: ConflictInfo,
        jira_data: dict[str, Any],
        openproject_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve a conflict between Jira and OpenProject data.

        Args:
            conflict: Conflict information
            jira_data: Current Jira entity data
            openproject_data: Current OpenProject entity data

        Returns:
            Resolved entity data
        """
        policy = self.preservation_policies.get(conflict["entity_type"])
        if not policy:
            self.logger.warning(
                "No preservation policy for %s, using PROMPT_USER",
                conflict["entity_type"],
            )
            # Default to preserving OpenProject data
            return openproject_data

        resolution = policy["conflict_resolution"]

        if resolution == ConflictResolution.JIRA_WINS:
            return self._apply_jira_wins_resolution(
                conflict, jira_data, openproject_data, policy
            )
        elif resolution == ConflictResolution.OPENPROJECT_WINS:
            return self._apply_openproject_wins_resolution(
                conflict, jira_data, openproject_data, policy
            )
        elif resolution == ConflictResolution.MERGE:
            return self._apply_merge_resolution(
                conflict, jira_data, openproject_data, policy
            )
        elif resolution == ConflictResolution.SKIP:
            # Don't update at all
            return openproject_data
        else:  # PROMPT_USER
            # For now, default to OpenProject wins (would need UI for user prompts)
            self.logger.warning(
                "User prompt not implemented, defaulting to OpenProject wins for %s",
                conflict["entity_id"],
            )
            return self._apply_openproject_wins_resolution(
                conflict, jira_data, openproject_data, policy
            )

    def _apply_jira_wins_resolution(
        self,
        conflict: ConflictInfo,
        jira_data: dict[str, Any],
        openproject_data: dict[str, Any],
        policy: PreservationPolicy,
    ) -> dict[str, Any]:
        """Apply Jira wins resolution strategy.

        Args:
            conflict: Conflict information
            jira_data: Jira entity data
            openproject_data: OpenProject entity data
            policy: Preservation policy

        Returns:
            Resolved entity data
        """
        resolved_data = openproject_data.copy()

        # Update with Jira data, respecting protected fields
        for field, value in jira_data.items():
            if field not in policy["protected_fields"]:
                resolved_data[field] = value

        return resolved_data

    def _apply_openproject_wins_resolution(
        self,
        conflict: ConflictInfo,
        jira_data: dict[str, Any],
        openproject_data: dict[str, Any],
        policy: PreservationPolicy,
    ) -> dict[str, Any]:
        """Apply OpenProject wins resolution strategy.

        Args:
            conflict: Conflict information
            jira_data: Jira entity data
            openproject_data: OpenProject entity data
            policy: Preservation policy

        Returns:
            Resolved entity data
        """
        resolved_data = openproject_data.copy()

        # Only add non-conflicted fields from Jira
        for field, value in jira_data.items():
            if (
                field not in conflict["conflicted_fields"]
                and field not in policy["protected_fields"]
            ):
                resolved_data[field] = value

        return resolved_data

    def _apply_merge_resolution(
        self,
        conflict: ConflictInfo,
        jira_data: dict[str, Any],
        openproject_data: dict[str, Any],
        policy: PreservationPolicy,
    ) -> dict[str, Any]:
        """Apply merge resolution strategy.

        Args:
            conflict: Conflict information
            jira_data: Jira entity data
            openproject_data: OpenProject entity data
            policy: Preservation policy

        Returns:
            Merged entity data
        """
        resolved_data = openproject_data.copy()

        # Process conflicted fields with merge logic
        for field in conflict["conflicted_fields"]:
            if field in policy["protected_fields"]:
                # Keep OpenProject value for protected fields
                continue
            elif field in policy["merge_fields"]:
                # Apply merge strategy
                resolved_data[field] = self._merge_field_values(
                    field,
                    jira_data.get(field),
                    openproject_data.get(field),
                    policy["merge_strategy"],
                )
            else:
                # Default to Jira value for non-protected, non-merge fields
                resolved_data[field] = jira_data.get(field)

        # Add non-conflicted fields from Jira
        for field, value in jira_data.items():
            if (
                field not in conflict["conflicted_fields"]
                and field not in policy["protected_fields"]
            ):
                resolved_data[field] = value

        return resolved_data

    def _merge_field_values(
        self,
        field_name: str,
        jira_value: Any,
        openproject_value: Any,
        merge_strategy: MergeStrategy,
    ) -> Any:
        """Merge conflicting field values using the specified strategy.

        Args:
            field_name: Name of the field being merged
            jira_value: Value from Jira
            openproject_value: Value from OpenProject
            merge_strategy: Strategy to use for merging

        Returns:
            Merged value
        """
        if jira_value is None:
            return openproject_value
        if openproject_value is None:
            return jira_value

        if merge_strategy == MergeStrategy.LATEST_TIMESTAMP:
            # For now, default to OpenProject value (would need timestamp comparison)
            return openproject_value
        elif merge_strategy == MergeStrategy.LONGEST_VALUE:
            jira_len = len(str(jira_value))
            op_len = len(str(openproject_value))
            return jira_value if jira_len > op_len else openproject_value
        elif merge_strategy == MergeStrategy.CONCATENATE:
            if isinstance(jira_value, str) and isinstance(openproject_value, str):
                return f"{openproject_value}\n\n[Merged from Jira]: {jira_value}"
            else:
                return openproject_value
        else:  # CUSTOM
            # Implement custom merge logic per field type
            return self._custom_merge_logic(field_name, jira_value, openproject_value)

    def _custom_merge_logic(
        self, field_name: str, jira_value: Any, openproject_value: Any
    ) -> Any:
        """Apply custom merge logic for specific fields.

        Args:
            field_name: Name of the field
            jira_value: Value from Jira
            openproject_value: Value from OpenProject

        Returns:
            Merged value using custom logic
        """
        # Custom merge logic for specific fields
        if field_name in ["description", "notes", "comments"]:
            # For text fields, concatenate with clear attribution
            if isinstance(jira_value, str) and isinstance(openproject_value, str):
                return f"{openproject_value}\n\n--- Updated from Jira ---\n{jira_value}"

        # Default to preserving OpenProject value
        return openproject_value

    def create_backup(
        self, entity_id: str, entity_type: str, entity_data: dict[str, Any]
    ) -> Path:
        """Create a backup of entity data before updating.

        Args:
            entity_id: Entity identifier
            entity_type: Type of entity
            entity_data: Entity data to backup

        Returns:
            Path to the backup file
        """
        timestamp = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
        backup_dir = self.preservation_dir / "backups" / entity_type
        backup_dir.mkdir(parents=True, exist_ok=True)

        backup_file = backup_dir / f"{entity_id}_{timestamp}.json"

        backup_data = {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "data": entity_data,
        }

        try:
            with backup_file.open("w") as f:
                json.dump(backup_data, f, indent=2)

            self.logger.debug(
                "Created backup for %s %s: %s", entity_type, entity_id, backup_file
            )
            return backup_file
        except Exception as e:
            self.logger.warning(
                "Failed to create backup for %s %s: %s", entity_type, entity_id, e
            )
            raise

    def analyze_preservation_status(
        self, jira_changes: dict[str, dict[str, Any]], entity_type: str
    ) -> ConflictReport:
        """Analyze potential conflicts for a set of entities.

        Args:
            jira_changes: Dictionary of entity_id -> changes from Jira
            entity_type: Type of entities being analyzed

        Returns:
            Report of all conflicts detected
        """
        conflicts = []
        conflicts_by_resolution = {}

        for entity_id, changes in jira_changes.items():
            if not self.openproject_client:
                # Can't check OpenProject state without client
                continue

            try:
                # Get current OpenProject data (would need actual client implementation)
                current_op_data = self._get_openproject_entity_data(
                    entity_id, entity_type
                )

                if current_op_data:
                    conflict = self.detect_conflicts(
                        changes, entity_id, entity_type, current_op_data
                    )
                    if conflict:
                        conflicts.append(conflict)
                        resolution = conflict["resolution_strategy"].value
                        conflicts_by_resolution[resolution] = (
                            conflicts_by_resolution.get(resolution, 0) + 1
                        )

            except Exception as e:
                self.logger.warning("Failed to analyze entity %s: %s", entity_id, e)

        # Generate statistics
        conflicts_by_type = {}
        for conflict in conflicts:
            entity_type = conflict["entity_type"]
            conflicts_by_type[entity_type] = conflicts_by_type.get(entity_type, 0) + 1

        return ConflictReport(
            total_conflicts=len(conflicts),
            conflicts_by_type=conflicts_by_type,
            conflicts_by_resolution=conflicts_by_resolution,
            conflicts=conflicts,
            timestamp=datetime.now(tz=UTC).isoformat(),
        )

    def _get_openproject_entity_data(
        self, entity_id: str, entity_type: str
    ) -> dict[str, Any] | None:
        """Get current entity data from OpenProject.

        Args:
            entity_id: Entity identifier
            entity_type: Type of entity

        Returns:
            Entity data or None if not found
        """
        # This would need actual OpenProject client implementation
        # For now, return None to indicate we can't fetch data
        if not self.openproject_client:
            return None

        # Placeholder for actual implementation
        try:
            # This would call the appropriate OpenProject client method
            # return self.openproject_client.get_entity(entity_id, entity_type)
            return None
        except Exception:
            return None

    def update_preservation_policy(
        self, entity_type: str, policy_updates: dict[str, Any]
    ) -> None:
        """Update preservation policy for an entity type.

        Args:
            entity_type: Type of entity
            policy_updates: Dictionary of policy fields to update
        """
        if entity_type not in self.preservation_policies:
            self.logger.warning(
                "Unknown entity type for policy update: %s", entity_type
            )
            return

        policy = self.preservation_policies[entity_type]

        for field, value in policy_updates.items():
            if field in policy:
                # Convert string enum values back to enums
                if field == "conflict_resolution":
                    value = ConflictResolution(value)
                elif field == "merge_strategy":
                    value = MergeStrategy(value)

                policy[field] = value
            else:
                self.logger.warning("Unknown policy field: %s", field)

        self._save_preservation_policies()
        self.logger.info("Updated preservation policy for %s", entity_type)
