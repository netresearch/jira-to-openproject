# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Audience boundaries on ordinary work package journals and their state snapshots."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import config
from src.application.components.work_package_migration import WorkPackageMigration
from src.infrastructure.openproject.openproject_bulk_create_service import LegacyJournalReplayError


@pytest.mark.parametrize("restriction", [{"visibility": {"type": "group", "value": "team"}}, {"jsdPublic": True}])
@pytest.mark.parametrize("sdk", [True, False])
def test_legacy_create_refuses_source_history_before_target_calls(restriction, sdk):
    held = {"field": "description", "toString": "private-history-marker"}
    history = {
        "histories": [
            {"items": [held], **restriction},
            {"items": [dict(held, **restriction), {"field": "summary", "toString": "Public"}]},
        ]
    }
    issue = SimpleNamespace(key="JOURNAL-1", raw={"changelog": history}) if sdk else {"changelog": history}
    migration = WorkPackageMigration.__new__(WorkPackageMigration)
    migration.logger = Mock()
    migration.op_client = Mock()
    with pytest.raises(LegacyJournalReplayError):
        migration.prepare_work_package(issue, 7)
    assert not migration.op_client.mock_calls
    assert not migration.logger.mock_calls


@pytest.mark.parametrize("existing", [True, False])
def test_legacy_migration_reports_blocked_journal_writes(monkeypatch, existing):
    monkeypatch.setattr(config, "jira_config", {"projects": ["HISTORY"]})
    monkeypatch.setattr(config, "migration_config", {"batch_size": 1})
    migration = WorkPackageMigration.__new__(WorkPackageMigration)
    migration.logger = Mock()
    migration.op_client = Mock()
    migration.project_mapping = {"HISTORY": {"jira_key": "HISTORY", "openproject_id": 8}}
    issue = SimpleNamespace(key="HISTORY-1", id="1000")
    migration._get_existing_work_packages = Mock(return_value={issue.key: {"id": 21}} if existing else {})
    migration._iter_all_project_issues = Mock(return_value=[issue])
    migration.prepare_work_package = Mock(return_value={"_rails_operations": [{"type": "journal"}]})
    result = migration.run()
    assert not result.success
    assert result.failed_count == 1
    assert "work_packages_content" in result.error
    assert not migration.op_client.mock_calls


def test_legacy_batch_refuses_to_replace_native_journals():
    migration = WorkPackageMigration.__new__(WorkPackageMigration)
    migration.op_client = Mock()
    assert migration._update_existing_work_packages_batch([]) == (0, 0)
    batch = [(SimpleNamespace(key="A-1"), {"id": 21}, 8)]
    with pytest.raises(LegacyJournalReplayError):
        migration._update_existing_work_packages_batch(batch)
    assert not migration.op_client.mock_calls


def test_confirmed_empty_history_is_a_read_only_rerun():
    migration = WorkPackageMigration.__new__(WorkPackageMigration)
    migration.op_client = Mock()
    issue = SimpleNamespace(
        key="P-1",
        raw={"changelog": {"histories": [], "total": 0}, "fields": {"comment": {"comments": [], "total": 0}}},
    )
    migration._update_existing_work_package(issue, {"id": 10}, 8)
    assert migration._update_existing_work_packages_batch([(issue, {"id": 10}, 8)]) == (0, 0)
    assert not migration.op_client.mock_calls


@pytest.mark.parametrize(("total", "comments"), [(1, []), (1, [{"body": "private-comment-marker"}])])
def test_legacy_create_refuses_comments_before_inspecting_body(total, comments):
    issue = {"fields": {"comment": {"total": total, "comments": comments}}}
    migration = WorkPackageMigration.__new__(WorkPackageMigration)
    migration.logger = Mock()
    migration.op_client = Mock()
    with pytest.raises(LegacyJournalReplayError):
        migration.prepare_work_package(issue, 7)
    assert not migration.op_client.mock_calls
    assert not migration.logger.mock_calls
