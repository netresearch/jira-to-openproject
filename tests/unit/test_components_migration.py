# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components.components_migration import ComponentsMigration
from src.migration import _invoke_component
from src.models import ComponentResult


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
