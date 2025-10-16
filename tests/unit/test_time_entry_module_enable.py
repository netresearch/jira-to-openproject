from unittest.mock import MagicMock

from src.utils.time_entry_migrator import TimeEntryMigrator


class DummyJira:
    pass


def test_time_entry_migrator_enables_time_tracking_once_per_project(monkeypatch, tmp_path):
    jira = DummyJira()

    # Mock OpenProjectClient with enable_project_modules spy
    op = MagicMock()
    op.enable_project_modules = MagicMock(return_value=True)

    mig = TimeEntryMigrator(jira_client=jira, op_client=op, data_dir=tmp_path)

    migrated = [
        {"jira_key": "SRVA-1", "work_package_id": 101, "project_id": 10},
        {"jira_key": "SRVA-2", "work_package_id": 102, "project_id": 10},
        {"jira_key": "SRVA-3", "work_package_id": 103, "project_id": 11},
    ]

    # Patch run_complete_migration to avoid heavy flows
    def _noop_run(**kwargs):
        return {
            "successful_migrations": 0,
            "failed_migrations": 0,
            "total_work_logs_found": 0,
            "jira_work_logs_extracted": 0,
            "tempo_entries_extracted": 0,
            "successful_transformations": 0,
            "failed_transformations": 0,
            "skipped_entries": 0,
            "errors": [],
            "warnings": [],
            "processing_time_seconds": 0.0,
        }

    monkeypatch.setattr(mig, "run_complete_migration", lambda **kwargs: _noop_run(**kwargs))

    _ = mig.migrate_time_entries_for_issues(migrated)

    # Should have been called once per distinct project (10 and 11)
    calls = op.enable_project_modules.call_args_list
    assert len(calls) == 2
    called_projects = sorted(call.args[0] for call in calls)
    assert called_projects == [10, 11]
    # Ensure correct module name ('costs' in OpenProject)
    for call in calls:
        assert call.args[1] == ["costs"]
