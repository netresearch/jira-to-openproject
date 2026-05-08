"""Unit tests for WorkPackageSkeletonMigration component (Phase 1).

Covers:
- Successful save_mapping persists JSON and reports last_save_succeeded=True.
- _save_mapping returns False on IO failure and exposes the failure flag.
- _load_existing_mapping picks up a pre-existing mapping for incremental runs.
- run() post-condition fails loud if save failed despite created skeletons.
- run() reports success when no skeletons were created (no save attempted).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "project": {"PROJ": {"openproject_id": 11}},
                "issue_type": {},
                "issue_type_id": {},
                "status": {},
                "user": {},
                "priority": {},
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

        def set_mapping(self, name: str, value):
            self._m[name] = value

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def _build_mig(tmp_path: Path):
    """Construct WorkPackageSkeletonMigration redirected to tmp_path."""
    from src.application.components.work_package_skeleton_migration import (
        WorkPackageSkeletonMigration,
    )

    jira = MagicMock()
    op = MagicMock()
    mig = WorkPackageSkeletonMigration(jira_client=jira, op_client=op)
    mig.data_dir = tmp_path
    mig.work_package_mapping_file = tmp_path / mig.WORK_PACKAGE_MAPPING_FILE
    return mig


def test_save_mapping_persists_to_disk(tmp_path: Path, _mock_mappings: None) -> None:
    """_save_mapping writes the in-memory mapping to disk as JSON."""
    mig = _build_mig(tmp_path)
    mig.work_package_mapping = {
        "1001": {"jira_key": "PROJ-1", "openproject_id": 5001, "project_key": "PROJ"},
    }

    assert mig._save_mapping() is True
    assert mig._last_save_succeeded is True

    on_disk = json.loads(mig.work_package_mapping_file.read_text())
    assert on_disk == mig.work_package_mapping


def test_save_mapping_records_failure_when_write_fails(
    tmp_path: Path,
    _mock_mappings: None,
) -> None:
    """A failing open() must flip _last_save_succeeded to False."""
    mig = _build_mig(tmp_path)
    # Point the mapping file at a non-existent directory to force a write failure
    # without touching the production file system.
    mig.work_package_mapping_file = tmp_path / "no" / "such" / "dir" / "wp_mapping.json"
    mig.work_package_mapping = {"1001": {"jira_key": "PROJ-1", "openproject_id": 5001}}

    assert mig._save_mapping() is False
    assert mig._last_save_succeeded is False
    assert not mig.work_package_mapping_file.exists()


def test_load_existing_mapping_for_incremental_run(
    tmp_path: Path,
    _mock_mappings: None,
) -> None:
    """A pre-existing mapping file is loaded by the constructor for incremental runs."""
    from src.application.components.work_package_skeleton_migration import (
        WorkPackageSkeletonMigration,
    )

    seeded = {"1001": {"jira_key": "PROJ-1", "openproject_id": 5001}}
    # Need to seed the file at the production data_dir location for the
    # constructor to find it; do this by patching get_path before construction.
    import src.config as cfg

    monkeypatch_data_dir = tmp_path / "var_data"
    monkeypatch_data_dir.mkdir()
    (monkeypatch_data_dir / "work_package_mapping.json").write_text(json.dumps(seeded))

    original_get_path = cfg.get_path
    cfg.get_path = lambda key: monkeypatch_data_dir if key == "data" else original_get_path(key)
    try:
        mig = WorkPackageSkeletonMigration(jira_client=MagicMock(), op_client=MagicMock())
    finally:
        cfg.get_path = original_get_path

    assert mig.work_package_mapping == seeded


def test_run_fails_loud_when_save_failed_despite_creations(
    tmp_path: Path,
    _mock_mappings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If _save_mapping recorded a failure but skeletons were created, run() returns failure."""
    mig = _build_mig(tmp_path)

    # Simulate a successful skeleton creation pipeline that nonetheless ended
    # with a failed save.
    mig._last_save_succeeded = False
    monkeypatch.setattr(
        mig,
        "_migrate_skeletons",
        lambda: {
            "total_processed": 1,
            "total_created": 1,
            "total_skipped": 0,
            "total_failed": 0,
            "projects": {"PROJ": {}},
        },
    )

    result = mig.run()

    assert result.success is False
    assert result.error == "work_package_mapping_save_failed"


def test_run_succeeds_when_no_skeletons_were_created(
    tmp_path: Path,
    _mock_mappings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No created skeletons → no save expected; run() must still succeed."""
    mig = _build_mig(tmp_path)

    # Migration ran but produced zero work packages (e.g. all already migrated).
    monkeypatch.setattr(
        mig,
        "_migrate_skeletons",
        lambda: {
            "total_processed": 5,
            "total_created": 0,
            "total_skipped": 5,
            "total_failed": 0,
            "projects": {"PROJ": {"processed": 5, "created": 0, "skipped": 5, "failed": 0}},
        },
    )

    result = mig.run()

    assert result.success is True
    assert result.details["total_created"] == 0
    assert result.details["total_skipped"] == 5
