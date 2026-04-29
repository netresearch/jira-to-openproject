"""Thread-safe entity cache with LRU-like eviction.

Two-tier caching used by migration components for entity-list lookups:

* **Local tier** — a per-call ``dict`` passed in by the caller. Lives only for
  the duration of a single migration run, holds the cache for one logical
  cohort of lookups.
* **Global tier** — an instance-owned cache shared across calls on the same
  ``EntityCache`` instance. Thread-safe via ``RLock``. Evicts largest cache
  buckets first when total entries exceed ``CLEANUP_THRESHOLD * MAX_TOTAL``.

A process-wide stats counter tracks aggregate hits / misses / evictions /
cleanups across all ``EntityCache`` instances and is read via
``EntityCache.process_stats()``.

Extracted from ``BaseMigration`` to separate caching infrastructure from
migration lifecycle logic (ADR-002 phase 1).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from collections.abc import Callable


_module_logger = logging.getLogger(__name__)


class EntityCache:
    """Two-tier thread-safe entity cache with size-based eviction."""

    MAX_PER_TYPE: ClassVar[int] = 1000
    MAX_TOTAL: ClassVar[int] = 5000
    CLEANUP_THRESHOLD: ClassVar[float] = 0.8

    _process_stats: ClassVar[dict[str, int]] = {
        "total_hits": 0,
        "total_misses": 0,
        "total_evictions": 0,
        "memory_cleanups": 0,
    }
    _process_stats_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or _module_logger
        self._lock = threading.RLock()
        self._global: dict[str, list[dict[str, Any]]] = {}
        self.stats: dict[str, int] = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "memory_cleanups": 0,
            "total_size": 0,
        }

    # ── public API ────────────────────────────────────────────────────────

    def clear_global(self) -> None:
        """Drop everything from the instance-wide cache."""
        with self._lock:
            self._global.clear()
            self._logger.debug("Cleared global cache for new migration run")

    def invalidate(self, entity_type: str) -> None:
        """Drop one entity type from the instance-wide cache (thread-safe)."""
        with self._lock:
            if entity_type in self._global:
                del self._global[entity_type]
                self._logger.debug(
                    "Invalidated global cache for entity type %s",
                    entity_type,
                )

    def global_size(self) -> int:
        """Return the number of distinct entity types currently in the global cache."""
        with self._lock:
            return len(self._global)

    def get_or_fetch(
        self,
        entity_type: str,
        fetch: Callable[[str], list[dict[str, Any]]],
        *,
        local: dict[str, list[dict[str, Any]]],
        invalidated: set[str],
    ) -> list[dict[str, Any]]:
        """Retrieve entities, consulting the local then the global cache before fetching.

        Args:
            entity_type: Cache key (typically a Jira/OP entity type name).
            fetch: Callable invoked on a cache miss; takes ``entity_type`` and
                returns a fresh entity list.
            local: Per-run local cache. Mutated in place.
            invalidated: Set of entity types that must bypass cached values.

        Returns:
            The cached or freshly-fetched entity list.

        """
        # Tier 1: local cache (fastest path)
        if entity_type in local and entity_type not in invalidated:
            self._record_hit()
            self._logger.debug("Cache hit (local): cached %s", entity_type)
            return local[entity_type]

        # Tier 2: global cache (thread-safe)
        with self._lock:
            if entity_type in self._global and entity_type not in invalidated:
                entities = self._global[entity_type].copy()
                local[entity_type] = entities
                self._record_hit()
                self._logger.debug("Cache hit (global): cached %s", entity_type)
                return entities

        # Miss: fetch via callback
        self._record_miss()
        self._logger.debug("Cache miss: fetching cached %s from API", entity_type)

        entities = fetch(entity_type)

        # Store with size validation
        if len(entities) <= self.MAX_PER_TYPE:
            local[entity_type] = entities
            with self._lock:
                self._cleanup_if_needed_locked()
                self._global[entity_type] = entities.copy()
                invalidated.discard(entity_type)
        else:
            self._logger.warning(
                "Entity list too large to cache: %s has %d entities (limit: %d)",
                entity_type,
                len(entities),
                self.MAX_PER_TYPE,
            )
            local[entity_type] = entities
            invalidated.discard(entity_type)

        return entities

    @classmethod
    def process_stats(cls) -> dict[str, int]:
        """Return a snapshot of process-wide aggregate counters."""
        with cls._process_stats_lock:
            return dict(cls._process_stats)

    # ── internals ─────────────────────────────────────────────────────────

    def _cleanup_if_needed_locked(self) -> None:
        """Evict largest cache buckets if total entries exceed the threshold.

        Caller must hold ``self._lock``.
        """
        current = sum(len(v) for v in self._global.values())
        self.stats["total_size"] = current

        if current <= self.MAX_TOTAL * self.CLEANUP_THRESHOLD:
            return

        # Sort cache types by size, largest first.
        sizes = sorted(
            ((k, len(v)) for k, v in self._global.items()),
            key=lambda kv: kv[1],
            reverse=True,
        )
        target = self.MAX_TOTAL // 2
        removed = 0
        removed_types: list[str] = []

        for cache_key, size in sizes:
            if current - removed <= target:
                break
            del self._global[cache_key]
            removed += size
            removed_types.append(cache_key)
            self.stats["evictions"] += 1
            with EntityCache._process_stats_lock:
                EntityCache._process_stats["total_evictions"] += 1

        self.stats["memory_cleanups"] += 1
        with EntityCache._process_stats_lock:
            EntityCache._process_stats["memory_cleanups"] += 1

        self._logger.info(
            "Cache cleanup: removed %d entities from %d types: %s",
            removed,
            len(removed_types),
            removed_types,
        )

    def _record_hit(self) -> None:
        with self._lock:
            self.stats["hits"] += 1
        with EntityCache._process_stats_lock:
            EntityCache._process_stats["total_hits"] += 1

    def _record_miss(self) -> None:
        with self._lock:
            self.stats["misses"] += 1
        with EntityCache._process_stats_lock:
            EntityCache._process_stats["total_misses"] += 1
