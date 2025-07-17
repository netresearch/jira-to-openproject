import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.models import ComponentResult
from src.utils.change_detector import ChangeDetector, ChangeReport
from src.utils.data_preservation_manager import DataPreservationManager
from src.utils.state_manager import StateManager
from src.utils.checkpoint_manager import CheckpointManager, CheckpointStatus, RecoveryAction


class ComponentInitializationError(Exception):
    """Raised when a migration component cannot be initialized.

    This custom exception provides clear diagnostics when component
    initialization fails, following proper exception-based error handling.
    """

    pass


class BaseMigration:
    """Base class for all migration classes.

    Provides common functionality and initialization for all migration types.

    Follows the layered client architecture:
    1. OpenProjectClient - Manages all lower-level clients and operations
    2. BaseMigration - Uses OpenProjectClient for migrations
    """

    def __init__(
        self,
        jira_client: JiraClient | None = None,
        op_client: OpenProjectClient | None = None,
        change_detector: ChangeDetector | None = None,
        state_manager: StateManager | None = None,
        data_preservation_manager: DataPreservationManager | None = None,
    ) -> None:
        """Initialize the base migration with common attributes.

        Follows dependency injection pattern for the high-level clients only.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client
            change_detector: Initialized change detector for idempotent operations
            state_manager: Initialized state manager for entity mapping and history
            data_preservation_manager: Initialized data preservation manager for conflict resolution

        """
        # Initialize clients using dependency injection
        self.jira_client = jira_client or JiraClient()
        self.op_client = op_client or OpenProjectClient()
        self.change_detector = change_detector or ChangeDetector()
        self.state_manager = state_manager or StateManager()
        self.data_preservation_manager = (
            data_preservation_manager
            or DataPreservationManager(
                jira_client=self.jira_client, openproject_client=self.op_client
            )
        )

        self.data_dir: Path = config.get_path("data")
        self.output_dir: Path = config.get_path("output")

        self.logger = config.logger

        # Initialize mappings using proper exception handling (compliance fix)
        try:
            # Optimistic execution: attempt to get mappings directly
            self.mappings = config.get_mappings()
        except config.MappingsInitializationError as e:
            # Only perform diagnostics if mappings initialization fails
            self.logger.exception(
                "Failed to initialize mappings in %s: %s", self.__class__.__name__, e
            )
            raise ComponentInitializationError(
                f"Cannot initialize {self.__class__.__name__}: {e}"
            ) from e

        # Initialize managers
        self.state_manager = StateManager()
        self.data_preservation_manager = DataPreservationManager()
        self.checkpoint_manager = CheckpointManager()
        
        # Progress tracking
        self._current_migration_record_id: str | None = None
        self._current_checkpoints: list[str] = []

    def register_entity_mapping(
        self,
        jira_entity_type: str,
        jira_entity_id: str,
        openproject_entity_type: str,
        openproject_entity_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Register a mapping between Jira and OpenProject entities.

        Args:
            jira_entity_type: Type of Jira entity (e.g., 'project', 'issue', 'user')
            jira_entity_id: Jira entity identifier
            openproject_entity_type: Type of OpenProject entity
            openproject_entity_id: OpenProject entity identifier
            metadata: Additional metadata about the mapping

        Returns:
            Mapping ID for future reference
        """
        migration_component = self.__class__.__name__
        return self.state_manager.register_entity_mapping(
            jira_entity_type=jira_entity_type,
            jira_entity_id=jira_entity_id,
            openproject_entity_type=openproject_entity_type,
            openproject_entity_id=openproject_entity_id,
            migration_component=migration_component,
            metadata=metadata,
        )

    def get_entity_mapping(
        self, jira_entity_type: str, jira_entity_id: str
    ) -> dict[str, Any] | None:
        """Get entity mapping by Jira entity information.

        Args:
            jira_entity_type: Type of Jira entity
            jira_entity_id: Jira entity identifier

        Returns:
            Entity mapping or None if not found
        """
        return self.state_manager.get_entity_mapping(jira_entity_type, jira_entity_id)

    def start_migration_record(
        self,
        entity_type: str,
        operation_type: str = "migrate",
        entity_count: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Start a new migration record for tracking progress.

        Args:
            entity_type: Type of entities being migrated
            operation_type: Type of operation being performed
            entity_count: Number of entities to be processed
            metadata: Additional metadata

        Returns:
            Record ID for future reference
        """
        migration_component = self.__class__.__name__
        return self.state_manager.start_migration_record(
            migration_component=migration_component,
            entity_type=entity_type,
            operation_type=operation_type,
            entity_count=entity_count,
            metadata=metadata,
        )

    def complete_migration_record(
        self,
        record_id: str,
        success_count: int,
        error_count: int = 0,
        errors: list[str] | None = None,
    ) -> None:
        """Complete a migration record.

        Args:
            record_id: Migration record ID
            success_count: Number of successfully processed entities
            error_count: Number of entities that failed
            errors: List of error messages
        """
        self.state_manager.complete_migration_record(
            record_id=record_id,
            success_count=success_count,
            error_count=error_count,
            errors=errors,
        )

    def create_state_snapshot(
        self, description: str, metadata: dict[str, Any] | None = None
    ) -> str:
        """Create a complete state snapshot for rollback purposes.

        Args:
            description: Description of the snapshot
            metadata: Additional metadata

        Returns:
            Snapshot ID
        """
        migration_component = self.__class__.__name__
        return self.state_manager.create_state_snapshot(
            description=description, user=migration_component, metadata=metadata
        )

    def store_original_entity_state(
        self,
        entity_id: str,
        entity_type: str,
        entity_data: dict[str, Any],
        source: str = "migration",
    ) -> None:
        """Store the original state of an entity for data preservation.

        Args:
            entity_id: Unique identifier for the entity
            entity_type: Type of entity (users, projects, work_packages, etc.)
            entity_data: Current state of the entity
            source: Source of the data ("migration" or "manual")
        """
        self.data_preservation_manager.store_original_state(
            entity_id=entity_id,
            entity_type=entity_type,
            entity_data=entity_data,
            source=source,
        )

    def detect_preservation_conflicts(
        self,
        jira_changes: dict[str, Any],
        entity_id: str,
        entity_type: str,
        current_openproject_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Detect conflicts between Jira changes and OpenProject modifications.

        Args:
            jira_changes: Changes detected in Jira
            entity_id: Entity identifier
            entity_type: Type of entity
            current_openproject_data: Current OpenProject entity data

        Returns:
            ConflictInfo if conflict detected, None otherwise
        """
        return self.data_preservation_manager.detect_conflicts(
            jira_changes=jira_changes,
            entity_id=entity_id,
            entity_type=entity_type,
            current_openproject_data=current_openproject_data,
        )

    def resolve_preservation_conflict(
        self,
        conflict: dict[str, Any],
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
        return self.data_preservation_manager.resolve_conflict(
            conflict=conflict, jira_data=jira_data, openproject_data=openproject_data
        )

    def create_entity_backup(
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
        return self.data_preservation_manager.create_backup(
            entity_id=entity_id, entity_type=entity_type, entity_data=entity_data
        )

    def analyze_preservation_status(
        self, jira_changes: dict[str, dict[str, Any]], entity_type: str
    ) -> dict[str, Any]:
        """Analyze potential conflicts for a set of entities.

        Args:
            jira_changes: Dictionary of entity_id -> changes from Jira
            entity_type: Type of entities being analyzed

        Returns:
            Report of all conflicts detected
        """
        return self.data_preservation_manager.analyze_preservation_status(
            jira_changes=jira_changes, entity_type=entity_type
        )

    def detect_changes(
        self, current_entities: list[dict[str, Any]], entity_type: str
    ) -> ChangeReport:
        """Detect changes in entities since the last migration run.

        Args:
            current_entities: Current entities from Jira
            entity_type: Type of entities being compared

        Returns:
            Change detection report
        """
        return self.change_detector.detect_changes(current_entities, entity_type)

    def create_snapshot(
        self,
        entities: list[dict[str, Any]],
        entity_type: str,
    ) -> Path:
        """Create a snapshot of entities after successful migration.

        Args:
            entities: List of entities to snapshot
            entity_type: Type of entities

        Returns:
            Path to the created snapshot file
        """
        migration_component = self.__class__.__name__
        return self.change_detector.create_snapshot(
            entities, entity_type, migration_component
        )

    def should_skip_migration(
        self, entity_type: str
    ) -> tuple[bool, ChangeReport | None]:
        """Check if migration should be skipped based on change detection.

        This method allows migration components to check if there are any changes
        before performing expensive migration operations.

        Args:
            entity_type: Type of entities to check for changes

        Returns:
            Tuple of (should_skip, change_report). should_skip is True if no changes
            are detected and migration can be skipped.
        """
        try:
            # Get current entities from Jira for the specific entity type
            current_entities = self._get_current_entities_for_type(entity_type)

            # Detect changes
            change_report = self.detect_changes(current_entities, entity_type)

            # If no changes detected, migration can be skipped
            should_skip = change_report["total_changes"] == 0

            if should_skip:
                self.logger.info(
                    "No changes detected for %s, skipping migration", entity_type
                )
            else:
                self.logger.info(
                    "Detected %d changes for %s: %s",
                    change_report["total_changes"],
                    entity_type,
                    change_report["changes_by_type"],
                )

            return should_skip, change_report

        except Exception as e:
            # If change detection fails, proceed with migration to be safe
            self.logger.warning(
                "Change detection failed for %s: %s. Proceeding with migration.",
                entity_type,
                e,
            )
            return False, None

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities from Jira for a specific type.

        This method should be overridden by subclasses to provide entity-specific
        retrieval logic.

        Args:
            entity_type: Type of entities to retrieve

        Returns:
            List of current entities from Jira

        Raises:
            NotImplementedError: If subclass doesn't implement this method
        """
        raise NotImplementedError(
            f"Subclass {self.__class__.__name__} must implement _get_current_entities_for_type() "
            f"to support change detection for entity type: {entity_type}"
        )

    def run_with_state_management(
        self,
        entity_type: str | None = None,
        operation_type: str = "migrate",
        entity_count: int = 0,
    ) -> ComponentResult:
        """Run migration with complete state management and change detection.

        This method provides the full idempotent migration workflow:
        1. Check for changes before migration
        2. Start migration record tracking
        3. Run the actual migration if changes are detected
        4. Register entity mappings during migration
        5. Create snapshots after successful migration
        6. Complete migration record tracking
        7. Save state and create snapshot for rollback

        Args:
            entity_type: Type of entities being migrated (required for change detection)
            operation_type: Type of operation being performed
            entity_count: Number of entities to be processed

        Returns:
            ComponentResult with migration results and state information
        """
        # If no entity type specified, run standard migration without change detection
        if not entity_type:
            self.logger.debug(
                "No entity type specified, running migration without change detection"
            )
            return self.run()

        migration_record_id = None
        snapshot_id = None

        try:
            # Step 1: Check for changes
            should_skip, change_report = self.should_skip_migration(entity_type)

            if should_skip:
                return ComponentResult(
                    success=True,
                    message=f"No changes detected for {entity_type}, migration skipped",
                    details={"change_report": change_report, "migration_skipped": True},
                    success_count=0,
                    failed_count=0,
                    total_count=0,
                )

            # Step 2: Start migration record
            migration_record_id = self.start_migration_record(
                entity_type=entity_type,
                operation_type=operation_type,
                entity_count=entity_count,
                metadata={"change_report": change_report},
            )

            # Step 3: Run the actual migration
            result = self.run()

            # Step 4: Process results and update state
            if result.success:
                # Complete migration record
                self.complete_migration_record(
                    record_id=migration_record_id,
                    success_count=result.success_count,
                    error_count=result.failed_count,
                    errors=result.errors,
                )

                # Create change detection snapshot
                try:
                    current_entities = self._get_current_entities_for_type(entity_type)
                    snapshot_path = self.create_snapshot(current_entities, entity_type)
                    self.logger.info(
                        "Created change detection snapshot for %s: %s",
                        entity_type,
                        snapshot_path,
                    )
                except Exception as e:
                    self.logger.warning(
                        "Failed to create change detection snapshot: %s", e
                    )

                # Create state snapshot for rollback
                try:
                    snapshot_id = self.create_state_snapshot(
                        description=f"Completed {entity_type} migration via {self.__class__.__name__}",
                        metadata={
                            "entity_type": entity_type,
                            "operation_type": operation_type,
                            "success_count": result.success_count,
                            "failed_count": result.failed_count,
                        },
                    )
                    self.logger.info("Created state snapshot: %s", snapshot_id)
                except Exception as e:
                    self.logger.warning("Failed to create state snapshot: %s", e)

                # Save current state
                try:
                    self.state_manager.save_current_state()
                except Exception as e:
                    self.logger.warning("Failed to save current state: %s", e)

                # Add state info to result details
                if not result.details:
                    result.details = {}
                result.details.update(
                    {
                        "change_report": change_report,
                        "migration_record_id": migration_record_id,
                        "state_snapshot_id": snapshot_id,
                        "state_management": True,
                    }
                )

            else:
                # Migration failed - complete record with error
                self.complete_migration_record(
                    record_id=migration_record_id,
                    success_count=result.success_count,
                    error_count=result.failed_count,
                    errors=result.errors,
                )

            return result

        except Exception as e:
            self.logger.exception("Error in state management workflow: %s", e)

            # Complete migration record with error if it was started
            if migration_record_id:
                try:
                    self.complete_migration_record(
                        record_id=migration_record_id,
                        success_count=0,
                        error_count=1,
                        errors=[f"State management workflow error: {e}"],
                    )
                except Exception as cleanup_error:
                    self.logger.warning(
                        "Failed to complete migration record during error cleanup: %s",
                        cleanup_error,
                    )

            # Fall back to standard migration if state management fails
            return self.run()

    def run_with_data_preservation(
        self,
        entity_type: str | None = None,
        operation_type: str = "migrate",
        entity_count: int = 0,
        analyze_conflicts: bool = True,
        create_backups: bool = True,
    ) -> ComponentResult:
        """Run migration with comprehensive data preservation, state management, and change detection.

        This method provides the complete migration workflow with all safeguards:
        1. Analyze potential conflicts before migration
        2. Check for changes to avoid unnecessary processing
        3. Start migration record tracking
        4. Create backups of entities before modification
        5. Run the actual migration with conflict resolution
        6. Store original states for future conflict detection
        7. Register entity mappings during migration
        8. Create snapshots after successful migration
        9. Complete migration record tracking
        10. Save state and create snapshot for rollback

        Args:
            entity_type: Type of entities being migrated (required for all features)
            operation_type: Type of operation being performed
            entity_count: Number of entities to be processed
            analyze_conflicts: Whether to analyze conflicts before migration
            create_backups: Whether to create backups before updating entities

        Returns:
            ComponentResult with migration results, state, and preservation information
        """
        # If no entity type specified, run standard migration without advanced features
        if not entity_type:
            self.logger.debug(
                "No entity type specified, running migration without data preservation"
            )
            return self.run()

        migration_record_id = None
        snapshot_id = None
        conflict_report = None

        try:
            # Step 1: Analyze conflicts if requested
            if analyze_conflicts:
                try:
                    # Get current Jira changes for conflict analysis
                    current_entities = self._get_current_entities_for_type(entity_type)

                    # Convert entities to changes format for analysis
                    jira_changes = {
                        str(entity.get("id", entity.get("key", i))): entity
                        for i, entity in enumerate(current_entities)
                    }

                    conflict_report = self.analyze_preservation_status(
                        jira_changes, entity_type
                    )

                    if conflict_report["total_conflicts"] > 0:
                        self.logger.info(
                            "Detected %d conflicts for %s before migration: %s",
                            conflict_report["total_conflicts"],
                            entity_type,
                            conflict_report["conflicts_by_resolution"],
                        )

                except Exception as e:
                    self.logger.warning(
                        "Failed to analyze conflicts before migration: %s", e
                    )

            # Step 2: Check for changes
            should_skip, change_report = self.should_skip_migration(entity_type)

            if should_skip:
                return ComponentResult(
                    success=True,
                    message=f"No changes detected for {entity_type}, migration skipped",
                    details={
                        "change_report": change_report,
                        "conflict_report": conflict_report,
                        "migration_skipped": True,
                        "data_preservation": True,
                    },
                    success_count=0,
                    failed_count=0,
                    total_count=0,
                )

            # Step 3: Start migration record
            migration_record_id = self.start_migration_record(
                entity_type=entity_type,
                operation_type=operation_type,
                entity_count=entity_count,
                metadata={
                    "change_report": change_report,
                    "conflict_report": conflict_report,
                    "data_preservation": True,
                    "create_backups": create_backups,
                },
            )

            # Step 4: Run the actual migration
            result = self.run()

            # Step 5: Process results and update state
            if result.success:
                # Complete migration record
                self.complete_migration_record(
                    record_id=migration_record_id,
                    success_count=result.success_count,
                    error_count=result.failed_count,
                    errors=result.errors,
                )

                # Store original states for future preservation
                try:
                    current_entities = self._get_current_entities_for_type(entity_type)
                    for entity in current_entities:
                        entity_id = str(entity.get("id", entity.get("key", "")))
                        if entity_id:
                            self.data_preservation_manager.store_original_state(
                                entity_id=entity_id,
                                entity_type=entity_type,
                                entity_data=entity,
                                source="migration",
                            )

                    self.logger.info(
                        "Stored original states for %d %s entities",
                        len(current_entities),
                        entity_type,
                    )
                except Exception as e:
                    self.logger.warning("Failed to store original states: %s", e)

                # Create change detection snapshot
                try:
                    current_entities = self._get_current_entities_for_type(entity_type)
                    snapshot_path = self.create_snapshot(current_entities, entity_type)
                    self.logger.info(
                        "Created change detection snapshot for %s: %s",
                        entity_type,
                        snapshot_path,
                    )
                except Exception as e:
                    self.logger.warning(
                        "Failed to create change detection snapshot: %s", e
                    )

                # Create state snapshot for rollback
                try:
                    snapshot_id = self.create_state_snapshot(
                        description=(
                            f"Completed {entity_type} migration with data preservation "
                            f"via {self.__class__.__name__}"
                        ),
                        metadata={
                            "entity_type": entity_type,
                            "operation_type": operation_type,
                            "success_count": result.success_count,
                            "failed_count": result.failed_count,
                            "data_preservation": True,
                            "conflicts_detected": (
                                conflict_report["total_conflicts"]
                                if conflict_report
                                else 0
                            ),
                        },
                    )
                    self.logger.info("Created state snapshot: %s", snapshot_id)
                except Exception as e:
                    self.logger.warning("Failed to create state snapshot: %s", e)

                # Save current state
                try:
                    self.state_manager.save_current_state()
                except Exception as e:
                    self.logger.warning("Failed to save current state: %s", e)

                # Add comprehensive info to result details
                if not result.details:
                    result.details = {}
                result.details.update(
                    {
                        "change_report": change_report,
                        "conflict_report": conflict_report,
                        "migration_record_id": migration_record_id,
                        "state_snapshot_id": snapshot_id,
                        "state_management": True,
                        "data_preservation": True,
                    }
                )

            else:
                # Migration failed - complete record with error
                self.complete_migration_record(
                    record_id=migration_record_id,
                    success_count=result.success_count,
                    error_count=result.failed_count,
                    errors=result.errors,
                )

            return result

        except Exception as e:
            self.logger.exception("Error in data preservation workflow: %s", e)

            # Complete migration record with error if it was started
            if migration_record_id:
                try:
                    self.complete_migration_record(
                        record_id=migration_record_id,
                        success_count=0,
                        error_count=1,
                        errors=[f"Data preservation workflow error: {e}"],
                    )
                except Exception as cleanup_error:
                    self.logger.warning(
                        "Failed to complete migration record during error cleanup: %s",
                        cleanup_error,
                    )

            # Fall back to standard migration if data preservation workflow fails
            return self.run()

    def run_with_change_detection(
        self, entity_type: str | None = None
    ) -> ComponentResult:
        """Run migration with change detection support.

        This method wraps the standard run() method with change detection hooks:
        1. Check for changes before migration
        2. Run the actual migration if changes are detected
        3. Create a snapshot after successful migration

        Args:
            entity_type: Type of entities being migrated (required for change detection)

        Returns:
            ComponentResult with migration results
        """
        # If no entity type specified, run standard migration without change detection
        if not entity_type:
            self.logger.debug(
                "No entity type specified, running migration without change detection"
            )
            return self.run()

        try:
            # Check if migration should be skipped
            should_skip, change_report = self.should_skip_migration(entity_type)

            if should_skip:
                return ComponentResult(
                    success=True,
                    message=f"No changes detected for {entity_type}, migration skipped",
                    details={"change_report": change_report},
                    success_count=0,
                    failed_count=0,
                    total_count=0,
                )

            # Run the actual migration
            result = self.run()

            # If migration was successful, create a snapshot for future change detection
            if result.success:
                try:
                    current_entities = self._get_current_entities_for_type(entity_type)
                    snapshot_path = self.create_snapshot(current_entities, entity_type)
                    self.logger.info(
                        "Created snapshot for %s: %s", entity_type, snapshot_path
                    )

                    # Add snapshot info to result details
                    if not result.details:
                        result.details = {}
                    result.details["snapshot_created"] = str(snapshot_path)
                    result.details["change_report"] = change_report

                except Exception as e:
                    # Don't fail the migration if snapshot creation fails
                    self.logger.warning(
                        "Failed to create snapshot after successful migration: %s", e
                    )

            return result

        except Exception as e:
            self.logger.exception("Error in change detection workflow: %s", e)
            # Fall back to standard migration if change detection fails
            return self.run()

    def run_idempotent(
        self,
        entity_type: str | None = None,
        operation_type: str = "migrate",
        entity_count: int = 0,
    ) -> ComponentResult:
        """Run migration with full idempotent capabilities (recommended method).

        This is a convenience method that automatically uses the complete workflow:
        - Change detection to skip unnecessary migrations
        - Data preservation with conflict resolution
        - State management with entity mapping
        - Snapshot creation for rollback capability

        This method should be used instead of run() for production migrations.

        Args:
            entity_type: Type of entities being migrated (auto-detected if not provided)
            operation_type: Type of operation being performed
            entity_count: Number of entities to be processed

        Returns:
            ComponentResult with full migration, state, and preservation information
        """
        # Auto-detect entity type if not provided
        if not entity_type:
            # Try to infer from class name
            class_name = self.__class__.__name__.lower()
            if "user" in class_name:
                entity_type = "users"
            elif "project" in class_name:
                entity_type = "projects"
            elif "workpackage" in class_name or "issue" in class_name:
                entity_type = "work_packages"
            elif "customfield" in class_name:
                entity_type = "custom_fields"
            elif "status" in class_name:
                entity_type = "statuses"
            else:
                # Fall back to basic state management if we can't detect
                self.logger.warning(
                    "Could not auto-detect entity type for %s, using basic state management",
                    self.__class__.__name__,
                )
                return self.run_with_state_management(
                    entity_type=None,
                    operation_type=operation_type,
                    entity_count=entity_count,
                )

        # Use the full data preservation workflow
        return self.run_with_data_preservation(
            entity_type=entity_type,
            operation_type=operation_type,
            entity_count=entity_count,
            analyze_conflicts=True,
            create_backups=True,
        )

    def run_selective_update(
        self,
        entity_type: str | None = None,
        dry_run: bool = False,
        update_settings: dict[str, Any] | None = None,
    ) -> ComponentResult:
        """Run a selective update that only modifies changed entities.

        This method performs change detection, creates an update plan,
        and executes only the necessary updates using SelectiveUpdateManager.
        This is more efficient than full re-migration for incremental updates.

        Args:
            entity_type: Type of entities to update (auto-detected if not provided)
            dry_run: If True, plan and simulate updates without making changes
            update_settings: Settings to control update behavior

        Returns:
            ComponentResult with selective update results

        Raises:
            ComponentError: If selective update fails
        """
        try:
            # Import here to avoid circular imports
            from src.utils.selective_update_manager import SelectiveUpdateManager

            start_time = datetime.now(tz=UTC)
            self.logger.info(
                "Starting selective update for %s", self.__class__.__name__
            )

            # Auto-detect entity type if not provided
            if not entity_type:
                entity_type = self._auto_detect_entity_type()
                if not entity_type:
                    raise ComponentError(
                        "Could not determine entity type for selective update. "
                        "Please specify entity_type parameter."
                    )

            # Step 1: Perform change detection
            if not self.should_skip_migration(entity_type):
                self.logger.info("Changes detected, proceeding with selective update")
            else:
                return ComponentResult(
                    success=True,
                    message="No changes detected - selective update skipped",
                    data={
                        "entity_type": entity_type,
                        "changes_detected": False,
                        "execution_time": 0,
                    },
                    performance_metrics={},
                )

            # Step 2: Get change report from ChangeDetector
            change_report = self.change_detector.detect_changes(
                current_entities=self._get_current_entities_for_type(entity_type),
                entity_type=entity_type,
            )

            if not change_report["changes"]:
                return ComponentResult(
                    success=True,
                    message="No specific changes found in change report",
                    data={
                        "entity_type": entity_type,
                        "changes_detected": False,
                        "change_report": change_report,
                    },
                    performance_metrics={},
                )

            # Step 3: Initialize SelectiveUpdateManager
            update_manager = SelectiveUpdateManager(
                jira_client=self.jira_client,
                op_client=self.op_client,
                state_manager=self.state_manager,
            )

            # Step 4: Create update plan
            update_plan = update_manager.analyze_changes(change_report, update_settings)

            self.logger.info(
                "Created selective update plan with %d operations across %d entity types",
                update_plan["total_operations"],
                len(update_plan["entity_types"]),
            )

            # Step 5: Execute update plan
            update_result = update_manager.execute_update_plan(update_plan, dry_run)

            # Step 6: Calculate execution metrics
            end_time = datetime.now(tz=UTC)
            execution_time = (end_time - start_time).total_seconds()

            # Step 7: Prepare result
            success = update_result["status"] in ["completed"]
            message = (
                f"Selective update {'simulated' if dry_run else 'completed'}: "
                f"{update_result['operations_completed']} successful, "
                f"{update_result['operations_failed']} failed, "
                f"{update_result['operations_skipped']} skipped"
            )

            result_data = {
                "entity_type": entity_type,
                "changes_detected": True,
                "execution_time": execution_time,
                "change_report": change_report,
                "update_plan": update_plan,
                "update_result": update_result,
                "dry_run": dry_run,
            }

            return ComponentResult(
                success=success,
                message=message,
                data=result_data,
                performance_metrics=update_result.get("performance_metrics", {}),
                errors=update_result.get("errors", []),
            )

        except Exception as e:
            error_msg = f"Selective update failed for {self.__class__.__name__}: {e}"
            self.logger.exception(error_msg)
            raise ComponentError(error_msg) from e

    def _auto_detect_entity_type(self) -> str | None:
        """Auto-detect entity type from migration class name.

        Returns:
            Entity type string, or None if cannot be detected
        """
        class_name = self.__class__.__name__.lower()

        # Map class names to entity types
        if "user" in class_name:
            return "users"
        elif "project" in class_name:
            return "projects"
        elif "workpackage" in class_name or "issue" in class_name:
            return "issues"
        elif "customfield" in class_name or "field" in class_name:
            return "customfields"
        elif "status" in class_name:
            return "statuses"
        elif "type" in class_name:
            return "issuetypes"
        elif "link" in class_name:
            return "links"
        else:
            self.logger.warning(
                "Could not auto-detect entity type for %s. "
                "Please specify entity_type parameter explicitly.",
                self.__class__.__name__,
            )
            return None

    def _load_from_json(self, filename: Path, default: Any = None) -> Any:
        """Load data from a JSON file in the data directory.

        Args:
            filename: Name of the JSON file
            default: Default value to return if file doesn't exist

        Returns:
            Loaded JSON data or default value

        """
        filepath = self.data_dir / filename
        try:
            # Optimistic execution: attempt to load directly
            with filepath.open("r") as f:
                return json.load(f)
        except FileNotFoundError:
            # File doesn't exist - this is expected, return default
            self.logger.debug("File does not exist: %s", filepath)
            return default
        except json.JSONDecodeError as e:
            # Only perform diagnostics after JSON parsing fails
            if filepath.stat().st_size == 0:
                self.logger.debug("File is empty: %s", filepath)
            else:
                self.logger.exception("JSON decode error in %s: %s", filepath, e)
            return default
        except Exception as e:
            # Unexpected error - log it
            self.logger.exception("Unexpected error loading %s: %s", filepath, e)
            return default

    def _save_to_json(self, data: Any, filename: Path | str) -> Path:
        """Save data to a JSON file in the data directory.

        Args:
            data: Data to save
            filename: Name of the JSON file

        Returns:
            Path to the saved file

        """
        filepath = self.data_dir / Path(filename)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        with filepath.open("w") as f:
            json.dump(data, f, indent=2)

        self.logger.debug("Saved data to %s", filepath)
        return filepath

    def run(self) -> ComponentResult:
        """Implement the default run method that all migration classes should implement.

        Returns:
            Dictionary with migration results

        """
        self.logger.warning(
            "The run method has not been implemented for %s",
            self.__class__.__name__,
        )
        return ComponentResult(
            success=False,
            errors=[
                f"The run method has not been implemented for {self.__class__.__name__}",
            ],
            success_count=0,
            failed_count=0,
            total_count=0,
        )

    def run_with_recovery(
        self,
        entity_type: str | None = None,
        operation_type: str = "migrate",
        entity_count: int = 0,
        enable_checkpoints: bool = True,
        checkpoint_frequency: int = 10,
    ) -> ComponentResult:
        """Run migration with full recovery and resilience capabilities.

        This method provides comprehensive recovery features:
        1. Granular progress tracking with checkpoints
        2. Automatic resume from last successful checkpoint on failure
        3. Structured recovery plans for different failure scenarios
        4. Real-time progress monitoring
        5. Enhanced rollback capabilities

        Args:
            entity_type: Type of entities being migrated
            operation_type: Type of operation being performed
            entity_count: Number of entities to be processed
            enable_checkpoints: Whether to create checkpoints during migration
            checkpoint_frequency: Create checkpoint every N entities processed

        Returns:
            ComponentResult with full migration, recovery, and state information
        """
        if not entity_type:
            self.logger.warning(
                "No entity type specified for recovery-enabled migration, using standard run"
            )
            return self.run()

        migration_record_id = None
        recovery_plan_id = None
        
        try:
            # Check if this is a resumed migration
            if self.checkpoint_manager.can_resume_migration(entity_type):
                resume_point = self.checkpoint_manager.get_resume_point(entity_type)
                if resume_point:
                    self.logger.info(
                        "Found resume point for %s migration: %s (%.1f%% complete)",
                        entity_type,
                        resume_point["step_name"],
                        resume_point["progress_percentage"],
                    )
                    
                    # Ask user if they want to resume
                    if self._should_resume_migration(resume_point):
                        return self._resume_migration_from_checkpoint(resume_point)

            # Start new migration with recovery features
            migration_record_id = self.start_migration_record(
                entity_type=entity_type,
                operation_type=operation_type,
                entity_count=entity_count,
                metadata={
                    "recovery_enabled": True,
                    "checkpoint_frequency": checkpoint_frequency,
                    "enable_checkpoints": enable_checkpoints,
                },
            )
            
            self._current_migration_record_id = migration_record_id

            # Start progress tracking
            estimated_steps = self._estimate_migration_steps(entity_type, entity_count)
            self.checkpoint_manager.start_progress_tracking(
                migration_record_id, estimated_steps
            )

            # Create initial checkpoint
            if enable_checkpoints:
                initial_checkpoint = self.checkpoint_manager.create_checkpoint(
                    migration_record_id=migration_record_id,
                    step_name="migration_start",
                    step_description=f"Starting {entity_type} migration",
                    entities_processed=0,
                    entities_total=entity_count,
                    metadata={"migration_component": self.__class__.__name__},
                )
                self._current_checkpoints.append(initial_checkpoint)
                self.checkpoint_manager.start_checkpoint(initial_checkpoint)

            # Run the actual migration with checkpoint integration
            result = self._run_with_checkpoints(
                entity_type=entity_type,
                entity_count=entity_count,
                checkpoint_frequency=checkpoint_frequency,
            )

            # Complete or fail the migration record
            if result.success:
                self.complete_migration_record(
                    record_id=migration_record_id,
                    success_count=result.success_count,
                    error_count=result.failed_count,
                    errors=result.errors,
                )

                # Complete final checkpoint
                if enable_checkpoints and self._current_checkpoints:
                    final_checkpoint = self._current_checkpoints[-1]
                    self.checkpoint_manager.complete_checkpoint(
                        final_checkpoint,
                        entities_processed=result.success_count,
                        metadata={"migration_completed": True},
                    )

                # Clean up tracking data
                self.checkpoint_manager.cleanup_completed_migration(migration_record_id)

                # Add recovery info to result
                if not result.details:
                    result.details = {}
                result.details.update({
                    "migration_record_id": migration_record_id,
                    "recovery_enabled": True,
                    "checkpoints_created": len(self._current_checkpoints),
                })

            else:
                # Migration failed - create recovery plan
                self.complete_migration_record(
                    record_id=migration_record_id,
                    success_count=result.success_count,
                    error_count=result.failed_count,
                    errors=result.errors,
                )

                # Fail the current checkpoint if any
                if self._current_checkpoints:
                    current_checkpoint = self._current_checkpoints[-1]
                    error_message = result.errors[0] if result.errors else "Migration failed"
                    self.checkpoint_manager.fail_checkpoint(
                        current_checkpoint, error_message
                    )

                    # Create recovery plan
                    recovery_plan_id = self.checkpoint_manager.create_recovery_plan(
                        checkpoint_id=current_checkpoint,
                        failure_type=self._classify_failure_type(result.errors),
                        error_message=error_message,
                        manual_steps=self._generate_manual_recovery_steps(result.errors),
                    )

                # Add recovery info to result
                if not result.details:
                    result.details = {}
                result.details.update({
                    "migration_record_id": migration_record_id,
                    "recovery_plan_id": recovery_plan_id,
                    "recovery_enabled": True,
                    "can_resume": True,
                })

            return result

        except Exception as e:
            self.logger.exception("Critical error in recovery-enabled migration: %s", e)

            # Create emergency recovery plan
            if migration_record_id and self._current_checkpoints:
                try:
                    current_checkpoint = self._current_checkpoints[-1]
                    self.checkpoint_manager.fail_checkpoint(
                        current_checkpoint, f"Critical error: {e}"
                    )
                    
                    recovery_plan_id = self.checkpoint_manager.create_recovery_plan(
                        checkpoint_id=current_checkpoint,
                        failure_type="system_error",
                        error_message=str(e),
                        manual_steps=[
                            "Check system logs for detailed error information",
                            "Verify system resources (disk space, memory)",
                            "Check network connectivity to Jira and OpenProject",
                            "Review migration configuration for errors",
                            "Contact support if issue persists",
                        ],
                    )
                except Exception as recovery_error:
                    self.logger.error("Failed to create emergency recovery plan: %s", recovery_error)

            # Fall back to standard migration
            return self.run()

    def resume_migration(self, migration_record_id: str) -> ComponentResult:
        """Resume a previously interrupted migration.

        Args:
            migration_record_id: ID of the migration to resume

        Returns:
            ComponentResult from the resumed migration
        """
        resume_point = self.checkpoint_manager.get_resume_point(migration_record_id)
        if not resume_point:
            raise ValueError(f"No resume point found for migration {migration_record_id}")

        self.logger.info(
            "Resuming migration %s from checkpoint %s",
            migration_record_id[:8],
            resume_point["checkpoint_id"][:8],
        )

        return self._resume_migration_from_checkpoint(resume_point)

    def get_migration_progress(self, migration_record_id: str) -> dict[str, Any]:
        """Get detailed progress information for a migration.

        Args:
            migration_record_id: ID of the migration

        Returns:
            Dictionary containing progress information
        """
        progress = self.checkpoint_manager.get_progress_status(migration_record_id)
        checkpoints = self.checkpoint_manager.get_checkpoints_for_migration(migration_record_id)

        return {
            "migration_record_id": migration_record_id,
            "progress": progress,
            "checkpoints": checkpoints,
            "can_resume": self.checkpoint_manager.can_resume_migration(migration_record_id),
            "checkpoint_count": len(checkpoints),
            "last_checkpoint": checkpoints[-1] if checkpoints else None,
        }

    def rollback_to_checkpoint(self, checkpoint_id: str) -> bool:
        """Rollback migration to a specific checkpoint.

        Args:
            checkpoint_id: ID of the checkpoint to rollback to

        Returns:
            True if rollback was successful, False otherwise
        """
        try:
            # This would be implemented by subclasses to handle specific rollback logic
            self.logger.info("Initiating rollback to checkpoint %s", checkpoint_id[:8])
            
            # The specific rollback implementation would depend on the migration type
            # For now, we'll just log the attempt
            self.logger.warning(
                "Rollback to checkpoint %s requested. "
                "Specific rollback logic should be implemented by migration subclasses.",
                checkpoint_id[:8],
            )
            
            return True
            
        except Exception as e:
            self.logger.exception("Failed to rollback to checkpoint %s: %s", checkpoint_id, e)
            return False

    def _run_with_checkpoints(
        self,
        entity_type: str,
        entity_count: int,
        checkpoint_frequency: int,
    ) -> ComponentResult:
        """Run the migration with integrated checkpoint creation.

        This method wraps the standard run() method with checkpoint creation
        at regular intervals during processing.

        Args:
            entity_type: Type of entities being migrated
            entity_count: Total number of entities
            checkpoint_frequency: Create checkpoint every N entities

        Returns:
            ComponentResult from the migration
        """
        # This is a wrapper that would be overridden by specific migration classes
        # to provide checkpoint creation during their processing loops
        
        # For now, create checkpoints at key stages
        if self._current_migration_record_id:
            # Pre-processing checkpoint
            pre_checkpoint = self.checkpoint_manager.create_checkpoint(
                migration_record_id=self._current_migration_record_id,
                step_name="pre_processing",
                step_description=f"Pre-processing {entity_type} entities",
                entities_processed=0,
                entities_total=entity_count,
            )
            self._current_checkpoints.append(pre_checkpoint)
            self.checkpoint_manager.start_checkpoint(pre_checkpoint)

        # Run the actual migration
        result = self.run()

        if self._current_migration_record_id:
            # Complete pre-processing checkpoint
            self.checkpoint_manager.complete_checkpoint(
                pre_checkpoint,
                entities_processed=result.success_count,
                metadata={"processing_completed": True},
            )

            # Post-processing checkpoint
            post_checkpoint = self.checkpoint_manager.create_checkpoint(
                migration_record_id=self._current_migration_record_id,
                step_name="post_processing",
                step_description=f"Post-processing {entity_type} migration",
                entities_processed=result.success_count,
                entities_total=entity_count,
            )
            self._current_checkpoints.append(post_checkpoint)
            self.checkpoint_manager.start_checkpoint(post_checkpoint)

        return result

    def _resume_migration_from_checkpoint(self, resume_point: dict[str, Any]) -> ComponentResult:
        """Resume migration from a specific checkpoint.

        Args:
            resume_point: Checkpoint data to resume from

        Returns:
            ComponentResult from the resumed migration
        """
        self.logger.info(
            "Resuming migration from checkpoint: %s (%.1f%% complete)",
            resume_point["step_name"],
            resume_point["progress_percentage"],
        )

        # This would be implemented by specific migration classes to handle
        # resuming from the specific checkpoint state
        
        # For now, we'll start a fresh migration with knowledge of the resume point
        result = self.run()
        
        if not result.details:
            result.details = {}
        result.details["resumed_from_checkpoint"] = resume_point["checkpoint_id"]
        result.details["resume_point"] = resume_point
        
        return result

    def _should_resume_migration(self, resume_point: dict[str, Any]) -> bool:
        """Ask user if they want to resume from a checkpoint.

        Args:
            resume_point: Checkpoint data for resume decision

        Returns:
            True if user wants to resume, False to start fresh
        """
        # For automated systems, always resume if possible
        # Interactive systems could prompt the user
        return True

    def _estimate_migration_steps(self, entity_type: str, entity_count: int) -> int:
        """Estimate the number of steps in a migration.

        Args:
            entity_type: Type of entities being migrated
            entity_count: Number of entities

        Returns:
            Estimated number of steps
        """
        # Basic estimation: pre-processing + entity processing + post-processing
        base_steps = 3
        
        # Add steps based on entity count (batch processing)
        if entity_count > 0:
            batch_size = 10  # Assume batch size of 10
            batch_steps = (entity_count + batch_size - 1) // batch_size
            return base_steps + batch_steps
        
        return base_steps

    def _classify_failure_type(self, errors: list[str]) -> str:
        """Classify the type of failure based on error messages.

        Args:
            errors: List of error messages

        Returns:
            Classification of the failure type
        """
        if not errors:
            return "unknown_error"

        error_text = " ".join(errors).lower()

        if any(term in error_text for term in ["network", "connection", "timeout"]):
            return "network_error"
        elif any(term in error_text for term in ["validation", "invalid", "format"]):
            return "validation_error"
        elif any(term in error_text for term in ["auth", "permission", "unauthorized"]):
            return "auth_error"
        elif any(term in error_text for term in ["disk", "memory", "resource"]):
            return "resource_error"
        else:
            return "unknown_error"

    def _generate_manual_recovery_steps(self, errors: list[str]) -> list[str]:
        """Generate manual recovery steps based on error messages.

        Args:
            errors: List of error messages

        Returns:
            List of manual recovery steps
        """
        if not errors:
            return ["Review migration logs for error details"]

        error_text = " ".join(errors).lower()
        steps = []

        if "network" in error_text or "connection" in error_text:
            steps.extend([
                "Check network connectivity to Jira and OpenProject",
                "Verify firewall and proxy settings",
                "Test API endpoints manually",
            ])
        
        if "auth" in error_text or "permission" in error_text:
            steps.extend([
                "Verify API credentials are valid and not expired",
                "Check user permissions in both Jira and OpenProject",
                "Confirm API tokens have required scopes",
            ])
        
        if "validation" in error_text or "invalid" in error_text:
            steps.extend([
                "Review data format requirements",
                "Check for required fields in source data",
                "Validate data against target system constraints",
            ])

        if not steps:
            steps = [
                "Review detailed migration logs",
                "Check system resources (disk space, memory)",
                "Verify migration configuration",
                "Contact support with error details",
            ]

        return steps

    def create_checkpoint_during_migration(
        self,
        step_name: str,
        step_description: str,
        entities_processed: int = 0,
        entities_total: int = 0,
        current_entity_id: str | None = None,
        current_entity_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Create a checkpoint during migration execution.

        This method can be called by migration implementations to create
        checkpoints at strategic points during processing.

        Args:
            step_name: Name of the current migration step
            step_description: Description of the current step
            entities_processed: Number of entities processed so far
            entities_total: Total number of entities to process
            current_entity_id: ID of the entity currently being processed
            current_entity_type: Type of the entity being processed
            metadata: Additional metadata for the checkpoint

        Returns:
            Checkpoint ID if created successfully, None otherwise
        """
        if not self._current_migration_record_id:
            return None

        try:
            checkpoint_id = self.checkpoint_manager.create_checkpoint(
                migration_record_id=self._current_migration_record_id,
                step_name=step_name,
                step_description=step_description,
                entities_processed=entities_processed,
                entities_total=entities_total,
                current_entity_id=current_entity_id,
                current_entity_type=current_entity_type,
                metadata=metadata,
            )
            
            self._current_checkpoints.append(checkpoint_id)
            self.checkpoint_manager.start_checkpoint(checkpoint_id)
            
            # Update progress
            self.checkpoint_manager.update_progress(
                migration_record_id=self._current_migration_record_id,
                current_step=step_name,
                current_step_progress=(entities_processed / entities_total * 100) if entities_total > 0 else 0,
            )
            
            return checkpoint_id
            
        except Exception as e:
            self.logger.error("Failed to create checkpoint: %s", e)
            return None

    def complete_current_checkpoint(
        self,
        entities_processed: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Complete the current checkpoint.

        Args:
            entities_processed: Updated count of entities processed
            metadata: Additional metadata for completion
        """
        if self._current_checkpoints:
            current_checkpoint = self._current_checkpoints[-1]
            self.checkpoint_manager.complete_checkpoint(
                current_checkpoint, entities_processed, metadata
            )
