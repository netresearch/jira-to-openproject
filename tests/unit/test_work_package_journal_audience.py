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
def test_ordinary_journals_keep_only_eligible_history(restriction):
    marker = "private-history-marker"
    held = {"field": "description", "fromString": "old", "toString": marker}
    visible = {"field": "summary", "fromString": "Before", "toString": "After"}
    histories = [
        {"created": "2024-01-01T09:00:00Z", "items": [held], **restriction},
        {"created": "2024-01-02T09:00:00Z", "items": [dict(held, **restriction), visible]},
    ]
    migration = WorkPackageMigration.__new__(WorkPackageMigration)
    migration.logger = Mock()
    migration.user_mapping = {}
    migration.enhanced_audit_trail_migrator = Mock()
    migration.enhanced_audit_trail_migrator.extract_changelog_from_issue.return_value = histories
    issue = SimpleNamespace(key="JOURNAL-1")
    operations = migration._journal_entries(issue)
    assert len(operations) == 1
    assert operations[0]["data"]["items"] == [visible]
    assert marker not in repr(operations)
    assert marker not in repr(migration.logger.mock_calls)


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
