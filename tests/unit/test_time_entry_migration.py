# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Unit tests for TimeEntryMigration component.

Covers preflight (Rails client missing), the no-mapping skip path, the
zero-created-with-input gating policy, and successful delegation to
TimeEntryMigrator.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.infrastructure.jira.jira_client import JiraApiError
from tests.unit.test_level_attachments import LEVEL_MAP
from tests.unit.test_level_attachments import LIVE as LEVEL_LIVE
from tests.unit.test_level_work_packages import level_issues
from tests.unit.test_level_work_packages import setup as level_work_package_setup  # noqa: F401  (fixture)


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


def test_run_succeeds_when_all_discovered_are_skipped_or_unmappable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _mock_mappings: None,
) -> None:
    """discovered=6, unmappable=1, skipped=5, migrated=0, failed=0 → success=True.

    This is the exact production scenario: 5 Jira entries already migrated
    (provenance match → skipped) + 1 Tempo entry with no user/WP mapping
    (unmappable).  Zero real failures → component must NOT fail loud.
    """
    mapping = {"1001": {"jira_key": "PROJ-1", "openproject_id": 5001}}
    (tmp_path / "work_package_mapping.json").write_text(json.dumps(mapping), encoding="utf-8")

    mig = _make_mig(tmp_path, monkeypatch, rails_present=True)

    # Migrator reports: 5 jira discovered (all skipped by provenance), 1 tempo discovered
    # (unmappable), 0 real failures, 0 actually migrated.
    mig.time_entry_migrator.migrate_time_entries_for_issues.return_value = {
        "status": "success",
        "jira_work_logs": {"discovered": 5},
        "tempo_time_entries": {"discovered": 1},
        "total_time_entries": {"migrated": 0, "failed": 0},
        # NEW key introduced by the fix: counts entries dropped at the transformer
        "unmappable": 1,
        # skipped_entries from the migrator (provenance dedup)
        "skipped": 5,
    }

    result = mig.run()

    assert result.success is True, f"Expected success but got: {result.message!r}"
    assert result.details["status"] == "success"
    # The unmappable count should be surfaced somewhere in the details
    assert result.details.get("unmappable", 0) == 1 or "unmappable" in str(result.message)


def test_run_net_actionable_clamped_to_zero_when_counts_inconsistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _mock_mappings: None,
) -> None:
    """net_actionable must not go negative when skipped+unmappable exceeds discovered.

    Inconsistent upstream counts (e.g. provenance snapshot larger than the
    current discovery run) must not produce a negative net_actionable that
    would mislead the gating logic.  Clamping to max(0, …) ensures success.
    """
    mapping = {"1001": {"jira_key": "PROJ-1", "openproject_id": 5001}}
    (tmp_path / "work_package_mapping.json").write_text(json.dumps(mapping), encoding="utf-8")

    mig = _make_mig(tmp_path, monkeypatch, rails_present=True)

    # discovered=3 but skipped=5 → raw net_actionable would be 3-0-5 = -2
    mig.time_entry_migrator.migrate_time_entries_for_issues.return_value = {
        "status": "success",
        "jira_work_logs": {"discovered": 3},
        "tempo_time_entries": {"discovered": 0},
        "total_time_entries": {"migrated": 0, "failed": 0},
        "unmappable": 0,
        "skipped": 5,
    }

    result = mig.run()

    # net_actionable is clamped to 0 → zero-created gate must NOT trip → success
    assert result.success is True, (
        f"Negative net_actionable must be clamped to 0, not trigger failure: {result.message!r}"
    )


def test_run_zero_created_failure_message_includes_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _mock_mappings: None,
) -> None:
    """The zero-created failure message must include the key counts for operator triage."""
    mapping = {"1001": {"jira_key": "PROJ-1", "openproject_id": 5001}}
    (tmp_path / "work_package_mapping.json").write_text(json.dumps(mapping), encoding="utf-8")

    mig = _make_mig(tmp_path, monkeypatch, rails_present=True)

    mig.time_entry_migrator.migrate_time_entries_for_issues.return_value = {
        "status": "failed",
        "jira_work_logs": {"discovered": 7},
        "tempo_time_entries": {"discovered": 0},
        "total_time_entries": {"migrated": 0, "failed": 3},
        "unmappable": 1,
        "skipped": 0,
    }

    result = mig.run()

    assert result.success is False
    assert result.details["reason"] == "zero_created_with_input"
    # Message must be operator-actionable and include key counts
    msg = result.message
    assert "discovered=" in msg, f"Message should include discovered count: {msg!r}"
    assert "failed" in msg, f"Message should mention failures: {msg!r}"


def test_repeated_enriched_worklogs_count_once(tmp_path, monkeypatch, _mock_mappings):
    mapping = {"1001": {"jira_key": "PROJ-1", "openproject_id": 5001}}
    (tmp_path / "work_package_mapping.json").write_text(json.dumps(mapping), encoding="utf-8")
    mig = _make_mig(tmp_path, monkeypatch, rails_present=True)
    mig.time_entry_migrator.migrate_time_entries_for_issues.return_value = {
        "status": "success",
        "jira_work_logs": {"discovered": 7},
        "tempo_time_entries": {"discovered": 7, "shared_jira_worklogs": 7, "attributes_verified": 7},
        "total_time_entries": {"migrated": 0, "failed": 0},
        "skipped": 7,
    }
    assert mig.run().success


def test_instance_catalogue_runs_without_mapped_work_packages(tmp_path, monkeypatch, _mock_mappings):
    from src.application.components import time_entry_migration as component

    migration = _make_mig(tmp_path, monkeypatch)
    receipt = {"verified": True, "written": True, "external_options_not_enumerated": 1}
    archive = MagicMock(return_value=receipt)
    monkeypatch.setattr(component, "archive_catalogue", archive)
    monkeypatch.setenv(component.SETTING, "private-archive")
    result = migration.run()

    archive.assert_called_once_with(migration.jira_client, migration.op_client, "private-archive")
    assert result.success
    assert result.details["tempo_instance_catalogue"] == receipt
    assert result.details["status"] == "success"
    assert any("External dynamic option providers" in warning for warning in result.warnings)


def test_configured_catalogue_cannot_silently_skip_denied_source(tmp_path, monkeypatch, _mock_mappings):
    from src.application.components import time_entry_migration as component
    from src.infrastructure.jira.jira_client import JiraAuthenticationError

    migration = _make_mig(tmp_path, monkeypatch)
    error = JiraAuthenticationError("sensitive upstream detail")
    monkeypatch.setattr(component, "archive_catalogue", MagicMock(side_effect=error))
    monkeypatch.setenv(component.SETTING, "private-archive")
    result = migration.run()

    assert result.success is False
    assert result.details["tempo_instance_catalogue"] == {"verified": False}
    assert result.details["status"] == "failed"
    assert result.failed_count == 1
    assert result.errors == ["The configured Tempo instance catalogue could not be verified (JiraAuthenticationError)."]


# --- Level work packages (ADR-009, step 5b) ---------------------------------------------------

ORDINARY = {"1": {"jira_key": "APP-1", "openproject_id": 500, "project_key": "APP", "openproject_project_id": 7}}


@pytest.fixture
def level_setup(level_work_package_setup):  # noqa: F811 (the imported fixture, requested by name)
    """The reports, mappings and switch of the level work package tests."""
    return level_work_package_setup


def _worklog(key):
    number = int(key.split("-")[1])
    return {
        "id": str(7000 + number),
        "issue_key": key,
        "started": "2026-01-01T10:00:00.000+0000",
        "author": {"key": "worker", "time_zone": "UTC"},
        "time_spent_seconds": 3600,
        "comment": "",
        "created": "2026-01-01T10:00:00.000+0000",
        "updated": "2026-01-01T10:00:00.000+0000",
    }


def _level_jira(changed=()):
    """Answer the level search and the work log reads; a key in ``changed`` fails the level check."""
    jira = MagicMock(base_url="https://jira.local")
    jira.get_projects.return_value = [{"key": "APP"}]
    jira.jira.search_issues.side_effect = lambda jql, startAt, maxResults, **kw: level_issues()[
        startAt : startAt + maxResults
    ]
    jira.reads = []

    def read(key, **kwargs):
        jira.reads.append((key, kwargs))
        if key in changed:
            msg = "Issue security cannot be represented on a native time entry."
            raise JiraApiError(msg)
        return [_worklog(key)]

    jira.get_work_logs_for_issue.side_effect = read
    jira.get_tempo_time_entries.return_value = []
    return jira


def _level_op(live=None):
    op = MagicMock()
    op.rails_client = MagicMock()
    op.live_reads = []

    def query(script):
        op.live_reads.append(json.loads(script.split("\n")[1]))
        return json.loads(json.dumps(LEVEL_LIVE if live is None else live))

    op.execute_query_to_json_file.side_effect = query
    op.get_time_entry_activities.return_value = [{"name": "Development", "id": 2}]
    return op


def _level_run(level_setup, monkeypatch, *, live=None, changed=(), level_map=None, ordinary=None, runs=1, **kw):
    from src.infrastructure.openproject.openproject_time_entry_service import OpenProjectTimeEntryService
    from src.utils.time_entry_migrator import TimeEntryMigrator

    mappings, data = level_setup(**kw)
    import src.config as cfg

    monkeypatch.setitem(cfg.jira_config, "projects", [])
    monkeypatch.delenv("J2O_TIME_ENTRY_FAST_FORWARD", raising=False)
    mappings.data["security_level_work_package"] = dict(LEVEL_MAP if level_map is None else level_map)
    wp_map = ORDINARY if ordinary is None else ordinary
    if wp_map:
        (data / "work_package_mapping.json").write_text(json.dumps(wp_map), encoding="utf-8")
    (data / "user_mapping.json").write_text(
        json.dumps({"worker": {"jira_key": "worker", "openproject_id": 3}}), encoding="utf-8"
    )
    writes = []
    original = mappings.set_mapping
    mappings.set_mapping = lambda name, value: (writes.append(name), original(name, value))
    rows = []

    def write(self, batch_size=200, *, dry_run=False):
        # The rows the Rails writer receives, built by its own projection.
        entries = list(self.transformed_time_entries)
        rows.append([OpenProjectTimeEntryService._entry_row(entry, i) for i, entry in enumerate(entries)])
        self.migration_results["successful_migrations"] += len(entries)
        return {}

    monkeypatch.setattr(TimeEntryMigrator, "migrate_time_entries_to_openproject", write)
    from src.application.components.time_entry_migration import TimeEntryMigration

    jira, op = _level_jira(changed), _level_op(live)
    migration = TimeEntryMigration(jira_client=jira, op_client=op)
    migration.data_dir = data
    results = [migration.run() for _ in range(runs)]
    return SimpleNamespace(
        result=results[-1], results=results, jira=jira, op=op, rows=rows, writes=writes, migration=migration
    )


def _targets(rows):
    return {(row["work_package_id"], row["target_project_id"]) for row in rows}


def _enabled(op):
    return [call.args[0] for call in op.enable_project_modules.call_args_list]


def test_level_switch_off_issues_exactly_the_calls_of_the_ordinary_path(level_setup, monkeypatch):
    outcome = _level_run(level_setup, monkeypatch, on=False, reports=False)
    assert outcome.result.success, outcome.result
    assert outcome.jira.reads == [("APP-1", {})]
    assert [_targets(rows) for rows in outcome.rows] == [{(500, 7)}]
    assert _enabled(outcome.op) == [7]
    assert not outcome.op.live_reads
    assert not outcome.jira.get_projects.called
    assert "security_level" not in outcome.result.details


def test_level_switch_on_logs_time_on_the_work_package_its_sub_project_holds(level_setup, monkeypatch):
    outcome = _level_run(level_setup, monkeypatch)
    assert outcome.result.success, outcome.result
    # The level read requires the selected level; archived and unmapped issues are never read.
    assert outcome.jira.reads == [("APP-1", {}), ("APP-10", {"level_id": "10000"}), ("APP-11", {"level_id": "10000"})]
    # The time entry's expected project is the sub-project; OpenProject takes it from the work package.
    assert [_targets(rows) for rows in outcome.rows] == [{(500, 7), (900, 100), (901, 100)}]
    assert _enabled(outcome.op) == [7, 100]
    assert outcome.op.live_reads == [{"project_id": 100, "field": "J2O Origin Key"}]
    assert outcome.result.details["security_level"] == {"work_packages": 2, "refused": 0}
    assert outcome.writes == []


@pytest.mark.parametrize(
    "options",
    [
        {"live": {"field": True, "work_packages": [901], "keys": [[901, "APP-11"]]}},
        {"ordinary": {**ORDINARY, "10": {"jira_key": "APP-10", "openproject_id": 510, "project_key": "APP"}}},
    ],
)
def test_level_issue_that_cannot_be_confirmed_gets_no_time_entries(level_setup, monkeypatch, options):
    outcome = _level_run(level_setup, monkeypatch, **options)
    assert not outcome.result.success
    assert [key for key, _ in outcome.jira.reads] == ["APP-1", "APP-11"]
    assert [_targets(rows) for rows in outcome.rows] == [{(500, 7), (901, 100)}]
    assert outcome.result.details["security_level"]["refused"] == 1


def test_level_issue_whose_level_changed_gets_no_time_entries(level_setup, monkeypatch):
    outcome = _level_run(level_setup, monkeypatch, changed={"APP-10"})
    assert not outcome.result.success
    assert ("APP-10", {"level_id": "10000"}) in outcome.jira.reads
    assert [_targets(rows) for rows in outcome.rows] == [{(500, 7), (901, 100)}]
    assert outcome.result.details["jira_work_logs"]["unreadable_issues"] == 1
    assert outcome.result.details["security_level"] == {"work_packages": 2, "refused": 1}
    assert outcome.result.failed == 1


def test_level_unselected_issue_with_an_entry_gets_nothing(level_setup, monkeypatch):
    level_map = {**LEVEL_MAP, "12": {**LEVEL_MAP["10"], "jira_key": "APP-12", "openproject_id": 902}}
    outcome = _level_run(level_setup, monkeypatch, level_map=level_map)
    assert not outcome.result.success
    assert "APP-12" not in [key for key, _ in outcome.jira.reads]
    assert outcome.result.details["security_level"]["refused"] == 1


def test_level_unreadable_sub_project_refuses_all_its_issues(level_setup, monkeypatch):
    outcome = _level_run(level_setup, monkeypatch, live={"field": True})
    assert not outcome.result.success
    assert outcome.jira.reads == [("APP-1", {})]
    assert outcome.result.details["security_level"]["refused"] == 2


def test_level_repeated_run_on_one_instance_gives_the_same_result(level_setup, monkeypatch):
    outcome = _level_run(
        level_setup, monkeypatch, live={"field": True, "work_packages": [900], "keys": [[900, "APP-10"]]}, runs=2
    )
    first, second = outcome.results
    assert first.details["security_level"] == second.details["security_level"] == {"work_packages": 1, "refused": 1}
    assert [_targets(rows) for rows in outcome.rows] == [{(500, 7), (900, 100)}] * 2
    assert len(outcome.op.live_reads) == 2


def test_level_refusals_are_reported_without_ordinary_work_packages(level_setup, monkeypatch):
    outcome = _level_run(level_setup, monkeypatch, live={"field": True}, ordinary={})
    assert not outcome.result.success
    assert outcome.result.details["reason"] == "no_migrated_work_packages"
    assert outcome.result.details["security_level"] == {"work_packages": 0, "refused": 2}
    assert not outcome.jira.reads


def test_level_switch_on_without_reports_stops_before_any_read(level_setup, monkeypatch):
    outcome = _level_run(level_setup, monkeypatch, reports=False)
    assert not outcome.result.success
    assert not outcome.jira.reads
    assert not outcome.rows


def test_level_state_of_an_earlier_run_is_not_reported_again(level_setup, monkeypatch):
    outcome = _level_run(level_setup, monkeypatch, live={"field": True})
    assert outcome.result.details["security_level"]["refused"] == 2
    # The next run stops before the resolution: no refusal of the earlier run is counted again.
    migration_op = outcome.op
    migration_op.rails_client = None
    from src.application.components.time_entry_migration import TimeEntryMigration

    second = TimeEntryMigration(jira_client=outcome.jira, op_client=migration_op)
    second._level = outcome.migration._level
    result = second.run()
    assert not result.success
    assert result.details["reason"] == "rails_client_missing"
    assert "security_level" not in result.details
