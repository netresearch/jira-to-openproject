#!/usr/bin/env python3
"""Regression test for the work-package mapping facade hand-off (issue #260).

GitHub issue #260: after ``work_packages_skeleton`` created ~11k work
packages, every *facade* consumer (attachments, attachment_provenance,
attachment_recovery, wp_metadata_backfill, watchers) failed with
``missing_work_package_mapping`` — while ``work_packages_content`` and
``time_entries`` (which read the JSON file directly) loaded all entries.

Root cause: a dual source of truth. ``_save_mapping`` writes
``work_package_mapping.json`` directly to disk and never publishes through
``config.mappings``. The shared ``config.mappings`` facade is a process-wide
singleton whose per-stem cache is seeded EMPTY at startup
(``migration.py`` calls ``get_all_mappings()`` before any component runs)
and never re-reads disk. So facade consumers kept seeing ``{}``.

Fix: at the end of the run the skeleton publishes the mapping through
``config.mappings.set_mapping("work_package", …)`` once, refreshing the
cache so every facade reader sees the data.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import src.config as cfg
from src.application.components.attachments_migration import compute_wp_lookup_by_jira_key
from src.infrastructure.persistence.mapping_repo import JsonFileMappingRepository
from src.mappings.mappings import Mappings


def _build_skeleton(tmp_path: Path):
    """Construct WorkPackageSkeletonMigration redirected to ``tmp_path``."""
    from src.application.components.work_package_skeleton_migration import (
        WorkPackageSkeletonMigration,
    )

    mig = WorkPackageSkeletonMigration(jira_client=MagicMock(), op_client=MagicMock())
    mig.data_dir = tmp_path
    mig.work_package_mapping_file = tmp_path / mig.WORK_PACKAGE_MAPPING_FILE
    return mig


def test_skeleton_publishes_mapping_to_shared_facade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#260: facade consumers must see the mapping skeleton persisted.

    Uses a real :class:`Mappings` over a real :class:`JsonFileMappingRepository`
    so the production cache-staleness is reproduced exactly.
    """
    facade = Mappings(repo=JsonFileMappingRepository(tmp_path))
    monkeypatch.setattr(cfg, "mappings", facade, raising=False)

    # Reproduce migration.py: get_all_mappings() runs before any component and
    # seeds an EMPTY 'work_package' override (the file does not exist yet).
    facade.get_all_mappings()
    assert facade.get_mapping("work_package") == {}

    mapping = {
        "1001": {"jira_key": "PROJ-1", "openproject_id": 5001, "project_key": "PROJ"},
        "1002": {"jira_key": "PROJ-2", "openproject_id": 5002, "project_key": "PROJ"},
    }
    mig = _build_skeleton(tmp_path)
    mig.work_package_mapping = dict(mapping)

    # A direct disk write — what _save_mapping does per batch — is invisible to
    # the already-seeded facade cache. This is the #260 bug.
    mig._save_mapping()
    assert json.loads(mig.work_package_mapping_file.read_text(encoding="utf-8")) == mapping
    assert facade.get_mapping("work_package") == {}, "disk write alone must not be expected to reach the facade"

    # Finalizing the run publishes through the facade so consumers see it.
    mig._finalize_mapping()

    published = facade.get_mapping("work_package")
    assert published == mapping

    # And the actual attachments consumer helper now resolves the lookup
    # instead of returning {} (which produced missing_work_package_mapping).
    lookup = compute_wp_lookup_by_jira_key(facade)
    assert lookup == {"PROJ-1": 5001, "PROJ-2": 5002}


def test_finalize_mapping_preserves_save_success_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_finalize_mapping`` must still set ``_last_save_succeeded`` (run() guard)."""
    facade = Mappings(repo=JsonFileMappingRepository(tmp_path))
    monkeypatch.setattr(cfg, "mappings", facade, raising=False)

    mig = _build_skeleton(tmp_path)
    mig.work_package_mapping = {"1001": {"jira_key": "PROJ-1", "openproject_id": 5001}}

    mig._finalize_mapping()

    assert mig._last_save_succeeded is True
    assert mig.work_package_mapping_file.exists()


def test_finalize_mapping_survives_facade_publish_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A facade publish error must not abort the run; the disk save is authoritative."""
    facade = MagicMock()
    facade.set_mapping.side_effect = RuntimeError("repo unavailable")
    monkeypatch.setattr(cfg, "mappings", facade, raising=False)

    mig = _build_skeleton(tmp_path)
    mig.work_package_mapping = {"1001": {"jira_key": "PROJ-1", "openproject_id": 5001}}

    # Must not raise despite set_mapping blowing up.
    mig._finalize_mapping()

    assert mig._last_save_succeeded is True
    assert mig.work_package_mapping_file.exists()
