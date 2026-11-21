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
    SecurityLevelProjectsMigration,
    level_identifier,
)
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

    def __init__(self, projects=()):
        self.projects = {row["identifier"]: dict(row) for row in projects}
        self.scripts = []

    def execute_query_to_json_file(self, script):
        self.scripts.append(script)
        values = json.loads(script.split("\n")[1])
        project = self.projects.get(values["identifier"])
        if "Project.new(" in script and project is None:
            public = re.search(r"public: (true|false)", script).group(1) == "true"
            project = {"id": 100 + len(self.projects), "identifier": values["identifier"], "public": public}
            project |= {"parent_id": values["parent_id"], "members": 0}
            self.projects[values["identifier"]] = project
        return {"found": True, **project} if project else {"found": False}

    def creations(self):
        return [script for script in self.scripts if "Project.new(" in script]


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
    def _run(levels, *, edition="community", admin_sha=SHA, admins=True, projects=(), mapping=None):
        report = tmp_path / "report.json"
        report.write_text(json.dumps({"format": REPORT_FORMAT, "levels": levels, "archive_sha256": SHA}))
        settings = {"security_level_report": str(report)}
        if admins:
            admin = tmp_path / "admins.json"
            admin.write_text(
                json.dumps({"format": ADMIN_REPORT_FORMAT, "archive_sha256": admin_sha, "edition": edition})
            )
            settings["security_level_admin_report"] = str(admin)
        mappings = mapping or FakeMappings({"APP": APP})
        monkeypatch.setattr("src.config.migration_config", settings)
        monkeypatch.setattr("src.config.mappings", mappings)
        monkeypatch.setattr("src.config.selected_jira_projects", list)
        target = projects if isinstance(projects, FakeOpenProject) else FakeOpenProject(projects)
        result = SecurityLevelProjectsMigration(jira_client=Mock(), op_client=target).run()
        return SimpleNamespace(result=result, target=target, mapping=mappings.data["security_level_project_mapping"])

    return _run


def test_creates_a_private_sub_project_without_members(run):
    outcome = run([level()])
    assert outcome.result.success, outcome.result.errors
    [script] = outcome.target.creations()
    assert "public: false, parent: parent" in script
    assert "Projects::CreateService" not in script
    assert not re.search(r"Members?::|Member\.(new|create)|members\s*<<|add_member", script)
    assert json.loads(script.split("\n")[1]) == {
        "identifier": "app_level_10000",
        "name": "App (Management)",
        "parent_id": 7,
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
    assert outcome.target.scripts == []


@pytest.mark.parametrize("key", ["APP_LEVEL_10000", "APP-LEVEL-10000"])
def test_level_identifier_never_equals_a_key_derived_identifier(key):
    assert level_identifier("APP", "10000") != project_identifier(key)


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
    [(False, SHA, "not configured"), (True, "b" * 64, "another archive")],
)
def test_missing_or_mismatched_admin_report_stops_before_any_write(run, admins, admin_sha, message):
    outcome = run([level()], admins=admins, admin_sha=admin_sha)
    assert not outcome.result.success
    assert message in outcome.result.message
    assert outcome.target.scripts == []
    assert outcome.mapping == {}


def test_component_is_available_but_not_in_the_default_profile():
    factories = _build_component_factories(Mock(), Mock())
    assert isinstance(factories["security_level_projects"](), SecurityLevelProjectsMigration)
    assert "security_level_projects" not in DEFAULT_COMPONENT_SEQUENCE
