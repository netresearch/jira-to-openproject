# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT
"""Confirmed writes and rerun boundaries in the legacy work package writer."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import config
from src.application.components import work_package_migration as legacy


@pytest.fixture
def migration(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "jira_config", {"projects": ["ORIGIN"]})
    monkeypatch.setattr(config, "migration_config", {"batch_size": 2})
    monkeypatch.setattr(legacy, "_apply_required_defaults", Mock())
    app = legacy.WorkPackageMigration.__new__(legacy.WorkPackageMigration)
    app.data_dir = tmp_path
    app.logger = Mock()
    app.jira_client = Mock()
    app.jira_client.jira.search_issues.return_value = []
    app.op_client = Mock()
    app.op_client.get_project_wp_cf_snapshot.return_value = []
    app.project_mapping = {"P": {"jira_key": "ORIGIN", "openproject_id": 7}}
    app.work_package_mapping = {}
    app.prepare_work_package = Mock(side_effect=lambda issue, project_id: {"subject": issue.key})
    app._extract_issue_meta = Mock(return_value={})
    app._iter_all_project_issues = Mock(
        return_value=[SimpleNamespace(key=f"ORIGIN-{number}", id=str(number)) for number in (1, 2, 3)]
    )
    return app


def test_full_and_tail_batches_preserve_previous_mappings(migration):
    path = migration.data_dir / "work_package_mapping.json"
    path.write_text('{"40":{"openproject_id":90}}')
    bulk = migration.op_client.bulk_create_records
    bulk.side_effect = [
        {"status": "success", "created": [{"index": 1, "id": 12}, {"index": 0, "id": 11}]},
        {"status": "success", "created": [{"index": 0, "id": 13}]},
    ]
    result = migration.run()
    assert result.success
    assert result.success_count == 3
    assert [len(args.args[1]) for args in bulk.call_args_list] == [2, 1]
    assert {key: row["openproject_id"] for key, row in json.loads(path.read_text()).items()} == {
        "1": 11,
        "2": 12,
        "3": 13,
        "40": 90,
    }
    migration.op_client.assign_user_roles.assert_not_called()


def test_ambiguous_write_stops_after_one_attempt(migration):
    bulk = migration.op_client.bulk_create_records
    bulk.side_effect = TimeoutError
    result = migration.run()
    assert not result.success
    assert "reconcile" in result.error
    pending = migration.data_dir / "legacy-work-package-create.pending.json"
    assert json.loads(pending.read_text()) == {
        "project_key": "ORIGIN",
        "openproject_project_id": 7,
        "issues": [{"jira_id": "1", "jira_key": "ORIGIN-1"}, {"jira_id": "2", "jira_key": "ORIGIN-2"}],
    }
    migration.work_package_mapping = {}
    retry = migration.run()
    assert not retry.success
    assert "reconcile" in retry.error
    bulk.assert_called_once()


def test_partial_batch_records_confirmed_ids_and_stops_rerun(migration):
    bulk = migration.op_client.bulk_create_records
    bulk.return_value = {"status": "success", "created": [{"index": 1, "id": 12}], "errors": [{"index": 0}]}
    result = migration.run()
    assert not result.success
    assert "confirmed 1 of 2" in result.error
    mapping = json.loads((migration.data_dir / "work_package_mapping.json").read_text())
    assert list(mapping) == ["2"]
    assert mapping["2"]["openproject_id"] == 12
    retry = migration.run()
    assert not retry.success
    assert "reconcile" in retry.error
    bulk.assert_called_once()

    # The operator reconciles the confirmed row and releases the interrupted batch.
    (migration.data_dir / "legacy-work-package-create.pending.json").unlink()
    migration.op_client.get_project_wp_cf_snapshot.return_value = [
        {"id": 12, "jira_source_id": "2", "jira_issue_key": None}
    ]
    original = migration._iter_all_project_issues.return_value
    original[1].changelog = {"total": 0, "histories": []}
    original[1].fields = SimpleNamespace(comment={"total": 0, "comments": []})
    bulk.return_value = {"status": "success", "created": [{"index": 0, "id": 11}, {"index": 1, "id": 13}]}
    result = migration.run()
    assert result.success, result.error
    assert result.success_count == 2
    assert [row["subject"] for row in bulk.call_args.args[1]] == ["ORIGIN-1", "ORIGIN-3"]


def test_unreadable_target_inventory_stops_before_writing(migration):
    migration.op_client.get_project_wp_cf_snapshot.side_effect = ConnectionError
    assert not migration.run().success
    migration.op_client.bulk_create_records.assert_not_called()


def test_directory_sync_failure_prevents_target_submission(migration, monkeypatch):
    monkeypatch.setattr(legacy, "sync_directory", Mock(side_effect=OSError("cannot sync directory")))
    assert not migration.run().success
    migration.op_client.bulk_create_records.assert_not_called()


@pytest.mark.parametrize("target", [{"id": 91, "jira_source_id": "2"}, {"id": 12, "jira_source_id": "91"}])
def test_local_mapping_cannot_exclude_an_unverified_target(migration, target):
    mapping = {"2": {"openproject_id": 12, "openproject_project_id": 7, "jira_key": "ORIGIN-2"}}
    (migration.data_dir / "work_package_mapping.json").write_text(json.dumps(mapping))
    migration.op_client.get_project_wp_cf_snapshot.return_value = [target]
    assert not migration.run().success
    migration.op_client.bulk_create_records.assert_not_called()
