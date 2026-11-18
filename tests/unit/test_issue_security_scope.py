# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Positive Jira scope and existing-work-package reconciliation."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components.issue_security import (
    mapped_project_issues,
    permits_unrestricted_result,
    unrestricted_project_jql,
    warn_if_restricted_mappings,
)
from src.application.components.work_package_skeleton_migration import WorkPackageSkeletonMigration


def issue(number, **fields):
    return SimpleNamespace(
        id=str(number), key=f"APP-{number}", fields=SimpleNamespace(**fields), raw={"fields": fields}
    )


@pytest.mark.parametrize("level", [{"id": "7"}, {}, "", False])
def test_explicit_issue_level_or_malformed_field_is_not_unrestricted(level):
    assert not permits_unrestricted_result(issue(1, security=level))
    assert not permits_unrestricted_result({"fields": {"security": level}})
    assert not permits_unrestricted_result({"fields": None})


def test_positive_query_includes_omitted_and_explicit_null():
    assert permits_unrestricted_result(issue(1))
    assert permits_unrestricted_result(issue(1, security=None))
    assert unrestricted_project_jql('A"B') == 'project = "A\\"B" AND level IS EMPTY'


def test_skeleton_creation_requires_positive_scope_before_payload(monkeypatch):
    monkeypatch.delenv("J2O_MAX_ISSUES", raising=False)
    migration = object.__new__(WorkPackageSkeletonMigration)
    migration.batch_size = 5
    migration.logger = Mock()
    migration.work_package_mapping = {}
    migration.jira_client = Mock()
    safe, restricted = issue(1), issue(2, security={"id": "7"})
    # A contradictory result from Jira must also be rejected before target writes.
    migration.jira_client.jira.search_issues.side_effect = [[safe, restricted], []]
    migration._build_skeleton_payload = Mock(return_value={"subject": "allowed"})
    migration._flush_skeletons = Mock()

    migration._create_project_skeletons("APP", 4, 5)

    for call in migration.jira_client.jira.search_issues.call_args_list:
        assert " AND level IS EMPTY ORDER BY" in call.args[0]
        assert "security" in call.kwargs["fields"].split(",")
    migration._build_skeleton_payload.assert_called_once_with(safe, 4, 5)
    assert migration._flush_skeletons.call_args.args[1] == [safe]


def test_previous_mappings_must_still_be_confirmed_in_project():
    known = mapped_project_issues(
        "APP",
        {"101": {"project_key": "APP", "jira_key": "APP-1"}, "APP-2": 22, "102": {"project_key": "APP"}, "ELSE-3": 3},
    )
    assert known == {"APP-1", "APP-2", "102"}
    jira = Mock()
    jira.jira.search_issues.side_effect = [[issue(1), issue(2, security={"id": 9})], []]
    log = Mock()
    warn_if_restricted_mappings(jira, "APP", known, log)
    assert " AND level IS EMPTY AND issue IN" in jira.jira.search_issues.call_args.args[0]
    assert log.warning.call_args.args[1:] == (2, "APP")
