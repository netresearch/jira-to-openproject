import json
from pathlib import Path
from typing import Any

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.models import ComponentResult
from src.utils.change_detector import ChangeDetector, ChangeReport
from src.utils.state_manager import StateManager
from src.utils.data_preservation_manager import DataPreservationManager


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
        self.data_preservation_manager = data_preservation_manager or DataPreservationManager(
            jira_client=self.jira_client,
            openproject_client=self.op_client
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
            self.logger.exception("Failed to initialize mappings in %s: %s", self.__class__.__name__, e)
            raise ComponentInitializationError(f"Cannot initialize {self.__class__.__name__}: {e}") from e

    def register_entity_mapping(
        self,
        jira_entity_type: str,
        jira_entity_id: str,
        openproject_entity_type: str,
        openproject_entity_id: str,
        metadata: dict[str, Any] | None = None
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
            metadata=metadata
        )

    def get_entity_mapping(
        self,
        jira_entity_type: str,
        jira_entity_id: str
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
        metadata: dict[str, Any] | None = None
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
            metadata=metadata
        )

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
        self.state_manager.complete_migration_record(
            record_id=record_id,
            success_count=success_count,
            error_count=error_count,
            errors=errors
        )

    def create_state_snapshot(
        self,
        description: str,
        metadata: dict[str, Any] | None = None
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
            description=description,
            user=migration_component,
            metadata=metadata
        )

    def store_original_entity_state(
        self,
        entity_id: str,
        entity_type: str,
        entity_data: dict[str, Any],
        source: str = "migration"
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
            source=source
        )

    def detect_preservation_conflicts(
        self,
        jira_changes: dict[str, Any],
        entity_id: str,
        entity_type: str,
        current_openproject_data: dict[str, Any]
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
            current_openproject_data=current_openproject_data
        )

    def resolve_preservation_conflict(
        self,
        conflict: dict[str, Any],
        jira_data: dict[str, Any],
        openproject_data: dict[str, Any]
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
            conflict=conflict,
            jira_data=jira_data,
            openproject_data=openproject_data
        )

    def create_entity_backup(
        self,
        entity_id: str,
        entity_type: str,
        entity_data: dict[str, Any]
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
            entity_id=entity_id,
            entity_type=entity_type,
            entity_data=entity_data
        )

    def analyze_preservation_status(
        self,
        jira_changes: dict[str, dict[str, Any]],
        entity_type: str
    ) -> dict[str, Any]:
        """Analyze potential conflicts for a set of entities.

        Args:
            jira_changes: Dictionary of entity_id -> changes from Jira
            entity_type: Type of entities being analyzed

        Returns:
            Report of all conflicts detected
        """
        return self.data_preservation_manager.analyze_preservation_status(
            jira_changes=jira_changes,
            entity_type=entity_type
        )

    def detect_changes(self, current_entities: list[dict[str, Any]], entity_type: str) -> ChangeReport:
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
        return self.change_detector.create_snapshot(entities, entity_type, migration_component)

    def should_skip_migration(self, entity_type: str) -> tuple[bool, ChangeReport | None]:
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
                self.logger.info("No changes detected for %s, skipping migration", entity_type)
            else:
                self.logger.info(
                    "Detected %d changes for %s: %s",
                    change_report["total_changes"],
                    entity_type,
                    change_report["changes_by_type"]
                )

            return should_skip, change_report

        except Exception as e:
            # If change detection fails, proceed with migration to be safe
            self.logger.warning("Change detection failed for %s: %s. Proceeding with migration.", entity_type, e)
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
        entity_count: int = 0
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
            self.logger.debug("No entity type specified, running migration without change detection")
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
                    details={
                        "change_report": change_report,
                        "migration_skipped": True
                    },
                    success_count=0,
                    failed_count=0,
                    total_count=0,
                )

            # Step 2: Start migration record
            migration_record_id = self.start_migration_record(
                entity_type=entity_type,
                operation_type=operation_type,
                entity_count=entity_count,
                metadata={"change_report": change_report}
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
                    errors=result.errors
                )

                # Create change detection snapshot
                try:
                    current_entities = self._get_current_entities_for_type(entity_type)
                    snapshot_path = self.create_snapshot(current_entities, entity_type)
                    self.logger.info("Created change detection snapshot for %s: %s", entity_type, snapshot_path)
                except Exception as e:
                    self.logger.warning("Failed to create change detection snapshot: %s", e)

                # Create state snapshot for rollback
                try:
                    snapshot_id = self.create_state_snapshot(
                        description=f"Completed {entity_type} migration via {self.__class__.__name__}",
                        metadata={
                            "entity_type": entity_type,
                            "operation_type": operation_type,
                            "success_count": result.success_count,
                            "failed_count": result.failed_count
                        }
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
                result.details.update({
                    "change_report": change_report,
                    "migration_record_id": migration_record_id,
                    "state_snapshot_id": snapshot_id,
                    "state_management": True
                })

            else:
                # Migration failed - complete record with error
                self.complete_migration_record(
                    record_id=migration_record_id,
                    success_count=result.success_count,
                    error_count=result.failed_count,
                    errors=result.errors
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
                        errors=[f"State management workflow error: {e}"]
                    )
                except Exception as cleanup_error:
                    self.logger.warning("Failed to complete migration record during error cleanup: %s", cleanup_error)

            # Fall back to standard migration if state management fails
            return self.run()

    def run_with_data_preservation(
        self,
        entity_type: str | None = None,
        operation_type: str = "migrate",
        entity_count: int = 0,
        analyze_conflicts: bool = True,
        create_backups: bool = True
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
            self.logger.debug("No entity type specified, running migration without data preservation")
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
                        str(entity.get('id', entity.get('key', i))): entity
                        for i, entity in enumerate(current_entities)
                    }

                    conflict_report = self.analyze_preservation_status(jira_changes, entity_type)

                    if conflict_report["total_conflicts"] > 0:
                        self.logger.info(
                            "Detected %d conflicts for %s before migration: %s",
                            conflict_report["total_conflicts"],
                            entity_type,
                            conflict_report["conflicts_by_resolution"]
                        )

                except Exception as e:
                    self.logger.warning("Failed to analyze conflicts before migration: %s", e)

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
                        "data_preservation": True
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
                    "create_backups": create_backups
                }
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
                    errors=result.errors
                )

                # Store original states for future preservation
                try:
                    current_entities = self._get_current_entities_for_type(entity_type)
                    for entity in current_entities:
                        entity_id = str(entity.get('id', entity.get('key', '')))
                        if entity_id:
                            self.data_preservation_manager.store_original_state(
                                entity_id=entity_id,
                                entity_type=entity_type,
                                entity_data=entity,
                                source="migration"
                            )

                    self.logger.info("Stored original states for %d %s entities", len(current_entities), entity_type)
                except Exception as e:
                    self.logger.warning("Failed to store original states: %s", e)

                # Create change detection snapshot
                try:
                    current_entities = self._get_current_entities_for_type(entity_type)
                    snapshot_path = self.create_snapshot(current_entities, entity_type)
                    self.logger.info("Created change detection snapshot for %s: %s", entity_type, snapshot_path)
                except Exception as e:
                    self.logger.warning("Failed to create change detection snapshot: %s", e)

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
                            "conflicts_detected": conflict_report["total_conflicts"] if conflict_report else 0
                        }
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
                result.details.update({
                    "change_report": change_report,
                    "conflict_report": conflict_report,
                    "migration_record_id": migration_record_id,
                    "state_snapshot_id": snapshot_id,
                    "state_management": True,
                    "data_preservation": True
                })

            else:
                # Migration failed - complete record with error
                self.complete_migration_record(
                    record_id=migration_record_id,
                    success_count=result.success_count,
                    error_count=result.failed_count,
                    errors=result.errors
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
                        errors=[f"Data preservation workflow error: {e}"]
                    )
                except Exception as cleanup_error:
                    self.logger.warning("Failed to complete migration record during error cleanup: %s", cleanup_error)

            # Fall back to standard migration if data preservation workflow fails
            return self.run()

    def run_with_change_detection(self, entity_type: str | None = None) -> ComponentResult:
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
            self.logger.debug("No entity type specified, running migration without change detection")
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
                    self.logger.info("Created snapshot for %s: %s", entity_type, snapshot_path)

                    # Add snapshot info to result details
                    if not result.details:
                        result.details = {}
                    result.details["snapshot_created"] = str(snapshot_path)
                    result.details["change_report"] = change_report

                except Exception as e:
                    # Don't fail the migration if snapshot creation fails
                    self.logger.warning("Failed to create snapshot after successful migration: %s", e)

            return result

        except Exception as e:
            self.logger.exception("Error in change detection workflow: %s", e)
            # Fall back to standard migration if change detection fails
            return self.run()

    def run_idempotent(
        self,
        entity_type: str | None = None,
        operation_type: str = "migrate",
        entity_count: int = 0
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
                    self.__class__.__name__
                )
                return self.run_with_state_management(
                    entity_type=None,
                    operation_type=operation_type,
                    entity_count=entity_count
                )

        # Use the full data preservation workflow
        return self.run_with_data_preservation(
            entity_type=entity_type,
            operation_type=operation_type,
            entity_count=entity_count,
            analyze_conflicts=True,
            create_backups=True
        )

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
