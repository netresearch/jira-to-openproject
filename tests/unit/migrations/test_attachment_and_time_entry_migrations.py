"""Integration-like smoke tests for attachment and time-entry migrations."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.models import ComponentResult
from src.migrations.attachments_migration import AttachmentsMigration
from src.migrations.attachment_provenance_migration import AttachmentProvenanceMigration
from src.migrations.time_entry_migration import TimeEntryMigration


class DummyMappings:
    def __init__(self, mapping):
        self._mapping = mapping

    def get_mapping(self, name):
        return self._mapping.get(name, {})


@pytest.fixture()
def dummy_mapping_data():
    return {"work_package": {"KEY-1": {"openproject_id": 42}}}


def configure_config(module, monkeypatch, tmp_path, mapping_data, extra_config=None):
    dummy = DummyMappings(mapping_data)
    monkeypatch.setattr(module.config, "get_mappings", lambda: dummy, raising=False)
    monkeypatch.setattr(module.config, "mappings", dummy, raising=False)
    monkeypatch.setattr(module.config, "get_path", lambda name: tmp_path, raising=False)
    cfg = dict(extra_config or {})
    monkeypatch.setattr(module.config, "migration_config", cfg, raising=False)


def test_attachments_migration_transfers_files(tmp_path, monkeypatch, dummy_mapping_data):
    from src.migrations import attachments_migration as module

    configure_config(
        module,
        monkeypatch,
        tmp_path,
        dummy_mapping_data,
        extra_config={"attachment_path": tmp_path.as_posix()},
    )

    jira_client = MagicMock()
    op_client = MagicMock()
    op_client.execute_script_with_data.return_value = {"updated": 1, "failed": 0}

    migration = AttachmentsMigration(jira_client=jira_client, op_client=op_client)

    migration._extract = MagicMock(  # type: ignore[attr-defined]
        return_value=ComponentResult(
            success=True,
            data={
                "attachments": {
                    "KEY-1": [
                        {
                            "filename": "foo.txt",
                            "url": "https://example.com/foo.txt",
                        }
                    ],
                },
            },
        ),
    )

    def fake_download(url, dest_path):
        path = Path(dest_path)
        path.write_bytes(b"content")
        return path

    migration._download_attachment = MagicMock(side_effect=fake_download)  # type: ignore[attr-defined]

    result = migration.run()

    assert result.success
    op_client.transfer_file_to_container.assert_called_once()
    execute_args = op_client.execute_script_with_data.call_args[0]
    payload = execute_args[1]
    assert len(payload) == 1
    assert payload[0]["filename"] == "foo.txt"


def test_attachment_provenance_updates_metadata(tmp_path, monkeypatch, dummy_mapping_data):
    from src.migrations import attachment_provenance_migration as module

    configure_config(module, monkeypatch, tmp_path, dummy_mapping_data)

    jira_client = MagicMock()
    op_client = MagicMock()
    op_client.execute_script_with_data.return_value = {"updated": 1, "failed": 0}

    migration = AttachmentProvenanceMigration(jira_client=jira_client, op_client=op_client)

    migration._extract = MagicMock(  # type: ignore[attr-defined]
        return_value=ComponentResult(
            success=True,
            data={
                "items": [
                    {
                        "jira_key": "KEY-1",
                        "filename": "foo.txt",
                        "author": {"accountId": "acc-1"},
                        "created": "2024-01-01T00:00:00Z",
                    }
                ]
            },
        ),
    )
    migration._resolve_user_id = MagicMock(return_value=99)  # type: ignore[attr-defined]

    result = migration.run()

    assert result.success
    updates = op_client.execute_script_with_data.call_args[0][1]
    assert updates[0]["author_id"] == 99
    assert updates[0]["filename"] == "foo.txt"


def test_time_entry_zero_created_guard(monkeypatch, tmp_path):
    from src.migrations import time_entry_migration as module

    configure_config(module, monkeypatch, tmp_path, {"work_package": {}}, extra_config={})

    jira_client = MagicMock()
    op_client = MagicMock()
    op_client.rails_client = object()

    migration = TimeEntryMigration(jira_client=jira_client, op_client=op_client)
    migration._load_migrated_work_packages = MagicMock(return_value=[{"jira_key": "KEY-1", "work_package_id": 101}])  # type: ignore[attr-defined]
    migration.time_entry_migrator = MagicMock()
    migration.time_entry_migrator.migrate_time_entries_for_issues.return_value = {
        "status": "success",
        "jira_work_logs": {"discovered": 1},
        "tempo_time_entries": {"discovered": 0},
        "total_time_entries": {"migrated": 0, "failed": 0},
    }

    result = migration.run()

    assert not result.success
    assert result.details["reason"] == "zero_created_with_input"

    # Success path when entries are migrated
    migration.time_entry_migrator.migrate_time_entries_for_issues.return_value = {
        "status": "success",
        "jira_work_logs": {"discovered": 0},
        "tempo_time_entries": {"discovered": 0},
        "total_time_entries": {"migrated": 2, "failed": 0},
    }

    success_result = migration.run()
    assert success_result.success

