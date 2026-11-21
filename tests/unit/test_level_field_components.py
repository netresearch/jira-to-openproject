# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Per-issue field components on level work packages (ADR-009, step 5).

Each case runs for every component that writes with a mapping keyed by Jira issue ID, as
production stores it. The assertions read the work packages named in the component's write calls.
"""

import json
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.application.components.customfields_generic_migration import CustomFieldsGenericMigration
from src.application.components.estimates_migration import EstimatesMigration
from src.application.components.priority_migration import PriorityMigration
from src.application.components.story_points_migration import StoryPointsMigration
from src.application.components.wp_metadata_backfill_migration import WpMetadataBackfillMigration
from tests.unit.test_level_attachments import LIGHT as LEVEL_SEARCH
from tests.unit.test_level_attachments import LIVE
from tests.unit.test_level_work_packages import level_issues
from tests.unit.test_level_work_packages import setup as level_work_package_setup  # noqa: F401  (fixture)

COMPONENTS = {
    "customfields_generic": CustomFieldsGenericMigration,
    "priorities": PriorityMigration,
    "story_points": StoryPointsMigration,
    "estimates": EstimatesMigration,
    "wp_metadata_backfill": WpMetadataBackfillMigration,
}
ALL = pytest.mark.parametrize("component", sorted(COMPONENTS))
# The components that enable their custom fields per project.
ENABLING = {"customfields_generic", "story_points"}
LEVEL_OF = {10: "10000", 11: "10000", 12: "10000", 13: "10000", 20: "10001"}
# Ordinary entries as the skeleton writes them: keyed by Jira issue ID, without a project ID.
ORDINARY = {
    "1": {"jira_key": "APP-1", "openproject_id": 500, "project_key": "APP"},
    "2": {"jira_key": "APP-2", "openproject_id": 501, "project_key": "APP"},
}
WORK_PACKAGES = re.compile(r"\b(500|501|777|900|901|902|903)\b")
READS = {"execute_query_to_json_file", "ensure_wp_custom_field_id", "ensure_custom_field", "get_issue_priorities"}
ENTERPRISE_LIVE = {
    "field": True,
    "work_packages": [900, 901, 902],
    "keys": [[900, "APP-10"], [901, "APP-11"], [902, "APP-12"]],
}


@pytest.fixture
def level_setup(level_work_package_setup):  # noqa: F811 (the imported fixture, requested by name)
    """The reports, mappings and switch of the level work package tests."""
    return level_work_package_setup


def _fields(number, level):
    fields = {
        "project": {"key": "APP"},
        "priority": {"name": "High", "id": "2"},
        "customfield_10001": f"text {number}",
        "customfield_10016": number,
        "timeoriginalestimate": 3600 * number,
        "timeestimate": 1800,
        "assignee": {"name": "alice"},
        "creator": {"name": "alice"},
        "reporter": {"name": "alice"},
        "created": "2026-01-01T00:00:00.000+0000",
        "updated": "2026-01-02T00:00:00.000+0000",
    }
    return fields | ({"security": {"id": level}} if level else {})


def _issue(number, level=None):
    raw = _fields(number, level)
    namespace = json.loads(json.dumps(raw), object_hook=lambda d: SimpleNamespace(**d))
    return SimpleNamespace(key=f"APP-{number}", id=str(number), raw={"fields": raw}, fields=namespace)


class FakeJira:
    """Answer the level searches and the ordinary batch read; record every call."""

    def __init__(self, moved=(), omitted=(), fails=False):
        self.base_url = "https://jira.local"
        self.moved, self.omitted, self.fails = set(moved), set(omitted), fails
        self.searches, self.batches = [], []
        self.get_projects = MagicMock(return_value=[{"key": "APP"}])
        self.get_priorities = MagicMock(return_value=[{"name": "High", "id": "2"}])
        self.jira = MagicMock()
        self.jira.search_issues.side_effect = self.search

    def search(self, jql, startAt, maxResults, **kwargs):
        assert jql == LEVEL_SEARCH, jql
        self.searches.append(kwargs.get("fields"))
        if kwargs.get("fields") == "security":
            return level_issues()[startAt : startAt + maxResults]
        assert kwargs.get("fields") == "*all"
        if self.fails:
            raise OSError("search unavailable")
        rows = [
            _issue(n, "10001" if n in self.moved else level) for n, level in LEVEL_OF.items() if n not in self.omitted
        ]
        return rows[startAt : startAt + maxResults]

    def batch_get_issues(self, keys):
        self.batches.append(list(keys))
        return {key: _issue(int(key.split("-")[1])) for key in keys}


def _op(live):
    op = MagicMock()
    op.execute_query_to_json_file.side_effect = lambda script: json.loads(json.dumps(live))
    op.ensure_wp_custom_field_id.return_value = 55
    op.ensure_custom_field.return_value = {"id": 56}
    op.get_issue_priorities.return_value = [{"name": "High", "id": 9}]
    op.execute_query.return_value = True
    op.batch_update_work_packages.side_effect = lambda updates: {"updated": len(updates), "failed": 0}

    def script(_script, data):
        if isinstance(data, dict):
            return {"data": True}
        counters = {"updated_assignee": 0, "updated_cf": len(data), "failed": 0, "skipped": 0, "wp_missing": 0}
        return {"status": "success", "data": counters}

    op.execute_script_with_data.side_effect = script
    return op


def _run(level_setup, component, *, live=None, ordinary=None, level_map=None, runs=1, **kw):
    moved, omitted, fails = kw.pop("moved", ()), kw.pop("omitted", ()), kw.pop("fails", False)
    mappings, _ = level_setup(**kw)
    mappings.data["work_package"] = dict(ORDINARY if ordinary is None else ordinary)
    mappings.data["user"] = {"alice": {"openproject_id": 5}}
    if level_map is not None:
        mappings.data["security_level_work_package"] = level_map
    jira, op = FakeJira(moved, omitted, fails), _op(LIVE if live is None else live)
    migration = COMPONENTS[component](jira_client=jira, op_client=op)
    results, written = [], []
    for _ in range(runs):
        start = len(op.method_calls)
        results.append(migration.run())
        written.append(_written(op.method_calls[start:]))
    return SimpleNamespace(result=results[-1], results=results, written=written, jira=jira, op=op, mappings=mappings)


def _written(calls):
    """The work packages named in the write calls of one run."""
    return {
        int(n) for name, args, kwargs in calls if name not in READS for n in WORK_PACKAGES.findall(repr((args, kwargs)))
    }


def _enabled(outcome):
    return {pid for call in outcome.op.enable_custom_field_for_projects.call_args_list for pid in call.args[1]}


def _refused(outcome):
    return outcome.result.details["security_level"]["refused"]


@ALL
def test_switch_off_writes_the_ordinary_work_packages_with_the_ordinary_calls(level_setup, component):
    outcome = _run(level_setup, component, on=False)
    assert outcome.result.success, outcome.result.message
    assert outcome.written == [{500, 501}]
    assert outcome.jira.searches == []
    assert outcome.jira.batches and all(keys == ["APP-1", "APP-2"] for keys in outcome.jira.batches)
    outcome.op.execute_query_to_json_file.assert_not_called()
    assert _enabled(outcome) == set()
    assert "security_level" not in outcome.result.details


@ALL
def test_selected_level_issues_are_written_into_their_sub_project(level_setup, component):
    outcome = _run(level_setup, component, runs=2)
    assert [r.success for r in outcome.results] == [True, True], outcome.result.message
    # A repeated run writes the same work packages.
    assert outcome.written == [{500, 501, 900, 901}] * 2
    # The level issues come from one level search per run, never from the batch read.
    assert outcome.jira.searches == ["security", "security", "*all", "*all"] * 2
    assert all(keys == ["APP-1", "APP-2"] for keys in outcome.jira.batches)
    # A level value enables its field in the sub-project only, never in the parent (7).
    assert _enabled(outcome) == ({100} if component in ENABLING else set())
    assert outcome.result.details["security_level"] == {"work_packages": 2, "refused": 0}


def test_level_work_packages_get_no_assignee_from_the_backfill(level_setup):
    outcome = _run(level_setup, "wp_metadata_backfill")
    records = [r for call in outcome.op.execute_script_with_data.call_args_list for r in call.args[1]]
    assignees = {r["work_package_id"]: r["assigned_to_id"] for r in records}
    assert assignees == {500: 5, 501: 5, 900: None, 901: None}
    assert all(r["custom_fields"] for r in records)


@ALL
def test_on_enterprise_a_per_issue_work_package_gets_its_values(level_setup, component):
    outcome = _run(level_setup, component, edition="enterprise", live=ENTERPRISE_LIVE)
    assert outcome.result.success, outcome.result.message
    assert outcome.written == [{500, 501, 900, 901, 902}]


@ALL
@pytest.mark.parametrize(
    ("options", "written", "refused"),
    [
        pytest.param(
            {"live": {"field": True, "work_packages": [901], "keys": [[901, "APP-11"]]}}, {901}, 1, id="moved"
        ),
        pytest.param({"live": {"field": True}}, set(), 2, id="unreadable-sub-project"),
        pytest.param({"moved": (10,)}, {901}, 1, id="level-changed"),
        pytest.param({"omitted": (10,)}, {901}, 1, id="omitted-by-search"),
        pytest.param({"fails": True}, set(), 2, id="level-search-fails"),
        pytest.param(
            {"level_map": {"13": {"jira_key": "APP-13", "openproject_id": 903, "security_level_project": "APP:10000"}}},
            {900, 901},
            1,
            id="unselected-issue",
        ),
        pytest.param(
            {"ordinary": {**ORDINARY, "10": {"jira_key": "APP-10", "openproject_id": 777, "project_key": "APP"}}},
            {901},
            1,
            id="ordinary-entry-of-a-level-issue",
        ),
    ],
)
def test_a_refused_level_issue_gets_nothing(level_setup, component, options, written, refused):
    outcome = _run(level_setup, component, **options)
    assert not outcome.result.success
    assert _refused(outcome) == refused
    # A refused issue is one failure; it is not counted again as a missing issue.
    assert outcome.result.failed == refused
    assert outcome.written == [{500, 501} | written]
    assert all(keys == ["APP-1", "APP-2"] for keys in outcome.jira.batches)


@ALL
def test_refusals_are_reported_on_an_early_return(level_setup, component):
    outcome = _run(level_setup, component, live={"field": True}, ordinary={})
    assert not outcome.result.success
    assert outcome.written == [set()]
    assert outcome.jira.batches == []
    assert _refused(outcome) == 2


@ALL
def test_a_missing_report_stops_before_any_read(level_setup, component):
    outcome = _run(level_setup, component, reports=False)
    assert not outcome.result.success
    assert outcome.result.errors == ["security_level"]
    assert outcome.written == [set()]
    assert outcome.jira.batches == []
