# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT
"""Regression checks for the legacy issue reader and its target defaults."""

import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import requests

from src.application.components import work_package_migration as legacy


def test_cache_and_change_detection_omit_private_text(tmp_path, monkeypatch):
    marker = "private-history-source-marker"
    private_item = {"field": "description", "toString": marker}
    public_item = {"field": "status", "from": "1", "to": "2", "toString": "Open"}
    source = SimpleNamespace(
        id="10",
        key="TEST-1",
        raw={
            "fields": {"comment": {"comments": [{"body": marker}]}},
            "changelog": {
                "histories": [
                    {"items": [{**private_item, "visibility": {"type": "role", "value": "agent"}}, public_item]},
                    {"jsdPublic": True, "items": [private_item]},
                ]
            },
        },
    )
    app = legacy.WorkPackageMigration.__new__(legacy.WorkPackageMigration)
    app.data_dir = tmp_path
    app.logger = Mock()
    app._project_latest_issue_ts = {}
    app._get_project_total_issues = Mock(return_value=None)
    app.iter_project_issues = Mock(return_value=[source])
    app._change_detection_projects = Mock(return_value=["TEST"])
    primary = tmp_path / "jira_issues_TEST.json"
    rows = app._extract_jira_issues("TEST")
    assert marker not in primary.read_text()
    assert public_item in rows[0]["changelog"]["histories"][0]["items"]
    original_write = legacy.write_issue_snapshot

    def fail_primary(rows, destination):
        if destination == primary:
            raise OSError("unable to write primary")
        original_write(rows, destination)

    monkeypatch.setattr(legacy, "write_issue_snapshot", fail_primary)
    app._extract_jira_issues("TEST")
    [backup] = tmp_path.glob("jira_issues_TEST_backup_*.json")
    assert json.loads(backup.read_text()) == rows
    current = app._get_current_entities_for_type("work_packages")
    assert marker not in json.dumps(current)
    assert marker not in str(app.logger.mock_calls)


def test_http_rate_limiting_retries_a_real_429_response(monkeypatch):
    app = legacy.WorkPackageMigration.__new__(legacy.WorkPackageMigration)
    app.jira_client = Mock()
    response = requests.Response()
    response.status_code = 429
    expected = [SimpleNamespace(id="1")]
    app.jira_client.jira.search_issues.side_effect = [requests.HTTPError(response=response), expected]
    pause = Mock()
    monkeypatch.setattr(time, "sleep", pause)
    assert (
        app._fetch_issues_with_retry(
            jql="project=TEST", start_at=0, max_results=20, fields=None, expand=None, project_key="TEST"
        )
        == expected
    )
    pause.assert_called_once_with(1.0)


def test_target_defaults_preserve_existing_values_and_normalize_invalid_dates():
    target = Mock()
    target.execute_large_query_to_json_file.side_effect = [[3, 4], [5, 6], [7], [8]]
    missing = {"status_id": "999", "start_date": "2025-01-02", "due_date": "2025-01-01"}
    present = {"project_id": 22, "type_id": 4, "status_id": "6", "priority_id": 11, "author_id": 12}
    legacy._apply_required_defaults([missing, present], project_id=21, op_client=target, fallback_admin_user_id=None)
    assert missing == {
        "project_id": 21,
        "type_id": 3,
        "status_id": 5,
        "priority_id": 7,
        "author_id": 8,
        "_links": {"type": {"href": "/api/v3/types/3"}},
        "start_date": "2025-01-02",
        "due_date": None,
    }
    assert present == {"project_id": 22, "type_id": 4, "status_id": "6", "priority_id": 11, "author_id": 12}


def test_incomplete_snapshots_never_replace_a_complete_file(tmp_path):
    import pytest

    from src.application.components.work_package_files import write_issue_snapshot

    target = tmp_path / "jira_issues_TEST.json"
    write_issue_snapshot([{"id": "old"}], target)
    with pytest.raises(TypeError):
        write_issue_snapshot([{"id": "new"}, {"unserializable": object()}], target)
    assert json.loads(target.read_text()) == [{"id": "old"}]
    backup = tmp_path / "jira_issues_TEST_backup_20261118.json"
    with pytest.raises(TypeError):
        write_issue_snapshot([{"id": "new"}, {"unserializable": object()}], backup)
    assert list(tmp_path.iterdir()) == [target]
