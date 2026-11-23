# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""A failed watcher read is retried once on a transient refusal and otherwise counts as failed.

In a full run, single watcher reads were refused with HTTP 401 about every 30 minutes; the
content step and the watcher step then treated the failed read as an issue without watchers.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.application.components.watcher_migration import WatcherMigration
from src.infrastructure.jira import jira_issue_service
from src.infrastructure.jira.jira_client import JiraApiError
from src.infrastructure.jira.jira_issue_service import JiraIssueService
from tests.unit.test_work_package_content_comment_idempotency import _build_mig


class _HttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status_code = status


class _Jira:
    """Answer watcher reads from a script of outcomes: an int raises that HTTP status."""

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def watchers(self, key: str) -> object:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, int):
            raise _HttpError(outcome)
        return outcome


def _service(jira: _Jira) -> JiraIssueService:
    return JiraIssueService(SimpleNamespace(jira=jira))  # type: ignore[arg-type]


def _watcher_list(*names: str) -> object:
    return SimpleNamespace(
        watchers=[
            SimpleNamespace(name=n, key=n, accountId=None, displayName=n, emailAddress=None, active=True) for n in names
        ]
    )


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jira_issue_service.time, "sleep", lambda _s: None)


@pytest.mark.parametrize("status", [401, 403])
def test_a_transient_refusal_is_retried_once(status: int) -> None:
    jira = _Jira(status, _watcher_list("alice"))

    watchers = _service(jira).get_issue_watchers("APP-1")

    assert jira.calls == 2
    assert [w["name"] for w in watchers] == ["alice"]


def test_a_second_refusal_raises() -> None:
    jira = _Jira(401, 401)

    with pytest.raises(JiraApiError):
        _service(jira).get_issue_watchers("APP-1")
    assert jira.calls == 2


def test_another_status_is_not_retried() -> None:
    jira = _Jira(500)

    with pytest.raises(JiraApiError):
        _service(jira).get_issue_watchers("APP-1")
    assert jira.calls == 1


@pytest.fixture
def _no_mappings(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.config as cfg

    monkeypatch.setattr(cfg, "mappings", SimpleNamespace(get_mapping=lambda _name: {}), raising=False)


def _issue() -> MagicMock:
    issue = MagicMock()
    issue.key = "APP-1"
    issue.fields.description = None
    issue.raw = {"fields": {}}
    return issue


def test_the_content_step_marks_a_failed_watcher_read(tmp_path: Path, _no_mappings: None) -> None:
    mig = _build_mig(tmp_path)
    mig.jira_client.select_comments_for_migration.return_value.comments = []
    mig.jira_client.get_issue_watchers.side_effect = JiraApiError("HTTP 401")

    collected = mig._collect_content_for_issue(_issue(), wp_id=5)

    assert collected["watcher_read_failed"] is True
    assert collected["watchers"] == []


def test_a_failed_watcher_read_counts_in_the_batch(tmp_path: Path, _no_mappings: None) -> None:
    mig = _build_mig(tmp_path)
    items = [
        {
            "wp_id": 1,
            "description_update": None,
            "custom_field_updates": {},
            "comments": [],
            "watchers": [],
            "watcher_read_failed": True,
        },
        {"wp_id": 2, "description_update": None, "custom_field_updates": {}, "comments": [], "watchers": []},
    ]

    results = mig._bulk_process_collected_content(items)

    assert results["watcher_reads_failed"] == 1


def test_a_failed_watcher_read_fails_the_content_component(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mig = _build_mig(tmp_path)
    mig.work_package_mapping = {"1": {"jira_key": "APP-1", "openproject_id": 1}}
    monkeypatch.setattr(
        mig,
        "_migrate_content",
        lambda: {
            "total_updated": 1,
            "total_skipped": 0,
            "total_failed": 0,
            "descriptions_updated": 0,
            "comments_migrated": 0,
            "watcher_reads_failed": 1,
        },
    )

    result = mig.run()

    assert result.success is False
    assert result.failed == 1
    assert result.details["watcher_reads_failed"] == 1


def test_the_single_issue_path_raises_on_a_failed_read(tmp_path: Path, _no_mappings: None) -> None:
    mig = _build_mig(tmp_path)
    mig.jira_client.get_issue_watchers.side_effect = JiraApiError("HTTP 401")

    with pytest.raises(JiraApiError):
        mig._populate_watchers(_issue(), wp_id=5)


class _Op:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def bulk_add_watchers(self, rows: list[dict]) -> dict:
        self.rows.extend(rows)
        return {"created": len(rows), "skipped": 0, "failed": 0}


@pytest.fixture
def _watcher_maps(monkeypatch: pytest.MonkeyPatch) -> None:
    maps = {
        "work_package": {"J1": {"openproject_id": 10}, "J2": {"openproject_id": 11}},
        "user": {"alice": {"openproject_id": 5}},
    }
    import src.config as cfg

    monkeypatch.setattr(
        cfg,
        "mappings",
        SimpleNamespace(get_mapping=lambda n: maps.get(n, {}), set_mapping=lambda *_: None),
        raising=False,
    )


class _FailingFor:
    def __init__(self, failing: str) -> None:
        self.failing = failing

    def get_issue_watchers(self, key: str) -> list[dict]:
        if key == self.failing:
            raise JiraApiError("HTTP 401")
        return [{"name": "alice"}]


def _watcher_step(tmp_path: Path, cache: str) -> tuple[WatcherMigration, _Op]:
    op = _Op()
    step = WatcherMigration(jira_client=_FailingFor("J2"), op_client=op)  # type: ignore[arg-type]
    data = tmp_path / "data"
    data.mkdir()
    (data / "jira_issues_cache.json").write_text(cache)
    step.data_dir = data
    return step, op


def test_the_watcher_step_fails_on_an_unread_issue(tmp_path: Path, _watcher_maps: None) -> None:
    step, op = _watcher_step(tmp_path, '{"J1": {"key": "J1"}, "J2": {"key": "J2"}}')

    result = step.run()

    assert result.success is False
    assert result.details["read_failures"] == ["J2"]
    assert [row["work_package_id"] for row in op.rows] == [10]


def test_the_watcher_step_uses_embedded_watchers_when_the_read_fails(tmp_path: Path, _watcher_maps: None) -> None:
    step, _op = _watcher_step(tmp_path, '{"J1": {"key": "J1"}}')
    embedded = SimpleNamespace(fields=SimpleNamespace(watches=SimpleNamespace(watchers=[{"name": "alice"}])))

    assert step._read_watchers("J2", embedded) == [{"name": "alice"}]
    assert (
        step._read_watchers("J2", SimpleNamespace(fields=SimpleNamespace(watches=SimpleNamespace(watchers=[])))) is None
    )
