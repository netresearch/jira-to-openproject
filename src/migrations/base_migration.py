"""Base migration class providing common functionality for all migrations."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable  # noqa: TC003
from pathlib import Path
from typing import Any, ClassVar

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import configure_logging
from src.models import ComponentResult, MigrationError

# Import dependencies
from src.utils.change_detector import ChangeDetector, ChangeReport


class EntityTypeRegistry:
    """Centralized registry for mapping migration classes to their supported entity types.

    This replaces brittle string matching with a robust registry system that ensures
    fail-fast behavior when entity types cannot be resolved.
    """

    _registry: ClassVar[dict[type[BaseMigration], list[str]]] = {}
    _type_to_class_map: ClassVar[dict[str, type[BaseMigration]]] = {}

    @classmethod
    def register(
        cls,
        migration_class: type[BaseMigration],
        entity_types: list[str],
    ) -> None:
        """Register a migration class with its supported entity types.

        Args:
            migration_class: The migration class to register
            entity_types: List of entity types this class supports

        Raises:
            ValueError: If entity_types is empty or migration_class is invalid

        """
        if migration_class is None:
            msg = "Migration class cannot be None"
            raise ValueError(msg)

        if not entity_types:
            msg = f"Migration class {migration_class.__name__} must support at least one entity type"
            raise TypeError(
                msg,
            )

        if not issubclass(migration_class, BaseMigration):
            msg = f"Class {migration_class.__name__} must inherit from BaseMigration"
            raise TypeError(
                msg,
            )

        cls._registry[migration_class] = entity_types.copy()

        # Build reverse mapping for quick lookups
        for entity_type in entity_types:
            if entity_type in cls._type_to_class_map:
                existing_class = cls._type_to_class_map[entity_type]
                if existing_class != migration_class:
                    logger = configure_logging("INFO", None)
                    logger.warning(
                        "Entity type '%s' is supported by multiple classes: %s and %s. Using %s.",
                        entity_type,
                        existing_class.__name__,
                        migration_class.__name__,
                        migration_class.__name__,
                    )
            cls._type_to_class_map[entity_type] = migration_class

    @classmethod
    def resolve(cls, migration_class: type[BaseMigration]) -> str:
        """Resolve the primary entity type for a migration class.

        Args:
            migration_class: The migration class to resolve

        Returns:
            The primary (first) entity type supported by this class

        Raises:
            ValueError: If the migration class is not registered or has no entity types

        """
        if migration_class is None:
            msg = "Migration class cannot be None"
            raise ValueError(msg)

        if migration_class not in cls._registry:
            msg = (
                f"Migration class {migration_class.__name__} is not registered with EntityTypeRegistry. "
                f"Add SUPPORTED_ENTITY_TYPES class attribute and ensure the class is properly registered."
            )
            raise ValueError(
                msg,
            )

        entity_types = cls._registry[migration_class]
        if not entity_types:
            msg = f"Migration class {migration_class.__name__} has no registered entity types"
            raise ValueError(
                msg,
            )

        return entity_types[0]  # Return primary (first) entity type

    @classmethod
    def get_supported_types(cls, migration_class: type[BaseMigration]) -> list[str]:
        """Get all entity types supported by a migration class.

        Args:
            migration_class: The migration class to query

        Returns:
            List of all entity types supported by this class

        Raises:
            ValueError: If the migration class is not registered

        """
        if migration_class not in cls._registry:
            msg = f"Migration class {migration_class.__name__} is not registered with EntityTypeRegistry"
            raise ValueError(
                msg,
            )

        return cls._registry[migration_class].copy()

    @classmethod
    def get_class_for_type(cls, entity_type: str) -> type[BaseMigration] | None:
        """Get the migration class that handles a specific entity type.

        Args:
            entity_type: The entity type to look up

        Returns:
            The migration class that handles this entity type, or None if not found

        """
        return cls._type_to_class_map.get(entity_type)

    @classmethod
    def clear_registry(cls) -> None:
        """Clear all registrations. Used primarily for testing."""
        cls._registry.clear()
        cls._type_to_class_map.clear()

    @classmethod
    def get_all_registered_types(cls) -> set[str]:
        """Get all registered entity types across all migration classes."""
        all_types = set()
        for entity_types in cls._registry.values():
            all_types.update(entity_types)
        return all_types


def register_entity_types(*entity_types: str) -> Callable[[type[BaseMigration]], type[BaseMigration]]:
    """Register entity types for a migration class.

    Args:
        entity_types: Entity types supported by the decorated class

    Returns:
        Decorator function that registers the class

    Example:
        @register_entity_types("users", "user_accounts")
        class UserMigration(BaseMigration):
            pass

    """

    def decorator(cls: type[BaseMigration]) -> type[BaseMigration]:
        EntityTypeRegistry.register(cls, list(entity_types))
        return cls

    return decorator


class ComponentInitializationError(Exception):
    """Raised when a migration component cannot be initialized.

    This custom exception provides clear diagnostics when component
    initialization fails, following proper exception-based error handling.
    """


class BaseMigration:
    """Base class for all migration classes.

    Provides common functionality and initialization for all migration types.

    Includes API call caching with thread safety and memory management.

    Follows the layered client architecture:
    1. OpenProjectClient - Manages all lower-level clients and operations
    2. BaseMigration - Uses OpenProjectClient for migrations
    """

    # Cache configuration - production-ready limits
    MAX_CACHE_SIZE_PER_TYPE = 1000  # Maximum entities per cache type
    MAX_TOTAL_CACHE_SIZE = 5000  # Maximum total cached entities across all types
    CACHE_CLEANUP_THRESHOLD = 0.8  # Cleanup when 80% full

    # Cache statistics tracking
    _global_cache_stats: ClassVar[dict[str, int]] = {
        "total_hits": 0,
        "total_misses": 0,
        "total_evictions": 0,
        "memory_cleanups": 0,
    }

    def __init__(
        self,
        jira_client: JiraClient | None = None,
        op_client: OpenProjectClient | None = None,
        change_detector: ChangeDetector | None = None,
    ) -> None:
        """Initialize the base migration with common attributes.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client
            change_detector: Initialized change detector for idempotent operations

        """
        # Initialize clients using dependency injection
        self.jira_client = jira_client or JiraClient()
        if op_client is not None:
            self.op_client = op_client
        else:
            try:
                self.op_client = OpenProjectClient()
            except Exception as e:  # noqa: BLE001
                self.logger = configure_logging("INFO", None)
                self.logger.debug(
                    "OpenProjectClient unavailable; proceeding without it: %s",
                    e,
                )
                self.op_client = None
        self.change_detector = change_detector or ChangeDetector()

        self.data_dir: Path = config.get_path("data")
        self.output_dir: Path = config.get_path("output")

        self.logger = configure_logging("INFO", None)

        # Initialize mappings using proper exception handling (compliance fix)
        try:
            # Optimistic execution: attempt to get mappings directly
            self.mappings = config.get_mappings()
        except config.MappingsInitializationError as e:
            # Only perform diagnostics if mappings initialization fails
            self.logger.exception(
                "Failed to initialize mappings in %s",
                self.__class__.__name__,
            )
            msg = f"Cannot initialize {self.__class__.__name__}: {e}"
            raise ComponentInitializationError(
                msg,
            ) from e

        # Initialize thread-safe cache infrastructure for API call optimization
        self._cache_lock = threading.RLock()
        self._global_entity_cache: dict[str, list[dict[str, Any]]] = {}
        self._cache_stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "memory_cleanups": 0,
            "total_size": 0,
        }

        # Setup performance features for enhanced clients
        self._setup_performance_features()

    def _setup_performance_features(self) -> None:
        """Set up performance features and shortcuts to enhanced client capabilities."""
        # Check if clients have enhanced features
        self.has_enhanced_jira = hasattr(self.jira_client, "performance_optimizer")
        self.has_enhanced_openproject = (
            hasattr(self.op_client, "performance_optimizer") if self.op_client is not None else False
        )

        if self.has_enhanced_jira:
            self.logger.debug(
                "Enhanced Jira client detected - batch operations and caching available",
            )

        if self.has_enhanced_openproject:
            self.logger.debug(
                "Enhanced OpenProject client detected - batch operations and caching available",
            )

    def get_performance_stats(self) -> dict[str, Any]:
        """Get performance statistics from enhanced clients."""
        stats = {}

        if self.has_enhanced_jira:
            stats["jira"] = self.jira_client.get_performance_stats()

        if self.has_enhanced_openproject:
            stats["openproject"] = self.op_client.get_performance_stats()

        return stats

    def is_batch_operations_available(self) -> bool:
        """Check if batch operations are available."""
        return self.has_enhanced_jira or self.has_enhanced_openproject

    def _cleanup_cache_if_needed(self) -> None:
        """Clean up global cache if memory usage exceeds thresholds.

        Implements LRU-like eviction strategy to prevent memory exhaustion.
        Thread-safe implementation with comprehensive logging.
        """
        with self._cache_lock:
            current_size = sum(len(entities) for entities in self._global_entity_cache.values())
            self._cache_stats["total_size"] = current_size

            if current_size > self.MAX_TOTAL_CACHE_SIZE * self.CACHE_CLEANUP_THRESHOLD:
                # Sort cache types by size (largest first) for efficient cleanup
                cache_sizes = [(k, len(v)) for k, v in self._global_entity_cache.items()]
                cache_sizes.sort(key=lambda x: x[1], reverse=True)

                target_size = self.MAX_TOTAL_CACHE_SIZE // 2
                removed_count = 0
                removed_types = []

                for cache_key, size in cache_sizes:
                    if current_size - removed_count <= target_size:
                        break
                    del self._global_entity_cache[cache_key]
                    removed_count += size
                    removed_types.append(cache_key)
                    self._cache_stats["evictions"] += 1
                    BaseMigration._global_cache_stats["total_evictions"] += 1

                self._cache_stats["memory_cleanups"] += 1
                BaseMigration._global_cache_stats["memory_cleanups"] += 1

                self.logger.info(
                    "Cache cleanup: removed %d entities from %d types: %s",
                    removed_count,
                    len(removed_types),
                    removed_types,
                )

    def _get_cached_entities_threadsafe(
        self,
        entity_type: str,
        cache_invalidated: set[str],
        entity_cache: dict[str, list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Thread-safe cached entity retrieval with size limits and error handling.

        Args:
            entity_type: Type of entities to retrieve
            cache_invalidated: Set of invalidated cache types
            entity_cache: Local cache for this migration run

        Returns:
            List of entities from cache or fresh API call

        Raises:
            MigrationError: If API call fails and no cached data available

        """
        try:
            # Check local cache first (fastest path)
            if entity_type in entity_cache and entity_type not in cache_invalidated:
                self._cache_stats["hits"] += 1
                BaseMigration._global_cache_stats["total_hits"] += 1
                self.logger.debug("Cache hit (local): cached %s", entity_type)
                return entity_cache[entity_type]

            # Check global cache with thread safety
            with self._cache_lock:
                if entity_type in self._global_entity_cache and entity_type not in cache_invalidated:
                    entities = self._global_entity_cache[entity_type].copy()
                    entity_cache[entity_type] = entities
                    self._cache_stats["hits"] += 1
                    BaseMigration._global_cache_stats["total_hits"] += 1
                    self.logger.debug("Cache hit (global): cached %s", entity_type)
                    return entities

            # Cache miss - perform API call with error handling
            self._cache_stats["misses"] += 1
            BaseMigration._global_cache_stats["total_misses"] += 1
            self.logger.debug("Cache miss: fetching cached %s from API", entity_type)

            try:
                entities = self._get_current_entities_for_type(entity_type)
            except Exception as e:
                self.logger.exception("Failed to fetch entities for %s", entity_type)
                msg = f"API call failed for {entity_type}: {e}"
                raise MigrationError(msg) from e

            # Store in caches with size validation
            if len(entities) <= self.MAX_CACHE_SIZE_PER_TYPE:
                entity_cache[entity_type] = entities

                with self._cache_lock:
                    # Check if cleanup needed before adding to global cache
                    self._cleanup_cache_if_needed()
                    self._global_entity_cache[entity_type] = entities.copy()
                    cache_invalidated.discard(entity_type)
            else:
                self.logger.warning(
                    "Entity list too large to cache: %s has %d entities (limit: %d)",
                    entity_type,
                    len(entities),
                    self.MAX_CACHE_SIZE_PER_TYPE,
                )
                # Still store in local cache for this run, but not global
                entity_cache[entity_type] = entities
                cache_invalidated.discard(entity_type)

            return entities  # noqa: TRY300

        except Exception:
            self.logger.exception("Critical error in cache retrieval for %s", entity_type)
            raise

    def detect_changes(
        self,
        current_entities: list[dict[str, Any]],
        entity_type: str,
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
            entities,
            entity_type,
            migration_component,
        )

    def should_skip_migration(
        self,
        entity_type: str,
        cache_func: Callable[[str], list[dict[str, Any]]] | None = None,
    ) -> tuple[bool, ChangeReport | None]:
        """Check if migration should be skipped based on change detection.

        This method allows migration components to check if there are any changes
        before performing expensive migration operations.

        Args:
            entity_type: Type of entities to check for changes
            cache_func: Optional function to use for cached entity retrieval

        Returns:
            Tuple of (should_skip, change_report). should_skip is True if no changes
            are detected and migration can be skipped.

        """
        try:
            # Get current entities from Jira for the specific entity type
            self.logger.info(
                f"Starting change detection for {entity_type} - fetching current entities from Jira",
            )
            if cache_func:
                current_entities = cache_func(entity_type)
            else:
                current_entities = self._get_current_entities_for_type(entity_type)

            self.logger.info(
                f"Fetched {len(current_entities)} current entities for {entity_type}",
            )

            # Detect changes
            self.logger.info(f"Running change detection for {entity_type}")
            change_report = self.detect_changes(current_entities, entity_type)

            # Log detailed change detection results
            summary = change_report.get("summary", {})
            self.logger.info(
                f"Change detection results for {entity_type}: "
                f"baseline={summary.get('baseline_entity_count', 0)}, "
                f"current={summary.get('current_entity_count', 0)}, "
                f"created={summary.get('entities_created', 0)}, "
                f"updated={summary.get('entities_updated', 0)}, "
                f"deleted={summary.get('entities_deleted', 0)}, "
                f"total_changes={change_report.get('total_changes', 0)}",
            )

            # If no changes detected, migration can be skipped
            should_skip = change_report["total_changes"] == 0

            if should_skip:
                self.logger.info(
                    "✓ No changes detected for %s, skipping migration (efficient!)",
                    entity_type,
                )
            else:
                self.logger.info(
                    "⚠ Detected %d changes for %s: %s - proceeding with migration",
                    change_report["total_changes"],
                    entity_type,
                    change_report["changes_by_type"],
                )

            return should_skip, change_report  # noqa: TRY300

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
        msg = (
            f"Subclass {self.__class__.__name__} must implement _get_current_entities_for_type() "
            f"to support change detection for entity type: {entity_type}"
        )
        raise NotImplementedError(
            msg,
        )

    def run_with_change_detection(
        self,
        entity_type: str | None = None,
    ) -> ComponentResult:
        """Run migration with change detection support and enhanced caching.

        This method wraps the standard run() method with change detection hooks:
        1. Check for changes before migration (using cached entities)
        2. Run the actual migration if changes are detected
        3. Create a snapshot after successful migration (using cached entities)

        Args:
            entity_type: Type of entities being migrated (required for change detection)

        Returns:
            ComponentResult with migration results

        """
        # If no entity type specified, run standard migration without change detection
        if not entity_type:
            self.logger.debug(
                "No entity type specified, running migration without change detection",
            )
            return self.run()

        # Initialize entity cache for this migration run
        entity_cache: dict[str, list[dict[str, Any]]] = {}
        cache_invalidated: set[str] = set()
        total_cache_invalidations = 0

        # Clear global cache to ensure isolation between migration runs
        with self._cache_lock:
            self._global_entity_cache.clear()
            self.logger.debug("Cleared global cache for new migration run")

        def get_cached_entities(type_name: str) -> list[dict[str, Any]]:
            """Get entities with enhanced thread-safe caching."""
            return self._get_cached_entities_threadsafe(
                type_name,
                cache_invalidated,
                entity_cache,
            )

        def invalidate_cache(type_name: str) -> None:
            """Invalidate cache for a specific entity type."""
            nonlocal total_cache_invalidations
            cache_invalidated.add(type_name)
            total_cache_invalidations += 1

            # Also invalidate from global cache for thread safety
            with self._cache_lock:
                if type_name in self._global_entity_cache:
                    del self._global_entity_cache[type_name]
                    self.logger.debug(
                        "Invalidated global cache for entity type %s",
                        type_name,
                    )

            self.logger.debug("Invalidated cache for entity type %s", type_name)

        try:
            # Check if migration should be skipped (using cached entities)
            should_skip, change_report = self.should_skip_migration(
                entity_type,
                get_cached_entities,
            )

            if should_skip:
                return ComponentResult(
                    success=True,
                    message=f"No changes detected for {entity_type}, migration skipped",
                    details={
                        "change_report": change_report,
                        "cache_stats": {
                            "types_cached": len(entity_cache),
                            "cache_invalidations": total_cache_invalidations,
                            "cache_hits": self._cache_stats["hits"],
                            "cache_misses": self._cache_stats["misses"],
                            "cache_evictions": self._cache_stats["evictions"],
                            "memory_cleanups": self._cache_stats["memory_cleanups"],
                            "total_cache_size": self._cache_stats["total_size"],
                            "global_cache_types": len(self._global_entity_cache),
                        },
                    },
                    success_count=0,
                    failed_count=0,
                    total_count=0,
                )

            # Run the actual migration
            result = self.run()

            # Invalidate cache after migration since entities may have been modified
            if result.success and entity_type:
                invalidate_cache(entity_type)

            # If migration was successful, create a snapshot for future change detection
            if result.success:
                try:
                    current_entities = get_cached_entities(entity_type)
                    snapshot_path = self.create_snapshot(current_entities, entity_type)
                    self.logger.info(
                        "Created snapshot for %s: %s",
                        entity_type,
                        snapshot_path,
                    )

                    # Add snapshot info to result details
                    if not result.details:
                        result.details = {}
                    result.details.update(
                        {
                            "snapshot_created": str(snapshot_path),
                            "change_report": change_report,
                            "cache_stats": {
                                "types_cached": len(entity_cache),
                                "cache_invalidations": total_cache_invalidations,
                                "cache_hits": self._cache_stats["hits"],
                                "cache_misses": self._cache_stats["misses"],
                                "cache_evictions": self._cache_stats["evictions"],
                                "memory_cleanups": self._cache_stats["memory_cleanups"],
                                "total_cache_size": self._cache_stats["total_size"],
                                "global_cache_types": len(self._global_entity_cache),
                            },
                        },
                    )

                except Exception as e:
                    # Don't fail the migration if snapshot creation fails
                    self.logger.warning(
                        "Failed to create snapshot after successful migration: %s",
                        e,
                    )

            return result

        except Exception as e:
            self.logger.exception("Error in change detection workflow: %s", e)
            # Fall back to standard migration if change detection fails
            return self.run()

    def _auto_detect_entity_type(self) -> str | None:
        """Auto-detect entity type using EntityTypeRegistry.

        This method uses the EntityTypeRegistry to resolve the primary entity type
        for this migration class, providing fail-fast behavior if the class is not
        properly registered.

        Returns:
            Entity type string from the registry

        Raises:
            ValueError: If the migration class is not registered with EntityTypeRegistry

        """
        try:
            return EntityTypeRegistry.resolve(self.__class__)
        except ValueError as e:
            # Log warning and provide helpful guidance
            self.logger.warning(
                "Migration class %s is not registered with EntityTypeRegistry. "
                "Add @register_entity_types decorator to the class. Error: %s",
                self.__class__.__name__,
                e,
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
        """Legacy run method (deprecated).

        DEPRECATED: This method is deprecated. Migration classes should implement
        run_with_change_detection() instead for idempotent workflow with caching,
        change detection, and API call reduction (30-50%).

        For transformation-only migrations (like RelationMigration), this method
        can still be implemented as a fallback when change detection is not supported.

        Returns:
            ComponentResult with migration results

        """
        self.logger.warning(
            "The run() method has not been implemented for %s. "
            "This is a legacy method - migrations should implement the actual migration logic here "
            "and be called via run_with_change_detection() for idempotent workflow support.",
            self.__class__.__name__,
        )
        return ComponentResult(
            success=False,
            errors=[
                f"The run() method has not been implemented for {self.__class__.__name__}. "
                f"Implement migration logic in run() and call via run_with_change_detection() "
                f"for idempotent workflow support.",
            ],
            success_count=0,
            failed_count=0,
            total_count=0,
        )
