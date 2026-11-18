# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Regression coverage for Jira Server's configurable search page ceiling."""

from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import config
from src.application.components.attachments_migration import AttachmentsMigration
from src.application.components.work_package_content_migration import WorkPackageContentMigration
from src.application.components.work_package_migration import WorkPackageMigration
from src.application.components.work_package_skeleton_migration import WorkPackageSkeletonMigration
from src.infrastructure.jira.jira_client import JiraApiError
from src.infrastructure.jira.jira_issue_service import JiraIssueService


class Page(list):
    """Match the pagination attributes exposed by python-jira ResultList."""

    def __init__(self, items, offset, total):
        super().__init__(items)
        self.startAt = offset
        self.total = len(self) if total is None else total
        self.isLast = False


@pytest.fixture(params=["skeleton", "content", "project", "stream", "legacy", "incremental"])
def reader(request, monkeypatch):
    monkeypatch.delenv("J2O_MAX_ISSUES", raising=False)
    monkeypatch.delenv("J2O_TEST_ISSUES", raising=False)
    monkeypatch.setenv("J2O_FAST_FORWARD", "0")
    monkeypatch.setattr(config, "migration_config", {"batch_size": 1000})
    client = Mock(batch_size=1000)
    kind = request.param
    if kind in {"project", "stream"}:
        service = JiraIssueService(client)
        read = service.get_all_issues_for_project if kind == "project" else service.stream_all_issues_for_project
    else:
        cls = {"skeleton": WorkPackageSkeletonMigration, "content": WorkPackageContentMigration}.get(
            kind,
            WorkPackageMigration,
        )
        component = object.__new__(cls)
        component.jira_client = client
        component.logger = Mock()
        component._project_latest_issue_ts = {}
        read = component._iter_all_project_issues if kind == "incremental" else component.iter_project_issues
    return read, client.jira.search_issues


@pytest.mark.parametrize("metadata", ["total", "sdk_without_total", "plain_list"])
def test_server_caps_each_page(reader, metadata):
    read, search = reader
    issues = [SimpleNamespace(key=f"TEST-{i}") for i in range(1, 6)]

    def capped_search(_jql, *, startAt, maxResults, **kwargs):
        rows = issues[startAt : startAt + min(2, maxResults)]
        if metadata == "plain_list":
            return rows
        return Page(rows, startAt, len(issues) if metadata == "total" else None)

    search.side_effect = capped_search
    assert list(read("TEST")) == issues
    offsets = [call.kwargs["startAt"] for call in search.call_args_list]
    assert offsets == ([0, 2, 4] if metadata == "total" else [0, 2, 4, 5])


@pytest.mark.parametrize("failure", ["read_error", "early_empty", "repeated_page", "overlap", "early_last"])
def test_partial_project_is_an_error(reader, failure):
    read, search = reader
    first = Page([SimpleNamespace(key="TEST-1")], 0, 5)
    early_last = Page([SimpleNamespace(key="TEST-2")], 1, 5)
    early_last.isLast = True
    second = {"read_error": OSError("connection lost"), "early_empty": Page([], 1, 5), "repeated_page": first}
    second["overlap"] = Page(first, 1, 5)
    second["early_last"] = early_last
    search.side_effect = [first, second[failure]]
    with pytest.raises(JiraApiError, match="TEST.*startAt=1"):
        list(read("TEST"))


def test_empty_project(reader):
    read, search = reader
    search.return_value = Page([], 0, 0)
    assert list(read("TEST")) == []
    assert search.call_count == 1


@pytest.mark.parametrize("cls", [WorkPackageSkeletonMigration, WorkPackageContentMigration])
@pytest.mark.parametrize("limit", [0, 3])
def test_pilot_limit_stops_at_exact_issue(cls, limit, monkeypatch):
    monkeypatch.setenv("J2O_MAX_ISSUES", str(limit))
    client = Mock()
    component = object.__new__(cls)
    component.jira_client = client
    component.logger = Mock()
    issues = [SimpleNamespace(key=f"TEST-{i}") for i in range(6)]
    client.jira.search_issues.side_effect = lambda _, startAt, maxResults, **kwargs: Page(
        issues[startAt : startAt + min(2, maxResults)],
        startAt,
        len(issues),
    )
    assert list(component.iter_project_issues("TEST")) == issues[:limit]
    assert client.jira.search_issues.call_count == (0 if limit == 0 else 2)


@pytest.mark.parametrize("missing", [True, False])
def test_attachment_search_covers_requested_keys_and_reports_omissions(missing):
    component = object.__new__(AttachmentsMigration)
    component.jira_client = Mock()
    component._loss_counters = Counter()
    component.logger = Mock()
    component._save_attachment_mapping = Mock()
    keys = ["TEST-1", "TEST-2", "TEST-3"]
    att = SimpleNamespace(id="17", filename="notes.txt", size=30, content="https://jira.example/17")
    rows = [SimpleNamespace(key=key, fields=SimpleNamespace(attachment=[att])) for key in keys]
    if missing:
        rows.pop(1)
    search = component.jira_client.jira.search_issues
    search.side_effect = lambda _, startAt, maxResults, **kwargs: Page(rows[startAt : startAt + 1], startAt, len(rows))
    extracted = component._extract_batch(keys)
    assert list(extracted) == [row.key for row in rows]
    assert component._loss_counters["extract_unread_issues"] == int(missing)
    component._process_batch_end_to_end = Mock(return_value=(len(rows), 0, {}))
    result = component._run_per_project_loop(
        by_project={"TEST": keys},
        batch_size=50,
        total_updated=0,
        total_failed=0,
        all_mappings={},
    )
    assert result.success is (not missing)
    assert result.failed == int(missing)


def test_paginated_dict_rows_cannot_repeat_an_earlier_nonadjacent_issue():
    from src.infrastructure.jira.issue_pagination import iter_issue_pages

    pages = [[{"key": "X-1"}], [{"key": "X-2"}], [{"key": "X-1"}]]
    with pytest.raises(JiraApiError, match="duplicate issue"):
        list(iter_issue_pages("X", lambda offset, size: pages[offset], 1))


@pytest.mark.parametrize(
    ("cls", "method"),
    [
        (WorkPackageMigration, "_iter_all_project_issues"),
        (WorkPackageMigration, "iter_project_issues"),
        (WorkPackageContentMigration, "iter_project_issues"),
    ],
)
def test_normal_work_package_reads_require_an_empty_security_level(cls, method, monkeypatch):
    monkeypatch.setenv("J2O_FAST_FORWARD", "0")
    monkeypatch.setenv("J2O_TEST_ISSUES", "TEST-1,TEST-2")
    monkeypatch.delenv("J2O_MAX_ISSUES", raising=False)
    migration = object.__new__(cls)
    migration.jira_client = Mock()
    migration.logger = Mock()
    migration._project_latest_issue_ts = {}
    allowed = SimpleNamespace(key="TEST-1", raw={"fields": {}})
    restricted = SimpleNamespace(key="TEST-2", raw={"fields": {"security": {"id": "7"}}})
    search = migration.jira_client.jira.search_issues
    search.return_value = Page([allowed, restricted], 0, 2)
    search.return_value.isLast = True
    assert list(getattr(migration, method)("TEST")) == [allowed]
    assert 'project = "TEST" AND level IS EMPTY' in search.call_args.args[0]
    assert search.call_args.kwargs["fields"] == "*all"
