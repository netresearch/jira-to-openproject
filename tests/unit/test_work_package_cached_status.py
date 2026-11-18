# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components.work_package_migration import LegacyJournalReplayError, WorkPackageMigration
from src.utils.markdown_converter import MarkdownConverter


@pytest.fixture
def migration():
    app = WorkPackageMigration.__new__(WorkPackageMigration)
    app.logger = Mock()
    app.start_date_fields = []
    app._project_latest_issue_ts = {}
    app._j2o_wp_cf_ids = {}
    app._j2o_wp_cf_ids_full = {}
    app.issue_type_id_mapping = {"1": 4}
    app._apply_source_users = Mock()
    app._apply_source_timestamps = Mock()
    return app


def source_issue():
    status = {"id": "5", "name": "Ready"}
    return SimpleNamespace(
        key="TEST-1",
        id="101",
        fields=SimpleNamespace(
            summary="A test issue",
            issuetype=SimpleNamespace(id="1"),
            status=SimpleNamespace(**status),
        ),
        raw={"fields": {"status": status, "summary": "A test issue"}},
    )


@pytest.mark.parametrize("entry", [9, "9", {"openproject_id": "9"}, {"id": 9}])
def test_live_flat_and_snapshot_status_match(migration, entry):
    migration.status_mapping = {"5": entry}
    issue = source_issue()
    cached = migration._snapshot_issue(issue, "TEST")
    assert "changelog" not in cached
    assert migration.prepare_work_package(issue, 4)["status_id"] == 9
    assert migration.prepare_work_package(cached, 4)["status_id"] == 9
    assert migration.prepare_work_package({"status": issue.raw["fields"]["status"]}, 4)["status_id"] == 9


def test_old_snapshot_with_null_history_uses_unmapped_status_fallback(migration):
    migration.status_mapping = {}
    cached = {"key": "TEST-1", "changelog": None, "status": {"id": "missing"}}
    result = migration.prepare_work_package(cached, 4)
    assert result["status_id"] == 1
    assert "start_date" not in result
    assert migration._extract_changelog_histories(SimpleNamespace(raw={"changelog": None})) == []


def test_nonempty_cached_history_is_rejected_before_projection(migration):
    cached = {"key": "TEST-1", "changelog": {"histories": [{"items": [{"field": "status"}]}]}}
    with pytest.raises(LegacyJournalReplayError):
        migration.prepare_work_package(cached, 4)


def test_persisted_project_snapshot_retains_summary_description_and_type(migration, tmp_path):
    issue = source_issue()
    issue.raw["fields"].update(
        summary="Titel München",
        description="First line\n\nSecond line",
        issuetype={"id": "1", "name": "Story"},
    )
    migration.data_dir = tmp_path
    migration.status_mapping = {"5": 9}
    migration.markdown_converter = MarkdownConverter()
    migration._get_project_total_issues = Mock(return_value=None)
    migration.iter_project_issues = Mock(return_value=[issue])

    migration._extract_jira_issues("TEST")
    [cached] = migration._load_from_json(Path("jira_issues_TEST.json"))
    result = migration.prepare_work_package(cached, 4)

    assert result["subject"] == "Titel München"
    assert result["description"] == "Jira Issue: TEST-1\n\nFirst line\n\nSecond line"
    assert result["type_id"] == 4
    assert result["status_id"] == 9


def test_explicit_flat_fields_take_precedence_over_nested_issue_fields(migration):
    cached = {
        "key": "TEST-1",
        "summary": "Flat title",
        "description": "",
        "issue_type": {"id": "1"},
        "fields": {"summary": "Older title", "description": "Older description", "issuetype": {"id": "99"}},
    }
    result = migration.prepare_work_package(cached, 4)
    assert result["subject"] == "Flat title"
    assert result["description"] == "Jira Issue: TEST-1\n\n"
    assert result["type_id"] == 4
