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
        self.fail_write = False

    def ensure_wp_custom_field_id(self, name, field_format):
        assert (name, field_format) == (module.FIELD_NAME, "text")
        return _FIELD_ID

    def execute_script_with_data(self, script, data):
        assert data["field"] == _FIELD_ID
        if script == module._READ:
            ids = data["ids"]
            rows = [[wp, 1, self.origins[wp]] for wp in ids] + [
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
    tagged = {**_SAFE, "field": "WorklogTimeSpent", "toString": "3h", "visibility": {"type": "role", "value": "Admins"}}
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


def test_worklog_items_stay_out_of_the_project_value(migration):
    worklog = {**_SAFE, "field": "WorklogId", "toString": "9001"}
    spent = {**_SAFE, "field": "WorklogTimeSpent", "toString": "3h"}
    result = run(migration, source(1, [history(5, _SAFE, worklog, spent)]))
    assert [c["field"] for c in stored(migration, 1)["events"][0]["changes"]] == ["summary"]
    assert result.details["withheld_changes"] == 2


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
