"""Composable decorators for :class:`OpenProjectClient`.

Phase 8b of ADR-002 replaces the inheritance-based ``EnhancedOpenProjectClient``
with a set of small decorators that wrap a base :class:`OpenProjectClient`
instance. Each decorator owns a single concern (caching, parallel REST reads,
file-based Rails batch writes) and forwards everything else through
``__getattr__``.

Composition example::

    client = OpenProjectClient(...)
    client = CachingDecorator(client, cache_ttl=300)
    client = ParallelReadsDecorator(client, parallel_workers=8)
    client = FileBasedBatchWritesDecorator(client)
    client = PerformanceMonitoringDecorator(client)
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import tempfile
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Any, Protocol, runtime_checkable

import requests

from src.display import configure_logging

logger = configure_logging("INFO", None)


@runtime_checkable
class OpenProjectClientLike(Protocol):
    """Structural type for the wrapped OpenProject client.

    Decorators only depend on the small surface they actually use; everything
    else is forwarded via ``__getattr__``. The Protocol exists for type
    documentation rather than runtime enforcement.
    """

    parallel_workers: int


class RailsExecutionError(Exception):
    """Raised when the Rails runner subprocess fails or returns invalid JSON."""


class _BaseOPDecorator:
    """Transparent attribute-delegation base for OpenProject decorators."""

    def __init__(self, wrapped: Any) -> None:
        object.__setattr__(self, "_wrapped", wrapped)

    def __getattr__(self, name: str) -> Any:
        wrapped = object.__getattribute__(self, "_wrapped")
        return getattr(wrapped, name)

    @property
    def wrapped(self) -> Any:
        """Return the directly-wrapped client (one layer down)."""
        return object.__getattribute__(self, "_wrapped")


class CachingDecorator(_BaseOPDecorator):
    """TTL cache for low-churn OpenProject read endpoints.

    Caches ``get_priorities``, ``get_types`` and similar reads. Cache misses
    fall through to the wrapped client.
    """

    _CACHED_METHODS: tuple[str, ...] = (
        "get_priorities",
        "get_types",
        "get_users",
        "get_projects",
        "get_statuses",
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

    def get_priorities_cached(self) -> list[dict[str, Any]]:
        """Cache wrapper around ``get_priorities``."""
        return self._cached_call("priorities", self._wrapped.get_priorities)

    def get_types_cached(self) -> list[dict[str, Any]]:
        """Cache wrapper around ``get_types``."""
        return self._cached_call("types", self._wrapped.get_types)

    def __getattr__(self, name: str) -> Any:
        wrapped = object.__getattribute__(self, "_wrapped")
        attr = getattr(wrapped, name)
        if name not in self._CACHED_METHODS or not callable(attr):
            return attr

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            cache_key = f"{name}:{args!r}:{sorted(kwargs.items())!r}"
            return self._cached_call(cache_key, attr, *args, **kwargs)

        return wrapper


class ParallelReadsDecorator(_BaseOPDecorator):
    """Parallel REST reads for bulk work-package fetches.

    Mirrors :meth:`EnhancedOpenProjectClient.bulk_get_work_packages` using a
    REST session against ``/api/v3/work_packages/<id>``. Failed lookups are
    coerced to ``None`` to keep the result map dense.
    """

    def __init__(self, wrapped: Any, parallel_workers: int | None = None) -> None:
        super().__init__(wrapped)
        object.__setattr__(
            self,
            "_parallel_workers",
            int(parallel_workers) if parallel_workers is not None else int(getattr(wrapped, "parallel_workers", 8)),
        )
        object.__setattr__(self, "_session", None)

    @property
    def parallel_workers(self) -> int:
        """Configured worker count for parallel reads."""
        return self._parallel_workers

    def _get_session(self) -> requests.Session:
        if self._session is None:
            object.__setattr__(self, "_session", requests.Session())
        return self._session  # type: ignore[return-value]

    def _get_work_package_safe(self, wp_id: int) -> dict[str, Any] | None:
        r"""Best-effort single work-package lookup via the wrapped Rails client.

        ``OpenProjectClient`` in this codebase is a Rails-runner-over-SSH/docker
        adapter — it has no HTTP base URL, so the historical
        ``EnhancedOpenProjectClient`` placeholder using a relative
        ``requests.Session.get("/api/v3/work_packages/...")`` could never run
        outside its mock test fixture (PR #170 review). Route through the
        wrapped client's ``execute_json_query`` instead, which is the documented
        path for arbitrary single-record reads.
        """
        wrapped = self.wrapped
        try:
            result = wrapped.execute_json_query(
                f"WorkPackage.where(id: {int(wp_id)}).as_json.first"
            )
        except Exception:
            return None
        if isinstance(result, dict) and result:
            return result
        return None

    def bulk_get_work_packages(self, ids: Iterable[int]) -> dict[int, dict[str, Any] | None]:
        """Fetch many work packages in parallel; failed reads map to ``None``."""
        ids_list = list(ids)
        if not ids_list:
            return {}

        results: dict[int, dict[str, Any] | None] = {}
        with ThreadPoolExecutor(max_workers=self._parallel_workers) as executor:
            futures = [executor.submit(self._get_work_package_safe, wp_id) for wp_id in ids_list]
            # Drain to surface exceptions; results below are matched by index.
            for _ in as_completed(futures):
                pass
        for idx, wp_id in enumerate(ids_list):
            try:
                results[wp_id] = futures[idx].result()
            except Exception:
                results[wp_id] = None
        return results


class FileBasedBatchWritesDecorator(_BaseOPDecorator):
    """File-based Rails-runner batch writes for OpenProject.

    Wraps batch creates and updates by serializing the payload to a temp JSON
    file and routing it through the wrapped client's existing remote Rails
    execution path (typically SSH+docker; ``OpenProjectClient`` runs Rails
    inside a remote container, not on the local host). The temp file is
    always cleaned up.

    The wrapped client is expected to expose an ``execute_script_with_data``
    method that takes ``(script_template, data_payload)`` — the same contract
    ``OpenProjectClient`` already implements (see
    :meth:`OpenProjectClient.execute_script_with_data`).
    """

    BATCH_CREATE_SCRIPT = "RunnerScripts::BatchCreateWorkPackages.run"
    BULK_UPDATE_SCRIPT = "RunnerScripts::BulkUpdateWorkPackages.run"

    def batch_create_work_packages(self, work_packages: list[dict[str, Any]]) -> dict[str, Any]:
        """Bulk-create work packages through the wrapped client's Rails path."""
        if not work_packages:
            return {"created": [], "errors": [], "stats": {"total": 0, "created": 0, "failed": 0}}
        return self._run_rails_through_wrapped(work_packages, self.BATCH_CREATE_SCRIPT)

    def bulk_update_work_packages(self, updates: list[dict[str, Any]]) -> dict[str, Any]:
        """Bulk-update work packages through the wrapped client's Rails path."""
        if not updates:
            return {"updated": [], "errors": [], "stats": {"total": 0, "updated": 0, "failed": 0}}
        return self._run_rails_through_wrapped(updates, self.BULK_UPDATE_SCRIPT)

    def _run_rails_through_wrapped(
        self,
        payload: list[dict[str, Any]],
        script: str,
    ) -> dict[str, Any]:
        """Delegate execution to the wrapped client's remote Rails path.

        Falls back to a local-subprocess + temp-file invocation if the
        wrapped client does not expose ``execute_script_with_data`` (e.g.,
        a fixture that intentionally bypasses the SSH/docker plumbing).
        The local fallback preserves test compatibility but is documented
        as inappropriate for production — production deployments should
        always wrap a real :class:`OpenProjectClient`.
        """
        wrapped = self.wrapped
        execute_with_data = getattr(wrapped, "execute_script_with_data", None)
        if callable(execute_with_data):
            try:
                result = execute_with_data(script, payload)
            except Exception as exc:
                msg = f"Rails script failed via wrapped client: {exc}"
                raise RailsExecutionError(msg) from exc
            if isinstance(result, dict):
                return result
            try:
                return json.loads(result) if isinstance(result, str) else dict(result)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                msg = f"Failed to coerce Rails output to dict: {exc}"
                raise RailsExecutionError(msg) from exc

        # Test-fixture fallback only — no remote execution available.
        temp_path: pathlib.Path | None = None
        try:
            temp_path = self._write_temp_json(payload)
            return self._exec_rails_local(script, temp_path)
        finally:
            if temp_path is not None and hasattr(temp_path, "unlink"):
                try:
                    temp_path.unlink()
                except Exception as exc:
                    logger.debug("Temp file cleanup failed: %s", exc)

    @staticmethod
    def _write_temp_json(payload: list[dict[str, Any]]) -> pathlib.Path:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as fh:
            fh.write(json.dumps(payload))
            name = fh.name
        return pathlib.Path(name)

    @staticmethod
    def _exec_rails_local(script: str, temp_file: pathlib.Path) -> dict[str, Any]:
        """Local-subprocess Rails invocation — test-fixture fallback only.

        Production ``OpenProjectClient`` runs Rails on a remote SSH+docker
        host; this path exists so test fixtures that wrap a Mock client
        without ``execute_script_with_data`` still exercise the temp-file
        cleanup logic. Real deployments should wrap a full client.
        """
        cmd = ["rails", "runner", script, str(temp_file)]
        try:
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        except Exception as exc:
            msg = f"Rails runner execution failed: {exc}"
            raise RailsExecutionError(msg) from exc

        if proc.returncode != 0:
            stderr = proc.stderr.strip() if proc.stderr else ""
            msg = f"Rails script failed: {stderr}"
            raise RailsExecutionError(msg)

        try:
            return json.loads(proc.stdout)
        except Exception as exc:
            msg = f"Failed to parse Rails output JSON: {exc}"
            raise RailsExecutionError(msg) from exc


class PerformanceMonitoringDecorator(_BaseOPDecorator):
    """Record per-method call counts and elapsed time for the wrapped client."""

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
    "CachingDecorator",
    "FileBasedBatchWritesDecorator",
    "OpenProjectClientLike",
    "ParallelReadsDecorator",
    "PerformanceMonitoringDecorator",
    "RailsExecutionError",
]
