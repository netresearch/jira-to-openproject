import os
from datetime import datetime, UTC, timedelta
from unittest.mock import MagicMock

from src.utils.time_entry_migrator import TimeEntryMigrator


class DummyJira:
    def __init__(self, logs_by_issue):
        self._logs_by_issue = logs_by_issue

    # Migrator calls jira_client.get_work_logs_for_issue via JiraClient wrapper,
    # but in unit mode we patch TimeEntryMigrator.jira_client directly.
    def get_work_logs_for_issue(self, issue_key):  # type: ignore[override]
        return self._logs_by_issue.get(issue_key, [])


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")


def test_fast_forward_filters_old_logs(monkeypatch, tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    cutoff = now - timedelta(days=1)

    logs = {
        "SRVA-1": [
            {"id": "1", "created": _iso(cutoff - timedelta(hours=1)), "updated": _iso(cutoff - timedelta(hours=1))},
            {"id": "2", "created": _iso(cutoff + timedelta(hours=1)), "updated": _iso(cutoff + timedelta(hours=2))},
        ]
    }
    jira = DummyJira(logs)

    op = MagicMock()
    # get_time_entries returns entries whose updated_at define cutoff
    op.get_time_entries = MagicMock(return_value=[{"updated_at": _iso(cutoff)}])

    mig = TimeEntryMigrator(jira_client=jira, op_client=op, data_dir=tmp_path)

    # Enable FF via env
    monkeypatch.setenv("J2O_TIME_ENTRY_FAST_FORWARD", "1")
    monkeypatch.setenv("J2O_TIME_ENTRY_FF_FIELD", "updated")

    res = mig.run_complete_migration(["SRVA-1"], include_tempo=False, batch_size=10, dry_run=True)

    # After transform/migrate, the internal counters should reflect only 1 log kept
    assert mig.migration_results["jira_work_logs_extracted"] == 1
    assert mig.migration_results["total_work_logs_found"] >= 1


def test_fast_forward_created_field(monkeypatch, tmp_path):
    now = datetime.now(UTC).replace(microsecond=0)
    cutoff = now - timedelta(days=7)

    logs = {
        "SRVA-2": [
            {"id": "3", "created": _iso(cutoff - timedelta(days=1)), "updated": _iso(cutoff + timedelta(hours=1))},
            {"id": "4", "created": _iso(cutoff + timedelta(days=1)), "updated": _iso(cutoff - timedelta(hours=1))},
        ]
    }
    jira = DummyJira(logs)

    op = MagicMock()
    op.get_time_entries = MagicMock(return_value=[{"created_at": _iso(cutoff)}])

    mig = TimeEntryMigrator(jira_client=jira, op_client=op, data_dir=tmp_path)

    monkeypatch.setenv("J2O_TIME_ENTRY_FAST_FORWARD", "1")
    monkeypatch.setenv("J2O_TIME_ENTRY_FF_FIELD", "created")

    res = mig.run_complete_migration(["SRVA-2"], include_tempo=False, batch_size=10, dry_run=True)

    # Only the entry strictly after created cutoff should remain (id=4 filtered out, id=3 filtered out => none)
    # Given data: created: (id3 < cutoff, id4 > cutoff) so keep id4 -> count=1
    assert mig.migration_results["jira_work_logs_extracted"] == 1
