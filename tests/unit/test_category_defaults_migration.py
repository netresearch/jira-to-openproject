# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import config
from src.application.components.category_defaults_migration import CategoryDefaultsMigration, _rule
from src.migration import _invoke_component

BASE = "https://source.example.invalid"
LEAD = {"name": "alice", "key": "juser-2", "active": True}
PROJECT = {"projectTypeKey": "software", "id": "7", "key": "ABC", "assigneeType": "UNASSIGNED", "lead": LEAD}
ROW = {
    "id": "17",
    "name": "Backend",
    "projectId": 7,
    "project": "ABC",
    "self": BASE + "/rest/api/2/component/17",
    "lead": LEAD,
    "assignee": LEAD,
    "realAssignee": LEAD,
    "isAssigneeTypeValid": True,
    "assigneeType": "COMPONENT_LEAD",
    "realAssigneeType": "COMPONENT_LEAD",
}
USERS = {"juser-2": {"openproject_id": 123}}


@pytest.mark.parametrize("mode", ["COMPONENT_LEAD", "PROJECT_LEAD"])
def test_only_the_confirmed_effective_lead_uses_a_mapped_user(mode):
    row = {**ROW, "assigneeType": mode, "realAssigneeType": mode}
    if mode == "PROJECT_LEAD":
        row["lead"] = None
    rule = _rule(row, PROJECT, BASE, USERS)
    assert rule["candidate"] == {
        "id": 123,
        "user_key": "juser-2",
        "user_id": "alice",
        "url": BASE + "/secure/ViewProfile.jspa?name=alice",
    }


@pytest.mark.parametrize(
    "patch",
    [
        {"assigneeType": "PROJECT_DEFAULT", "realAssigneeType": "PROJECT_DEFAULT"},
        {"assigneeType": "UNASSIGNED", "realAssigneeType": "UNASSIGNED"},
        {"isAssigneeTypeValid": False},
        {"realAssigneeType": "PROJECT_DEFAULT"},
        {"realAssignee": {"name": "someone else"}},
        {"realAssignee": None},
        {"archived": True},
        {"deleted": True},
    ],
)
def test_preserve_policies_without_applying_an_unproven_default(patch):
    rule = _rule({**ROW, **patch}, PROJECT, BASE, USERS)
    assert rule["candidate"] is None
    assert rule["policy"]["lead"] == LEAD


def test_preserve_customer_facing_project_rule_for_separate_access_decision():
    rule = _rule(ROW, {**PROJECT, "projectTypeKey": "service_desk"}, BASE, USERS)
    assert rule["candidate"] is None
    assert rule["policy"]["assigneeType"] == "COMPONENT_LEAD"


def test_reject_conflicting_component_origin():
    with pytest.raises(ValueError, match="component identity"):
        _rule({**ROW, "projectId": 9}, PROJECT, BASE, USERS)


def test_default_invocation_uses_sdk_and_reports_held_policies(monkeypatch):
    monkeypatch.setitem(config.jira_config, "projects", ["ABC"])
    jira = Mock(base_url=BASE)
    jira.jira.project.return_value.raw = PROJECT
    jira.jira.project_components.return_value = [SimpleNamespace(raw=ROW)]
    op = Mock()
    op.execute_large_query_to_json_file.return_value = {"updated": 1, "held": 1, "native": 0, "preserved": 1}
    migration = CategoryDefaultsMigration(jira_client=jira, op_client=op)
    migration.mappings = Mock()
    migration.mappings.get_mapping.side_effect = {"project": {"ABC": 7, "OTHER": 8}, "user": USERS}.get
    result = _invoke_component("category_defaults", migration)
    assert result.success
    assert result.updated == 1
    assert result.warnings
    assert result.details["held"] == 1
    jira.jira.project_components.assert_called_once_with("ABC")
    jira.jira.project_components.side_effect = ConnectionError("source unavailable")
    assert not _invoke_component("category_defaults", migration).success
    op.execute_large_query_to_json_file.assert_called_once()
