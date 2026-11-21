# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Late access grant of the level sub-projects: exactly the readers, read back (ADR-009 step 6, ADR-012)."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components.security_level_access_migration import SecurityLevelAccessMigration
from src.restricted_issues import READERS_FORMAT
from tests.unit.test_level_work_packages import LEVELS, SHA, level_issues, row
from tests.unit.test_level_work_packages import setup as level_work_package_setup  # noqa: F401  (fixture)

READER, MEMBER, VIEWER = 4, 3, 8
ARCHIVED = [
    {"key": "APP-12", "level_id": "10000", "class": "per_issue", "extra_readers": 1},
    {"key": "APP-13", "level_id": "10000", "class": "unexplained", "extra_readers": 1},
]
READERS = {
    "format": READERS_FORMAT,
    "archive_sha256": SHA,
    "levels": [
        {
            "project": "APP",
            "level_id": "10000",
            "static_readers": ["reader.one", "reader.two"],
            "per_issue": {"APP-12": ["guest.reader"]},
        },
        {"project": "APP", "level_id": "10001", "static_readers": ["reader.three"], "per_issue": {}},
    ],
}
USERS = {
    "JIRAUSER1": {"jira_name": "reader.one", "openproject_id": 41},
    "JIRAUSER2": {"jira_name": "Reader.Two", "openproject_id": 42},
    "JIRAUSER3": {"jira_name": "guest.reader", "openproject_id": 43},
}
LIVE = {"field": True, "work_packages": [900, 901, 902], "keys": [[900, "APP-10"], [901, "APP-11"], [902, "APP-12"]]}


class FakeRails:
    """The membership rows of sub-project 100 and the parent roles; records every script's input."""

    def __init__(self, members=(), parent_roles=None, *, locked=(), users=(41, 42, 43), roles=True, drop=False):
        self.members = [list(m) for m in members]
        self.parent_roles = {"41": [MEMBER]} if parent_roles is None else parent_roles
        self.locked, self.users, self.roles, self.drop = list(locked), list(users), roles, drop
        self.states, self.writes = [], []

    def execute_query_to_json_file(self, script):
        values = json.loads(script.split("\n")[1])
        if "WorkPackageCustomField" in script:
            return json.loads(json.dumps(LIVE))
        if "create" in values:
            self.writes.append(values)
            for grant in values["create"]:
                self.members.append(["User", grant["user"], None, None, sorted(grant["roles"])])
            for share in values["shares"]:
                self.members.append(["User", share["user"], "WorkPackage", share["wp"], [share["role"]]])
            return {"members": self.members[:-1] if self.drop else self.members}
        self.states.append(values)
        asked = set(values["users"])
        return {
            "reader_role": READER if self.roles else None,
            "viewer_role": VIEWER,
            "parent_roles": {k: v for k, v in self.parent_roles.items() if int(k) in asked},
            "locked": [u for u in self.locked if u in asked],
            "found": [u for u in self.users if u in asked],
            "members": self.members,
        }


@pytest.fixture
def grant(level_work_package_setup, tmp_path):  # noqa: F811 (the imported fixture, requested by name)
    def _grant(op=None, *, readers=READERS, users=USERS, runs=1, **kw):
        import src.config as cfg

        levels = [row() | {"static_readers": 2}, row("10001", ("APP-20",), 0, 0) | {"static_readers": 1}]
        mappings, _ = level_work_package_setup(report_levels=levels, archived=ARCHIVED, **kw)
        path = tmp_path / "readers.json"
        path.write_text(json.dumps(readers))
        path.chmod(0o600)
        cfg.migration_config["security_level_readers"] = str(path)
        mappings.data["user"] = users
        jira = Mock()
        jira.get_projects.return_value = [{"key": "APP"}]
        jira.jira.search_issues.side_effect = lambda jql, startAt, maxResults, **_: (
            level_issues()[startAt : startAt + maxResults] if jql == LEVELS else pytest.fail(jql)
        )
        op = op or FakeRails()
        component = SecurityLevelAccessMigration(jira_client=jira, op_client=op)
        results = [component.run() for _ in range(runs)]
        return SimpleNamespace(result=results[-1], results=results, op=op, jira=jira, component=component)

    return _grant


def test_switch_off_changes_nothing(grant):
    outcome = grant(on=False)
    assert outcome.result.success
    assert outcome.result.details["status"] == "skipped"
    assert (outcome.op.states, outcome.op.writes) == ([], [])


def test_community_grants_the_static_readers_with_their_parent_roles(grant):
    outcome = grant()
    assert outcome.result.success, outcome.result.errors
    # reader.one holds Member in the parent; Reader.Two holds no parent role and gets Reader.
    assert outcome.op.writes == [
        {
            "project_id": 100,
            "parent_id": 7,
            "create": [{"user": 41, "roles": [MEMBER]}, {"user": 42, "roles": [READER]}],
            "shares": [],
        }
    ]
    assert outcome.op.states[0]["users"] == [41, 42]
    assert outcome.result.details["memberships_created"] == 2
    assert outcome.result.details["shares_created"] == 0
    outcome.jira.jira.search_issues.assert_not_called()


def test_a_repeat_run_reads_back_and_writes_nothing(grant):
    outcome = grant(runs=2)
    assert [r.success for r in outcome.results] == [True, True]
    assert len(outcome.op.writes) == 1
    assert (outcome.result.details["granted"], outcome.result.details["verified"]) == (0, 1)


def test_enterprise_shares_each_per_issue_work_package_with_its_extra_readers_only(grant):
    outcome = grant(edition="enterprise")
    assert outcome.result.success, outcome.result.errors
    assert outcome.op.writes[0]["shares"] == [{"wp": 902, "user": 43, "role": VIEWER}]
    assert outcome.result.details["shares_created"] == 1
    again = grant(outcome.op, edition="enterprise")
    assert len(outcome.op.writes) == 1
    assert again.result.details["verified"] == 1


def test_a_reader_without_an_openproject_user_is_reported_not_guessed(grant):
    users = {**USERS, "JIRAUSER2": {"jira_name": "Reader.Two", "openproject_id": 42, "restored_from_op": True}}
    users["JIRAUSER9"] = {"jira_name": "reader.one", "openproject_id": 49}
    outcome = grant(FakeRails(users=(41, 42, 43, 49)), users=users)
    assert outcome.result.success
    # reader.one maps to two OpenProject users, Reader.Two only through a guessed entry, and
    # reader.three (the unmapped level) has no entry.
    assert outcome.op.writes == []
    assert outcome.result.details["readers_without_user"] == 3


@pytest.mark.parametrize(
    "member",
    [
        pytest.param(["User", 99, None, None, [MEMBER]], id="another-user"),
        pytest.param(["Group", 50, None, None, [MEMBER]], id="a-group"),
        pytest.param(["User", 41, None, None, [5]], id="another-role"),
        pytest.param(["User", 43, "WorkPackage", 900, [VIEWER]], id="a-share-of-a-static-work-package"),
    ],
)
def test_a_foreign_membership_refuses_the_sub_project_and_is_not_removed(grant, member):
    op = FakeRails(members=[member])
    outcome = grant(op, edition="enterprise")
    assert not outcome.result.success
    assert outcome.op.writes == []
    assert op.members == [member]
    assert outcome.result.details["refused"] == 1


def test_a_locked_reader_gets_no_membership_and_is_counted(grant):
    outcome = grant(FakeRails(locked=[42]))
    assert outcome.result.success
    assert outcome.op.writes[0]["create"] == [{"user": 41, "roles": [MEMBER]}]
    assert outcome.result.details["locked_readers"] == 1


def test_a_read_back_that_differs_fails(grant):
    outcome = grant(FakeRails(drop=True))
    assert not outcome.result.success
    assert outcome.result.errors == ["APP:10000: the memberships read back differ from the readers"]


def test_without_the_reader_role_nothing_is_written(grant):
    outcome = grant(FakeRails(parent_roles={}, roles=False))
    assert not outcome.result.success
    assert outcome.op.writes == []


def test_a_readers_file_of_another_inventory_stops_before_any_openproject_read(grant):
    outcome = grant(readers={**READERS, "archive_sha256": "c" * 64})
    assert not outcome.result.success
    assert outcome.result.errors == ["security_level"]
    assert outcome.op.states == []


def test_the_component_runner_reaches_the_grant(grant):
    from src.migration import _invoke_component

    outcome = grant()
    op = FakeRails()
    result = _invoke_component(
        "security_level_access", SecurityLevelAccessMigration(jira_client=outcome.jira, op_client=op)
    )
    assert result.success, result.message
    assert len(op.writes) == 1


def test_community_audience_has_no_shares_and_counts_only_static_readers():
    from src.application.components.level_access import audience
    from src.application.components.level_readers import Readers

    readers = Readers({"APP:10000": frozenset({"reader.one"})}, {"APP-12": frozenset({"nobody"})})
    community = audience(readers, USERS, shares=False)
    assert (community.members, community.shares) == ({"APP:10000": {41}}, {})
    assert sum(community.unmapped.values()) == 0
    enterprise = audience(readers, USERS, shares=True)
    assert enterprise.shares == {"APP-12": frozenset()}
    assert enterprise.unmapped["APP-12"] == 1


def test_shares_belong_to_the_sub_project_of_their_work_package():
    from src.application.components.level_access import Audience
    from src.application.components.level_issues import LevelTarget, LevelWorkPackages

    here = LevelTarget("APP:10000", "10000", 100, 7, "app_level_10000", "per_issue")
    there = LevelTarget("APP:10001", "10001", 101, 7, "app_level_10001", "per_issue")
    level = LevelWorkPackages(work_packages={"APP-12": 902, "APP-21": 903}, targets={"APP-12": here, "APP-21": there})
    shared = Audience(shares={"APP-12": frozenset({43}), "APP-21": frozenset({44}), "APP-99": frozenset({45})})
    assert SecurityLevelAccessMigration._shares(level, shared, here) == {902: {43}}
    assert SecurityLevelAccessMigration._shares(None, shared, here) == {}
