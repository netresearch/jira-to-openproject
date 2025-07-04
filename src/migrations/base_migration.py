import json
from pathlib import Path
from typing import Any

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.models import ComponentResult
from src.utils.change_detector import ChangeDetector, ChangeReport


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
    ) -> None:
        """Initialize the base migration with common attributes.

        Follows dependency injection pattern for the high-level clients only.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client
            change_detector: Initialized change detector for idempotent operations

        """
        # Initialize clients using dependency injection
        self.jira_client = jira_client or JiraClient()
        self.op_client = op_client or OpenProjectClient()
        self.change_detector = change_detector or ChangeDetector()

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
