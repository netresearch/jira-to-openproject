# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Worklog durations must round-trip to the original positive integer seconds."""

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


@pytest.mark.parametrize("seconds", [1, 17, 60, 3599])
def test_duration_preserves_seconds(transformer: TimeEntryTransformer, seconds: int) -> None:
    out = transformer.transform_jira_work_log(_worklog(seconds), "TEST-1")
    assert out["hours"] * 3600 == pytest.approx(seconds)


def test_eight_hour_worklog(transformer: TimeEntryTransformer) -> None:
    """Long worklog must NOT be capped or otherwise rewritten."""
    out = transformer.transform_jira_work_log(_worklog(28800), "TEST-1")  # 8h
    assert out["hours"] == pytest.approx(8.0)


@pytest.mark.parametrize("seconds", [0, -1])
def test_nonpositive_duration_is_rejected(transformer: TimeEntryTransformer, seconds: int) -> None:
    worklog = _worklog(seconds)
    with pytest.raises(ValueError, match="positive integer"):
        transformer.transform_jira_work_log(worklog, "TEST-1")
