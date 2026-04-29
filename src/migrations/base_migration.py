"""Base migration class providing common functionality for all migrations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import configure_logging
from src.models import ComponentResult

# Import dependencies
from src.utils.change_aware_runner import ChangeAwareRunner
from src.utils.change_detector import ChangeDetector, ChangeReport
from src.utils.entity_cache import EntityCache
from src.utils.json_store import JsonStore


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
            raise ValueError(msg)

        if not issubclass(migration_class, BaseMigration):
            msg = f"Class {migration_class.__name__} must inherit from BaseMigration"
            raise ValueError(msg)

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

    Caching is delegated to ``EntityCache`` (see ``src.utils.entity_cache``).

    Follows the layered client architecture:
    1. OpenProjectClient - Manages all lower-level clients and operations
    2. BaseMigration - Uses OpenProjectClient for migrations
    """

    def __init__(
        self,
        jira_client: JiraClient | None = None,
        op_client: OpenProjectClient | None = None,
        change_detector: ChangeDetector | None = None,
        entity_cache: EntityCache | None = None,
    ) -> None:
        """Initialize the base migration with common attributes.

        Args:
            jira_client: Initialized Jira client
            op_client: Initialized OpenProject client
            change_detector: Initialized change detector for idempotent operations
            entity_cache: Optional pre-built entity cache; otherwise a fresh one
                is created.

        """
        # Initialize clients using dependency injection
        self.jira_client = jira_client or JiraClient()
        if op_client is not None:
            self.op_client = op_client
        else:
            try:
                self.op_client = OpenProjectClient()
            except Exception as e:
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

        # Use config.mappings (proxy) which supports test monkeypatching.
        # The _MappingsProxy delegates to get_mappings() in production;
        # tests replace it via monkeypatch.setattr(cfg, "mappings", DummyMappings()).
        self.mappings = config.mappings

        self.entity_cache = entity_cache or EntityCache(self.logger)
        self.json_store = JsonStore(self.data_dir, self.logger)

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

    def detect_changes(
        self,
        current_entities: list[dict[str, Any]],
        entity_type: str,
    ) -> ChangeReport:
        """Detect changes in entities since the last migration run.

        Thin delegator over ``self.change_detector``.
        """
        return self.change_detector.detect_changes(current_entities, entity_type)

    def create_snapshot(
        self,
        entities: list[dict[str, Any]],
        entity_type: str,
    ) -> Path:
        """Create a snapshot of entities after successful migration.

        Thin delegator over ``self.change_detector`` that fills in this
        migration's class name as the component label.
        """
        return self.change_detector.create_snapshot(
            entities,
            entity_type,
            self.__class__.__name__,
        )

    def should_skip_migration(
        self,
        entity_type: str,
        cache_func: Callable[[str], list[dict[str, Any]]] | None = None,
    ) -> tuple[bool, ChangeReport | None]:
        """Check if migration should be skipped based on change detection.

        Delegates to ``ChangeAwareRunner`` so the implementation lives in one
        place. Subclasses (notably ``CompanyMigration``) override this to add
        component-specific skip logic and call ``super().should_skip_migration``
        — that override-and-super pattern still works because this method
        keeps the same signature and contract.
        """
        return ChangeAwareRunner(self).should_skip(entity_type, cache_func)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities from Jira for a specific type.

        This method should be overridden by subclasses to provide entity-specific
        retrieval logic.
        """
        msg = (
            f"Subclass {self.__class__.__name__} must implement _get_current_entities_for_type() "
            f"to support change detection for entity type: {entity_type}"
        )
        raise NotImplementedError(msg)

    def run_with_change_detection(
        self,
        entity_type: str | None = None,
    ) -> ComponentResult:
        """Run migration with change detection support and enhanced caching.

        Delegates to ``ChangeAwareRunner`` which owns the workflow body
        (cache isolation, skip-on-no-changes, run, snapshot).
        """
        return ChangeAwareRunner(self).run(entity_type)

    def _auto_detect_entity_type(self) -> str | None:
        """Resolve the migration class's primary entity type via the registry.

        Direct call to ``EntityTypeRegistry.resolve`` — does not go via
        ``ChangeAwareRunner`` because tests instantiate migrations with a
        minimal ``__init__`` that doesn't set ``change_detector`` or
        ``entity_cache``, and this method only needs the registry.
        """
        try:
            return EntityTypeRegistry.resolve(type(self))
        except ValueError as e:
            self.logger.warning(
                "Migration class %s is not registered with EntityTypeRegistry. "
                "Add @register_entity_types decorator to the class. Error: %s",
                self.__class__.__name__,
                e,
            )
            return None

    def _json_store(self) -> JsonStore:
        """Return the bound JsonStore, building one on demand if ``__init__`` was bypassed.

        Several tests instantiate migrations via ``cls.__new__(cls)`` and set
        ``data_dir`` manually, so ``self.json_store`` is not populated. Falling
        back to a fresh JsonStore preserves that pattern without forcing
        every fixture to be updated.
        """
        store: JsonStore | None = getattr(self, "json_store", None)
        if store is None:
            store = JsonStore(self.data_dir, getattr(self, "logger", None))
        return store

    def _load_from_json(self, filename: Path, default: Any = None) -> Any:
        """Load data from a JSON file in the data directory.

        Thin delegator over ``self.json_store`` so subclasses keep the existing
        ``self._load_from_json(...)`` call shape.
        """
        return self._json_store().load(filename, default)

    def _save_to_json(self, data: Any, filename: Path | str) -> Path:
        """Save data to a JSON file in the data directory.

        Thin delegator over ``self.json_store`` so subclasses keep the existing
        ``self._save_to_json(...)`` call shape.
        """
        return self._json_store().save(data, filename)

    # ── DRY helper methods ─────────────────────────────────────────────

    def _merge_batch_issues(self, keys: list[str]) -> dict[str, Any]:
        """Fetch issues via batch_get_issues and merge batch results into a single dict.

        batch_get_issues may return list[dict] (from batch processor) or dict.
        This helper normalizes either return type into a single merged dict.

        Args:
            keys: List of Jira issue keys to fetch

        Returns:
            Merged dict mapping issue key to issue data

        """
        result = self.jira_client.batch_get_issues(keys)
        issues: dict[str, Any] = {}
        if isinstance(result, list):
            for batch_dict in result:
                if isinstance(batch_dict, dict):
                    issues.update(batch_dict)
        elif isinstance(result, dict):
            issues = result
        return issues

    @staticmethod
    def _issue_project_key(issue_key: str) -> str:
        """Extract the project key from a Jira issue key (e.g. 'PROJ-123' -> 'PROJ')."""
        try:
            return str(issue_key).split("-", 1)[0]
        except Exception:
            return str(issue_key)

    @staticmethod
    def _resolve_wp_id(wp_map: dict[str, Any], key: str) -> int | None:
        """Resolve OP work package ID from a mapping entry.

        Args:
            wp_map: Work package mapping dict
            key: Jira issue key to look up

        Returns:
            OpenProject work package ID, or None if not found

        """
        entry = wp_map.get(key)
        if isinstance(entry, dict) and entry.get("openproject_id"):
            return int(entry["openproject_id"])
        if isinstance(entry, int):
            return entry
        return None

    def _ensure_wp_custom_field(self, name: str, field_format: str = "text") -> int:
        """Ensure a WorkPackageCustomField exists, creating it if needed.

        Thin delegator over ``OpenProjectClient.ensure_wp_custom_field_id``.
        The actual Ruby script and CF semantics (``is_for_all: false`` so the
        caller can selectively enable on specific projects) live there.
        """
        return self.op_client.ensure_wp_custom_field_id(name, field_format)

    def _enable_cf_for_projects(self, cf_id: int, project_ids: set[int], cf_name: str | None = None) -> None:
        """Enable a custom field for specific projects only.

        Thin delegator over ``OpenProjectClient.enable_custom_field_for_projects``.
        """
        self.op_client.enable_custom_field_for_projects(cf_id, project_ids, cf_name=cf_name)

    def _run_etl_pipeline(self, name: str) -> ComponentResult:
        """Standard ETL run method for extract -> map -> load pattern.

        Subclasses that follow the standard ETL pattern can use this in their
        run() method instead of duplicating the boilerplate.

        Args:
            name: Human-readable name for log messages (e.g. "Labels")

        Returns:
            ComponentResult from the load phase

        """
        self.logger.info("Starting %s migration...", name)
        try:
            extracted = self._extract()
            if not extracted.success:
                return extracted
            mapped = self._map(extracted)
            if not mapped.success:
                return mapped
            result = self._load(mapped)
            self.logger.info(
                "%s migration completed: success=%s, updated=%s, failed=%s",
                name,
                result.success,
                getattr(result, "updated", getattr(result, "success_count", "?")),
                getattr(result, "failed", getattr(result, "failed_count", "?")),
            )
            return result
        except Exception as e:
            self.logger.exception("%s migration failed", name)
            return ComponentResult(
                success=False,
                message=f"{name} migration failed: {e}",
                errors=[str(e)],
            )

    def _extract(self) -> ComponentResult:
        """Extract phase - override in subclass."""
        msg = f"{self.__class__.__name__} must implement _extract()"
        raise NotImplementedError(msg)

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        """Map phase - default pass-through. Override in subclass if needed."""
        return extracted

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        """Load phase - override in subclass."""
        msg = f"{self.__class__.__name__} must implement _load()"
        raise NotImplementedError(msg)

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
