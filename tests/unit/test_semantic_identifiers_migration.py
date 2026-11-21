# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Jira keys become semantic work package identifiers; refusals are never overwritten (ADR-011)."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components import level_issues
from src.application.components import semantic_identifiers_migration as module
from src.application.components.semantic_identifiers_migration import SemanticIdentifiersMigration
from src.migration import _invoke_component

pytestmark = pytest.mark.unit

UNRESTRICTED = 'project = "APP" AND level IS EMPTY ORDER BY id'
SUB = level_issues.LevelTarget("APP:10000", "10000", 100, 7, "app_level_10000", "static")


def key_change(old, new):
    return {"id": "1", "items": [{"field": "Key", "fromString": old, "toString": new}]}


def source(number, *histories, key=None, level=None):
    raw = {
        "fields": {"security": None if level is None else {"id": level}},
        "changelog": {"histories": list(histories)},
    }
    return SimpleNamespace(key=key or f"APP-{number}", id=str(100 + number), raw=raw)


class FakeOpenProject:
    """Record every script payload and answer it like the Rails side would."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.project_refusal = None
        self.write_answer = None
        self.write_fails = False

    def execute_script_with_data(self, script, data):
        name = {module._PROJECT: "project", module._WRITE: "write", module._COUNTER: "counter"}[script]
        self.calls.append((name, data))
        if name == "project":
            return {"status": "success", "data": {"refused": self.project_refusal}}
        if name == "counter":
            return {"status": "success", "data": {"counters": {str(data["project"]): 9}}}
        if self.write_fails:
            return {"status": "error", "data": None}
        answer = self.write_answer or {"written": len(data["rows"]), "unchanged": 0, "refused": []}
        return {"status": "success", "data": answer}

    def payloads(self, name):
        return [data for call, data in self.calls if call == name]


@pytest.fixture
def migration(monkeypatch):
    from src import config

    monkeypatch.setitem(config.jira_config, "projects", ["APP"])
    monkeypatch.setitem(config.migration_config, module.SWITCH, True)
    monkeypatch.setitem(config.migration_config, level_issues.SWITCH, False)
    app = SemanticIdentifiersMigration(jira_client=Mock(), op_client=FakeOpenProject())
    maps = {
        "project": {"APP": 7},
        "work_package": {str(100 + n): {"jira_key": f"APP-{n}", "openproject_id": 10 + n} for n in (1, 2)},
    }
    app.mappings = Mock()
    app.mappings.get_mapping.side_effect = maps.get
    return app


def run(migration, *issues):
    migration.jira_client.jira.search_issues.side_effect = [list(issues), []]
    return _invoke_component("semantic_identifiers", migration)


def row(number, *aliases):
    return {"wp": 10 + number, "id": str(100 + number), "key": f"APP-{number}", "seq": number, "aliases": list(aliases)}


@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_switch_off_reads_and_writes_nothing(migration, monkeypatch, value):
    from src import config

    monkeypatch.setitem(config.migration_config, module.SWITCH, value)
    result = run(migration, source(1))
    assert result.success
    assert result.details == {"enabled": False}
    assert migration.op_client.calls == []
    assert not migration.jira_client.jira.search_issues.called
    assert not migration.mappings.get_mapping.called


def test_identifier_and_sequence_number_are_the_jira_key(migration):
    result = run(migration, source(1), source(2))
    assert result.success, result
    assert [name for name, _ in migration.op_client.calls] == ["project", "write", "counter"]
    assert migration.op_client.payloads("project") == [{"project": 7, "key": "APP"}]
    assert migration.op_client.payloads("write") == [{"project": 7, "rows": [row(1), row(2)]}]
    assert result.updated == 2
    for call in migration.jira_client.jira.search_issues.call_args_list:
        assert call.args[0] == UNRESTRICTED
        assert call.kwargs["expand"] == "changelog"
    assert not migration.mappings.set_mapping.called


def test_counter_is_raised_for_the_key_prefix_of_the_project(migration):
    result = run(migration, source(1))
    assert migration.op_client.payloads("counter") == [{"project": 7, "key": "APP", "sub_projects": []}]
    assert result.details["counters"] == {"7": 9}


def test_earlier_keys_from_the_key_changelog_become_aliases(migration):
    histories = [
        key_change("OLD-5", "MID-3"),
        key_change("MID-3", "APP-1"),
        {"id": "2", "items": [{"field": "status", "fromString": "OLD-9", "toString": "Done"}]},
        key_change("not a key", None),
    ]
    run(migration, source(1, *histories))
    assert migration.op_client.payloads("write")[0]["rows"] == [row(1, "MID-3", "OLD-5")]


def test_unchanged_work_packages_are_counted_on_a_repeat_run(migration):
    run(migration, source(1))
    migration.op_client.write_answer = {"written": 0, "unchanged": 1, "refused": []}
    result = run(migration, source(1))
    assert result.success
    assert result.updated == 0
    assert result.details["unchanged"] == 1
    first, second = migration.op_client.payloads("write")
    assert first == second


@pytest.mark.parametrize(
    "reason", ["work package has another identifier", "identifier or alias owned by another work package"]
)
def test_a_refused_work_package_fails_the_run(migration, reason):
    migration.op_client.write_answer = {"written": 1, "unchanged": 0, "refused": [{"key": "APP-2", "reason": reason}]}
    result = run(migration, source(1), source(2))
    assert not result.success
    assert result.failed == 1
    assert result.details["refused"] == 1
    assert result.updated == 1


def test_a_mapped_issue_the_search_does_not_return_is_counted(migration):
    result = run(migration, source(1))
    assert result.details["not_returned"] == 1
    assert migration.op_client.payloads("write")[0]["rows"] == [row(1)]


@pytest.mark.parametrize(
    "answer",
    [
        {"written": 1, "unchanged": 0, "refused": []},
        {"written": 2, "unchanged": 1, "refused": []},
        {"written": True, "unchanged": 1, "refused": []},
        {"written": 3, "unchanged": -1, "refused": []},
        {"written": 1, "unchanged": 0, "refused": ["APP-1"]},
    ],
)
def test_an_answer_that_does_not_account_for_every_row_fails_the_batch(migration, answer):
    migration.op_client.write_answer = answer
    result = run(migration, source(1), source(2))
    assert not result.success
    assert result.details["failed"] == 2
    assert result.updated == 0


def test_a_failed_batch_counts_every_row(migration):
    migration.op_client.write_fails = True
    result = run(migration, source(1), source(2))
    assert not result.success
    assert result.failed == 2
    assert result.details["failed"] == 2


@pytest.mark.parametrize("key", ["APPLICATION1", "1APP", "AP-P", "APP_LONGKEY"])
def test_a_key_that_is_no_semantic_project_identifier_refuses_the_project(migration, key):
    migration.mappings.get_mapping.side_effect = {"project": {key: 7}, "work_package": {}}.get
    from src import config

    config.jira_config["projects"] = [key]
    result = run(migration)
    assert not result.success
    assert migration.op_client.calls == []
    assert result.details["projects_refused"] == 1


def test_a_project_the_rails_side_refuses_gets_no_work_package_writes(migration):
    migration.op_client.project_refusal = "project key taken by another project"
    result = run(migration, source(1))
    assert not result.success
    assert [name for name, _ in migration.op_client.calls] == ["project"]
    assert not migration.jira_client.jira.search_issues.called


def test_an_issue_whose_key_is_not_of_the_project_is_refused(migration):
    result = run(migration, source(1), source(2, key="OTHER-2"))
    assert not result.success
    assert migration.op_client.payloads("write")[0]["rows"] == [row(1)]
    assert result.details["refused"] == 1


@pytest.fixture
def level(migration, monkeypatch):
    from src import config

    monkeypatch.setitem(config.migration_config, level_issues.SWITCH, True)
    selected = level_issues.LevelWorkPackages(
        work_packages={"APP-10": 900}, level_keys={"APP-10"}, level_ids={"APP-10": "10000"}, targets={"APP-10": SUB}
    )
    monkeypatch.setattr(level_issues, "level_work_packages", lambda *args: selected)
    searches = []

    def iter_level(jira, key, size, fields, expand):
        searches.append((key, fields, expand))
        return iter(migration.level_source)

    monkeypatch.setattr(level_issues, "iter_level_issues", iter_level)
    migration.level_source = [source(10, key_change("OLD-4", "APP-10"), level="10000")]
    migration.searches = searches
    return migration


def test_level_work_packages_get_the_jira_key_in_their_sub_project(level):
    result = run(level, source(1))
    assert result.success, result
    assert level.searches == [("APP", "security", "changelog")]
    writes = level.op_client.payloads("write")
    assert writes == [
        {"project": 7, "rows": [row(1)]},
        {"project": 100, "rows": [{**row(10, "OLD-4"), "wp": 900}]},
    ]
    assert level.op_client.payloads("counter") == [{"project": 7, "key": "APP", "sub_projects": [100]}]
    assert result.details["security_level"] == {"work_packages": 1, "refused": 0}


def test_level_issue_with_an_ordinary_work_package_is_refused_on_both_sides(level):
    maps = {
        "project": {"APP": 7},
        "work_package": {str(100 + n): {"jira_key": f"APP-{n}", "openproject_id": 10 + n} for n in (1, 10)},
    }
    level.mappings.get_mapping.side_effect = maps.get
    result = run(level, source(1), source(10))
    assert not result.success
    assert level.op_client.payloads("write") == [{"project": 7, "rows": [row(1)]}]
    assert result.details["security_level"] == {"work_packages": 0, "refused": 1}


def test_level_work_package_whose_level_changed_is_refused(level):
    level.level_source = [source(10, level="10001")]
    result = run(level, source(1))
    assert not result.success
    assert [w["project"] for w in level.op_client.payloads("write")] == [7]
    assert level.op_client.payloads("counter")[0]["sub_projects"] == []


def test_level_work_package_the_search_omits_is_refused(level):
    level.level_source = []
    result = run(level, source(1))
    assert not result.success
    assert result.details["security_level"] == {"work_packages": 1, "refused": 1}
    assert [w["project"] for w in level.op_client.payloads("write")] == [7]
