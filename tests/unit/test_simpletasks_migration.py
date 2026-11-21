# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import config
from src.application.components.simpletasks_migration import SimpleTasksMigration
from src.infrastructure.jira.sidebar_tasks import issue_task_records
from src.migration import _invoke_component
from tests.unit.test_level_work_packages import level_issues
from tests.unit.test_level_work_packages import setup as level_work_package_setup  # noqa: F401  (fixture)


def _render(identifier="42", key="J-1", checked=True):
    flag = " checked" if checked else ""
    return (
        f'<div class="task" data-issue="{key}" data-task-id="{identifier}"><div>'
        f'<input type="checkbox" class="task-input" data-task-id="{identifier}"{flag}/></div></div>'
    )


def _raw(source, rendered):
    return {"key": "J-1", "fields": {"description": source}, "renderedFields": {"description": rendered}}


@pytest.mark.parametrize(
    ("source", "rendered"),
    [
        ("{task:id=42}title{task}", _render(key="X-1")),
        ("{task:id=42}title{task}", _render("41")),
        ("{task:id=42}title{task}", _render() * 2),
        ("{task:id=42}one{task}{task:id=42}two{task}", _render()),
        ("{task:type=other}title{task}", _render()),
        ("{task}title{task}", _render()),
        ("{task:id=42}title", _render()),
    ],
)
def test_inconsistent_description_tasks_abort_before_writing(source, rendered):
    from src.infrastructure.jira.description_tasks import description_tasks

    raw = _raw(source, rendered)
    with pytest.raises(ValueError):
        description_tasks("J-1", raw)


def test_missing_description_tasks_and_issue_identity():
    from src.infrastructure.jira.description_tasks import description_tasks

    assert description_tasks("J-1", _raw(None, None)) == []
    other = _raw(None, None)
    with pytest.raises(ValueError, match="different issue"):
        description_tasks("J-2", other)


def test_issue_property_only_falls_back_on_missing():
    from unittest.mock import Mock

    from src.infrastructure.jira.jira_client import JiraApiError, JiraResourceNotFoundError
    from src.infrastructure.jira.jira_field_service import JiraFieldService

    client = Mock()
    fields = JiraFieldService(client)
    client._make_request.side_effect = JiraApiError("unavailable")
    with pytest.raises(JiraApiError, match="unavailable"):
        fields.get_issue_property("J-1", "example/key")
    client._make_request.assert_called_with("/rest/api/2/issue/J-1/properties/example%2Fkey")
    client._make_request.side_effect = JiraResourceNotFoundError("missing")
    assert fields.get_issue_property("J-1", "example") is None
    sdk_error = Exception("missing")
    sdk_error.status_code = 404
    wrapped = JiraApiError("upstream request failed")
    wrapped.__cause__ = sdk_error
    client._make_request.side_effect = wrapped
    assert fields.get_issue_property("J-1", "example") is None
    client._make_request.side_effect = None
    client._make_request.return_value.json.return_value = {"value": []}
    assert fields.get_issue_property("J-1", "example") == []
    client._make_request.return_value.json.return_value = "bad"
    with pytest.raises(JiraApiError, match="Expected an object"):
        fields.get_issue_property("J-1", "example")


def test_checklist_writer_requires_a_verified_result():
    from unittest.mock import Mock

    from src.infrastructure.openproject.openproject_work_package_content_service import (
        OpenProjectWorkPackageContentService,
    )

    client = Mock()
    service = OpenProjectWorkPackageContentService(client)
    client.execute_script_with_data.return_value = {"status": "success", "data": {"verified": True}}
    assert service.set_checklist_section(12, "example")
    script, data = client.execute_script_with_data.call_args.args
    assert "example" not in script
    assert data == {"id": 12, "markdown": "example"}
    client.execute_script_with_data.return_value = {"status": "success"}
    assert not service.set_checklist_section(12, "example")


def _project_issue():
    raw = _raw("{task:id=42}inline{task}", _render())
    raw.update(id="90")
    raw["fields"].update({"project": {"id": "7", "key": "J", "projectTypeKey": "software"}, "customfield_42": "3.0"})
    return raw


def _rows():
    common = {"issue": "J-1", "issueId": 90, "projectId": 7, "checked": True, "manuallyCreated": False}
    return [
        {**common, "id": 42, "text": "inline", "ordinal": 4, "mandatory": True, "lastChangedBy": "example"},
        {**common, "id": 43, "text": "direct task", "ordinal": 2, "manuallyCreated": True, "checked": False},
        {**common, "id": 44, "text": "restricted comment", "ordinal": 3},
    ]


@pytest.fixture
def stage(monkeypatch):
    jira = Mock(base_url="https://jira.example.invalid")
    jira.get_custom_fields.return_value = [
        {"id": "customfield_42", "schema": {"custom": "com.topshelfsolution.simpletasklists:all-tasks-count-cftype"}}
    ]
    jira.jira.server_info.return_value = {"deploymentType": "Data Center"}
    raw = _project_issue()
    raw["fields"]["comment"] = {
        "comments": [
            {
                "id": "555",
                "visibility": {"type": "role", "value": "Reviewers"},
                "body": "{task:id=44}restricted comment{task}",
            }
        ]
    }
    issue = SimpleNamespace(key="J-1", id="90", raw=raw, fields=raw["fields"])
    jira.jira.search_issues.side_effect = lambda jql, **kwargs: [issue] if kwargs["startAt"] == 0 else []
    jira.jira.issue.return_value.raw = raw
    jira.get_issue_property.return_value = {"tasks": [{"id": 44, "text": "sensitive property"}]}
    jira.get_issue_simpletasks.return_value = _rows()
    target = Mock(return_value={"updated": 1, "tasks": 2})
    monkeypatch.setattr("src.application.components.simpletasks_migration.apply_tasklists", target)
    monkeypatch.setitem(config.jira_config, "projects", ["J"])
    migration = SimpleTasksMigration(jira_client=jira, op_client=Mock())
    migration.mappings = Mock()
    migration.mappings.get_mapping.side_effect = {
        "work_package": {"90": {"jira_key": "J-1", "openproject_id": 101}, "OTHER-2": 44},
        "project": {"J": 77},
    }.get
    return migration, jira, target


def test_full_profile_schedules_tasklists_once_immediately_after_work_package_content():
    from src.application.components.registry import DEFAULT_COMPONENT_SEQUENCE, PREDEFINED_PROFILES

    for profile in (DEFAULT_COMPONENT_SEQUENCE, PREDEFINED_PROFILES["full"]):
        assert profile.count("simpletasks") == 1
        assert profile.index("simpletasks") == profile.index("work_packages_content") + 1


def test_missing_addon_skips_source_issues_and_leaves_existing_target_data_alone(stage):
    migration, jira, target = stage
    jira.get_custom_fields.return_value = []
    result = migration.run()
    assert result.success
    assert result.details["source_tasklist_detected"] is False
    jira.jira.search_issues.assert_not_called()
    jira.get_issue_property.assert_not_called()
    target.assert_not_called()


def test_unavailable_task_count_does_not_remove_a_previously_imported_checklist(stage):
    migration, jira, target = stage
    source = jira.jira.issue.return_value.raw["fields"]
    source["description"] = None
    del source["customfield_42"]
    assert migration.run().success
    jira.jira.issue.assert_not_called()
    target.assert_not_called()


def test_regular_stage_preserves_the_confirmed_task_order_and_withholds_comment_and_property(stage):
    migration, jira, target = stage
    result = _invoke_component("simpletasks", migration)
    assert result.success
    assert result.updated == 1
    assert result.details["tasks"] == 2
    assert result.details["unverified_task_audience"] == 1
    assert jira.jira.search_issues.call_args.args[0] == 'project = "J" AND level IS EMPTY ORDER BY id'
    [row] = target.call_args.args[1]["rows"]
    assert row["wp"] == 101
    assert [t["id"] for t in row["tasks"]] == ["43", "42"]
    assert row["tasks"][1]["lastChangedBy"] == "example"
    assert row["markdown"] == "- [ ] ` direct task `\n- [x] ` inline ` (mandatory)"
    assert "sensitive" not in str(row)
    assert "restricted" not in str(row)


def test_personal_or_comment_task_is_never_published_as_a_direct_sidebar_task(stage):
    migration, jira, target = stage
    jira.get_issue_simpletasks.return_value[1]["commentId"] = 555
    result = migration.run()
    assert result.details["unverified_task_audience"] == 2
    [row] = target.call_args.args[1]["rows"]
    assert [t["id"] for t in row["tasks"]] == ["42"]


def test_cloud_sidebar_tasks_are_held_for_a_separate_audience_check(stage):
    migration, jira, target = stage
    jira.jira.server_info.return_value = {"deploymentType": "Cloud"}
    result = migration.run()
    assert result.details["unverified_task_audience"] == 2
    assert [t["id"] for t in target.call_args.args[1]["rows"][0]["tasks"]] == ["42"]
    jira.get_issue_simpletasks.assert_not_called()


def test_property_cannot_publish_task_content_or_modify_confirmed_labels(stage):
    migration, jira, target = stage
    raw = jira.jira.issue.return_value.raw
    raw["fields"].pop("customfield_42")
    jira.get_issue_property.return_value = [{"id": 42, "text": "private source body"}]
    result = migration.run()
    assert result.success
    [row] = target.call_args.args[1]["rows"]
    assert row["markdown"] == "- [x] ` inline `"
    assert "private source body" not in str(row)


def test_ordinary_scope_requires_empty_level_and_excludes_customer_projects(stage):
    migration, jira, target = stage
    jira.jira.issue.return_value.raw["fields"]["security"] = {"id": "10"}
    result = migration.run()
    assert result.success
    assert result.details["unconfirmed_issues"] == 1
    target.assert_not_called()
    del jira.jira.issue.return_value.raw["fields"]["security"]
    jira.jira.issue.return_value.raw["fields"]["project"]["projectTypeKey"] = "service_desk"
    assert migration.run().details["unconfirmed_issues"] == 1
    target.assert_not_called()
    jira.get_issue_property.assert_not_called()


@pytest.mark.parametrize("count", ["NaN", "1.2", "-1", "4"])
def test_source_mismatch_aborts_before_first_write(stage, count):
    migration, jira, target = stage
    jira.jira.issue.return_value.raw["fields"]["customfield_42"] = count
    assert not migration.run().success
    target.assert_not_called()


@pytest.mark.parametrize(
    ("index", "change"),
    [
        (0, {"manuallyCreated": True}),
        (0, {"text": "changed"}),
        (0, {"id": 61}),
        (1, {"id": 42}),
        (1, {"projectId": 71}),
        (1, {"issue": "J-2"}),
        (1, {"issueId": 91}),
        (1, {"manuallyCreated": "yes"}),
        (1, {"id": ""}),
        (1, {"text": None}),
    ],
)
def test_inconsistent_sidebar_identity_aborts_the_write(index, change):
    rows = _rows()
    rows[index].update(change)
    issue = _project_issue()
    with pytest.raises((ValueError, KeyError)):
        issue_task_records(issue, [{"id": "42", "title": "inline", "checked": True}], rows, 3)


# --- Level work packages (ADR-009, step 5b) ---------------------------------------------------


@pytest.fixture
def level_setup(level_work_package_setup):  # noqa: F811 (the imported fixture, requested by name)
    """The reports, mappings and switch of the level work package tests."""
    return level_work_package_setup


LEVEL_LIGHT = 'project = "APP" AND level IS NOT EMPTY ORDER BY created ASC, key ASC'
LEVEL_ORDINARY = 'project = "APP" AND level IS EMPTY ORDER BY id'
LEVEL_LIVE = {"field": True, "work_packages": [900, 901], "keys": [[900, "APP-10"], [901, "APP-11"]]}
LEVEL_MAP = {
    str(n): {
        "jira_key": f"APP-{n}",
        "openproject_id": wp,
        "project_key": "APP",
        "openproject_project_id": 100,
        "security_level_id": "10000",
        "security_level_project": "APP:10000",
        "security_class": "static",
    }
    for n, wp in ((10, 900), (11, 901))
}
LEVEL_OF = {10: "10000", 11: "10000", 12: "10000", 13: "10000", 20: "10001"}


def _task_issue(number, level=None):
    """An issue with one description task, as the full search and the re-read return it."""
    key = f"APP-{number}"
    fields = {
        "project": {"id": "7", "key": "APP", "projectTypeKey": "business"},
        "description": f"{{task:id={number}}}task {number}{{task}}",
        "customfield_42": "1",
    }
    if level:
        fields["security"] = {"id": level}
    raw = {
        "key": key,
        "id": str(number),
        "fields": fields,
        "renderedFields": {"description": _render(str(number), key)},
    }
    return SimpleNamespace(key=key, id=str(number), raw=raw, fields=fields)


class _LevelJira:
    """Answer the level and ordinary searches and the re-read; record every call."""

    def __init__(self, moved=(), reread_moved=(), addon=True, omitted=()):
        self.searches, self.reads = [], []
        self.moved, self.reread_moved, self.omitted = set(moved), set(reread_moved), set(omitted)
        self.base_url = "https://jira.local"
        self.get_projects = Mock(return_value=[{"key": "APP"}])
        count = {
            "id": "customfield_42",
            "schema": {"custom": "com.topshelfsolution.simpletasklists:all-tasks-count-cftype"},
        }
        self.get_custom_fields = Mock(return_value=[count] if addon else [])
        self.get_issue_property = Mock(return_value=None)
        self.jira = Mock()
        self.jira.server_info.return_value = {"deploymentType": "Data Center"}
        self.jira.search_issues.side_effect = self.search
        self.jira.issue.side_effect = self.issue

    def _level(self, number, moved):
        return "10001" if number in moved else LEVEL_OF.get(number)

    def search(self, jql, startAt, maxResults, **kwargs):
        self.searches.append((jql, kwargs.get("fields")))
        if jql == LEVEL_LIGHT and kwargs.get("fields") == "security":
            rows = level_issues()
        elif jql == LEVEL_LIGHT:
            rows = [_task_issue(n, self._level(n, self.moved)) for n in LEVEL_OF if n not in self.omitted]
        elif jql == LEVEL_ORDINARY:
            rows = [_task_issue(1)]
        else:
            raise AssertionError(jql)
        return rows[startAt : startAt + maxResults]

    def issue(self, key, fields, expand):
        self.reads.append(key)
        number = int(key.split("-")[1])
        return SimpleNamespace(raw=_task_issue(number, self._level(number, self.reread_moved)).raw)


class _LevelOpenProject:
    """Answer the live sub-project read; decode and record each tasklist payload."""

    def __init__(self, live=None):
        self.live = LEVEL_LIVE if live is None else live
        self.live_reads, self.payloads = [], []

    def execute_query_to_json_file(self, script):
        self.live_reads.append(json.loads(script.split("\n")[1]))
        return json.loads(json.dumps(self.live))

    def execute_large_query_to_json_file(self, query, timeout):
        encoded = query.split('Base64.strict_decode64("')[1].split('"')[0]
        project = json.loads(base64.b64decode(encoded))
        self.payloads.append(project)
        return {"updated": len(project["rows"]), "tasks": len(project["rows"])}


def _level_run(
    level_setup,
    *,
    live=None,
    moved=(),
    reread_moved=(),
    omitted=(),
    addon=True,
    level_map=None,
    ordinary=None,
    runs=1,
    **kwargs,
):
    mappings, _ = level_setup(**kwargs)
    mappings.data["work_package"] = {"1": {"jira_key": "APP-1", "openproject_id": 500, "project_key": "APP"}}
    mappings.data["work_package"].update(ordinary or {})
    mappings.data["security_level_work_package"] = dict(LEVEL_MAP if level_map is None else level_map)
    writes = []
    original = mappings.set_mapping
    mappings.set_mapping = lambda name, value: (writes.append(name), original(name, value))
    jira, op = _LevelJira(moved, reread_moved, addon, omitted), _LevelOpenProject(live)
    migration = SimpleTasksMigration(jira_client=jira, op_client=op)
    results = [migration.run() for _ in range(runs)]
    return SimpleNamespace(result=results[-1], results=results, jira=jira, op=op, writes=writes)


def _targets(op):
    return {(p["project"], p.get("parent"), row["key"], row["wp"]) for p in op.payloads for row in p["rows"]}


def test_level_switch_off_issues_exactly_the_calls_of_the_ordinary_path(level_setup):
    outcome = _level_run(level_setup, on=False, reports=False)
    assert outcome.result.success, outcome.result
    # The first page and the empty page that ends the search.
    assert outcome.jira.searches == [(LEVEL_ORDINARY, "project,security,description,customfield_42")] * 2
    assert outcome.jira.reads == ["APP-1"]
    assert [sorted(p) for p in outcome.op.payloads] == [["base", "key", "project", "rows", "source_id"]]
    assert _targets(outcome.op) == {(7, None, "APP-1", 500)}
    assert not outcome.op.live_reads
    assert not outcome.jira.get_projects.called
    assert "security_level" not in outcome.result.details


def test_level_switch_on_writes_selected_level_tasks_into_the_work_package_their_sub_project_holds(level_setup):
    outcome = _level_run(level_setup)
    assert outcome.result.success, outcome.result
    assert _targets(outcome.op) == {(7, None, "APP-1", 500), (100, 7, "APP-10", 900), (100, 7, "APP-11", 901)}
    level = next(p for p in outcome.op.payloads if p["project"] == 100)
    assert (level["key"], level["source_id"], level["base"]) == ("APP", "7", "https://jira.local")
    # The full level search reads the level with the tasks; archived and unmapped issues are never re-read.
    assert (LEVEL_LIGHT, "project,security,description,customfield_42") in outcome.jira.searches
    assert sorted(outcome.jira.reads) == ["APP-1", "APP-10", "APP-11"]
    assert outcome.op.live_reads == [{"project_id": 100, "field": "J2O Origin Key"}]
    assert outcome.result.details["security_level"] == {"work_packages": 2, "refused": 0}
    assert outcome.writes == []


@pytest.mark.parametrize(
    "options",
    [
        # APP-10's work package moved out of the sub-project.
        {"live": {"field": True, "work_packages": [901], "keys": [[901, "APP-11"]]}},
        # APP-10 changed its level since the selection, seen in the search or in the re-read.
        {"moved": {10}},
        {"reread_moved": {10}},
        # The full search no longer returns APP-10.
        {"omitted": {10}},
    ],
)
def test_level_issue_that_cannot_be_confirmed_gets_no_tasks(level_setup, options):
    outcome = _level_run(level_setup, **options)
    assert not outcome.result.success
    assert _targets(outcome.op) == {(7, None, "APP-1", 500), (100, 7, "APP-11", 901)}
    assert outcome.result.details["security_level"]["refused"] == 1
    assert outcome.result.failed == 1


def test_level_issue_the_selection_does_not_keep_gets_nothing_even_with_an_entry(level_setup):
    # APP-12 is per_issue and stays in the operator archive on Community.
    level_map = {**LEVEL_MAP, "12": {**LEVEL_MAP["10"], "jira_key": "APP-12", "openproject_id": 902}}
    outcome = _level_run(level_setup, level_map=level_map)
    assert not outcome.result.success
    assert _targets(outcome.op) == {(7, None, "APP-1", 500), (100, 7, "APP-10", 900), (100, 7, "APP-11", 901)}
    assert "APP-12" not in outcome.jira.reads
    assert outcome.result.details["security_level"]["refused"] == 1


def test_level_unreadable_sub_project_refuses_all_its_issues(level_setup):
    outcome = _level_run(level_setup, live={"field": True})
    assert not outcome.result.success
    assert _targets(outcome.op) == {(7, None, "APP-1", 500)}
    assert outcome.jira.reads == ["APP-1"]
    assert outcome.result.details["security_level"]["refused"] == 2


def test_level_issue_with_an_ordinary_work_package_gets_nothing_on_either_side(level_setup):
    extra = {"10": {"jira_key": "APP-10", "openproject_id": 510, "project_key": "APP"}}
    outcome = _level_run(level_setup, ordinary=extra)
    assert not outcome.result.success
    assert _targets(outcome.op) == {(7, None, "APP-1", 500), (100, 7, "APP-11", 901)}
    assert outcome.result.details["security_level"]["refused"] == 1


def test_level_repeated_run_on_one_instance_gives_the_same_result(level_setup):
    outcome = _level_run(level_setup, moved={10}, runs=2)
    first, second = outcome.results
    assert first.details["security_level"] == second.details["security_level"] == {"work_packages": 2, "refused": 1}
    assert first.failed == second.failed == 1
    # Each run resolves the level work packages again.
    assert len(outcome.op.live_reads) == 2
    level = [p for p in outcome.op.payloads if p["project"] == 100]
    assert len(level) == 2
    assert level[0] == level[1]


def test_level_refusals_are_reported_when_the_addon_is_absent(level_setup):
    outcome = _level_run(level_setup, addon=False, live={"field": True})
    assert not outcome.result.success
    assert outcome.result.details["security_level"] == {"work_packages": 0, "refused": 2}
    assert not outcome.op.payloads


def test_level_switch_on_without_reports_stops_before_any_search_or_write(level_setup):
    outcome = _level_run(level_setup, reports=False)
    assert not outcome.result.success
    assert not outcome.jira.searches
    assert not outcome.op.payloads
