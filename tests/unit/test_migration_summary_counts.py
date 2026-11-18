# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

from src.migration import _extract_counts
from src.models.component_results import ComponentResult


def test_extract_counts_from_details() -> None:
    result = ComponentResult(
        success=True,
        details={
            "success_count": 3,
            "failed_count": 1,
            "total_count": 4,
        },
    )
    sc, fc, tc = _extract_counts(result)
    assert (sc, fc, tc) == (3, 1, 4)


def test_extract_counts_from_model_fields() -> None:
    result = ComponentResult(
        success=True,
        success_count=2,
        failed_count=1,
        total_count=3,
    )
    sc, fc, tc = _extract_counts(result)
    assert (sc, fc, tc) == (2, 1, 3)


def test_extract_counts_wp_shape_total_created_and_total_issues() -> None:
    result = ComponentResult(
        success=True,
        details={
            "total_created": 5,
            "total_issues": 6,
        },
    )
    sc, fc, tc = _extract_counts(result)
    assert (sc, fc, tc) == (5, 1, 6)


def test_extract_counts_te_shape_total_time_entries() -> None:
    result = ComponentResult(
        success=True,
        details={
            "total_time_entries": {"migrated": 7, "failed": 2},
        },
    )
    sc, fc, tc = _extract_counts(result)
    assert (sc, fc, tc) == (7, 2, 9)


def test_extract_counts_generic_total_and_error_count() -> None:
    result = ComponentResult(
        success=True,
        details={
            "total": 10,
            "error_count": 3,
        },
    )
    sc, fc, tc = _extract_counts(result)
    assert (sc, fc, tc) == (7, 3, 10)


def test_extract_counts_final_fallback_sum() -> None:
    result = ComponentResult(
        success=True,
        success_count=4,
        failed_count=0,
        total_count=0,  # missing total, should derive
    )
    sc, fc, tc = _extract_counts(result)
    assert (sc, fc, tc) == (4, 0, 4)


def test_change_detection_failure_after_write_is_not_retried_via_legacy_run() -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from src.migration import _run_component

    tracked = Mock(side_effect=AttributeError("missing result attribute after write"))
    legacy = Mock()
    component = SimpleNamespace(run_with_change_detection=tracked, run=legacy)
    result = _run_component("test_component", component)
    assert result.success is False
    assert result.errors == ["missing result attribute after write"]
    tracked.assert_called_once()
    legacy.assert_not_called()
