# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Change detection must see every entity a component reports, or run the component."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.application.components.admin_scheme_migration import AdminSchemeMigration
from src.application.components.reporting_migration import ReportingMigration
from src.application.components.workflow_migration import WorkflowMigration
from src.utils.change_aware_runner import ChangeAwareRunner
from src.utils.change_detector import ChangeDetector


class _Mappings:
    def __init__(self) -> None:
        self._m = {
            "project": {"PROJ": {"openproject_id": 11}, "OTHER": {"openproject_id": 12}},
            "user": {"alice": {"openproject_id": 21}},
            "group": {"devs": {"openproject_id": 31}},
        }

    def get_mapping(self, name: str):
        return self._m.get(name, {})

    def set_mapping(self, name: str, value) -> None:
        self._m[name] = value


@pytest.fixture
def mappings(monkeypatch: pytest.MonkeyPatch) -> _Mappings:
    import src.config as cfg

    installed = _Mappings()
    monkeypatch.setattr(cfg, "mappings", installed, raising=False)
    return installed


class _Jira:
    def __init__(self) -> None:
        self.roles = {
            "PROJ": [{"name": "Developers", "actors": [{"type": "atlassian-group-role-actor", "name": "devs"}]}],
            "OTHER": [{"name": "Developers", "actors": []}],
        }

    def get_project_roles(self, project_key: str):
        return self.roles[project_key]

    def get_project_permission_scheme(self, project_key: str):
        return {"id": 1, "name": "Default"}


class _Op:
    def __init__(self) -> None:
        self.group_calls: list[list[dict]] = []

    def get_roles(self):
        return [{"id": 4, "name": "Project member"}]

    def assign_group_roles(self, assignments):
        self.group_calls.append(assignments)
        return {"updated": len(assignments), "errors": 0}

    def assign_user_roles(self, **kwargs):
        return {"success": True}


def test_admin_schemes_run_cold_skip_unchanged_and_rerun_on_role_change(mappings, tmp_path) -> None:
    jira, op = _Jira(), _Op()
    migration = AdminSchemeMigration(jira_client=jira, op_client=op)  # type: ignore[arg-type]
    migration.change_detector = ChangeDetector(tmp_path)

    first = migration.run_with_change_detection("admin_schemes")
    assert first.success is True
    assert len(op.group_calls) == 1

    repeat = migration.run_with_change_detection("admin_schemes")
    assert repeat.details["status"] == "skipped"
    assert len(op.group_calls) == 1

    jira.roles["OTHER"][0]["actors"].append({"type": "atlassian-group-role-actor", "name": "devs"})
    changed = migration.run_with_change_detection("admin_schemes")
    assert changed.success is True
    assert len(op.group_calls) == 2
    assert changed.details["change_report"]["changes_by_type"] == {"updated": 1}


def test_entities_without_identifier_never_report_no_changes(tmp_path) -> None:
    calls = []
    migration = SimpleNamespace(
        change_detector=ChangeDetector(tmp_path),
        entity_cache=SimpleNamespace(clear_global=lambda: None, invalidate=lambda _name: None),
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, debug=lambda *a, **k: None),
        _get_current_entities_for_type=lambda _name: [{"anonymous": True}],
        run=lambda: calls.append(1) or SimpleNamespace(success=False),
    )
    migration.should_skip_migration = lambda name, cache: ChangeAwareRunner(migration).should_skip(name, cache)

    skip, report = ChangeAwareRunner(migration).should_skip("anything")

    assert skip is False
    assert report["summary"]["current_entity_count"] == 0


@pytest.mark.parametrize(
    "case",
    [
        (
            WorkflowMigration,
            "workflows",
            SimpleNamespace(get_issue_types=list, get_workflow_schemes=list),
            SimpleNamespace(get_roles=list),
        ),
        (
            ReportingMigration,
            "reporting",
            SimpleNamespace(get_filters=list, get_dashboards=list),
            SimpleNamespace(),
        ),
    ],
)
def test_aggregate_inventories_are_identifiable(mappings, tmp_path, case) -> None:
    component, entity_type, jira, op = case
    migration = component(jira_client=jira, op_client=op)  # type: ignore[arg-type]
    entities = migration._get_current_entities_for_type(entity_type)

    report = ChangeDetector(tmp_path).detect_changes(entities, entity_type)

    assert report["summary"]["current_entity_count"] == len(entities) == 1
