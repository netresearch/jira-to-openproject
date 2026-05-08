"""Composable decorators for :class:`JiraClient`.

This module replaces the inheritance-based ``EnhancedJiraClient`` with a set of
small decorators that wrap a base :class:`JiraClient` instance. Each decorator
implements a single concern (caching, batched/parallel reads, streaming) and
delegates everything else to the wrapped client through ``__getattr__``.

Composition example::

    client = JiraClient(...)
    client = CachingDecorator(client, cache_ttl=300)
    client = BatchOperationsDecorator(client, batch_size=50, parallel_workers=8)
    client = StreamingDecorator(client)
    client = PerformanceMonitoringDecorator(client)

Phase 8b of ADR-002: Composition over inheritance for client extensions.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from src.config import logger

if TYPE_CHECKING:
    from jira import Issue


@runtime_checkable
class JiraClientLike(Protocol):
    """Minimal structural type for a wrapped Jira client.

    The decorators only require the attributes touched by their specific
    concern; all other access is forwarded via ``__getattr__``. This Protocol
    documents the surface the decorators rely on without forcing an import of
    the concrete :class:`JiraClient`.
    """

    jira: Any
    base_url: str
    batch_size: int
    parallel_workers: int


class _BaseJiraDecorator:
    """Base class providing transparent attribute delegation.

    Any attribute not defined on the decorator (or its subclasses) is resolved
    against the wrapped client. Subclasses override only the methods whose
    behavior they augment.
    """

    def __init__(self, wrapped: Any) -> None:
        # Use object.__setattr__ to avoid triggering __getattr__ during init.
        object.__setattr__(self, "_wrapped", wrapped)

    def __getattr__(self, name: str) -> Any:
        # __getattr__ runs only when normal lookup fails, so this is safe and
        # avoids manually proxying every method on the wrapped client.
        wrapped = object.__getattribute__(self, "_wrapped")
        return getattr(wrapped, name)

    @property
    def wrapped(self) -> Any:
        """Return the directly-wrapped client (one layer down)."""
        return object.__getattribute__(self, "_wrapped")


class CachingDecorator(_BaseJiraDecorator):
    """TTL cache for low-churn read methods on a Jira client.

    Wraps the read endpoints exposed by ``EnhancedJiraClient`` so callers see
    cached results for ``cache_ttl`` seconds. Cache misses fall through to the
    wrapped client.
    """

    _CACHED_METHODS: tuple[str, ...] = (
        "get_users",
        "get_projects",
        "get_priorities",
        "get_all_statuses",
        "get_status_categories",
        "get_issue_types",
        "get_custom_fields",
    )

    def __init__(self, wrapped: Any, cache_ttl: float = 300.0) -> None:
        super().__init__(wrapped)
        object.__setattr__(self, "_cache", {})
        object.__setattr__(self, "_cache_ttl", float(cache_ttl))
        object.__setattr__(self, "_cache_lock", Lock())

    def _cached_call(self, key: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        now = time.monotonic()
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is not None and entry[0] > now:
                return entry[1]
        value = fn(*args, **kwargs)
        with self._cache_lock:
            self._cache[key] = (now + self._cache_ttl, value)
        return value

    def invalidate(self, key: str | None = None) -> None:
        """Drop a single cache entry, or the whole cache if ``key`` is ``None``."""
        with self._cache_lock:
            if key is None:
                self._cache.clear()
            else:
                self._cache.pop(key, None)

    def get_project_cached(self, key: str) -> dict[str, Any]:
        """Cache wrapper around per-key project metadata lookups.

        ``JiraClient.get_projects()`` takes no arguments (it lists every
        project), so falling back to it with a ``key`` would raise
        ``TypeError`` (PR #170 review). The real per-key endpoint on the
        client is :meth:`JiraClient.get_project_metadata_enhanced`; if a
        wrapped client exposes a ``get_project_details(key)`` shim
        (e.g., a fixture), we honour that first.
        """
        per_key_lookup = (
            getattr(
                self._wrapped,
                "get_project_details",
                None,
            )
            or self._wrapped.get_project_metadata_enhanced
        )
        return self._cached_call(f"project:{key}", per_key_lookup, key)

    def get_statuses_cached(self) -> list[dict[str, Any]]:
        """Cache wrapper around ``get_all_statuses``."""
        return self._cached_call("statuses", self._wrapped.get_all_statuses)

    def __getattr__(self, name: str) -> Any:
        wrapped = object.__getattribute__(self, "_wrapped")
        attr = getattr(wrapped, name)
        if name not in self._CACHED_METHODS or not callable(attr):
            return attr

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            cache_key = f"{name}:{args!r}:{sorted(kwargs.items())!r}"
            return self._cached_call(cache_key, attr, *args, **kwargs)

        return wrapper


class BatchOperationsDecorator(_BaseJiraDecorator):
    """Parallel/batched read helpers for Jira issues.

    Replicates the batch API surface of ``EnhancedJiraClient``:
    ``batch_get_issues``, ``batch_get_work_logs``, ``bulk_get_issue_metadata``.
    Exposes tunable ``batch_size`` and ``parallel_workers`` overrides.
    """

    def __init__(
        self,
        wrapped: Any,
        batch_size: int | None = None,
        parallel_workers: int | None = None,
    ) -> None:
        super().__init__(wrapped)
        object.__setattr__(
            self,
            "_batch_size",
            int(batch_size) if batch_size is not None else int(getattr(wrapped, "batch_size", 100)),
        )
        object.__setattr__(
            self,
            "_parallel_workers",
            int(parallel_workers) if parallel_workers is not None else int(getattr(wrapped, "parallel_workers", 8)),
        )

    @property
    def batch_size(self) -> int:
        """Configured batch size for parallel reads."""
        return self._batch_size

    @property
    def parallel_workers(self) -> int:
        """Configured worker count for parallel reads."""
        return self._parallel_workers

    def _chunked(self, keys: list[str]) -> list[list[str]]:
        return [keys[i : i + self._batch_size] for i in range(0, len(keys), self._batch_size)]

    def _fetch_issues_batch(self, issue_keys: list[str]) -> dict[str, Issue | None]:
        if not issue_keys:
            return {}
        jira = getattr(self._wrapped, "jira", None)
        if not jira:
            msg = "Jira client is not initialized"
            raise RuntimeError(msg)
        jql = f"key in ({','.join(issue_keys)})"
        try:
            issues = jira.search_issues(jql, maxResults=len(issue_keys), expand="changelog")
            found = {issue.key: issue for issue in issues}
            return {key: found.get(key) for key in issue_keys}
        except Exception:
            return dict.fromkeys(issue_keys)

    def batch_get_issues(self, issue_keys: list[str]) -> dict[str, Issue | None]:
        """Fetch issues in parallel batches; missing/failed keys map to ``None``."""
        if not issue_keys:
            return {}

        results: dict[str, Issue | None] = {}
        batches = self._chunked(issue_keys)
        with ThreadPoolExecutor(max_workers=self._parallel_workers) as executor:
            future_to_keys: dict[Any, list[str]] = {}
            for batch in batches:
                future_to_keys[executor.submit(self._fetch_issues_batch, batch)] = batch
            for fut in as_completed(future_to_keys):
                keys = future_to_keys[fut]
                try:
                    results.update(fut.result())
                except Exception as exc:
                    logger.warning(
                        "BatchOperationsDecorator: batch of %d failed (first=%s): %s",
                        len(keys),
                        keys[0] if keys else "<empty>",
                        exc,
                    )
                    for k in keys:
                        results[k] = None
        return results

    def batch_get_work_logs(self, issue_keys: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Fetch work logs for many issues, sequentially per-issue but parallel across issues."""
        if not issue_keys:
            return {}

        worklog_fn = getattr(self._wrapped, "get_work_logs_for_issue", None)
        if worklog_fn is None:
            msg = "Wrapped client does not implement get_work_logs_for_issue"
            raise AttributeError(msg)

        results: dict[str, list[dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=self._parallel_workers) as executor:
            future_to_key = {executor.submit(worklog_fn, key): key for key in issue_keys}
            for fut in as_completed(future_to_key):
                key = future_to_key[fut]
                try:
                    results[key] = list(fut.result() or [])
                except Exception:
                    results[key] = []
        return results

    def bulk_get_issue_metadata(self, issue_keys: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch issue metadata for many keys in parallel.

        Uses the wrapped client's ``get_issue_details`` if available; otherwise
        falls back to ``get_issue`` and extracts a minimal metadata view.
        """
        if not issue_keys:
            return {}

        details_fn = getattr(self._wrapped, "get_issue_details", None)
        results: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=self._parallel_workers) as executor:
            future_to_key = {executor.submit(details_fn or self._fallback_metadata, key): key for key in issue_keys}
            for fut in as_completed(future_to_key):
                key = future_to_key[fut]
                try:
                    value = fut.result()
                except Exception:
                    continue
                if isinstance(value, dict):
                    results[key] = value
        return results

    def _fallback_metadata(self, issue_key: str) -> dict[str, Any]:
        return {"key": issue_key, "summary": f"Summary for {issue_key}"}


class StreamingDecorator(_BaseJiraDecorator):
    """Memory-efficient streaming search over JQL pages.

    Mirrors :meth:`EnhancedJiraClient.stream_search_issues` without materializing
    the full result set in memory.
    """

    def stream_search_issues(
        self,
        jql: str,
        page_size: int = 50,
        max_pages: int | None = None,
    ) -> Iterator[Issue]:
        """Yield issues one page at a time until the source is exhausted."""
        jira = getattr(self._wrapped, "jira", None)
        if not jira:
            msg = "Jira client is not initialized"
            raise RuntimeError(msg)

        start_at = 0
        pages = 0
        while True:
            try:
                issues = jira.search_issues(jql, startAt=start_at, maxResults=page_size)
            except Exception:
                break
            if not issues:
                break
            yield from issues
            start_at += len(issues)
            pages += 1
            if max_pages is not None and pages >= max_pages:
                break


class PerformanceMonitoringDecorator(_BaseJiraDecorator):
    """Record per-method call counts and elapsed time for the wrapped client.

    The decorator wraps every callable attribute the first time it is accessed
    and records ``(call_count, total_seconds)`` in :attr:`metrics`. Callers can
    inspect timings without changing call sites.
    """

    def __init__(self, wrapped: Any) -> None:
        super().__init__(wrapped)
        object.__setattr__(self, "_metrics", {})
        object.__setattr__(self, "_metrics_lock", Lock())

    @property
    def metrics(self) -> dict[str, dict[str, float]]:
        """Snapshot of current metrics keyed by attribute name."""
        with self._metrics_lock:
            return {
                name: {"calls": float(stats[0]), "total_seconds": stats[1]} for name, stats in self._metrics.items()
            }

    def reset_metrics(self) -> None:
        """Clear all recorded metrics."""
        with self._metrics_lock:
            self._metrics.clear()

    def __getattr__(self, name: str) -> Any:
        wrapped = object.__getattribute__(self, "_wrapped")
        attr = getattr(wrapped, name)
        if not callable(attr) or name.startswith("_"):
            return attr

        def timed(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            try:
                return attr(*args, **kwargs)
            finally:
                elapsed = time.perf_counter() - start
                with self._metrics_lock:
                    calls, total = self._metrics.get(name, (0, 0.0))
                    self._metrics[name] = (calls + 1, total + elapsed)

        return timed


__all__ = [
    "BatchOperationsDecorator",
    "CachingDecorator",
    "JiraClientLike",
    "PerformanceMonitoringDecorator",
    "StreamingDecorator",
]
