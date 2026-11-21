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
from src.application.components.labels_migration import LabelsMigration
from src.application.components.native_tags_migration import NativeTagsMigration
from src.application.components.priority_migration import PriorityMigration
from src.application.components.remote_links_migration import RemoteLinksMigration
from src.application.components.resolution_migration import ResolutionMigration
from src.application.components.security_levels_migration import SecurityLevelsMigration
from src.application.components.story_points_migration import StoryPointsMigration
from src.application.components.votes_migration import VotesMigration
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
    # Fixed for a mapping keyed by Jira issue ID (#498).
    "labels": LabelsMigration,
    "votes": VotesMigration,
    "native_tags": NativeTagsMigration,
    "remote_links": RemoteLinksMigration,
    "resolution": ResolutionMigration,
    "security_levels": SecurityLevelsMigration,
}
ALL = pytest.mark.parametrize("component", sorted(COMPONENTS))
# The components that enable their custom fields per project.
ENABLING = {"customfields_generic", "story_points", "labels", "votes", "resolution", "security_levels"}
# A Tag is an instance-wide record, so ``native_tags`` writes no level work package.
INSTANCE_WIDE = {"native_tags"}
# With the switch off, an issue with a level is an ordinary issue; its batch read carries the level.
ORDINARY_LEVEL = "10002"
# With the switch on, an ordinary issue has no level, so ``security_levels`` has nothing to write for it.
LEVEL_ONLY = {"security_levels"}
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
        "labels": [f"label-{number}"],
        "votes": {"votes": 7},
        "resolution": {"name": "Fixed", "id": "1"},
        "remotelinks": [{"object": {"url": f"https://example.org/{number}", "title": f"Link {number}"}}],
    }
    return fields | ({"security": {"id": level, "name": f"Level {level}"}} if level else {})


def _issue(number, level=None):
    raw = _fields(number, level)
    namespace = json.loads(json.dumps(raw), object_hook=lambda d: SimpleNamespace(**d))
    return SimpleNamespace(key=f"APP-{number}", id=str(number), raw={"fields": raw}, fields=namespace)


class FakeJira:
    """Answer the level searches and the ordinary batch read; record every call."""

    def __init__(self, moved=(), omitted=(), fails=False, batch_level=None):
        self.base_url = "https://jira.local"
        self.batch_level = batch_level
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
        return {key: _issue(int(key.split("-")[1]), self.batch_level) for key in keys}


def _op(live):
    op = MagicMock()
    op.execute_query_to_json_file.side_effect = lambda script: json.loads(json.dumps(live))
    op.ensure_wp_custom_field_id.return_value = 55
    op.ensure_custom_field.return_value = {"id": 56}
    op.get_issue_priorities.return_value = [{"name": "High", "id": 9}]
    op.execute_query.return_value = True
    op.batch_update_work_packages.side_effect = lambda updates: {"updated": len(updates), "failed": 0}
    op.bulk_set_wp_custom_field_values.side_effect = lambda rows: {"updated": len(rows), "failed": 0}
    op.bulk_upsert_wp_description_sections.side_effect = lambda rows: {"updated": len(rows), "failed": 0}

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
    batch_level = None if kw.get("on", True) else ORDINARY_LEVEL
    jira, op = FakeJira(moved, omitted, fails, batch_level), _op(LIVE if live is None else live)
    migration = COMPONENTS[component](jira_client=jira, op_client=op)
    results, written = [], []
    for _ in range(runs):
        start = len(op.method_calls)
        results.append(migration.run())
        written.append(_written(op.method_calls[start:]))
    return SimpleNamespace(
        result=results[-1] if results else None, results=results, written=written, jira=jira, op=op, mappings=mappings
    )


def _written(calls):
    """The work packages named in the write calls of one run."""
    return {
        int(n) for name, args, kwargs in calls if name not in READS for n in WORK_PACKAGES.findall(repr((args, kwargs)))
    }


def _enabled(outcome):
    return {pid for call in outcome.op.enable_custom_field_for_projects.call_args_list for pid in call.args[1]}


def _refused(outcome):
    return outcome.result.details["security_level"]["refused"]


def _expected(component, level_work_packages):
    """The work packages a component writes with the switch on."""
    ordinary = set() if component in LEVEL_ONLY else {500, 501}
    return ordinary | (set() if component in INSTANCE_WIDE else set(level_work_packages))


@ALL
def test_switch_off_writes_the_ordinary_work_packages_with_the_ordinary_calls(level_setup, component):
    outcome = _run(level_setup, component, on=False)
    assert outcome.result.success, outcome.result.message
    assert outcome.written == [{500, 501}]
    assert outcome.jira.searches == []
    assert outcome.jira.batches
    assert all(keys == ["APP-1", "APP-2"] for keys in outcome.jira.batches)
    outcome.op.execute_query_to_json_file.assert_not_called()
    assert _enabled(outcome) == set()
    assert "security_level" not in outcome.result.details


@ALL
def test_selected_level_issues_are_written_into_their_sub_project(level_setup, component):
    outcome = _run(level_setup, component, runs=2)
    assert [r.success for r in outcome.results] == [True, True], outcome.result.message
    # A repeated run writes the same work packages.
    assert outcome.written == [_expected(component, {900, 901})] * 2
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
    assert outcome.written == [_expected(component, {900, 901, 902})]


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
    assert outcome.written == [_expected(component, written)]
    assert all(keys == ["APP-1", "APP-2"] for keys in outcome.jira.batches)


@ALL
def test_refusals_are_reported_on_an_early_return(level_setup, component):
    outcome = _run(level_setup, component, live={"field": True}, ordinary={})
    assert not outcome.result.success
    assert outcome.written == [set()]
    assert outcome.jira.batches == []
    assert _refused(outcome) == 2


@ALL
def test_the_component_runner_reaches_the_level_path(level_setup, component):
    # The migration runner calls run_with_change_detection, which ends in run().
    from src.migration import _invoke_component

    outcome = _run(level_setup, component, runs=0)
    start = len(outcome.op.method_calls)
    result = _invoke_component(component, COMPONENTS[component](jira_client=outcome.jira, op_client=outcome.op))
    assert result.success, result.message
    assert _written(outcome.op.method_calls[start:]) == _expected(component, {900, 901})
    assert result.details["security_level"] == {"work_packages": 2, "refused": 0}


@ALL
def test_a_missing_report_stops_before_any_read(level_setup, component):
    outcome = _run(level_setup, component, reports=False)
    assert not outcome.result.success
    assert outcome.result.errors == ["security_level"]
    assert outcome.written == [set()]
    assert outcome.jira.batches == []


# The components of #498.
FIXED = pytest.mark.parametrize(
    "component", ["labels", "native_tags", "remote_links", "resolution", "security_levels", "votes"]
)
# The issue number behind each work package of the fixtures.
NUMBER = {500: 1, 501: 2, 900: 10, 901: 11, 902: 12}
VALUE = {
    "labels": lambda number, level: f"label-{number}",
    "native_tags": lambda number, level: f"label-{number}",
    "votes": lambda number, level: "7",
    "remote_links": lambda number, level: f"- [Link {number}](https://example.org/{number})",
    "resolution": lambda number, level: "Fixed",
    "security_levels": lambda number, level: f"Level {level}" if level else None,
}
_SCRIPT_VALUE = re.compile(r"WorkPackage\.find\((\d+)\).*cv\.value = '([^']*)'")


def _values(op):
    """``(work package, value)`` of every write call of the fixed components."""
    values = set()
    for name, args, _kwargs in op.method_calls:
        if name in {"bulk_set_wp_custom_field_values", "bulk_upsert_wp_description_sections"}:
            values |= {(row["work_package_id"], row.get("value", row.get("content"))) for row in args[0]}
        elif name == "execute_query" and (match := _SCRIPT_VALUE.search(args[0])):
            values.add((int(match[1]), match[2]))
        elif name == "execute_script_with_data":
            values |= {(row["work_package_id"], ", ".join(t["name"] for t in row["tags"])) for row in args[1]}
    return values


def _expected_values(component, levels):
    rows = {(wp, VALUE[component](NUMBER[wp], level)) for wp, level in levels.items()}
    return {row for row in rows if row[1] is not None}


@FIXED
def test_switch_off_writes_each_value_with_a_mapping_keyed_by_jira_issue_id(level_setup, component):
    # The production shape (#498): the skeleton keys the mapping by Jira issue ID.
    outcome = _run(level_setup, component, on=False)
    assert outcome.result.success, outcome.result.message
    assert _values(outcome.op) == _expected_values(component, {500: ORDINARY_LEVEL, 501: ORDINARY_LEVEL})


@ALL
def test_switch_off_issues_the_same_calls_for_both_mapping_shapes(level_setup, component):
    # The key-shaped mapping of legacy and test data: keyed by issue key, without an inner jira_key.
    by_key = {entry["jira_key"]: {k: v for k, v in entry.items() if k != "jira_key"} for entry in ORDINARY.values()}
    by_id = _run(level_setup, component, on=False)
    keyed = _run(level_setup, component, on=False, ordinary=by_key)
    assert keyed.written == [{500, 501}]
    assert by_id.op.method_calls == keyed.op.method_calls
    assert by_id.jira.batches == keyed.jira.batches


@FIXED
def test_level_work_packages_get_the_values_of_their_level_issue(level_setup, component):
    outcome = _run(level_setup, component)
    assert outcome.result.success, outcome.result.message
    ordinary = {} if component in LEVEL_ONLY else {500: None, 501: None}
    level = {} if component in INSTANCE_WIDE else {900: "10000", 901: "10000"}
    assert _values(outcome.op) == _expected_values(component, ordinary | level)


def test_a_level_work_package_gets_no_other_level_name(level_setup):
    # APP-10 moved to level 10001 after the selection: it is refused, so no call names that level.
    outcome = _run(level_setup, "security_levels", moved=(10,))
    assert _refused(outcome) == 1
    assert _values(outcome.op) == {(901, "Level 10000")}
    assert "Level 10001" not in repr(outcome.op.method_calls)


def test_native_tags_never_put_the_labels_of_a_level_issue_into_a_tag(level_setup):
    outcome = _run(level_setup, "native_tags")
    assert outcome.result.success, outcome.result.message
    calls = repr(outcome.op.method_calls)
    assert "label-10" not in calls
    assert "label-11" not in calls
    assert outcome.result.details["level_tags_withheld"] == 2
    assert outcome.result.details["security_level"] == {"work_packages": 2, "refused": 0}
