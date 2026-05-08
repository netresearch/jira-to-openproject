"""Unit tests for TimeEntryMigration component.

Covers preflight (Rails client missing), the no-mapping skip path, the
zero-created-with-input gating policy, and successful delegation to
TimeEntryMigrator.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    """Install an empty DummyMappings via cfg.mappings (proxy seam)."""
    import src.config as cfg

    class DummyMappings:
        def get_mapping(self, name: str):
            return {}

        def set_mapping(self, name: str, value):
            return None

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def _make_mig(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rails_present: bool = True,
):
    """Build a TimeEntryMigration with mocked clients and tmp data dir.

    The TimeEntryMigrator constructor is stubbed so the test does not need
    to satisfy its full surface — we only care about its
    ``migrate_time_entries_for_issues`` method, which we override per-test.
    """
    from src.application.components import time_entry_migration as tem

    # Patch TimeEntryMigrator so __init__ doesn't probe the OP client.
    monkeypatch.setattr(tem, "TimeEntryMigrator", MagicMock())

    # Isolate from any J2O_JIRA_PROJECTS env override the developer may have
    # set: TimeEntryMigration._load_migrated_work_packages() filters by
    # config.jira_config['projects'] when non-empty, which can silently drop
    # the seeded PROJ-1 mapping and route the test through the
    # "no_migrated_work_packages" skip path.
    import src.config as cfg

    monkeypatch.setitem(cfg.jira_config, "projects", [])

    jira = MagicMock()
    op = MagicMock()
    if rails_present:
        op.rails_client = MagicMock()
    else:
        # Explicitly set to None so getattr(..., "rails_client", None) returns None.
        op.rails_client = None

    mig = tem.TimeEntryMigration(jira_client=jira, op_client=op)
    mig.data_dir = tmp_path
    return mig


def test_run_fails_loud_when_rails_client_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _mock_mappings: None,
) -> None:
    """Rails console is required for OP time entry creation; absence is a hard fail."""
    mig = _make_mig(tmp_path, monkeypatch, rails_present=False)

    result = mig.run()

    assert result.success is False
    assert result.details["reason"] == "rails_client_missing"


def test_run_skips_with_warning_when_no_migrated_work_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _mock_mappings: None,
) -> None:
    """No work_package_mapping.json on disk → skipped, success=True (idempotent re-runs)."""
    mig = _make_mig(tmp_path, monkeypatch, rails_present=True)

    result = mig.run()

    assert result.success is True
    assert result.details["status"] == "skipped"
    assert result.details["reason"] == "no_migrated_work_packages"


def test_run_zero_created_with_input_fails_loud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _mock_mappings: None,
) -> None:
    """Discovered>0 but migrated==0 → ComponentResult(success=False) per gating policy."""
    # Seed a mapping file so the loader returns at least one WP.
    mapping = {
        "1001": {"jira_key": "PROJ-1", "openproject_id": 5001},
    }
    (tmp_path / "work_package_mapping.json").write_text(json.dumps(mapping), encoding="utf-8")

    mig = _make_mig(tmp_path, monkeypatch, rails_present=True)

    # Configure the migrator to return discovered>0, migrated==0.
    mig.time_entry_migrator.migrate_time_entries_for_issues.return_value = {
        "status": "success",
        "jira_work_logs": {"discovered": 5},
        "tempo_time_entries": {"discovered": 0},
        "total_time_entries": {"migrated": 0, "failed": 0},
    }

    result = mig.run()

    assert result.success is False
    assert result.details["reason"] == "zero_created_with_input"
    assert result.details["total_discovered"] == 5


def test_run_returns_success_when_migrator_creates_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _mock_mappings: None,
) -> None:
    """Happy path: migrator reports migrated entries → success."""
    mapping = {"1001": {"jira_key": "PROJ-1", "openproject_id": 5001}}
    (tmp_path / "work_package_mapping.json").write_text(json.dumps(mapping), encoding="utf-8")

    mig = _make_mig(tmp_path, monkeypatch, rails_present=True)
    mig.time_entry_migrator.migrate_time_entries_for_issues.return_value = {
        "status": "success",
        "jira_work_logs": {"discovered": 3, "migrated": 3, "failed": 0},
        "tempo_time_entries": {"discovered": 0, "migrated": 0, "failed": 0},
        "total_time_entries": {"migrated": 3, "failed": 0},
    }

    result = mig.run()

    assert result.success is True
    assert result.success_count == 3
    assert result.failed_count == 0


def test_get_current_entities_for_type_raises_value_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _mock_mappings: None,
) -> None:
    """Transformation-only migration: idempotent workflow is unsupported."""
    mig = _make_mig(tmp_path, monkeypatch, rails_present=True)

    with pytest.raises(ValueError, match="transformation-only"):
        mig._get_current_entities_for_type("time_entries")
