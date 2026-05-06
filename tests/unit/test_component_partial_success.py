"""Partial-success classification for ``_component_has_errors``.

Migrations like ``time_entries`` legitimately produce partial output: in the
NRS run (2026-05-04) 5303 of 7143 entries were migrated and 1840 were
correctly skipped because their author lived in Jira but never made it
into OP (deleted/inactive system accounts). The migration's own
``run()`` returned ``success=True`` to indicate "I did my job", but the
orchestrator's ``_component_has_errors`` flagged the component as failed
purely because ``failed_count > 0``.

The new contract:

* If ``result.success`` is ``False``, ``errors`` non-empty, ``error`` set,
  or ``details.status`` indicates failure, the component is in error
  (existing behaviour).
* Otherwise, count successes and failures. Only mark the component as
  errored if there are failures **and zero successes** — i.e. nothing
  worked. ``failed_count > 0`` alongside ``success_count > 0`` is a
  partial success and is tolerated.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from src.migration import _component_has_errors


def _result(**fields: Any) -> Any:
    """Build a fake ComponentResult-like object exposing only the fields we set."""
    obj = MagicMock(spec=[])
    obj.success = fields.pop("success", True)
    obj.errors = fields.pop("errors", [])
    obj.error = fields.pop("error", None)
    obj.details = fields.pop("details", {})
    obj.data = fields.pop("data", None)
    obj.success_count = fields.pop("success_count", 0)
    obj.failed_count = fields.pop("failed_count", 0)
    obj.total_count = fields.pop("total_count", 0)
    obj.failed = fields.pop("failed", 0)
    obj.failed_types = fields.pop("failed_types", 0)
    obj.failed_issues = fields.pop("failed_issues", 0)
    return obj


def test_none_result_is_error() -> None:
    assert _component_has_errors(None) is True


def test_success_false_is_error() -> None:
    assert _component_has_errors(_result(success=False)) is True


def test_explicit_errors_list_is_error() -> None:
    assert _component_has_errors(_result(errors=["boom"])) is True


def test_explicit_error_field_is_error() -> None:
    assert _component_has_errors(_result(error="boom")) is True


def test_details_status_failed_is_error() -> None:
    assert _component_has_errors(_result(details={"status": "failed"})) is True
    assert _component_has_errors(_result(details={"status": "error"})) is True
    assert _component_has_errors(_result(details={"status": "errors"})) is True


def test_clean_success_is_not_error() -> None:
    """No failures, no errors — clean success."""
    assert _component_has_errors(_result(success=True)) is False


def test_partial_success_is_not_error() -> None:
    """Some succeeded, some failed — partial. Migration considers itself complete.

    This is the time_entries case: 5303 migrated / 1840 failed because of
    missing user mappings. The component's ``run()`` returned success=True;
    we should respect that and report partial.
    """
    result = _result(
        success=True,
        details={"success_count": 5303, "failed_count": 1840, "total_count": 7143},
    )
    assert _component_has_errors(result) is False


def test_all_failed_is_error() -> None:
    """No successes, only failures — real failure."""
    result = _result(
        success=True,
        details={"success_count": 0, "failed_count": 10, "total_count": 10},
    )
    assert _component_has_errors(result) is True


def test_partial_success_via_model_fields() -> None:
    """Counts can come from the ``ComponentResult`` model fields too."""
    result = _result(
        success=True,
        success_count=586,
        failed_count=1,
        total_count=587,
    )
    assert _component_has_errors(result) is False


def test_legacy_failed_counter_with_no_other_signal_is_error() -> None:
    """If the migration sets ``failed=N`` but reports zero successes,
    that's still an error.
    """
    result = _result(success=True, failed=5)
    # No success counters present; treat as all-failed
    assert _component_has_errors(result) is True


def test_legacy_failed_counter_with_successes_is_partial() -> None:
    """``failed_types`` / ``failed_issues`` legacy counters with successes
    elsewhere are partial successes.
    """
    result = _result(
        success=True,
        success_count=80,
        failed_types=2,
    )
    assert _component_has_errors(result) is False


def test_partial_success_in_details_legacy_failed_key() -> None:
    """Some migrations stash ``failed`` directly in details with ``success_count``."""
    result = _result(
        success=True,
        details={"success_count": 5, "failed": 1},
    )
    assert _component_has_errors(result) is False
