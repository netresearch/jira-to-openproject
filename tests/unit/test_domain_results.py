"""Unit tests for the discriminated-union step result types (phase 8a).

Covers:
* construction and discriminator wiring of Success / Skipped / Failed
* Pydantic v2 round-trips (model_dump / model_validate)
* JSON round-trips (model_dump_json / model_validate_json)
* legacy bridge — from_component_result mapping table
* legacy bridge — to_component_result inverse round-trip
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter

from src.domain.results import (
    Failed,
    Skipped,
    StepResult,
    Success,
    from_component_result,
    to_component_result,
)
from src.models.component_results import ComponentResult

# A Pydantic TypeAdapter is the v2-idiomatic way to validate a bare
# discriminated union (the union itself is not a BaseModel).
_StepResultAdapter: TypeAdapter[StepResult] = TypeAdapter(StepResult)


# ── Construction ──────────────────────────────────────────────────────────


@pytest.mark.unit
class TestVariantConstruction:
    """Each variant should set its discriminator literal automatically."""

    def test_success_default_kind(self):
        result = Success()
        assert result.kind == "success"
        assert result.data is None
        assert result.message == ""
        assert result.counts == {}
        assert result.warnings == []

    def test_skipped_default_kind(self):
        result = Skipped()
        assert result.kind == "skipped"
        assert result.reason == ""
        assert result.message == ""

    def test_failed_default_kind(self):
        result = Failed()
        assert result.kind == "failed"
        assert result.error == ""
        assert result.message == ""
        assert result.errors == []
        assert result.counts == {}

    def test_success_with_payload(self):
        result = Success(
            data={"created": [1, 2, 3]},
            message="ok",
            counts={"created": 3},
            warnings=["minor"],
        )
        assert result.data == {"created": [1, 2, 3]}
        assert result.message == "ok"
        assert result.counts == {"created": 3}
        assert result.warnings == ["minor"]

    def test_skipped_with_reason(self):
        result = Skipped(reason="no_work", message="nothing to migrate")
        assert result.reason == "no_work"
        assert result.message == "nothing to migrate"

    def test_failed_with_errors(self):
        result = Failed(
            error="boom",
            errors=["boom", "second"],
            counts={"failed": 2},
        )
        assert result.error == "boom"
        assert result.errors == ["boom", "second"]
        assert result.counts == {"failed": 2}


# ── Discriminator dispatch ────────────────────────────────────────────────


@pytest.mark.unit
class TestDiscriminatorDispatch:
    """Pydantic should pick the concrete variant from the ``kind`` field."""

    def test_dict_kind_success(self):
        result = _StepResultAdapter.validate_python({"kind": "success", "message": "ok"})
        assert isinstance(result, Success)
        assert result.message == "ok"

    def test_dict_kind_skipped(self):
        result = _StepResultAdapter.validate_python({"kind": "skipped", "reason": "no_work"})
        assert isinstance(result, Skipped)
        assert result.reason == "no_work"

    def test_dict_kind_failed(self):
        result = _StepResultAdapter.validate_python({"kind": "failed", "error": "boom", "errors": ["boom"]})
        assert isinstance(result, Failed)
        assert result.error == "boom"
        assert result.errors == ["boom"]

    def test_extra_fields_ignored(self):
        # ``extra="ignore"`` on each variant means unknown fields don't
        # fail validation — they're just dropped.
        result = _StepResultAdapter.validate_python({"kind": "success", "unexpected": 42})
        assert isinstance(result, Success)
        assert not hasattr(result, "unexpected")


# ── Round-trip via Pydantic ───────────────────────────────────────────────


@pytest.mark.unit
class TestRoundTrip:
    """model_dump / model_validate and JSON variants should be lossless."""

    def test_success_python_round_trip(self):
        original = Success(
            data=[1, 2, 3],
            message="hello",
            counts={"created": 3},
            warnings=["w"],
        )
        clone = _StepResultAdapter.validate_python(original.model_dump())
        assert isinstance(clone, Success)
        assert clone == original

    def test_skipped_python_round_trip(self):
        original = Skipped(reason="no_work", message="nothing")
        clone = _StepResultAdapter.validate_python(original.model_dump())
        assert isinstance(clone, Skipped)
        assert clone == original

    def test_failed_python_round_trip(self):
        original = Failed(
            error="boom",
            errors=["boom", "second"],
            counts={"failed": 2},
            message="went wrong",
        )
        clone = _StepResultAdapter.validate_python(original.model_dump())
        assert isinstance(clone, Failed)
        assert clone == original

    def test_success_json_round_trip(self):
        original = Success(
            data={"k": "v"},
            message="ok",
            counts={"created": 1},
        )
        json_blob = original.model_dump_json()
        clone = _StepResultAdapter.validate_json(json_blob)
        assert isinstance(clone, Success)
        assert clone == original

    def test_skipped_json_round_trip(self):
        original = Skipped(reason="feature_disabled", message="off")
        clone = _StepResultAdapter.validate_json(original.model_dump_json())
        assert isinstance(clone, Skipped)
        assert clone == original

    def test_failed_json_round_trip(self):
        original = Failed(error="boom", errors=["boom"])
        clone = _StepResultAdapter.validate_json(original.model_dump_json())
        assert isinstance(clone, Failed)
        assert clone == original


# ── Legacy bridge: from_component_result ──────────────────────────────────


@pytest.mark.unit
class TestFromComponentResult:
    """Mapping table from the legacy envelope to the discriminated union."""

    def test_success_no_errors_no_warnings(self):
        legacy = ComponentResult(
            success=True,
            message="migrated 5 issues",
            data={"created": [1, 2, 3, 4, 5]},
            created_issues=5,
            total_issues=5,
        )
        result = from_component_result(legacy)
        assert isinstance(result, Success)
        assert result.message == "migrated 5 issues"
        assert result.data == {"created": [1, 2, 3, 4, 5]}
        assert result.counts == {"created_issues": 5, "total_issues": 5}
        assert result.warnings == []

    def test_success_with_warnings_preserved(self):
        legacy = ComponentResult(
            success=True,
            message="migrated with caveats",
            warnings=["non-fatal w1", "non-fatal w2"],
        )
        result = from_component_result(legacy)
        assert isinstance(result, Success)
        assert result.warnings == ["non-fatal w1", "non-fatal w2"]
        assert result.message == "migrated with caveats"

    def test_failure_with_errors(self):
        legacy = ComponentResult(
            success=False,
            message="Migration failed: 2 errors",
            errors=["primary boom", "secondary"],
            failed_issues=2,
        )
        result = from_component_result(legacy)
        assert isinstance(result, Failed)
        assert result.error == "primary boom"
        assert result.errors == ["primary boom", "secondary"]
        assert result.message == "Migration failed: 2 errors"
        assert result.counts == {"failed_issues": 2}

    def test_failure_with_error_field_only(self):
        # ``ComponentResult`` has both an ``error`` (str|None) and an
        # ``errors`` (list) field. Either should trigger the Failed arm.
        legacy = ComponentResult(
            success=False,
            error="single boom",
        )
        result = from_component_result(legacy)
        assert isinstance(result, Failed)
        assert result.error == "single boom"
        assert result.errors == []

    def test_skip_heuristic_no_work_message(self):
        legacy = ComponentResult(
            success=True,
            message="No issues to migrate",
        )
        result = from_component_result(legacy)
        assert isinstance(result, Skipped)
        assert result.reason == "No issues to migrate"
        assert result.message == "No issues to migrate"

    def test_skip_heuristic_other_no_message(self):
        legacy = ComponentResult(
            success=True,
            message="No custom fields found",
        )
        result = from_component_result(legacy)
        assert isinstance(result, Skipped)
        assert result.reason == "No custom fields found"

    def test_success_message_not_starting_with_no_is_success(self):
        # "Normal" success messages should not be mis-classified as Skipped.
        legacy = ComponentResult(
            success=True,
            message="Found 3 records but kept them as-is",
        )
        result = from_component_result(legacy)
        assert isinstance(result, Success)
        assert result.message == "Found 3 records but kept them as-is"

    def test_default_legacy_falls_through_to_success_with_no_error_signal(self):
        # A bare ``ComponentResult()`` has ``success=False`` and no
        # errors — by our rule that lands on the success arm (no
        # signal that anything went wrong). Documenting the edge.
        legacy = ComponentResult()
        result = from_component_result(legacy)
        # No errors, no error string → falls through to Success arm.
        assert isinstance(result, Success)


# ── Legacy bridge: to_component_result ────────────────────────────────────


@pytest.mark.unit
class TestToComponentResult:
    """Inverse mapping back to the legacy envelope."""

    def test_success_to_legacy(self):
        # ``ComponentResult.data`` is ``dict | list[dict] | None``; the
        # discriminated-union ``Success.data`` is intentionally ``Any``,
        # so callers bridging back to the legacy envelope must stay
        # within the legacy schema.
        payload = [{"id": 1}, {"id": 2}, {"id": 3}]
        result = Success(
            data=payload,
            message="ok",
            counts={"created_issues": 3, "total_issues": 3},
            warnings=["w"],
        )
        legacy = to_component_result(result)
        assert legacy.success is True
        assert legacy.message == "ok"
        assert legacy.data == payload
        assert legacy.warnings == ["w"]
        assert legacy.created_issues == 3
        assert legacy.total_issues == 3

    def test_skipped_to_legacy(self):
        result = Skipped(reason="no_work", message="nothing to do")
        legacy = to_component_result(result)
        # Skipped maps to success=True (legacy has no third arm).
        assert legacy.success is True
        assert legacy.message == "nothing to do"

    def test_skipped_without_message_uses_reason(self):
        result = Skipped(reason="No issues to migrate")
        legacy = to_component_result(result)
        assert legacy.success is True
        assert legacy.message == "No issues to migrate"

    def test_failed_to_legacy(self):
        result = Failed(
            error="boom",
            errors=["boom", "second"],
            message="2 failures",
            counts={"failed_issues": 2},
        )
        legacy = to_component_result(result)
        assert legacy.success is False
        assert legacy.error == "boom"
        assert legacy.errors == ["boom", "second"]
        assert legacy.message == "2 failures"
        assert legacy.failed_issues == 2

    def test_failed_with_no_error_string_drops_to_none(self):
        # ``error`` field on ``ComponentResult`` is ``str | None``;
        # an empty Failed.error should map back to None.
        result = Failed(errors=["only"])
        legacy = to_component_result(result)
        assert legacy.success is False
        assert legacy.error is None
        assert legacy.errors == ["only"]

    def test_unknown_count_keys_silently_dropped(self):
        # Counts not matching a ``ComponentResult`` integer field are
        # ignored (legacy has no escape hatch for arbitrary counters).
        result = Success(counts={"completely_made_up_field": 42})
        legacy = to_component_result(result)
        assert not hasattr(legacy, "completely_made_up_field")
        assert legacy.success is True


# ── Bridge round-trips ────────────────────────────────────────────────────


@pytest.mark.unit
class TestBridgeRoundTrip:
    """End-to-end legacy ↔ union ↔ legacy traversal."""

    def test_success_round_trip(self):
        original = ComponentResult(
            success=True,
            message="all good",
            data={"k": "v"},
            warnings=["w"],
            created_issues=3,
        )
        bridged = to_component_result(from_component_result(original))
        assert bridged.success is True
        assert bridged.message == "all good"
        assert bridged.data == {"k": "v"}
        assert bridged.warnings == ["w"]
        assert bridged.created_issues == 3

    def test_failed_round_trip(self):
        original = ComponentResult(
            success=False,
            message="failure",
            errors=["boom"],
            failed_issues=1,
        )
        bridged = to_component_result(from_component_result(original))
        assert bridged.success is False
        assert bridged.message == "failure"
        assert bridged.errors == ["boom"]
        assert bridged.error == "boom"  # promoted from errors[0]
        assert bridged.failed_issues == 1

    def test_skipped_round_trip(self):
        original = ComponentResult(
            success=True,
            message="No issues to migrate",
        )
        bridged = to_component_result(from_component_result(original))
        # Skipped collapses to success=True with reason as message.
        assert bridged.success is True
        assert bridged.message == "No issues to migrate"
