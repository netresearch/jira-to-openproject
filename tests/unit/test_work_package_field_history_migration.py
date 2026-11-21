# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Write only audience-safe Jira field changes, once, to confirmed unrestricted work packages."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components import work_package_field_history_migration as module
from src.application.components.work_package_field_history_migration import WorkPackageFieldHistoryMigration
from src.migration import _invoke_component
from tests.unit.test_level_work_packages import level_issues
from tests.unit.test_level_work_packages import setup as level_work_package_setup  # noqa: F401  (fixture)

pytestmark = pytest.mark.unit

_FIELD_ID = 77
_SAFE = {"field": "summary", "fieldtype": "jira", "from": None, "fromString": "old", "to": None, "toString": "new"}


def history(hid, *items, **extra):
    return {
        "id": str(hid),
        "created": "2026-01-01T10:00:00.000+0000",
        "author": {"key": "alice"},
        "items": list(items),
        **extra,
    }


def source(number, histories, *, total=None, project_type="software", **fields):
    page = {
        "startAt": 0,
        "maxResults": 100,
        "total": len(histories) if total is None else total,
        "histories": histories,
    }
    raw = {
        "fields": {"project": {"key": "APP", "id": "10", "projectTypeKey": project_type}, "security": None, **fields},
        "changelog": page,
    }
    return SimpleNamespace(key=f"APP-{number}", id=str(100 + number), raw=raw)


class FakeOpenProject:
    """Store custom values the way the read and write scripts would."""

    def __init__(self):
        self.values: dict[int, str] = {}
        self.writes: list[list[dict]] = []
        self.origins = {10 + n: str(100 + n) for n in range(1, 10)}
        self.projects = dict.fromkeys(self.origins, 7)
        self.keys = {10 + n: f"APP-{n}" for n in range(1, 10)}
        self.fail_write = False

    def ensure_wp_custom_field_id(self, name, field_format):
        assert (name, field_format) == (module.FIELD_NAME, "text")
        return _FIELD_ID

    def execute_script_with_data(self, script, data):
        if script == module._STORED:
            assert data["name"] == module.FIELD_NAME
            ids = [wp for wp in self.values if self.projects[wp] == data["project"]]
            values = [[wp, 1, self.origins[wp]] for wp in ids if wp in self.origins]
            values += [[wp, 2, self.keys[wp]] for wp in ids if wp in self.keys]
            return {"status": "success", "data": {"origin": 1, "key": 2, "ids": ids, "values": values}}
        assert data["field"] == _FIELD_ID
        if script == module._READ:
            ids = data["ids"]
            rows = [[wp, 1, self.origins[wp]] for wp in ids if wp in self.origins] + [
                [wp, _FIELD_ID, self.values[wp]] for wp in ids if wp in self.values
            ]
            return {
                "status": "success",
                "data": {"origin": 1, "projects": [[wp, self.projects[wp]] for wp in ids], "rows": rows},
            }
        assert script == module._WRITE
        if self.fail_write:
            return {"status": "error", "data": None}
        self.writes.append(data["rows"])
        cleared = 0
        for row in data["rows"]:
            if row["value"] is None:
                cleared += self.values.pop(row["wp"], None) is not None
            else:
                self.values[row["wp"]] = row["value"]
        return {"status": "success", "data": {"updated": len(data["rows"]) - cleared, "cleared": cleared}}


@pytest.fixture
def migration(monkeypatch):
    from src import config

    monkeypatch.setitem(config.jira_config, "projects", ["APP"])
    monkeypatch.setitem(config.migration_config, "history_approved_field_ids", [])
    jira = Mock(base_url="https://jira.example.org")
    jira.get_custom_fields.return_value = []
    app = WorkPackageFieldHistoryMigration(jira_client=jira, op_client=FakeOpenProject())
    maps = {
        "project": {"APP": 7},
        "work_package": {str(100 + n): {"jira_key": f"APP-{n}", "openproject_id": 10 + n} for n in range(1, 10)},
    }
    app.mappings = Mock()
    app.mappings.get_mapping.side_effect = maps.get
    return app


def run(migration, *issues):
    migration.jira_client.jira.search_issues.side_effect = [list(issues), []]
    return _invoke_component("work_package_field_history", migration)


def stored(migration, number):
    return json.loads(migration.op_client.values[10 + number])


def test_allowed_field_change_is_written_as_canonical_json(migration):
    result = run(migration, source(1, [history(5, _SAFE)]))
    assert result.success
    expected = {
        "jiraIssueId": "101",
        "events": [
            {
                **{k: v for k, v in history(5).items() if k != "items"},
                "author": "alice",
                "changes": [{"position": 0, **_SAFE}],
            }
        ],
    }
    assert migration.op_client.values[11] == json.dumps(
        expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert result.details["issues_written"] == 1
    assert result.details["kept_changes"] == 1
    for call in migration.jira_client.jira.search_issues.call_args_list:
        assert call.args[0] == 'project = "APP" AND level IS EMPTY ORDER BY id'
        assert call.kwargs["expand"] == "changelog"


def test_comment_change_is_withheld(migration):
    comment = {**_SAFE, "field": "Comment", "toString": "private remark"}
    run(migration, source(1, [history(5, _SAFE, comment)]))
    assert [c["field"] for c in stored(migration, 1)["events"][0]["changes"]] == ["summary"]
    assert "private remark" not in migration.op_client.values[11]


def test_audience_tagged_event_is_withheld(migration):
    worklog = {**_SAFE, "field": "WorklogId", "toString": "9001"}
    metadata = {"extraData": {"internal": True}}
    tagged = history(6, {**_SAFE, "toString": "internal"}, worklog, historyMetadata=metadata)
    result = run(migration, source(1, [history(5, _SAFE), tagged]))
    assert [e["id"] for e in stored(migration, 1)["events"]] == ["5"]
    assert result.details["withheld_changes"] == 2


def test_audience_tagged_item_is_withheld(migration):
    tagged = {**_SAFE, "field": "status", "toString": "Done", "visibility": {"type": "role", "value": "Admins"}}
    run(migration, source(1, [history(5, _SAFE, tagged)]))
    assert [c["field"] for c in stored(migration, 1)["events"][0]["changes"]] == ["summary"]


def test_issue_with_security_level_is_skipped(migration):
    result = run(migration, source(1, [history(5, _SAFE)]), source(2, [history(6, _SAFE)], security={"id": "30"}))
    assert set(migration.op_client.values) == {11}
    assert result.details["restricted_skipped"] == 1


def test_incomplete_changelog_page_is_skipped_and_counted(migration):
    result = run(migration, source(1, [history(5, _SAFE)], total=2), source(2, [history(6, _SAFE)]))
    assert set(migration.op_client.values) == {12}
    assert result.details["incomplete"] == 1


def test_repeat_run_writes_nothing(migration):
    run(migration, source(1, [history(5, _SAFE)]))
    assert len(migration.op_client.writes) == 1
    result = run(migration, source(1, [history(5, _SAFE)]))
    assert len(migration.op_client.writes) == 1
    assert result.details["unchanged"] == 1
    assert result.details["issues_written"] == 0


def test_events_touching_a_worklog_stay_out_of_the_project_value(migration):
    worklog = {**_SAFE, "field": "WorklogId", "toString": "9001"}
    total = {**_SAFE, "field": "timespent", "toString": "10800"}
    by_id = {**_SAFE, "field": "Worklog Time Spent", "fieldId": "WorklogTimeSpent", "toString": "3h"}
    result = run(migration, source(1, [history(5, _SAFE), history(6, total, worklog), history(7, total, by_id)]))
    assert [e["id"] for e in stored(migration, 1)["events"]] == ["5"]
    assert result.details["withheld_changes"] == 4


def test_worklog_items_stay_in_the_operator_archive():
    from src.infrastructure.jira.field_history import kept_events

    worklog = {**_SAFE, "field": "WorklogId", "toString": "9001"}
    assert [c["field"] for c in kept_events([history(5, _SAFE, worklog)], set())[0]["changes"]] == [
        "summary",
        "WorklogId",
    ]


def test_value_is_cleared_when_the_issue_gains_a_security_level(migration):
    run(migration, source(1, [history(5, _SAFE)]))
    assert 11 in migration.op_client.values
    result = run(migration)  # the unrestricted search no longer returns APP-1
    assert 11 not in migration.op_client.values
    assert result.details["cleared"] == 1


def test_value_is_cleared_for_a_mapping_without_issue_id(migration):
    migration.mappings.get_mapping.side_effect = {
        "project": {"APP": 7},
        "work_package": {"APP-1": {"jira_key": "APP-1", "openproject_id": 11}},
    }.get
    migration.op_client.values[11] = "stale"
    result = run(migration)
    assert 11 not in migration.op_client.values
    assert result.details["cleared"] == 1


def test_value_is_cleared_when_no_change_is_kept_any_more(migration):
    run(migration, source(1, [history(5, _SAFE)]))
    comment = {**_SAFE, "field": "Comment", "toString": "x"}
    result = run(migration, source(1, [history(5, comment)]))
    assert 11 not in migration.op_client.values
    assert result.details["cleared"] == 1


def test_customer_facing_project_issue_is_skipped(migration):
    result = run(migration, source(1, [history(5, _SAFE)]), source(2, [history(6, _SAFE)], project_type="service_desk"))
    assert set(migration.op_client.values) == {11}
    assert result.details["unsupported_project_type"] == 1


def test_target_with_other_origin_or_project_is_skipped(migration):
    migration.op_client.origins[12] = "999"
    migration.op_client.projects[13] = 8
    issues = [source(n, [history(n, _SAFE)]) for n in (1, 2, 3)]
    result = run(migration, *issues)
    assert set(migration.op_client.values) == {11}
    assert result.details["target_unconfirmed"] == 2


@pytest.mark.parametrize("moved", ["origin", "project"])
def test_clear_for_target_with_other_origin_or_project_fails_the_run(migration, moved):
    migration.op_client.values[11] = "stale"
    if moved == "origin":
        migration.op_client.origins[11] = "999"
    else:
        migration.op_client.projects[11] = 8
    result = run(migration)  # the unrestricted search no longer returns APP-1
    assert not result.success
    assert result.details["clear_unconfirmed"] == 1
    assert result.details["target_unconfirmed"] == 0
    assert migration.op_client.values[11] == "stale"
    assert migration.op_client.writes == []


def test_stored_value_is_cleared_when_the_mapping_is_empty(migration):
    migration.mappings.get_mapping.side_effect = {"project": {"APP": 7}, "work_package": {}}.get
    migration.op_client.values[11] = "stale"
    result = run(migration)  # the unrestricted search no longer returns APP-1
    assert result.success
    assert 11 not in migration.op_client.values
    assert result.details["cleared"] == 1


def test_target_missing_from_the_mapping_is_recovered_from_provenance(migration):
    migration.mappings.get_mapping.side_effect = {
        "project": {"APP": 7},
        "work_package": {"101": {"jira_key": "APP-1", "openproject_id": 11}},
    }.get
    migration.op_client.values[12] = "stale"
    result = run(migration, source(1, [history(5, _SAFE)]), source(2, [history(6, _SAFE)]))
    assert result.success
    assert stored(migration, 2)["jiraIssueId"] == "102"
    assert result.details["issues_written"] == 2


def test_stored_value_without_origin_key_is_cleared(migration):
    migration.mappings.get_mapping.side_effect = {"project": {"APP": 7}, "work_package": {}}.get
    migration.op_client.values[11] = "stale"
    del migration.op_client.keys[11]
    result = run(migration, source(1, [history(5, _SAFE)]))
    assert result.success
    assert 11 not in migration.op_client.values
    assert result.details["cleared"] == 1


def test_value_another_jira_project_maps_is_not_recovered(migration):
    migration.mappings.get_mapping.side_effect = {
        "project": {"APP": 7, "OTH": 7},
        "work_package": {
            "101": {"jira_key": "APP-1", "openproject_id": 11},
            "102": {"jira_key": "OTH-1", "openproject_id": 12},
        },
    }.get
    migration.op_client.keys[12] = "OTH-1"
    migration.op_client.values[12] = "kept"
    result = run(migration, source(1, [history(5, _SAFE)]))  # OTH is not a selected project
    assert result.success
    assert migration.op_client.values[12] == "kept"
    assert result.details["cleared"] == 0


def test_stored_value_without_origin_fails_the_run(migration):
    migration.mappings.get_mapping.side_effect = {"project": {"APP": 7}, "work_package": {}}.get
    migration.op_client.values[11] = "stale"
    del migration.op_client.origins[11]
    result = run(migration)
    assert not result.success
    assert result.details["clear_unconfirmed"] == 1
    assert migration.op_client.values[11] == "stale"


def test_standard_custom_field_needs_review(migration, monkeypatch):
    from src import config

    kind = "com.atlassian.jira.plugin.system.customfieldtypes:textfield"
    migration.jira_client.get_custom_fields.return_value = [
        {"id": f"customfield_{n}", "name": name, "schema": {"custom": kind}} for n, name in ((1, "Scope"), (2, "Notes"))
    ]
    monkeypatch.setitem(config.migration_config, "history_approved_field_ids", "customfield_1")
    reviewed = {**_SAFE, "field": "Scope", "fieldId": "customfield_1", "fieldtype": "custom"}
    unreviewed = {**_SAFE, "field": "Notes", "fieldId": "customfield_2", "fieldtype": "custom"}
    run(migration, source(1, [history(5, reviewed, unreviewed)]))
    assert [c["field"] for c in stored(migration, 1)["events"][0]["changes"]] == ["Scope"]


def test_failed_write_is_counted_once_and_outcomes_reconcile(migration):
    migration.op_client.fail_write = True
    comment_only = history(8, {**_SAFE, "field": "Comment"})
    issues = [
        source(1, [history(5, _SAFE)]),
        source(2, [history(6, _SAFE)], security={"id": "30"}),
        source(3, [history(7, _SAFE)], total=2),
        source(4, [comment_only]),
        source(5, [history(9, _SAFE)]),
    ]
    unchanged = module.canonical_history("105", module.kept_events([history(9, _SAFE)], set()))
    migration.op_client.values[15] = unchanged
    result = run(migration, *issues)
    details = result.details
    assert not result.success
    assert details["failed"] == 2  # The whole batch, including the row already up to date.
    outcomes = ("issues_written", "unchanged", "no_kept_changes", "incomplete", "restricted_skipped")
    outcomes += ("unsupported_project_type", "target_unconfirmed", "failed")
    assert sum(details[name] for name in outcomes) == details["issues_seen"] == 5
    assert details["not_returned"] == 4


# --- Level work packages (ADR-009, step 5b) ---------------------------------------------------

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
# The level was set on the issue; Jira shows this change to everyone who can browse the issue.
SECURITY = {
    "field": "security",
    "fieldtype": "jira",
    "from": None,
    "fromString": None,
    "to": "10000",
    "toString": "Mgmt",
}


def _history_issue(number, level=None):
    page = {"startAt": 0, "maxResults": 100, "total": 1, "histories": [history(number, _SAFE, SECURITY)]}
    fields = {"project": {"key": "APP", "id": "10", "projectTypeKey": "software"}}
    fields["security"] = {"id": level} if level else None
    return SimpleNamespace(key=f"APP-{number}", id=str(number), raw={"fields": fields, "changelog": page})


class _LevelJira:
    """Answer the level and ordinary searches; record every search with its fields and expand."""

    def __init__(self, moved=(), omitted=()):
        self.searches = []
        self.moved, self.omitted = set(moved), set(omitted)
        self.base_url = "https://jira.local"
        self.get_projects = Mock(return_value=[{"key": "APP"}])
        self.get_custom_fields = Mock(return_value=[])
        self.jira = Mock()
        self.jira.search_issues.side_effect = self.search

    def search(self, jql, startAt, maxResults, **kwargs):
        self.searches.append((jql, kwargs.get("fields"), kwargs.get("expand")))
        if jql == LEVEL_LIGHT and kwargs.get("fields") == "security":
            rows = level_issues()
        elif jql == LEVEL_LIGHT:
            rows = [
                _history_issue(n, "10001" if n in self.moved else level)
                for n, level in LEVEL_OF.items()
                if n not in self.omitted
            ]
        elif jql == LEVEL_ORDINARY:
            rows = [_history_issue(1)]
        else:
            raise AssertionError(jql)
        return rows[startAt : startAt + maxResults]


class _LevelOpenProject(FakeOpenProject):
    """The field history fake with an ordinary project 7, the level sub-project 100 and the live read."""

    def __init__(self, live=None):
        super().__init__()
        self.live = LEVEL_LIVE if live is None else live
        self.live_reads = []
        self.origins = {500: "1", 510: "10", 900: "10", 901: "11"}
        self.projects = {500: 7, 510: 7, 900: 100, 901: 100}
        self.keys = {500: "APP-1", 510: "APP-10", 900: "APP-10", 901: "APP-11"}

    def execute_query_to_json_file(self, script):
        self.live_reads.append(json.loads(script.split("\n")[1]))
        return json.loads(json.dumps(self.live))


@pytest.fixture
def level_setup(level_work_package_setup):  # noqa: F811 (the imported fixture, requested by name)
    """The reports, mappings and switch of the level work package tests."""
    return level_work_package_setup


def _level_run(
    level_setup,
    *,
    live=None,
    moved=(),
    omitted=(),
    level_map=None,
    ordinary=None,
    stored=None,
    approved=(),
    runs=1,
    **kw,
):
    mappings, _ = level_setup(**kw)
    from src import config

    config.migration_config["history_approved_field_ids"] = approved
    mappings.data["work_package"] = {"1": {"jira_key": "APP-1", "openproject_id": 500, "project_key": "APP"}}
    mappings.data["work_package"].update(ordinary or {})
    mappings.data["security_level_work_package"] = dict(LEVEL_MAP if level_map is None else level_map)
    writes = []
    original = mappings.set_mapping
    mappings.set_mapping = lambda name, value: (writes.append(name), original(name, value))
    jira, op = _LevelJira(moved, omitted), _LevelOpenProject(live)
    op.values.update(stored or {})
    migration = WorkPackageFieldHistoryMigration(jira_client=jira, op_client=op)
    results = [migration.run() for _ in range(runs)]
    return SimpleNamespace(result=results[-1], results=results, jira=jira, op=op, writes=writes)


def _written(op):
    return {(row["project"], row["wp"]) for batch in op.writes for row in batch if row["value"] is not None}


def test_level_switch_off_issues_exactly_the_calls_of_the_ordinary_path(level_setup):
    outcome = _level_run(level_setup, on=False, reports=False)
    assert outcome.result.success, outcome.result
    # The first page and the empty page that ends the search.
    assert outcome.jira.searches == [(LEVEL_ORDINARY, "project,security", "changelog")] * 2
    assert _written(outcome.op) == {(7, 500)}
    assert not outcome.op.live_reads
    assert not outcome.jira.get_projects.called
    assert outcome.writes == []
    assert "security_level" not in outcome.result.details


def test_level_switch_on_writes_history_into_the_work_package_its_sub_project_holds(level_setup):
    outcome = _level_run(level_setup)
    assert outcome.result.success, outcome.result
    assert _written(outcome.op) == {(7, 500), (100, 900), (100, 901)}
    assert (LEVEL_LIGHT, "project,security", "changelog") in outcome.jira.searches
    assert outcome.op.live_reads == [{"project_id": 100, "field": "J2O Origin Key"}]
    value = json.loads(outcome.op.values[900])
    assert value["jiraIssueId"] == "10"
    # The security change is kept: everyone who reads the sub-project can browse the issue in Jira.
    assert [c["field"] for c in value["events"][0]["changes"]] == ["summary", "security"]
    # Archived and unmapped issues get nothing.
    assert set(outcome.op.values) == {500, 900, 901}
    assert outcome.result.details["security_level"] == {"work_packages": 2, "refused": 0}
    assert outcome.writes == []


@pytest.mark.parametrize(
    "options",
    [
        {"live": {"field": True, "work_packages": [901], "keys": [[901, "APP-11"]]}},
        {"moved": {10}},
        {"omitted": {10}},
    ],
)
def test_level_issue_that_cannot_be_confirmed_gets_no_history(level_setup, options):
    outcome = _level_run(level_setup, **options)
    assert not outcome.result.success
    assert _written(outcome.op) == {(7, 500), (100, 901)}
    assert outcome.result.details["security_level"]["refused"] == 1
    assert outcome.result.failed == 1


def test_level_issue_the_selection_does_not_keep_gets_nothing_even_with_an_entry(level_setup):
    level_map = {**LEVEL_MAP, "12": {**LEVEL_MAP["10"], "jira_key": "APP-12", "openproject_id": 902}}
    outcome = _level_run(level_setup, level_map=level_map)
    assert not outcome.result.success
    assert _written(outcome.op) == {(7, 500), (100, 900), (100, 901)}
    assert outcome.result.details["security_level"]["refused"] == 1


def test_level_unreadable_sub_project_refuses_all_its_issues(level_setup):
    outcome = _level_run(level_setup, live={"field": True})
    assert not outcome.result.success
    assert _written(outcome.op) == {(7, 500)}
    assert outcome.result.details["security_level"]["refused"] == 2


def test_level_issue_with_an_ordinary_work_package_still_has_that_value_cleared(level_setup):
    # APP-10 has an earlier ordinary work package with a stored value: it is cleared as before,
    # and the level work package gets nothing.
    ordinary = {"10": {"jira_key": "APP-10", "openproject_id": 510, "project_key": "APP"}}
    outcome = _level_run(level_setup, ordinary=ordinary, stored={510: "old"})
    assert not outcome.result.success
    assert 510 not in outcome.op.values
    assert outcome.result.details["cleared"] == 1
    assert _written(outcome.op) == {(7, 500), (100, 901)}
    assert outcome.result.details["security_level"]["refused"] == 1


def test_level_repeated_run_on_one_instance_gives_the_same_result(level_setup):
    outcome = _level_run(level_setup, moved={10}, runs=2)
    first, second = outcome.results
    assert first.details["security_level"] == second.details["security_level"] == {"work_packages": 2, "refused": 1}
    assert first.failed == second.failed == 1
    assert second.details["unchanged"] == 2
    assert len(outcome.op.live_reads) == 2
    assert 900 not in outcome.op.values


def test_level_refusals_are_reported_when_the_extract_fails(level_setup):
    outcome = _level_run(level_setup, live={"field": True}, approved="not-a-field-id")
    assert not outcome.result.success
    assert not outcome.op.writes
    assert outcome.result.details["security_level"] == {"work_packages": 0, "refused": 2}


def test_level_switch_on_without_reports_stops_before_any_search_or_write(level_setup):
    outcome = _level_run(level_setup, reports=False)
    assert not outcome.result.success
    assert not outcome.jira.searches
    assert not outcome.op.writes
