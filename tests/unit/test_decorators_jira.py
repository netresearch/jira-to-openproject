"""Unit tests for the composable Jira client decorators (ADR-002 phase 8b)."""

from __future__ import annotations

import time
import warnings
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.infrastructure.jira.decorators import (
    BatchOperationsDecorator,
    CachingDecorator,
    PerformanceMonitoringDecorator,
    StreamingDecorator,
)
from src.infrastructure.jira.enhanced_jira_client import EnhancedJiraClient
from src.infrastructure.jira.jira_client import JiraClient


def _make_jira_stub(**overrides: Any) -> SimpleNamespace:
    """Build a minimal stub that mimics the JiraClient surface decorators rely on."""
    stub = SimpleNamespace(
        jira=MagicMock(),
        base_url="https://jira.example/",
        batch_size=50,
        parallel_workers=4,
        get_users=MagicMock(return_value=[{"id": 1}]),
        get_projects=MagicMock(return_value=[{"key": "P"}]),
        get_priorities=MagicMock(return_value=[{"name": "High"}]),
        get_all_statuses=MagicMock(return_value=[{"name": "Open"}]),
        get_status_categories=MagicMock(return_value=[{"key": "new"}]),
        get_issue_types=MagicMock(return_value=[{"name": "Bug"}]),
        get_custom_fields=MagicMock(return_value=[{"id": "customfield_1"}]),
        get_issue_details=MagicMock(side_effect=lambda key: {"key": key, "summary": f"S-{key}"}),
        get_work_logs_for_issue=MagicMock(side_effect=lambda key: [{"id": f"{key}-log"}]),
        get_project_details=MagicMock(side_effect=lambda key: {"key": key}),
    )
    for k, v in overrides.items():
        setattr(stub, k, v)
    return stub


class TestGetattrDelegation:
    """Unhandled attribute access should fall through to the wrapped client."""

    def test_arbitrary_attribute_resolves_to_wrapped(self) -> None:
        stub = _make_jira_stub()
        decorator = CachingDecorator(stub)
        assert decorator.base_url == "https://jira.example/"

    def test_unrelated_method_call_is_proxied(self) -> None:
        stub = _make_jira_stub(get_groups=MagicMock(return_value=[{"name": "g"}]))
        decorator = StreamingDecorator(stub)
        # StreamingDecorator does not own get_groups, so __getattr__ should forward.
        result = decorator.get_groups()
        assert result == [{"name": "g"}]
        stub.get_groups.assert_called_once_with()

    def test_missing_attribute_raises_attributeerror(self) -> None:
        stub = _make_jira_stub()
        decorator = CachingDecorator(stub)
        with pytest.raises(AttributeError):
            decorator.this_method_does_not_exist


class TestCachingDecorator:
    def test_cached_method_called_once_within_ttl(self) -> None:
        stub = _make_jira_stub()
        decorator = CachingDecorator(stub, cache_ttl=60)

        first = decorator.get_users()
        second = decorator.get_users()

        assert first == second == [{"id": 1}]
        stub.get_users.assert_called_once()

    def test_invalidate_forces_refresh(self) -> None:
        stub = _make_jira_stub()
        decorator = CachingDecorator(stub, cache_ttl=60)

        decorator.get_users()
        decorator.invalidate()
        decorator.get_users()

        assert stub.get_users.call_count == 2

    def test_expired_entry_is_refetched(self) -> None:
        stub = _make_jira_stub()
        decorator = CachingDecorator(stub, cache_ttl=0.01)

        decorator.get_users()
        time.sleep(0.05)
        decorator.get_users()

        assert stub.get_users.call_count == 2

    def test_get_statuses_cached_uses_explicit_helper(self) -> None:
        stub = _make_jira_stub()
        decorator = CachingDecorator(stub)

        decorator.get_statuses_cached()
        decorator.get_statuses_cached()

        stub.get_all_statuses.assert_called_once()

    def test_non_cached_method_is_pass_through(self) -> None:
        stub = _make_jira_stub()
        decorator = CachingDecorator(stub)
        # `base_url` isn't a callable; CachingDecorator should hand back the raw attribute.
        assert decorator.base_url == "https://jira.example/"

    def test_get_project_cached_uses_get_project_metadata_enhanced_fallback(self) -> None:
        # Real ``JiraClient`` exposes per-key project metadata via
        # ``get_project_metadata_enhanced``, NOT ``get_projects`` (which takes
        # no args). The decorator must fall back to the per-key method when no
        # ``get_project_details`` shim is available.
        stub = _make_jira_stub(
            get_project_metadata_enhanced=MagicMock(return_value={"key": "PROJ", "id": "10001"}),
        )
        # Strip the optional shim so we exercise the fallback path.
        if hasattr(stub, "get_project_details"):
            delattr(stub, "get_project_details")
        decorator = CachingDecorator(stub)

        first = decorator.get_project_cached("PROJ")
        second = decorator.get_project_cached("PROJ")

        assert first == second == {"key": "PROJ", "id": "10001"}
        stub.get_project_metadata_enhanced.assert_called_once_with("PROJ")

    def test_get_project_cached_prefers_get_project_details_shim(self) -> None:
        stub = _make_jira_stub(
            get_project_details=MagicMock(return_value={"key": "PROJ", "shim": True}),
            get_project_metadata_enhanced=MagicMock(),
        )
        decorator = CachingDecorator(stub)

        result = decorator.get_project_cached("PROJ")

        assert result == {"key": "PROJ", "shim": True}
        stub.get_project_details.assert_called_once_with("PROJ")
        stub.get_project_metadata_enhanced.assert_not_called()


class TestBatchOperationsDecorator:
    def test_batch_get_issues_aggregates_across_batches(self) -> None:
        stub = _make_jira_stub()
        # search_issues returns objects that have a .key attribute
        stub.jira.search_issues.side_effect = lambda jql, **_: [
            SimpleNamespace(key=key) for key in jql.split("(", 1)[1].rstrip(")").split(",")
        ]
        decorator = BatchOperationsDecorator(stub, batch_size=2, parallel_workers=2)

        result = decorator.batch_get_issues(["A-1", "A-2", "A-3"])

        assert set(result.keys()) == {"A-1", "A-2", "A-3"}
        assert all(v is not None for v in result.values())

    def test_batch_get_issues_returns_none_for_failed_batch(self) -> None:
        stub = _make_jira_stub()
        stub.jira.search_issues.side_effect = RuntimeError("boom")
        decorator = BatchOperationsDecorator(stub, batch_size=10)

        result = decorator.batch_get_issues(["A-1", "A-2"])

        assert result == {"A-1": None, "A-2": None}

    def test_batch_get_issues_empty_input(self) -> None:
        stub = _make_jira_stub()
        decorator = BatchOperationsDecorator(stub)
        assert decorator.batch_get_issues([]) == {}

    def test_batch_get_work_logs_uses_per_issue_call(self) -> None:
        stub = _make_jira_stub()
        decorator = BatchOperationsDecorator(stub, parallel_workers=2)

        result = decorator.batch_get_work_logs(["X-1", "X-2"])

        assert set(result.keys()) == {"X-1", "X-2"}
        assert result["X-1"] == [{"id": "X-1-log"}]
        assert stub.get_work_logs_for_issue.call_count == 2

    def test_batch_get_work_logs_handles_failures(self) -> None:
        stub = _make_jira_stub(get_work_logs_for_issue=MagicMock(side_effect=RuntimeError("nope")))
        decorator = BatchOperationsDecorator(stub)

        result = decorator.batch_get_work_logs(["A-1"])

        assert result == {"A-1": []}

    def test_bulk_get_issue_metadata_uses_get_issue_details(self) -> None:
        stub = _make_jira_stub()
        decorator = BatchOperationsDecorator(stub, parallel_workers=2)

        result = decorator.bulk_get_issue_metadata(["A-1", "A-2"])

        assert result["A-1"] == {"key": "A-1", "summary": "S-A-1"}
        assert result["A-2"] == {"key": "A-2", "summary": "S-A-2"}

    def test_batch_size_override_is_honored(self) -> None:
        stub = _make_jira_stub()
        decorator = BatchOperationsDecorator(stub, batch_size=7, parallel_workers=3)
        assert decorator.batch_size == 7
        assert decorator.parallel_workers == 3


class TestStreamingDecorator:
    def test_stream_search_issues_iterates_pages(self) -> None:
        stub = _make_jira_stub()
        # Two non-empty pages followed by an empty one; default page_size=50
        pages = [
            [SimpleNamespace(key=f"P-{i}") for i in range(50)],
            [SimpleNamespace(key=f"P-{i}") for i in range(50, 75)],
            [],
        ]
        stub.jira.search_issues.side_effect = pages
        decorator = StreamingDecorator(stub)

        results = list(decorator.stream_search_issues("project=X"))

        assert len(results) == 75
        assert stub.jira.search_issues.call_count == 3

    def test_stream_search_respects_max_pages(self) -> None:
        stub = _make_jira_stub()
        stub.jira.search_issues.return_value = [SimpleNamespace(key="K-1")]
        decorator = StreamingDecorator(stub)

        results = list(decorator.stream_search_issues("project=X", page_size=1, max_pages=2))

        assert len(results) == 2
        assert stub.jira.search_issues.call_count == 2

    def test_stream_search_breaks_on_exception(self) -> None:
        stub = _make_jira_stub()
        stub.jira.search_issues.side_effect = RuntimeError("boom")
        decorator = StreamingDecorator(stub)

        results = list(decorator.stream_search_issues("project=X"))

        assert results == []

    def test_stream_search_raises_when_jira_unavailable(self) -> None:
        stub = _make_jira_stub(jira=None)
        decorator = StreamingDecorator(stub)

        with pytest.raises(RuntimeError, match="not initialized"):
            next(iter(decorator.stream_search_issues("project=X")))


class TestPerformanceMonitoringDecorator:
    def test_metrics_track_call_count(self) -> None:
        stub = _make_jira_stub()
        decorator = PerformanceMonitoringDecorator(stub)

        decorator.get_users()
        decorator.get_users()
        decorator.get_projects()

        metrics = decorator.metrics
        assert metrics["get_users"]["calls"] == 2
        assert metrics["get_projects"]["calls"] == 1
        assert metrics["get_users"]["total_seconds"] >= 0.0

    def test_reset_metrics_clears_state(self) -> None:
        stub = _make_jira_stub()
        decorator = PerformanceMonitoringDecorator(stub)

        decorator.get_users()
        decorator.reset_metrics()

        assert decorator.metrics == {}

    def test_metrics_record_failed_calls(self) -> None:
        stub = _make_jira_stub(get_users=MagicMock(side_effect=RuntimeError("x")))
        decorator = PerformanceMonitoringDecorator(stub)

        with pytest.raises(RuntimeError):
            decorator.get_users()

        assert decorator.metrics["get_users"]["calls"] == 1


class TestComposition:
    def test_outer_decorator_runs_first(self) -> None:
        """``Outer(Inner(client)).method()`` calls Outer's hook before Inner's."""
        stub = _make_jira_stub()
        order: list[str] = []

        class _Tracer:
            def __init__(self, wrapped: Any, label: str) -> None:
                object.__setattr__(self, "_wrapped", wrapped)
                object.__setattr__(self, "_label", label)

            def __getattr__(self, name: str) -> Any:
                attr = getattr(self._wrapped, name)
                if not callable(attr):
                    return attr

                def proxy(*a: Any, **kw: Any) -> Any:
                    order.append(self._label)
                    return attr(*a, **kw)

                return proxy

        composed = _Tracer(_Tracer(stub, "inner"), "outer")
        composed.get_users()

        assert order == ["outer", "inner"]
        stub.get_users.assert_called_once()

    def test_caching_under_monitoring(self) -> None:
        """A cached call still counts as one invocation per monitor entry."""
        stub = _make_jira_stub()
        cached = CachingDecorator(stub, cache_ttl=60)
        monitored = PerformanceMonitoringDecorator(cached)

        monitored.get_users()
        monitored.get_users()

        # Monitor records both calls (it sees the cache decorator's bound method).
        assert monitored.metrics["get_users"]["calls"] == 2
        # But the underlying client only saw one because of caching.
        stub.get_users.assert_called_once()


class TestEnhancedJiraClientDeprecation:
    """Ensure the legacy subclass emits a DeprecationWarning on instantiation."""

    def test_instantiation_emits_deprecation_warning(self) -> None:
        with (
            patch.object(JiraClient, "_connect"),
            patch.object(JiraClient, "_patch_jira_client"),
            warnings.catch_warnings(record=True) as caught,
        ):
            warnings.simplefilter("always")
            EnhancedJiraClient(
                server="https://test.atlassian.net",
                username="u",
                password="p",
            )

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecations, "Expected at least one DeprecationWarning"
        assert any("decorators" in str(w.message) for w in deprecations)
