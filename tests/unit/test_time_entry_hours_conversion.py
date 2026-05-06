"""Time entry hours conversion (Bug B).

The audit of the live OP database showed 10,606 time entries summing to
**106.06 hours total** — every entry stored exactly ``hours = 0.01``,
~100x too small for typical worklogs.

Root cause was a key-name mismatch between the Jira-side extractor and
the transformer:

* ``jira_worklog_service.extract_work_logs`` produces snake_case
  ``time_spent_seconds`` (line 105 in that file).
* ``time_entry_transformer.transform_jira_work_log`` was reading the
  camelCase ``timeSpentSeconds`` (the raw Jira REST shape) — so
  ``work_log.get("timeSpentSeconds", 0)`` always returned ``0``,
  ``hours = 0/3600 = 0.0``, and the existing min-hours clamp then
  floored every entry to ``min_time_entry_hours`` (default 0.01).

The clamp expression itself is unchanged. The fix reads both keys
(snake_case primary, camelCase fallback) so the transformer is robust
to either upstream shape — both paths now produce realistic hours
distributions on real data.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.utils.time_entry_transformer import TimeEntryTransformer


@pytest.fixture
def transformer() -> TimeEntryTransformer:
    return TimeEntryTransformer(
        user_mapping={"tester": 7},
        work_package_mapping={"TEST-1": 42},
        default_activity_id=1,
    )


def _worklog(seconds: int) -> dict[str, Any]:
    """Worklog dict shaped like the real output of
    ``jira_worklog_service.extract_work_logs``: snake_case
    ``time_spent_seconds`` (NOT the camelCase ``timeSpentSeconds`` from
    Jira's raw REST payload). The transformer was reading the wrong key
    in production — silently defaulting to 0 → clamp → 0.01 hours for
    every entry.
    """
    return {
        "id": "wl-1",
        "issue_key": "TEST-1",
        "time_spent_seconds": seconds,
        "started": "2024-01-15T10:00:00.000+0000",
        "comment": "x",
        "author": {"name": "tester", "display_name": "Tester"},
    }


def test_one_hour_worklog_yields_one_hour(transformer: TimeEntryTransformer) -> None:
    out = transformer.transform_jira_work_log(_worklog(3600), "TEST-1")
    assert out["hours"] == pytest.approx(1.0)


def test_thirty_minute_worklog_yields_half_hour(transformer: TimeEntryTransformer) -> None:
    out = transformer.transform_jira_work_log(_worklog(1800), "TEST-1")
    assert out["hours"] == pytest.approx(0.5)


def test_two_hour_thirty_min_worklog(transformer: TimeEntryTransformer) -> None:
    out = transformer.transform_jira_work_log(_worklog(9000), "TEST-1")  # 2h30m
    assert out["hours"] == pytest.approx(2.5)


def test_seventeen_second_worklog_clamps_to_min(transformer: TimeEntryTransformer) -> None:
    """Sub-floor positive worklogs clamp to ``min_time_entry_hours`` (0.01)."""
    out = transformer.transform_jira_work_log(_worklog(17), "TEST-1")  # 17 s = 0.0047 h
    assert out["hours"] == pytest.approx(0.01)


def test_one_minute_worklog_does_not_clamp(transformer: TimeEntryTransformer) -> None:
    """A 1-minute worklog (0.0167 h) must survive the rounding logic — it
    rounds to 0.02, not 0.0, so it's *above* the clamp floor.
    """
    out = transformer.transform_jira_work_log(_worklog(60), "TEST-1")  # 60 s = 0.0167 h
    assert out["hours"] == pytest.approx(0.02)


def test_eight_hour_worklog(transformer: TimeEntryTransformer) -> None:
    """Long worklog must NOT be capped or otherwise rewritten."""
    out = transformer.transform_jira_work_log(_worklog(28800), "TEST-1")  # 8h
    assert out["hours"] == pytest.approx(8.0)


def test_zero_seconds_clamps_to_min(transformer: TimeEntryTransformer) -> None:
    """Zero/negative source clamps to floor (a worklog must store *something*)."""
    out = transformer.transform_jira_work_log(_worklog(0), "TEST-1")
    assert out["hours"] == pytest.approx(0.01)
