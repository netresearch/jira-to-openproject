"""Unit tests for the composable OpenProject client decorators (ADR-002 phase 8b)."""

from __future__ import annotations

import json
import time
import warnings
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.infrastructure.openproject.decorators import (
    CachingDecorator,
    FileBasedBatchWritesDecorator,
    ParallelReadsDecorator,
    PerformanceMonitoringDecorator,
    RailsExecutionError,
)
from src.infrastructure.openproject.enhanced_openproject_client import EnhancedOpenProjectClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient


def _make_op_stub(**overrides: Any) -> SimpleNamespace:
    """Build a minimal stub for the OpenProjectClient surface decorators rely on."""
    stub = SimpleNamespace(
        parallel_workers=4,
        get_users=MagicMock(return_value=[{"id": 1}]),
        get_projects=MagicMock(return_value=[{"id": 2, "identifier": "p"}]),
        get_priorities=MagicMock(return_value=[{"name": "Normal"}]),
        get_types=MagicMock(return_value=[{"name": "Task"}]),
        get_statuses=MagicMock(return_value=[{"name": "New"}]),
        get_work_package=MagicMock(return_value={"id": 99}),
    )
    for k, v in overrides.items():
        setattr(stub, k, v)
    return stub


class TestGetattrDelegation:
    def test_attribute_falls_through(self) -> None:
        stub = _make_op_stub()
        decorator = CachingDecorator(stub)
        assert decorator.parallel_workers == 4

    def test_callable_proxy(self) -> None:
        stub = _make_op_stub(get_groups=MagicMock(return_value=[]))
        decorator = ParallelReadsDecorator(stub)
        # ParallelReadsDecorator does not own get_groups, so __getattr__ forwards.
        assert decorator.get_groups() == []
        stub.get_groups.assert_called_once_with()

    def test_missing_attribute_raises(self) -> None:
        stub = _make_op_stub()
        decorator = CachingDecorator(stub)
        with pytest.raises(AttributeError):
            decorator.nonexistent


class TestCachingDecorator:
    def test_priorities_cached(self) -> None:
        stub = _make_op_stub()
        decorator = CachingDecorator(stub, cache_ttl=60)

        decorator.get_priorities_cached()
        decorator.get_priorities_cached()

        stub.get_priorities.assert_called_once()

    def test_types_cached(self) -> None:
        stub = _make_op_stub()
        decorator = CachingDecorator(stub, cache_ttl=60)

        decorator.get_types_cached()
        decorator.get_types_cached()

        stub.get_types.assert_called_once()

    def test_invalidate_specific_key(self) -> None:
        stub = _make_op_stub()
        decorator = CachingDecorator(stub, cache_ttl=60)

        decorator.get_priorities_cached()
        decorator.invalidate("priorities")
        decorator.get_priorities_cached()

        assert stub.get_priorities.call_count == 2

    def test_listed_method_is_cached_via_getattr(self) -> None:
        stub = _make_op_stub()
        decorator = CachingDecorator(stub, cache_ttl=60)

        decorator.get_users()
        decorator.get_users()

        stub.get_users.assert_called_once()

    def test_ttl_expiry(self) -> None:
        stub = _make_op_stub()
        decorator = CachingDecorator(stub, cache_ttl=0.01)

        decorator.get_priorities_cached()
        time.sleep(0.05)
        decorator.get_priorities_cached()

        assert stub.get_priorities.call_count == 2


class TestParallelReadsDecorator:
    def test_bulk_get_work_packages_returns_dict_in_input_order(self) -> None:
        stub = _make_op_stub()
        decorator = ParallelReadsDecorator(stub, parallel_workers=2)

        with patch.object(decorator, "_get_work_package_safe", side_effect=lambda i: {"id": i}):
            result = decorator.bulk_get_work_packages([3, 1, 2])

        assert result == {3: {"id": 3}, 1: {"id": 1}, 2: {"id": 2}}

    def test_bulk_get_work_packages_handles_failures(self) -> None:
        stub = _make_op_stub()
        decorator = ParallelReadsDecorator(stub)

        def _maybe(i: int) -> dict[str, int] | None:
            if i == 2:
                msg = "fail"
                raise RuntimeError(msg)
            return {"id": i}

        with patch.object(decorator, "_get_work_package_safe", side_effect=_maybe):
            result = decorator.bulk_get_work_packages([1, 2, 3])

        assert result == {1: {"id": 1}, 2: None, 3: {"id": 3}}

    def test_bulk_get_work_packages_empty(self) -> None:
        stub = _make_op_stub()
        decorator = ParallelReadsDecorator(stub)
        assert decorator.bulk_get_work_packages([]) == {}

    def test_parallel_workers_override(self) -> None:
        stub = _make_op_stub()
        decorator = ParallelReadsDecorator(stub, parallel_workers=12)
        assert decorator.parallel_workers == 12

    def test_safe_fetch_returns_none_on_error(self) -> None:
        stub = _make_op_stub()
        decorator = ParallelReadsDecorator(stub)

        fake_session = MagicMock()
        fake_session.get.side_effect = RuntimeError("network down")
        object.__setattr__(decorator, "_session", fake_session)

        assert decorator._get_work_package_safe(42) is None

    def test_safe_fetch_uses_wrapped_execute_json_query(self) -> None:
        # ``OpenProjectClient`` is Rails-runner-based (no HTTP base url);
        # ``_get_work_package_safe`` must route through the wrapped client's
        # documented Rails query API rather than a relative-URL HTTP GET.
        stub = _make_op_stub(
            execute_json_query=MagicMock(return_value={"id": 42, "subject": "x"}),
        )
        decorator = ParallelReadsDecorator(stub)

        result = decorator._get_work_package_safe(42)

        assert result == {"id": 42, "subject": "x"}
        stub.execute_json_query.assert_called_once()
        # Sanity: the script targets the requested id.
        script = stub.execute_json_query.call_args.args[0]
        assert "WorkPackage" in script
        assert "42" in script

    def test_safe_fetch_returns_none_when_wrapped_query_fails(self) -> None:
        stub = _make_op_stub(
            execute_json_query=MagicMock(side_effect=RuntimeError("rails down")),
        )
        decorator = ParallelReadsDecorator(stub)
        assert decorator._get_work_package_safe(42) is None


class TestFileBasedBatchWritesDecorator:
    def test_batch_create_empty_input_short_circuits(self) -> None:
        stub = _make_op_stub()
        decorator = FileBasedBatchWritesDecorator(stub)
        result = decorator.batch_create_work_packages([])
        assert result["stats"] == {"total": 0, "created": 0, "failed": 0}

    def test_bulk_update_empty_input_short_circuits(self) -> None:
        stub = _make_op_stub()
        decorator = FileBasedBatchWritesDecorator(stub)
        result = decorator.bulk_update_work_packages([])
        assert result["stats"] == {"total": 0, "updated": 0, "failed": 0}

    def test_batch_create_invokes_rails_runner(self) -> None:
        stub = _make_op_stub()
        decorator = FileBasedBatchWritesDecorator(stub)

        fake_proc = SimpleNamespace(returncode=0, stdout=json.dumps({"created": [1, 2]}), stderr="")

        with patch("src.infrastructure.openproject.decorators.subprocess.run", return_value=fake_proc) as run_mock:
            result = decorator.batch_create_work_packages([{"subject": "x"}])

        assert result == {"created": [1, 2]}
        run_mock.assert_called_once()
        cmd = run_mock.call_args.args[0]
        assert cmd[0] == "rails"
        assert cmd[1] == "runner"
        assert cmd[2] == FileBasedBatchWritesDecorator.BATCH_CREATE_SCRIPT

    def test_bulk_update_invokes_rails_runner(self) -> None:
        stub = _make_op_stub()
        decorator = FileBasedBatchWritesDecorator(stub)

        fake_proc = SimpleNamespace(returncode=0, stdout=json.dumps({"updated": [9]}), stderr="")
        with patch("src.infrastructure.openproject.decorators.subprocess.run", return_value=fake_proc) as run_mock:
            result = decorator.bulk_update_work_packages([{"id": 9, "subject": "y"}])

        assert result == {"updated": [9]}
        cmd = run_mock.call_args.args[0]
        assert cmd[2] == FileBasedBatchWritesDecorator.BULK_UPDATE_SCRIPT

    def test_rails_failure_raises_execution_error(self) -> None:
        stub = _make_op_stub()
        decorator = FileBasedBatchWritesDecorator(stub)
        fake_proc = SimpleNamespace(returncode=1, stdout="", stderr="boom")

        with (
            patch("src.infrastructure.openproject.decorators.subprocess.run", return_value=fake_proc),
            pytest.raises(RailsExecutionError, match="Rails script failed"),
        ):
            decorator.batch_create_work_packages([{"subject": "x"}])

    def test_invalid_json_output_raises(self) -> None:
        stub = _make_op_stub()
        decorator = FileBasedBatchWritesDecorator(stub)
        fake_proc = SimpleNamespace(returncode=0, stdout="not json", stderr="")

        with (
            patch("src.infrastructure.openproject.decorators.subprocess.run", return_value=fake_proc),
            pytest.raises(RailsExecutionError, match="parse Rails output"),
        ):
            decorator.batch_create_work_packages([{"subject": "x"}])

    def test_temp_file_is_cleaned_up_on_success(self) -> None:
        stub = _make_op_stub()
        decorator = FileBasedBatchWritesDecorator(stub)

        fake_path = MagicMock()
        fake_proc = SimpleNamespace(returncode=0, stdout=json.dumps({"created": []}), stderr="")
        with (
            patch.object(FileBasedBatchWritesDecorator, "_write_temp_json", return_value=fake_path),
            patch("src.infrastructure.openproject.decorators.subprocess.run", return_value=fake_proc),
        ):
            decorator.batch_create_work_packages([{"subject": "x"}])

        fake_path.unlink.assert_called_once()

    def test_temp_file_cleanup_runs_even_on_failure(self) -> None:
        stub = _make_op_stub()
        decorator = FileBasedBatchWritesDecorator(stub)

        fake_path = MagicMock()
        fake_proc = SimpleNamespace(returncode=1, stdout="", stderr="bad")
        with (
            patch.object(FileBasedBatchWritesDecorator, "_write_temp_json", return_value=fake_path),
            patch("src.infrastructure.openproject.decorators.subprocess.run", return_value=fake_proc),
            pytest.raises(RailsExecutionError),
        ):
            decorator.batch_create_work_packages([{"subject": "x"}])

        fake_path.unlink.assert_called_once()

    def test_uses_wrapped_execute_script_with_data_when_available(self) -> None:
        # Wrapped client exposes the remote-Rails entrypoint — the decorator
        # must delegate to it instead of falling through to local subprocess.
        # The real OpenProjectRailsRunnerService.execute_script_with_data
        # returns a {status, message, data, output} envelope; the decorator
        # unwraps ``data`` on success.
        stub = _make_op_stub(
            execute_script_with_data=MagicMock(
                return_value={
                    "status": "success",
                    "message": "ok",
                    "data": {"created": [42]},
                    "output": "",
                },
            ),
        )
        decorator = FileBasedBatchWritesDecorator(stub)

        with patch("src.infrastructure.openproject.decorators.subprocess.run") as run_mock:
            result = decorator.batch_create_work_packages([{"subject": "x"}])

        assert result == {"created": [42]}
        stub.execute_script_with_data.assert_called_once_with(
            FileBasedBatchWritesDecorator.BATCH_CREATE_SCRIPT,
            [{"subject": "x"}],
        )
        run_mock.assert_not_called()

    def test_envelope_with_error_status_raises(self) -> None:
        # status != "success" must surface as RailsExecutionError, not
        # silently pass through as the legacy "{created: …}" shape.
        stub = _make_op_stub(
            execute_script_with_data=MagicMock(
                return_value={
                    "status": "error",
                    "message": "Type 'invalid' not found",
                    "data": None,
                    "output": "",
                },
            ),
        )
        decorator = FileBasedBatchWritesDecorator(stub)

        with pytest.raises(RailsExecutionError, match="status='error'"):
            decorator.batch_create_work_packages([{"subject": "x"}])

    def test_envelope_with_list_data_aggregates_to_dict(self) -> None:
        # Some scripts return a list under data[]; the unwrapper must
        # coerce to the dict-shape contract callers expect.
        stub = _make_op_stub(
            execute_script_with_data=MagicMock(
                return_value={
                    "status": "success",
                    "message": "ok",
                    "data": [{"id": 1}, {"id": 2}],
                    "output": "",
                },
            ),
        )
        decorator = FileBasedBatchWritesDecorator(stub)

        result = decorator.batch_create_work_packages([{"subject": "x"}])

        assert result["created"] == [{"id": 1}, {"id": 2}]
        assert result["stats"] == {"total": 2, "created": 2, "failed": 0}


class TestPerformanceMonitoringDecorator:
    def test_metrics_track_call_count(self) -> None:
        stub = _make_op_stub()
        decorator = PerformanceMonitoringDecorator(stub)

        decorator.get_users()
        decorator.get_projects()
        decorator.get_users()

        m = decorator.metrics
        assert m["get_users"]["calls"] == 2
        assert m["get_projects"]["calls"] == 1

    def test_reset_metrics(self) -> None:
        stub = _make_op_stub()
        decorator = PerformanceMonitoringDecorator(stub)
        decorator.get_users()
        decorator.reset_metrics()
        assert decorator.metrics == {}

    def test_metrics_record_failed_calls(self) -> None:
        stub = _make_op_stub(get_users=MagicMock(side_effect=RuntimeError("x")))
        decorator = PerformanceMonitoringDecorator(stub)

        with pytest.raises(RuntimeError):
            decorator.get_users()

        assert decorator.metrics["get_users"]["calls"] == 1


class TestComposition:
    def test_outer_decorator_runs_first(self) -> None:
        stub = _make_op_stub()
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

    def test_caching_under_monitoring(self) -> None:
        stub = _make_op_stub()
        cached = CachingDecorator(stub, cache_ttl=60)
        monitored = PerformanceMonitoringDecorator(cached)

        monitored.get_users()
        monitored.get_users()

        assert monitored.metrics["get_users"]["calls"] == 2
        stub.get_users.assert_called_once()


class TestEnhancedOpenProjectClientDeprecation:
    """Legacy subclass should emit a DeprecationWarning on instantiation."""

    def test_instantiation_emits_deprecation_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Stub the heavy base __init__ so we exercise only the warning path.
        monkeypatch.setattr(OpenProjectClient, "__init__", lambda self, **_: None)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            EnhancedOpenProjectClient()

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecations, "Expected at least one DeprecationWarning"
        assert any("decorators" in str(w.message) for w in deprecations)
