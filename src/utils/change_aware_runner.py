"""Change-aware runner for migration components.

Wraps a ``BaseMigration`` with the change-detection + entity-cache workflow
that used to live on ``BaseMigration`` itself. Extraction follows ADR-002
phase 1, leaving ``BaseMigration`` as a smaller pure-ETL skeleton.

A single runner instance is short-lived (one per migration run). It holds
references to the migration's existing ``ChangeDetector`` and ``EntityCache``
instances, so wrapping is essentially free.

The runner provides three public entry points:

* ``run(entity_type)`` — replaces ``BaseMigration.run_with_change_detection``.
  Skips work when no changes are detected, otherwise runs the migration's
  ``run()`` and creates a fresh snapshot.
* ``should_skip(entity_type, cache_func=None)`` — replaces
  ``BaseMigration.should_skip_migration``. Returns ``(should_skip, change_report)``.
* ``detect_changes(entities, entity_type)`` and ``create_snapshot(entities,
  entity_type)`` — thin wrappers around the underlying ``ChangeDetector``
  that fill in the migration's component name automatically.

``BaseMigration``'s same-named methods are now thin delegators to a runner
instance, so existing call sites and subclass overrides (notably
``CompanyMigration.should_skip_migration`` calling ``super()``) continue to
work unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.models import ComponentResult, MigrationError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from src.migrations.base_migration import BaseMigration
    from src.utils.change_detector import ChangeReport


class ChangeAwareRunner:
    """Run a migration with change detection and entity caching.

    Composes ``ChangeDetector`` and ``EntityCache`` (taken from the migration
    instance) so a single migration class only needs to implement
    ``_extract`` / ``_map`` / ``_load`` / ``run`` plus
    ``_get_current_entities_for_type``.
    """

    def __init__(self, migration: BaseMigration) -> None:
        self.migration = migration
        self.change_detector = migration.change_detector
        self.entity_cache = migration.entity_cache
        self.logger = migration.logger

    # ── public API ────────────────────────────────────────────────────────

    def run(self, entity_type: str | None = None) -> ComponentResult:
        """Run the migration with change-detection + entity-caching.

        If ``entity_type`` is None, falls back to ``migration.run()`` directly.
        Otherwise: clear the global cache for isolation, check for changes,
        skip if none, run the migration, and create a snapshot on success.
        """
        if not entity_type:
            self.logger.debug(
                "No entity type specified, running migration without change detection",
            )
            return self.migration.run()

        # Per-run local cache and invalidation set
        local_cache: dict[str, list[dict[str, Any]]] = {}
        cache_invalidated: set[str] = set()
        total_cache_invalidations = 0

        # Clear instance-wide cache for run isolation
        self.entity_cache.clear_global()

        def get_cached_entities(type_name: str) -> list[dict[str, Any]]:
            return self._get_cached_entities(
                type_name,
                local=local_cache,
                invalidated=cache_invalidated,
            )

        def invalidate(type_name: str) -> None:
            nonlocal total_cache_invalidations
            cache_invalidated.add(type_name)
            total_cache_invalidations += 1
            self.entity_cache.invalidate(type_name)
            self.logger.debug("Invalidated cache for entity type %s", type_name)

        # Change detection is an optimisation; if it fails, fall back to a
        # plain migration run. Scope this `try` narrowly so that exceptions
        # raised by `migration.run()` itself propagate to the caller — running
        # the migration twice on a transient error is dangerous (the migration
        # may not be idempotent) and the original error would be hidden.
        try:
            # Call back through the migration's method so subclass overrides
            # (e.g. CompanyMigration) and test monkeypatches take effect.
            should_skip, change_report = self.migration.should_skip_migration(
                entity_type,
                get_cached_entities,
            )
        except Exception:
            self.logger.exception(
                "Change detection failed for %s; running migration without it",
                entity_type,
            )
            return self.migration.run()

        if should_skip:
            return ComponentResult(
                success=True,
                message=f"No changes detected for {entity_type}, migration skipped",
                details={
                    "change_report": change_report,
                    "cache_stats": self._cache_stats_snapshot(
                        len(local_cache),
                        total_cache_invalidations,
                    ),
                },
                success_count=0,
                failed_count=0,
                total_count=0,
            )

        # Run the actual migration. Errors here propagate — change detection
        # already gave us a green light, so a failure here is a real failure.
        result = self.migration.run()

        # Invalidate cached source data after the migration touched the world
        if result.success and entity_type:
            invalidate(entity_type)

        # Snapshot for next-run change detection (best-effort; failure does
        # not fail the migration itself).
        if result.success:
            try:
                current_entities = get_cached_entities(entity_type)
                # Call back through the migration so subclass overrides of
                # create_snapshot take effect.
                snapshot_path = self.migration.create_snapshot(current_entities, entity_type)
                self.logger.info(
                    "Created snapshot for %s: %s",
                    entity_type,
                    snapshot_path,
                )
                if not result.details:
                    result.details = {}
                result.details.update(
                    {
                        "snapshot_created": str(snapshot_path),
                        "change_report": change_report,
                        "cache_stats": self._cache_stats_snapshot(
                            len(local_cache),
                            total_cache_invalidations,
                        ),
                    },
                )
            except Exception as e:
                self.logger.warning(
                    "Failed to create snapshot after successful migration: %s",
                    e,
                )

        return result

    def should_skip(
        self,
        entity_type: str,
        cache_func: Callable[[str], list[dict[str, Any]]] | None = None,
    ) -> tuple[bool, ChangeReport | None]:
        """Decide whether the migration can skip based on change detection.

        On any failure inside change detection, return ``(False, None)`` so the
        migration runs anyway — change detection is an optimisation, not a gate.
        """
        try:
            self.logger.info(
                f"Starting change detection for {entity_type} - fetching current entities from Jira",
            )
            if cache_func:
                current_entities = cache_func(entity_type)
            else:
                current_entities = self.migration._get_current_entities_for_type(entity_type)

            self.logger.info(
                f"Fetched {len(current_entities)} current entities for {entity_type}",
            )

            self.logger.info(f"Running change detection for {entity_type}")
            change_report = self.detect_changes(current_entities, entity_type)

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

            return should_skip, change_report

        except Exception as e:
            self.logger.warning(
                "Change detection failed for %s: %s. Proceeding with migration.",
                entity_type,
                e,
            )
            return False, None

    def detect_changes(
        self,
        current_entities: list[dict[str, Any]],
        entity_type: str,
    ) -> ChangeReport:
        """Delegate to the underlying ``ChangeDetector``."""
        return self.change_detector.detect_changes(current_entities, entity_type)

    def create_snapshot(
        self,
        entities: list[dict[str, Any]],
        entity_type: str,
    ) -> Path:
        """Snapshot current entities, tagging with the migration's class name."""
        return self.change_detector.create_snapshot(
            entities,
            entity_type,
            self.migration.__class__.__name__,
        )

    def auto_detect_entity_type(self) -> str | None:
        """Resolve the migration class's primary entity type via the registry."""
        from src.migrations.base_migration import EntityTypeRegistry

        try:
            return EntityTypeRegistry.resolve(type(self.migration))
        except ValueError as e:
            self.logger.warning(
                "Migration class %s is not registered with EntityTypeRegistry. "
                "Add @register_entity_types decorator to the class. Error: %s",
                self.migration.__class__.__name__,
                e,
            )
            return None

    # ── internals ─────────────────────────────────────────────────────────

    def _get_cached_entities(
        self,
        entity_type: str,
        *,
        local: dict[str, list[dict[str, Any]]],
        invalidated: set[str],
    ) -> list[dict[str, Any]]:
        """Cache-aware fetch wrapping the migration's API exception in MigrationError."""

        def _fetch(name: str) -> list[dict[str, Any]]:
            try:
                return self.migration._get_current_entities_for_type(name)
            except Exception as e:
                self.logger.exception("Failed to fetch entities for %s", name)
                msg = f"API call failed for {name}: {e}"
                raise MigrationError(msg) from e

        return self.entity_cache.get_or_fetch(
            entity_type,
            _fetch,
            local=local,
            invalidated=invalidated,
        )

    def _cache_stats_snapshot(
        self,
        types_cached: int,
        cache_invalidations: int,
    ) -> dict[str, int]:
        """Build the cache_stats dict embedded in ComponentResult.details."""
        return {
            "types_cached": types_cached,
            "cache_invalidations": cache_invalidations,
            "cache_hits": self.entity_cache.stats["hits"],
            "cache_misses": self.entity_cache.stats["misses"],
            "cache_evictions": self.entity_cache.stats["evictions"],
            "memory_cleanups": self.entity_cache.stats["memory_cleanups"],
            "total_cache_size": self.entity_cache.stats["total_size"],
            "global_cache_types": self.entity_cache.global_size(),
        }
