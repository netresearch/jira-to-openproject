# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Private, memberless sub-projects per Jira project and security level (ADR-009, step 3)."""

import json
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components.project_migration import project_identifier
from src.application.components.registry import DEFAULT_COMPONENT_SEQUENCE
from src.application.components.security_level_projects_migration import (
    LevelProjectError,
    SecurityLevelProjectsMigration,
    level_identifier,
)
from src.infrastructure.exceptions import QueryExecutionError
from src.migration import _build_component_factories
from src.restricted_issues import ADMIN_REPORT_FORMAT, REPORT_FORMAT

SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
APP = {
    "jira_key": "APP",
    "jira_name": "App",
    "openproject_id": 7,
    "openproject_identifier": "app",
    "openproject_name": "App",
}


class FakeOpenProject:
    """Answer the component's Rails scripts from an in-memory project table and record them."""

    def __init__(self, projects=(), parents=None, provenance=None):
        self.projects = {row["identifier"]: dict(row) for row in projects}
        self.parents = {7: "App", 8: "Ops"} if parents is None else parents
        self.provenance = provenance or {}
        self.restores = []
        self.scripts = []

    def restore_entity_mappings_from_provenance(self, entity_type):
        self.restores.append(entity_type)
        return self.provenance

    def execute_query_to_json_file(self, script):
        self.scripts.append(script)
        values = json.loads(script.split("\n")[1])
        project = self.projects.get(values["identifier"])
        if "Project.new(" not in script:
            state = {"found": True, **project} if project else {"found": False}
            if project:
                # The Ruby check with the audience the component sent: who may already be a member.
                allowed = set(values["audience"]["members"])
                state["audience"] = set(project.get("member_ids", [])) <= allowed and project["members"] == len(
                    project.get("member_ids", [])
                )
            reads_parent = 'parent = Project.find_by(id: input.fetch("parent_id"))' in script
            if reads_parent and '.merge("parent_name" => parent&.name)' in script:
                state["parent_name"] = self.parents.get(values.get("parent_id"))
            return state
        if "Project.new(" in script and project is None:
            public = re.search(r"public: (true|false)", script).group(1) == "true"
            project = {"id": 100 + len(self.projects), "identifier": values["identifier"], "public": public}
            project |= {"parent_id": values["parent_id"], "members": 0}
            self.projects[values["identifier"]] = project
        return {"found": True, **project, "audience": True} if project else {"found": False}

    def creations(self):
        return [script for script in self.scripts if "Project.new(" in script]

    def finds(self):
        return [json.loads(script.split("\n")[1]) for script in self.scripts if "Project.new(" not in script]


class FailingOpenProject(FakeOpenProject):
    """Raise like the Rails runner does when the script for one identifier fails."""

    def __init__(self, failing):
        super().__init__()
        self.failing = failing

    def execute_query_to_json_file(self, script):
        if self.failing in script:
            raise QueryExecutionError("save failed")
        return super().execute_query_to_json_file(script)


class FakeMappings:
    def __init__(self, project):
        self.data = {"project_mapping": project, "security_level_project_mapping": {}}

    def get_mapping(self, name):
        return self.data.get(f"{name}_mapping", {})

    def set_mapping(self, name, value):
        self.data[f"{name}_mapping"] = dict(value)


def level(level_id="10000", static=1, per_issue=0, unexplained=0, project="APP"):
    return {
        "project": project,
        "level_id": level_id,
        "level": "Management",
        "static": static,
        "per_issue": per_issue,
        "unexplained": unexplained,
    }


@pytest.fixture
def run(monkeypatch, tmp_path):
    def _run(levels, *, edition="community", admin_sha=SHA, admins=True, projects=(), mapping=None, selected=()):
        report = tmp_path / "report.json"
        report.write_text(json.dumps({"format": REPORT_FORMAT, "levels": levels, "archive_sha256": SHA}))
        settings = {"security_level_report": str(report)}
        if admins:
            admin = tmp_path / "admins.json"
            admin.write_text(
                json.dumps({"format": ADMIN_REPORT_FORMAT, "archive_sha256": admin_sha, "edition": edition})
            )
            settings["security_level_admin_report"] = str(admin)
        elif admins is None:
            settings["security_level_admin_report"] = str(tmp_path / "missing.json")
        mappings = mapping or FakeMappings({"APP": APP})
        monkeypatch.setattr("src.config.migration_config", settings)
        monkeypatch.setattr("src.config.mappings", mappings)
        monkeypatch.setattr("src.config.selected_jira_projects", lambda: list(selected))
        target = projects if isinstance(projects, FakeOpenProject) else FakeOpenProject(projects)
        result = SecurityLevelProjectsMigration(jira_client=Mock(), op_client=target).run()
        return SimpleNamespace(result=result, target=target, mapping=mappings.data["security_level_project_mapping"])

    return _run


def test_creates_a_private_sub_project_without_members(run):
    outcome = run([level()])
    assert outcome.result.success, outcome.result.errors
    [script] = outcome.target.creations()
    assert "public: false, parent: parent" in script
    assert "p.project_types.build(type_id: t.type_id" in script
    assert "p.types" not in script
    assert "Projects::CreateService" not in script
    assert not re.search(r"Members?::|Member\.(new|create)|members\s*<<|add_member", script)
    assert json.loads(script.split("\n")[1]) == {
        "identifier": "app_level_10000",
        "name": "App (Management)",
        "parent_id": 7,
        "audience": {"members": [], "shares": {}},
        "description": "Issues of APP with the Jira security level Management (ID 10000).",
    }
    assert outcome.target.projects["app_level_10000"] == {
        "id": 100,
        "identifier": "app_level_10000",
        "public": False,
        "parent_id": 7,
        "members": 0,
    }
    assert outcome.mapping == {
        "APP:10000": {
            "jira_project_key": "APP",
            "level_id": "10000",
            "level": "Management",
            "openproject_id": 100,
            "openproject_identifier": "app_level_10000",
            "parent_openproject_id": 7,
        }
    }
    assert outcome.result.details["created"] == 1


def test_rerun_creates_nothing_new(run):
    first = run([level()])
    second = run([level()], projects=first.target)
    assert len(first.target.creations()) == 1
    assert second.result.success
    assert (second.result.details["created"], second.result.details["verified"]) == (0, 1)
    assert second.mapping == first.mapping


@pytest.mark.parametrize(
    ("existing", "reason"),
    [
        ({"public": True, "members": 0, "parent_id": 7}, "public"),
        ({"public": False, "members": 1, "parent_id": 7}, "members"),
        ({"public": False, "members": 0, "parent_id": 8}, "another parent"),
    ],
)
def test_existing_project_with_the_identifier_is_refused_and_left_alone(run, existing, reason):
    outcome = run([level()], projects=[{"id": 55, "identifier": "app_level_10000", **existing}])
    assert not outcome.result.success
    assert reason in outcome.result.errors[0]
    assert outcome.target.creations() == []
    assert outcome.mapping == {}


def test_identifier_of_a_migrated_jira_project_is_refused(run):
    mappings = FakeMappings(
        {"APP": APP, "OTHER": {**APP, "openproject_id": 9, "openproject_identifier": "app_level_10000"}}
    )
    projects = [{"id": 9, "identifier": "app_level_10000", "public": False, "parent_id": 7, "members": 0}]
    outcome = run([level()], projects=projects, mapping=mappings)
    assert not outcome.result.success
    assert "migrated Jira project" in outcome.result.errors[0]
    assert outcome.target.scripts == []


def test_name_of_a_migrated_jira_project_is_refused(run):
    mappings = FakeMappings(
        {"APP": APP, "MGT": {**APP, "jira_name": "App (Management)", "openproject_identifier": "mgt"}}
    )
    outcome = run([level()], mapping=mappings)
    assert "name is taken" in outcome.result.errors[0]
    assert outcome.target.finds() == [
        {"identifier": "app_level_10000", "parent_id": 7, "audience": {"members": [], "shares": {}}}
    ]
    assert outcome.target.creations() == []


RESTORED = {"jira_key": "APP", "jira_name": "App", "openproject_id": 7, "matched_by": "j2o_provenance"}


def creation(outcome):
    [script] = outcome.target.creations()
    return json.loads(script.split("\n")[1])


def test_the_name_comes_from_the_parent_in_openproject(run):
    outcome = run([level()], projects=FakeOpenProject(parents={7: "App Renamed"}))
    assert outcome.result.success, outcome.result.errors
    assert creation(outcome)["name"] == "App Renamed (Management)"


def test_restored_entry_without_a_name_uses_the_parents_live_name(run):
    target = FakeOpenProject(parents={7: "App Live"})
    outcome = run([level()], projects=target, mapping=FakeMappings({"APP": {**RESTORED, "restored_from_op": True}}))
    assert outcome.result.success, outcome.result.errors
    assert target.finds() == [
        {"identifier": "app_level_10000", "parent_id": 7, "audience": {"members": [], "shares": {}}}
    ]
    assert (creation(outcome)["name"], creation(outcome)["parent_id"]) == ("App Live (Management)", 7)
    assert target.restores == []
    assert outcome.mapping["APP:10000"]["parent_openproject_id"] == 7


def test_empty_mapping_resolves_the_parent_from_provenance_once(run):
    target = FakeOpenProject(parents={7: "App Live"}, provenance={"APP": RESTORED})
    outcome = run([level("10000"), level("10001")], projects=target, mapping=FakeMappings({}))
    assert outcome.result.success, outcome.result.errors
    assert target.restores == ["project"]
    assert [json.loads(s.split("\n")[1])["parent_id"] for s in target.creations()] == [7, 7]
    assert {json.loads(s.split("\n")[1])["name"] for s in target.creations()} == {"App Live (Management)"}


def test_names_restored_from_provenance_are_known(run):
    other = {**RESTORED, "jira_key": "MGT", "jira_name": "App Live (Management)", "openproject_id": 9}
    target = FakeOpenProject(parents={7: "App Live"}, provenance={"APP": RESTORED, "MGT": other})
    outcome = run([level()], projects=target, mapping=FakeMappings({}))
    assert "name is taken" in outcome.result.errors[0]
    assert target.creations() == []


def test_parent_found_nowhere_is_refused_without_a_script(run):
    target = FakeOpenProject()
    outcome = run([level()], projects=target, mapping=FakeMappings({}))
    assert not outcome.result.success
    assert target.restores == ["project"]
    assert "provenance registry" in outcome.result.errors[0]
    assert target.scripts == []
    assert outcome.mapping == {}


def test_parent_missing_in_openproject_is_refused(run):
    outcome = run([level()], projects=FakeOpenProject(parents={}))
    assert not outcome.result.success
    assert "parent project is not in OpenProject" in outcome.result.errors[0]
    assert outcome.target.creations() == []


@pytest.mark.parametrize("key", ["APP_LEVEL_10000", "APP-LEVEL-10000"])
def test_level_identifier_never_equals_a_key_derived_identifier(key):
    assert level_identifier("APP", "10000") != project_identifier(key)


@pytest.mark.parametrize("level_id", ["", "0", "01", "1\u0663", "10000 "])
def test_level_identifier_needs_an_ascii_level_number(level_id):
    with pytest.raises(LevelProjectError):
        level_identifier("APP", level_id)


def test_levels_without_migratable_issues_get_no_sub_project(run):
    outcome = run([level(static=0, per_issue=0, unexplained=3)], edition="enterprise")
    assert outcome.result.success
    assert outcome.target.scripts == []


@pytest.mark.parametrize(("edition", "created"), [("community", 0), ("enterprise", 1)])
def test_per_issue_only_levels_need_enterprise(run, edition, created):
    outcome = run([level(static=0, per_issue=2)], edition=edition)
    assert outcome.result.success
    assert len(outcome.target.creations()) == created


@pytest.mark.parametrize(
    ("admins", "admin_sha", "message"),
    [(False, SHA, "not configured"), (None, SHA, "FileNotFoundError"), (True, "b" * 64, "another archive")],
)
def test_missing_or_mismatched_admin_report_stops_before_any_write(run, admins, admin_sha, message):
    outcome = run([level()], admins=admins, admin_sha=admin_sha)
    assert not outcome.result.success
    assert message in outcome.result.message
    assert outcome.target.scripts == []
    assert outcome.mapping == {}


def test_a_failing_level_does_not_stop_the_others_or_the_mapping(run):
    target = FailingOpenProject("app_level_10001")
    outcome = run([level("10000"), level("10001"), level("10002")], projects=target)
    assert not outcome.result.success
    assert outcome.result.errors == ["APP:10001: save failed"]
    assert sorted(outcome.mapping) == ["APP:10000", "APP:10002"]


def test_rerun_keeps_other_entries_and_honours_the_project_filter(run):
    mappings = FakeMappings(
        {"APP": APP, "OPS": {**APP, "jira_name": "Ops", "openproject_name": "Ops", "openproject_id": 8}}
    )
    mappings.data["security_level_project_mapping"] = {"OLD:1": {"openproject_id": 3}}
    outcome = run([level(project="APP"), level(project="OPS")], mapping=mappings, selected=["OPS"])
    assert sorted(outcome.mapping) == ["OLD:1", "OPS:10000"]
    assert [json.loads(s.split("\n")[1])["identifier"] for s in outcome.target.creations()] == ["ops_level_10000"]


def test_component_is_available_but_not_in_the_default_profile():
    factories = _build_component_factories(Mock(), Mock())
    assert isinstance(factories["security_level_projects"](), SecurityLevelProjectsMigration)
    assert "security_level_projects" not in DEFAULT_COMPONENT_SEQUENCE
