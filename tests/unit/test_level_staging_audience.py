# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""After the late access grant the level stages accept exactly the readers' rows (ADR-012)."""

import json
from unittest.mock import Mock

import pytest

from src.application.components.level_access import Audience, staging_audience
from src.application.components.security_level_projects_migration import SecurityLevelProjectsMigration, load_reports
from src.infrastructure.openproject.component_assignments import apply_component_assignments
from src.infrastructure.openproject.tasklist_origins import apply_tasklists
from tests.unit import test_components_migration as components
from tests.unit import test_level_versions as versions
from tests.unit import test_level_work_packages as wp
from tests.unit import test_simpletasks_migration as tasks
from tests.unit.test_level_work_packages import STATE, issue, level_issues
from tests.unit.test_level_work_packages import setup as level_work_package_setup  # noqa: F401  (fixture)
from tests.unit.test_security_level_access import grant as access_grant  # noqa: F401  (fixture)

NOBODY = {"members": [], "shares": {}}
READERS = Audience(
    members={"APP:10000": frozenset({41, 42})},
    shares={"APP-12": frozenset({43})},
    share_levels={"APP-12": "APP:10000"},
)
OF_10000 = {"members": [41, 42], "shares": {"APP-12": [43]}}


@pytest.fixture
def level_setup(level_work_package_setup):  # noqa: F811 (the imported fixture, requested by name)
    return level_work_package_setup


@pytest.fixture
def granted(access_grant):  # noqa: F811 (the imported fixture, requested by name)
    """The readers file, reports and user mapping of the access grant tests."""
    return access_grant


@pytest.fixture
def readers(monkeypatch):
    """The staging audience the level scope gets from a readers file of the same inventory."""
    monkeypatch.setattr(
        "src.application.components.level_issues.staging_audience", lambda mappings, report, edition: READERS
    )


def test_without_a_readers_file_nobody_may_be_a_member(level_setup):
    mappings, _ = level_setup()
    assert staging_audience(mappings, {}, "community").of_level("APP:10000") == NOBODY


def test_a_readers_file_gives_each_level_its_own_readers(granted):
    outcome = granted(edition="enterprise")
    audience = staging_audience(outcome.component.mappings, *load_reports())
    assert audience.of_level("APP:10000") == OF_10000
    assert audience.of_level("APP:10001") == {"members": [], "shares": {}}


def test_a_readers_file_of_another_inventory_stops_the_staging_checks(granted):
    from src.application.components.security_level_projects_migration import LevelProjectError
    from tests.unit.test_security_level_access import READERS as FILE

    outcome = granted(readers={**FILE, "archive_sha256": "c" * 64})
    with pytest.raises(LevelProjectError, match="another inventory"):
        staging_audience(outcome.component.mappings, *load_reports())


def _skeleton(level_setup, state, **kw):
    return wp.skeleton(level_setup, unrestricted=[issue(1)], levels=level_issues(), state=state, **kw)


def _members_state(member_ids):
    """The sub-project read answered like the Ruby check: every member must be in the sent audience."""
    return {**STATE, "members": len(member_ids), "member_ids": member_ids}


@pytest.fixture
def judged(monkeypatch):
    """A skeleton double that judges the members against the audience the component sent."""
    original = wp.FakeOpenProject

    def factory(state=None, existing=None, fail=False):
        op = original(state, existing, fail)
        read = op.execute_query_to_json_file.side_effect

        def judge(script):
            answer = read(script)
            if "member_ids" in answer:
                allowed = set(json.loads(script.split("\n")[1])["audience"]["members"])
                answer["audience"] = set(answer.pop("member_ids")) <= allowed
            return answer

        op.execute_query_to_json_file.side_effect = judge
        return op

    monkeypatch.setattr(wp, "FakeOpenProject", factory)


def test_the_skeleton_writes_into_a_sub_project_whose_members_are_its_readers(level_setup, readers, judged):
    outcome = _skeleton(level_setup, _members_state([41, 42]))
    assert outcome.result.success, outcome.result.error
    assert outcome.op.reads[0]["audience"] == OF_10000
    assert {p["project_id"] for p in outcome.op.payloads} == {7, 100}


@pytest.mark.parametrize("member_ids", [[41, 99], [43]], ids=["another-user", "a-share-reader-as-member"])
def test_the_skeleton_refuses_a_sub_project_with_another_member(level_setup, readers, judged, member_ids):
    outcome = _skeleton(level_setup, _members_state(member_ids))
    assert not outcome.result.success
    assert {p["project_id"] for p in outcome.op.payloads} == {7}


def test_without_readers_the_skeleton_still_needs_a_memberless_sub_project(level_setup, judged):
    outcome = _skeleton(level_setup, _members_state([41]))
    assert not outcome.result.success
    assert outcome.op.reads[0]["audience"] == NOBODY


def test_components_tasks_and_version_copies_send_the_readers_of_their_sub_project(level_setup, readers):
    categories = components._level_run(level_setup)
    assert categories.result.success, categories.result
    assert [p["audience"] for p in categories.op.payloads if "parent_id" in p] == [OF_10000]
    tasklists = tasks._level_run(level_setup)
    assert [p["audience"] for p in tasklists.op.payloads if "parent" in p] == [OF_10000]
    copies = versions._run(level_setup, "versions")
    assert [p["audience"] for p in copies.op.copy_syncs] == [OF_10000]


def test_the_ruby_writers_check_the_sent_audience_instead_of_any_member():
    class Recorder:
        def __init__(self):
            self.queries = []

        def execute_large_query_to_json_file(self, query, timeout):
            self.queries.append(query)
            return {}

    client = Recorder()
    apply_component_assignments(client, {"project_id": 100, "parent_id": 7, "audience": OF_10000}, {})
    apply_tasklists(client, {"project": 100, "parent": 7, "audience": OF_10000, "rows": []})
    for query in client.queries:
        assert "level_audience = lambda do |project, audience|" in query
        assert 'level_audience.call(project, data.fetch("audience"))' in query
        assert "Member.where(project_id: project.id).exists?" not in query


def test_security_level_projects_accepts_an_existing_sub_project_of_readers(monkeypatch, tmp_path):
    import src.config as cfg
    from src.restricted_issues import ADMIN_REPORT_FORMAT, REPORT_FORMAT
    from tests.unit.test_security_level_projects_migration import APP, FakeMappings, FakeOpenProject, level

    sha = "a" * 64
    report, admin = tmp_path / "report.json", tmp_path / "admins.json"
    report.write_text(json.dumps({"format": REPORT_FORMAT, "levels": [level()], "archive_sha256": sha}))
    admin.write_text(json.dumps({"format": ADMIN_REPORT_FORMAT, "archive_sha256": sha, "edition": "community"}))
    monkeypatch.setattr(
        cfg, "migration_config", {"security_level_report": str(report), "security_level_admin_report": str(admin)}
    )
    monkeypatch.setattr(cfg, "mappings", FakeMappings({"APP": APP}))
    audience = Audience(members={"APP:10000": frozenset({41})})
    monkeypatch.setattr(
        "src.application.components.level_access.staging_audience", lambda mappings, report, edition: audience
    )
    existing = {"id": 9, "identifier": "app_level_10000", "public": False, "parent_id": 7}
    for member_ids, success in (([41], True), ([41, 99], False)):
        target = FakeOpenProject(projects=[existing | {"members": len(member_ids), "member_ids": member_ids}])
        result = SecurityLevelProjectsMigration(jira_client=Mock(), op_client=target).run()
        assert result.success is success
        assert target.finds()[0]["audience"] == {"members": [41], "shares": {}}
        assert target.creations() == []
