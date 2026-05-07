"""Post-condition guard for ``WorkPackageSkeletonMigration.run``.

Live TEST audit (2026-05-06) caught a 100% attachment loss whose
upstream cause was the WP-skeleton mapping file not getting persisted
— ``_save_mapping`` swallows write errors with a log line, and
``run()`` reported ``success=True`` regardless. Every downstream
migration (attachments, watchers, relations) then silently skipped
with an empty mapping.

These tests pin the post-condition added in PR #197: when
``total_created > 0`` but the mapping file is missing or empty,
``run()`` returns ``success=False`` with a stable error tag.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.application.components.work_package_skeleton_migration import (
    WorkPackageSkeletonMigration,
)

pytestmark = pytest.mark.unit


def _logger_stub() -> SimpleNamespace:
    return SimpleNamespace(
        info=lambda *a, **kw: None,
        warning=lambda *a, **kw: None,
        debug=lambda *a, **kw: None,
        error=lambda *a, **kw: None,
        exception=lambda *a, **kw: None,
        success=lambda *a, **kw: None,
        notice=lambda *a, **kw: None,
    )


def _make_migration(tmp_path: Path) -> WorkPackageSkeletonMigration:
    """Bypass ``__init__`` so we don't need a real Jira/OP client."""
    instance = WorkPackageSkeletonMigration.__new__(WorkPackageSkeletonMigration)
    instance.logger = _logger_stub()
    instance.work_package_mapping = {}
    instance.work_package_mapping_file = tmp_path / WorkPackageSkeletonMigration.WORK_PACKAGE_MAPPING_FILE
    # Default: no save attempted. Tests that exercise the "save
    # failed" path explicitly set this to ``False``; tests that
    # exercise the "save succeeded" path set it to ``True``. Tests
    # that don't touch the save path keep the default to assert
    # the post-condition tolerates ``None`` correctly.
    instance._last_save_succeeded = None
    return instance


def test_run_fails_loud_when_mapping_file_missing(tmp_path: Path) -> None:
    """``total_created > 0`` + missing mapping file → ``success=False``.

    Without this guard, ``_save_mapping``'s swallowed write error
    propagates as ``success=True`` to the orchestrator. Downstream
    components then see an empty WP map and skip everything —
    exactly the 100% attachment loss class caught on TEST.
    """
    instance = _make_migration(tmp_path)
    # Simulate "skeletons were created but the save silently failed".
    instance._migrate_skeletons = MagicMock(
        return_value={"total_created": 5, "total_skipped": 0, "total_failed": 0},
    )

    result = instance.run()

    assert result.success is False, result
    assert result.error == "missing_work_package_mapping_file", result
    # Stable error tag also surfaced in the ``errors`` list so machine
    # consumers that match on the list (every other component in the
    # codebase) see the same tag.
    assert "missing_work_package_mapping_file" in (result.errors or []), result
    assert "downstream" in (result.message or "").lower(), result.message


def test_run_fails_loud_when_mapping_file_empty(tmp_path: Path) -> None:
    """``total_created > 0`` + zero-byte mapping file → ``success=False``."""
    instance = _make_migration(tmp_path)
    # Empty / ``{}`` file — same downstream effect as missing.
    instance.work_package_mapping_file.write_text("{}")
    # Save "succeeded" — the file IS on disk, just empty. The
    # post-condition must catch the empty content even when the
    # save flag is healthy.
    instance._last_save_succeeded = True
    instance._migrate_skeletons = MagicMock(
        return_value={"total_created": 5, "total_skipped": 0, "total_failed": 0},
    )

    result = instance.run()

    assert result.success is False, result
    assert result.error == "empty_work_package_mapping_file", result
    assert "empty_work_package_mapping_file" in (result.errors or []), result


def test_run_fails_loud_when_save_raised_with_stale_file_present(tmp_path: Path) -> None:
    """Stale-file false-negative: an older mapping file exists from a
    previous successful run, but the *current* ``_save_mapping`` call
    raised (e.g. permissions / disk full). ``exists()`` alone passes,
    but the file content is stale relative to the current run's
    skeletons. The new ``_last_save_succeeded`` guard catches this.

    Closes review thread on PR #197.
    """
    instance = _make_migration(tmp_path)
    # Stale file from an "earlier run" — well-formed and non-empty.
    instance.work_package_mapping_file.write_text(
        json.dumps({"99999": {"jira_key": "OLD-1", "openproject_id": 1}}),
    )
    # Current run: skeletons created, but save raised.
    instance._last_save_succeeded = False
    instance._migrate_skeletons = MagicMock(
        return_value={"total_created": 5, "total_skipped": 0, "total_failed": 0},
    )

    result = instance.run()

    assert result.success is False, result
    assert result.error == "work_package_mapping_save_failed", result
    assert "work_package_mapping_save_failed" in (result.errors or []), result


def test_run_fails_loud_when_mapping_file_corrupt(tmp_path: Path) -> None:
    """Corrupt JSON (mid-write crash) → ``success=False``.

    The previous ``stat().st_size <= 2`` heuristic only caught
    zero-byte and ``{}`` files. A partially-written JSON has
    non-trivial size but ``json.load`` raises — operators would
    see ``success=True`` followed by a hard crash on the next
    component's ``json.load``.
    """
    instance = _make_migration(tmp_path)
    # Truncated mid-write — looks like JSON started but got cut off.
    instance.work_package_mapping_file.write_text('{"10001": {"jira_key": "P-')
    instance._last_save_succeeded = True
    instance._migrate_skeletons = MagicMock(
        return_value={"total_created": 5, "total_skipped": 0, "total_failed": 0},
    )

    result = instance.run()

    assert result.success is False, result
    assert result.error == "corrupt_work_package_mapping_file", result
    assert "corrupt_work_package_mapping_file" in (result.errors or []), result


def test_run_passes_when_mapping_file_has_content(tmp_path: Path) -> None:
    """Healthy state: file exists, non-empty → ``success=True``."""
    instance = _make_migration(tmp_path)
    # Realistic mapping shape (str(jira_id) → entry dict).
    instance.work_package_mapping_file.write_text(
        json.dumps({"10001": {"jira_key": "PROJ-1", "openproject_id": 42}}),
    )
    instance._last_save_succeeded = True
    instance._migrate_skeletons = MagicMock(
        return_value={"total_created": 1, "total_skipped": 0, "total_failed": 0},
    )

    result = instance.run()

    assert result.success is True, result
    assert result.details["total_created"] == 1


def test_run_passes_when_zero_skeletons_created(tmp_path: Path) -> None:
    """``total_created == 0`` → no post-condition check fires.

    Legitimate state when nothing changed since the last run (re-run
    on a fully-migrated project). Mapping file may or may not exist;
    either way, success.
    """
    instance = _make_migration(tmp_path)
    # Mapping file deliberately not created.
    instance._migrate_skeletons = MagicMock(
        return_value={"total_created": 0, "total_skipped": 0, "total_failed": 0},
    )

    result = instance.run()

    assert result.success is True, result
