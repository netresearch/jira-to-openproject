# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components.components_migration import ComponentsMigration
from src.migration import _invoke_component
from src.models import ComponentResult
from tests.unit.test_level_attachments import LEVEL_MAP, LEVEL_OF
from tests.unit.test_level_attachments import LIGHT as LEVEL_LIGHT
from tests.unit.test_level_attachments import LIVE as LEVEL_LIVE
from tests.unit.test_level_work_packages import SUB_PROJECT, level_issues
from tests.unit.test_level_work_packages import setup as level_work_package_setup  # noqa: F401  (fixture)


def source(number, components, **fields):
    defaults = {"project": {"key": "APP", "id": "10", "projectTypeKey": "software"}, "components": components}
    return SimpleNamespace(key=f"APP-{number}", id=str(100 + number), raw={"fields": {**defaults, **fields}})


def component(sid, name):
    return {"id": str(sid), "name": name, "self": f"https://jira.example.org/rest/api/2/component/{sid}"}


@pytest.fixture
def migration(monkeypatch):
    from src import config

    monkeypatch.setitem(config.jira_config, "projects", ["APP"])
    jira = Mock(base_url="https://jira.example.org")
    app = ComponentsMigration(jira_client=jira, op_client=Mock())
    app.op_client.get_project_wp_cf_snapshot.return_value = []
    maps = {
        "project": {"APP": 7, "OTHER": 99},
        "work_package": {
            "101": {"jira_key": "APP-1", "openproject_id": 10},
            "APP-2": 12,
            "APP-3": 13,
            "APP-4": 14,
            "APP-5": 15,
            "APP-6": 16,
        },
        "category": {},
    }
    app.mappings = Mock()
    app.mappings.get_mapping.side_effect = maps.get
    app.mappings.set_mapping.side_effect = maps.__setitem__
    return app


def test_normal_component_import_checks_visibility_and_preserves_source_order(migration, monkeypatch):
    a, b = component(420, "Backend"), component(37, "API")
    jira = migration.jira_client.jira
    jira.project_components.return_value = [SimpleNamespace(raw=a), SimpleNamespace(raw=b)]
    jira.search_issues.side_effect = [
        [
            source(1, [a, b]),
            source(2, [b]),
            source(3, [a], security={"id": "30"}),
            source(4, [a], project={"projectTypeKey": "service_desk"}),
            source(5, [], project={}),
            source(6, []),
        ],
        [],
    ]
    apply = Mock(return_value={"updated": 2, "categories": {"Backend": 51, "API": 52}})
    monkeypatch.setattr("src.application.components.components_migration.apply_component_assignments", apply)
    result = _invoke_component("components", migration)
    assert result.success
    assert result.updated == 2
    assert result.details["unconfirmed_work_packages"] == 3
    project = apply.call_args.args[1]
    assert project["key"] == "APP"
    assert project["jira_id"] == "10"
    assert project["project_id"] == 7
    assert [r["wp"] for r in project["issues"]] == [10, 12, 16]
    assert [[(ref["id"], ref["name"], ref["url"]) for ref in row["components"]] for row in project["issues"]] == [
        [(a["id"], a["name"], a["self"]), (b["id"], b["name"], b["self"])],
        [(b["id"], b["name"], b["self"])],
        [],
    ]
    for call in jira.search_issues.call_args_list:
        assert call.args[0] == 'project = "APP" AND level IS EMPTY ORDER BY id'
        assert call.kwargs["fields"] == "project,components,security"
    assert migration.mappings.get_mapping("category") == {"7": {"Backend": 51, "API": 52}}


@pytest.mark.parametrize(
    "bad", [None, [component(1, "A"), component(1, "A")], [{"id": "2", "name": "A", "self": "https://other.invalid"}]]
)
def test_incomplete_or_inconsistent_component_data_fails_before_writes(migration, bad):
    migration.jira_client.jira.search_issues.side_effect = [[source(1, bad)], []]
    assert not _invoke_component("components", migration).success
    migration.op_client.execute_large_query_to_json_file.assert_not_called()


def test_checkpoints_work_packages_in_bounded_transactions(monkeypatch):
    migration = ComponentsMigration(jira_client=Mock(), op_client=Mock())
    mapping = {"8": {"Old": 13, "Local": 500}}
    migration.mappings = Mock()
    migration.mappings.get_mapping.return_value = mapping
    calls = []

    def write(client, batch, legacy):
        calls.append(len(batch["issues"]))
        if len(calls) == 2:
            raise RuntimeError("failure on a later batch")
        return {"updated": len(batch["issues"]), "categories": {"New": 13}}

    monkeypatch.setattr("src.application.components.components_migration.apply_component_assignments", write)
    result = migration._load(
        ComponentResult(success=True, data={"ABC": {"project_id": 8, "key": "ABC", "issues": list(range(102))}})
    )
    assert not result.success
    assert result.updated == 100
    assert result.failed == 2
    assert calls == [100, 2]
    migration.mappings.set_mapping.assert_called_once_with("category", {"8": {"New": 13, "Local": 500}})


# --- Level work packages (ADR-009, step 5b) ---------------------------------------------------


@pytest.fixture
def level_setup(level_work_package_setup):  # noqa: F811 (the imported fixture, requested by name)
    """The reports, mappings and switch of the level work package tests."""
    return level_work_package_setup


LEVEL_ORDINARY = 'project = "APP" AND level IS EMPTY ORDER BY id'
# A second sub-project of the same Jira project, for level 10001 (APP-20).
SECOND_LEVEL = {**SUB_PROJECT, "level_id": "10001", "openproject_id": 101, "openproject_identifier": "app_level_10001"}
SECOND_LIVE = {101: {"field": True, "work_packages": [920], "keys": [[920, "APP-20"]]}}
BACKEND = {"id": "420", "name": "Backend", "self": "https://jira.local/rest/api/2/component/420"}


def _component_issue(number, level=None):
    fields = {"project": {"key": "APP", "id": "10", "projectTypeKey": "software"}, "components": [BACKEND]}
    if level:
        fields["security"] = {"id": level}
    return SimpleNamespace(key=f"APP-{number}", id=str(number), raw={"fields": fields})


class _LevelJira:
    """Answer the level and ordinary searches and the component catalogue; record every search."""

    def __init__(self, moved=(), omitted=()):
        self.searches = []
        self.moved, self.omitted = set(moved), set(omitted)
        self.base_url = "https://jira.local"
        self.get_projects = Mock(return_value=[{"key": "APP"}])
        self.jira = Mock()
        self.jira.search_issues.side_effect = self.search
        self.jira.project_components.return_value = [SimpleNamespace(raw=BACKEND)]

    def search(self, jql, startAt, maxResults, **kwargs):
        self.searches.append((jql, kwargs.get("fields")))
        if jql == LEVEL_LIGHT and kwargs.get("fields") == "security":
            rows = level_issues()
        elif jql == LEVEL_LIGHT:
            rows = [
                _component_issue(n, "10001" if n in self.moved else level)
                for n, level in LEVEL_OF.items()
                if n not in self.omitted
            ]
        elif jql == LEVEL_ORDINARY:
            rows = [_component_issue(1)]
        else:
            raise AssertionError(jql)
        return rows[startAt : startAt + maxResults]


class _LevelOpenProject:
    """Answer the live sub-project read and the parent snapshot; decode and record each payload."""

    def __init__(self, live=None, by_project=None, snapshot=()):
        self.live = LEVEL_LIVE if live is None else live
        self.by_project = by_project or {}
        self.snapshot = list(snapshot)
        self.live_reads, self.payloads, self.snapshots = [], [], []

    def execute_query_to_json_file(self, script):
        values = json.loads(script.split("\n")[1])
        self.live_reads.append(values)
        return json.loads(json.dumps(self.by_project.get(values["project_id"], self.live)))

    def get_project_wp_cf_snapshot(self, pid):
        self.snapshots.append(pid)
        return [dict(row) for row in self.snapshot]

    def execute_large_query_to_json_file(self, query, timeout):
        encoded = query.split('Base64.strict_decode64("')[1].split('"')[0]
        project = json.loads(base64.b64decode(encoded))
        self.payloads.append(project)
        return {"updated": len(project["issues"]), "categories": {"Backend": 50 + project["project_id"]}}


def _level_run(
    level_setup,
    *,
    live=None,
    moved=(),
    omitted=(),
    level_map=None,
    ordinary=None,
    categories=None,
    catalogue=None,
    runs=1,
    second_level=False,
    snapshot=(),
    **kw,
):
    if second_level:
        kw["mapping"] = {"security_level_project": {"APP:10000": SUB_PROJECT, "APP:10001": SECOND_LEVEL}}
    mappings, _ = level_setup(**kw)
    mappings.data["work_package"] = {"1": {"jira_key": "APP-1", "openproject_id": 500, "project_key": "APP"}}
    mappings.data["work_package"].update(ordinary or {})
    mappings.data["security_level_work_package"] = dict(LEVEL_MAP if level_map is None else level_map)
    mappings.data["category"] = dict(categories or {})
    writes = []
    original = mappings.set_mapping
    mappings.set_mapping = lambda name, value: (writes.append(name), original(name, value))
    jira, op = _LevelJira(moved, omitted), _LevelOpenProject(live, SECOND_LIVE if second_level else None, snapshot)
    if catalogue is not None:
        jira.jira.project_components.return_value = catalogue
    migration = ComponentsMigration(jira_client=jira, op_client=op)
    results = [migration.run() for _ in range(runs)]
    return SimpleNamespace(result=results[-1], results=results, jira=jira, op=op, writes=writes, mappings=mappings)


def _targets(op):
    return {(p["project_id"], p.get("parent_id"), row["key"], row["wp"]) for p in op.payloads for row in p["issues"]}


def test_level_switch_off_issues_exactly_the_calls_of_the_ordinary_path(level_setup):
    outcome = _level_run(level_setup, on=False, reports=False)
    assert outcome.result.success, outcome.result
    # The first page and the empty page that ends the search.
    assert outcome.jira.searches == [(LEVEL_ORDINARY, "project,components,security")] * 2
    assert [sorted(p) for p in outcome.op.payloads] == [
        ["issues", "jira", "jira_id", "key", "legacy", "project_id", "register", "systems"]
    ]
    assert _targets(outcome.op) == {(7, None, "APP-1", 500)}
    assert outcome.op.snapshots == [7]
    assert not outcome.op.live_reads
    assert not outcome.jira.get_projects.called
    assert outcome.writes == ["category"]
    assert "security_level" not in outcome.result.details


def test_level_switch_on_assigns_categories_of_the_sub_project_only(level_setup):
    # A category mapping of the parent never reaches the sub-project's payload.
    outcome = _level_run(level_setup, categories={"7": {"Backend": 51}})
    assert outcome.result.success, outcome.result
    assert _targets(outcome.op) == {(7, None, "APP-1", 500), (100, 7, "APP-10", 900), (100, 7, "APP-11", 901)}
    parent, level = sorted(outcome.op.payloads, key=lambda p: p["project_id"])
    assert parent["legacy"] == {"Backend": 51}
    assert level["legacy"] == {}
    assert (level["key"], level["jira_id"], level["jira"]) == ("APP", "10", "https://jira.local")
    assert (LEVEL_LIGHT, "project,components,security") in outcome.jira.searches
    # The parent snapshot is read for ordinary targets only.
    assert outcome.op.snapshots == [7]
    assert outcome.op.live_reads == [{"project_id": 100, "field": "J2O Origin Key"}]
    assert outcome.result.details["security_level"] == {"work_packages": 2, "refused": 0}
    # Categories are recorded per OpenProject project; no work-package mapping is written.
    assert outcome.writes == ["category", "category"]
    assert outcome.mappings.data["category"] == {"7": {"Backend": 57}, "100": {"Backend": 150}}


def test_level_issue_with_a_parent_work_package_only_the_snapshot_names_is_refused(level_setup):
    # APP-10 also has an unmapped work package in the parent project.
    outcome = _level_run(level_setup, snapshot=[{"id": 510, "jira_issue_key": "APP-10", "jira_source_id": "10"}])
    assert not outcome.result.success
    assert _targets(outcome.op) == {(7, None, "APP-1", 500), (100, 7, "APP-11", 901)}
    assert outcome.result.details["security_level"] == {"work_packages": 1, "refused": 1}


def test_level_sub_projects_of_one_jira_project_share_one_level_search(level_setup):
    outcome = _level_run(level_setup, second_level=True)
    assert outcome.result.success, outcome.result
    assert (101, 7, "APP-20", 920) in _targets(outcome.op)
    assert sorted(p["project_id"] for p in outcome.op.payloads) == [7, 100, 101]
    # One search per Jira project: the first page and the empty page that ends it.
    assert outcome.jira.searches.count((LEVEL_LIGHT, "project,components,security")) == 2
    assert outcome.mappings.data["category"]["101"] == {"Backend": 151}


@pytest.mark.parametrize(
    "options",
    [
        {"live": {"field": True, "work_packages": [901], "keys": [[901, "APP-11"]]}},
        {"moved": {10}},
        {"omitted": {10}},
        {"ordinary": {"10": {"jira_key": "APP-10", "openproject_id": 510, "project_key": "APP"}}},
    ],
)
def test_level_issue_that_cannot_be_confirmed_gets_no_category(level_setup, options):
    outcome = _level_run(level_setup, **options)
    assert not outcome.result.success
    assert _targets(outcome.op) == {(7, None, "APP-1", 500), (100, 7, "APP-11", 901)}
    assert outcome.result.details["security_level"]["refused"] == 1
    assert outcome.result.failed == 1


def test_level_unreadable_sub_project_refuses_all_its_issues(level_setup):
    outcome = _level_run(level_setup, live={"field": True})
    assert not outcome.result.success
    assert _targets(outcome.op) == {(7, None, "APP-1", 500)}
    assert outcome.result.details["security_level"]["refused"] == 2


def test_level_issue_the_selection_does_not_keep_gets_nothing_even_with_an_entry(level_setup):
    level_map = {**LEVEL_MAP, "12": {**LEVEL_MAP["10"], "jira_key": "APP-12", "openproject_id": 902}}
    outcome = _level_run(level_setup, level_map=level_map)
    assert not outcome.result.success
    assert all(key != "APP-12" for _, _, key, _ in _targets(outcome.op))
    assert outcome.result.details["security_level"]["refused"] == 1


def test_level_repeated_run_on_one_instance_gives_the_same_result(level_setup):
    outcome = _level_run(level_setup, moved={10}, runs=2)
    first, second = outcome.results
    assert first.details["security_level"] == second.details["security_level"] == {"work_packages": 2, "refused": 1}
    assert len(outcome.op.live_reads) == 2
    level = [p for p in outcome.op.payloads if p["project_id"] == 100]
    assert len(level) == 2
    assert [r["key"] for r in level[0]["issues"]] == [r["key"] for r in level[1]["issues"]] == ["APP-11"]


def test_level_refusals_are_reported_when_the_extract_fails(level_setup):
    # A component outside the Jira catalogue stops the extract before any write.
    outcome = _level_run(level_setup, live={"field": True}, catalogue=[])
    assert not outcome.result.success
    assert "migration failed" in outcome.result.message
    assert not outcome.op.payloads
    assert outcome.result.details["security_level"] == {"work_packages": 0, "refused": 2}
    assert outcome.result.failed == 2


def test_level_switch_on_without_reports_stops_before_any_search_or_write(level_setup):
    outcome = _level_run(level_setup, reports=False)
    assert not outcome.result.success
    assert not outcome.jira.searches
    assert not outcome.op.payloads
