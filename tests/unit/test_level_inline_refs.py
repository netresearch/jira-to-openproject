# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Inline attachment references of level work packages (ADR-009)."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components.inline_refs_migration import InlineRefsMigration
from tests.unit.test_level_attachments import LIGHT as LEVEL_SEARCH
from tests.unit.test_level_attachments import LIVE
from tests.unit.test_level_work_packages import level_issues
from tests.unit.test_level_work_packages import setup as level_work_package_setup  # noqa: F401  (fixture)

ORDINARY = {
    "1": {"jira_key": "APP-1", "openproject_id": 500, "project_key": "APP"},
    "2": {"jira_key": "APP-2", "openproject_id": 501, "project_key": "APP"},
}


@pytest.fixture
def level_setup(level_work_package_setup):  # noqa: F811 (the imported fixture, requested by name)
    """The reports, mappings and switch of the level work package tests."""
    return level_work_package_setup


class FakeOpenProject:
    """Answer the live sub-project read; record the work package IDs of every rewrite call."""

    def __init__(self, live=None):
        self.live = LIVE if live is None else live
        self.live_reads, self.rewrites = [], []

    def execute_query_to_json_file(self, script):
        values = json.loads(script.split("\n")[1])
        self.live_reads.append(values)
        return json.loads(json.dumps(self.live))

    def execute_script_with_data(self, script, data):
        self.rewrites.append(list(data))
        return {"updated": len(data), "failed": 0}


def _run(level_setup, *, live=None, ordinary=None, level_map=None, runs=1, **kw):
    mappings, _ = level_setup(**kw)
    mappings.data["work_package"] = dict(ORDINARY if ordinary is None else ordinary)
    if level_map is not None:
        mappings.data["security_level_work_package"] = level_map
    jira = Mock()
    jira.get_projects.return_value = [{"key": "APP"}]
    jira.jira.search_issues.side_effect = lambda jql, startAt, maxResults, **_: (
        level_issues()[startAt : startAt + maxResults] if jql == LEVEL_SEARCH else pytest.fail(jql)
    )
    op = FakeOpenProject(live)
    migration = InlineRefsMigration(jira_client=jira, op_client=op)
    results = [migration.run() for _ in range(runs)]
    return SimpleNamespace(result=results[-1], results=results, jira=jira, op=op, mappings=mappings)


def test_switch_off_rewrites_the_ordinary_work_packages_only(level_setup):
    outcome = _run(level_setup, on=False)
    assert outcome.result.success
    assert outcome.op.rewrites == [[500, 501]]
    outcome.jira.jira.search_issues.assert_not_called()
    assert outcome.op.live_reads == []
    assert "security_level" not in outcome.result.details


def test_level_work_packages_are_rewritten_in_their_sub_project(level_setup):
    outcome = _run(level_setup, runs=2)
    assert [r.success for r in outcome.results] == [True, True]
    # The same work packages on a repeated run: the ordinary ones and those the sub-project holds.
    assert outcome.op.rewrites == [[500, 501, 900, 901]] * 2
    assert outcome.result.details["security_level"] == {"work_packages": 2, "refused": 0}


@pytest.mark.parametrize(
    ("options", "rewritten"),
    [
        pytest.param({"live": {"field": True, "work_packages": [901], "keys": [[901, "APP-11"]]}}, [901], id="moved"),
        pytest.param({"live": {"field": True}}, [], id="unreadable-sub-project"),
        pytest.param(
            {"level_map": {"13": {"jira_key": "APP-13", "openproject_id": 903, "security_level_project": "APP:10000"}}},
            [900, 901],
            id="unselected-issue",
        ),
        pytest.param(
            {"ordinary": {**ORDINARY, "10": {"jira_key": "APP-10", "openproject_id": 777, "project_key": "APP"}}},
            [901],
            id="ordinary-entry-of-a-level-issue",
        ),
    ],
)
def test_a_refused_level_issue_is_not_rewritten(level_setup, options, rewritten):
    outcome = _run(level_setup, **options)
    assert not outcome.result.success
    assert outcome.result.details["security_level"]["refused"] == 1 + (options.get("live") == {"field": True})
    assert outcome.op.rewrites == [[500, 501, *rewritten]]


def test_refusals_are_reported_when_there_is_nothing_to_rewrite(level_setup):
    outcome = _run(level_setup, live={"field": True}, ordinary={})
    assert not outcome.result.success
    assert outcome.op.rewrites == []
    assert outcome.result.details["security_level"] == {"work_packages": 0, "refused": 2}
    assert outcome.result.failed == 2


def test_a_missing_report_stops_before_any_write(level_setup):
    outcome = _run(level_setup, reports=False)
    assert not outcome.result.success
    assert outcome.result.errors == ["security_level"]
    assert outcome.op.rewrites == []
