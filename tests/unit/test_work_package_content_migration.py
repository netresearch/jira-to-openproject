"""Unit tests for WorkPackageContentMigration component (Phase 2).

Covers:
- Hard-fail when work_package_mapping.json is absent.
- Successful loading of mapping + jira_key→wp_id lookup index build.
- _convert_jira_links rewrites known Jira keys (the markdown converter
  produces ``#<wp_id>`` for known keys; unmapped keys are flagged).
- Skipping legacy bare-int rows that have no recoverable jira_key.
- Successful end-to-end run() returning success status.
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
                "user": {"alice": {"openproject_id": 21}},
                "custom_field": {"customfield_10001": {"openproject_id": 99}},
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

        def set_mapping(self, name: str, value):
            self._m[name] = value

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def _build_mig(tmp_path: Path):
    """Construct WorkPackageContentMigration redirected to tmp_path."""
    from src.application.components.work_package_content_migration import (
        WorkPackageContentMigration,
    )

    jira = MagicMock()
    op = MagicMock()
    mig = WorkPackageContentMigration(jira_client=jira, op_client=op)
    # Redirect file paths to tmp_path; need to also re-load now-empty state.
    mig.data_dir = tmp_path
    mig.work_package_mapping_file = tmp_path / mig.WORK_PACKAGE_MAPPING_FILE
    mig.attachment_mapping_file = tmp_path / mig.ATTACHMENT_MAPPING_FILE
    return mig


def test_run_hard_fails_when_mapping_file_missing(
    tmp_path: Path,
    _mock_mappings: None,
) -> None:
    """No work_package_mapping.json → run() returns ComponentResult(success=False)."""
    mig = _build_mig(tmp_path)
    # The constructor already attempted a load; ensure clean state.
    mig.work_package_mapping = {}

    result = mig.run()

    assert result.success is False
    assert "skeleton" in (result.error or "").lower()


def test_load_work_package_mapping_builds_jira_key_lookup(
    tmp_path: Path,
    _mock_mappings: None,
) -> None:
    """Dict-shaped rows populate the jira_key → wp_id index used by link rewriting."""
    mapping = {
        "1001": {"jira_key": "PROJ-1", "openproject_id": 5001},
        "1002": {"jira_key": "PROJ-2", "openproject_id": 5002},
    }
    (tmp_path / "work_package_mapping.json").write_text(json.dumps(mapping), encoding="utf-8")

    mig = _build_mig(tmp_path)
    # Re-trigger load against the seeded file.
    assert mig._load_work_package_mapping() is True
    assert mig.jira_key_to_wp_id == {"PROJ-1": 5001, "PROJ-2": 5002}


def test_load_work_package_mapping_skips_bare_int_legacy_rows(
    tmp_path: Path,
    _mock_mappings: None,
) -> None:
    """Bare-int legacy rows (no recoverable jira_key) must be skipped, not crash."""
    mapping = {
        "1001": {"jira_key": "PROJ-1", "openproject_id": 5001},
        "1002": 9999,  # legacy bare-int row
    }
    (tmp_path / "work_package_mapping.json").write_text(json.dumps(mapping), encoding="utf-8")

    mig = _build_mig(tmp_path)
    assert mig._load_work_package_mapping() is True
    # Bare-int row excluded from the lookup index.
    assert mig.jira_key_to_wp_id == {"PROJ-1": 5001}


def test_convert_jira_links_rewrites_known_keys(
    tmp_path: Path,
    _mock_mappings: None,
) -> None:
    """Known Jira keys are converted to OP work-package references in the output.

    The markdown converter produces ``#<wp_id>`` (not the literal ``WP#<id>``)
    for matched keys; unmapped keys are wrapped to flag them as missing. We
    care that PROJ-1 is *no longer present as a bare key* and that 5001 is
    embedded in the output — the exact decoration is a converter concern.
    """
    mapping = {"1001": {"jira_key": "PROJ-1", "openproject_id": 5001}}
    (tmp_path / "work_package_mapping.json").write_text(json.dumps(mapping), encoding="utf-8")

    mig = _build_mig(tmp_path)
    mig._load_work_package_mapping()
    mig._init_markdown_converter()

    text = "See PROJ-1 for details"
    converted = mig._convert_jira_links(text, jira_key="PROJ-1")

    # PROJ-1 was rewritten — it must not survive verbatim as a bare key in the output.
    assert "See PROJ-1 for" not in converted
    # The mapped WP id appears in the converted output.
    assert "5001" in converted


def test_run_returns_success_on_clean_migration(
    tmp_path: Path,
    _mock_mappings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: with a non-empty mapping, run() returns success=True.

    Patches _migrate_content to avoid driving the full Jira+OP pipeline; we
    only assert the run() wrapper produces the expected ComponentResult shape.
    """
    mapping = {"1001": {"jira_key": "PROJ-1", "openproject_id": 5001}}
    (tmp_path / "work_package_mapping.json").write_text(json.dumps(mapping), encoding="utf-8")

    mig = _build_mig(tmp_path)
    mig._load_work_package_mapping()

    monkeypatch.setattr(
        mig,
        "_migrate_content",
        lambda: {
            "total_processed": 1,
            "total_updated": 1,
            "total_skipped": 0,
            "total_failed": 0,
            "descriptions_updated": 1,
            "custom_fields_updated": 0,
            "comments_migrated": 0,
            "watchers_added": 0,
            "projects": {},
        },
    )

    result = mig.run()

    assert result.success is True
    assert result.details["total_updated"] == 1
    assert result.details["descriptions_updated"] == 1
