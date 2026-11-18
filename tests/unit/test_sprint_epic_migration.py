# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components.registry import DEFAULT_COMPONENT_SEQUENCE, PREDEFINED_PROFILES
from src.application.components.sprint_epic_migration import SprintEpicMigration


@pytest.fixture
def migration(monkeypatch):
    mappings = {
        "work_package": {"111": {"jira_key": "DEMO-1", "openproject_id": 10}, "DEMO-2": 20},
        "project": {"DEMO": {"openproject_id": 4}},
        "sprint": {
            "42": {"openproject_id": 9, "project_id": 4},
            "Closed, sprint": {"openproject_id": 999, "project_id": 5},
        },
    }
    monkeypatch.setattr("src.config.mappings", SimpleNamespace(get_mapping=mappings.get))
    jira = Mock()
    jira.get_custom_fields.return_value = [
        {"id": "customfield_123", "schema": {"custom": "com.pyxis.greenhopper.jira:gh-sprint"}},
        {"id": "customfield_456", "schema": {"custom": "com.pyxis.greenhopper.jira:gh-epic-link"}},
    ]
    jira.batch_get_issues.return_value = {
        "DEMO-1": SimpleNamespace(fields=SimpleNamespace()),
        "DEMO-2": {
            "fields": {
                "customfield_456": "DEMO-1",
                "customfield_123": [
                    {"id": 42, "name": "Closed, sprint"},
                    {"id": 43, "name": "Next sprint"},
                ],
                "customfield_10008": "WRONG-1",
            }
        },
    }
    op = Mock()
    op.ensure_wp_custom_field_id.return_value = 101
    op.batch_update_work_packages.side_effect = lambda rows: {"updated": len(rows), "failed": 0}
    op.bulk_set_wp_custom_field_values.side_effect = lambda rows: {"updated": len(rows), "failed": 0}
    return SprintEpicMigration(jira_client=jira, op_client=op), mappings


@pytest.mark.parametrize("sequence", [DEFAULT_COMPONENT_SEQUENCE, PREDEFINED_PROFILES["full"]])
def test_cold_run_dependencies(sequence):
    assert sequence.index("work_packages_skeleton") < sequence.index("agile_boards") < sequence.index("sprint_epic")
    assert (
        sequence.index("work_packages_skeleton")
        < sequence.index("work_packages_content")
        < sequence.index("sprint_epic")
    )


def test_real_field_ids_and_confirmed_writes(migration):
    mig, _ = migration
    result = mig.run_with_change_detection("sprint_epic")
    assert result.success, result
    assert result.updated == 4
    assert [call.args[0] for call in mig.op_client.batch_update_work_packages.call_args_list] == [
        [{"id": 20, "parent_id": 10}],
        [{"id": 20, "version_id": 9}],
    ]
    fields = mig.op_client.bulk_set_wp_custom_field_values.call_args_list
    assert fields[0].args[0] == [
        {"work_package_id": 20, "custom_field_id": 101, "value": "Closed, sprint, Next sprint"}
    ]
    assert json.loads(fields[1].args[0][0]["value"]) == [
        {"id": "42", "name": "Closed, sprint"},
        {"id": "43", "name": "Next sprint"},
    ]


def test_missing_issue_parent_and_unconfirmed_writes(migration):
    mig, _ = migration
    del mig.jira_client.batch_get_issues.return_value["DEMO-1"]
    mig.jira_client.batch_get_issues.return_value["DEMO-2"]["fields"]["customfield_456"] = "OTHER-7"
    mig.op_client.batch_update_work_packages.side_effect = lambda rows: {"updated": 0, "failed": len(rows)}
    mig.op_client.bulk_set_wp_custom_field_values.side_effect = lambda rows: {}
    result = mig.run()
    assert not result.success
    assert result.failed == 5
    assert result.updated == 0
    assert (
        result.details["parent_links"]
        == result.details["version_assignments"]
        == result.details["sprint_cf_updates"]
        == 0
    )
    assert "DEMO-1" in result.details["missing_issues"]
    assert "OTHER-7" in result.details["unresolved_epics"].values()


def test_release_and_sprint_history_coexist(migration):
    mig, _ = migration
    fields = mig.jira_client.batch_get_issues.return_value["DEMO-2"]["fields"]
    fields["fixVersions"] = [{"name": "Release 1"}]
    result = mig.run()
    assert result.success
    assert result.details["version_assignments"] == 0
    assert result.details["sprint_cf_updates"] == 1


def test_unavailable_field_and_missing_version(migration):
    mig, mappings = migration
    mappings["sprint"].clear()
    mig.op_client.ensure_wp_custom_field_id.return_value = 0
    result = mig.run()
    assert not result.success
    assert result.updated == 1
    assert result.failed == 3
    assert "DEMO-2" in result.details["unresolved_sprints"]
    assert result.details["sprint_cf_updates"] == 0


def test_unavailable_mapping(migration):
    mig, mappings = migration
    mappings["work_package"].clear()
    result = mig.run()
    assert not result.success
    mig.jira_client.batch_get_issues.assert_not_called()
    mig.op_client.bulk_set_wp_custom_field_values.assert_not_called()


def test_serialized_server_sprint_and_sdk_epic(migration):
    mig, _ = migration
    mig.jira_client.batch_get_issues.return_value["DEMO-2"] = SimpleNamespace(
        fields=SimpleNamespace(
            customfield_456="DEMO-1",
            customfield_123=[
                "com.atlassian.greenhopper.service.sprint.Sprint@10ab[id=42,rapidViewId=7,state=CLOSED,name=Closed, sprint,startDate=2026-04-01,endDate=2026-04-15]",
            ],
        )
    )
    result = mig.run()
    assert result.updated == 4
    assert mig.op_client.bulk_set_wp_custom_field_values.call_args_list[0].args[0][0]["value"] == "Closed, sprint"


def test_subtask_keeps_immediate_parent_instead_of_inherited_epic(migration):
    mig, mappings = migration
    mappings["work_package"]["DEMO-3"] = 30
    mig.jira_client.batch_get_issues.return_value["DEMO-3"] = {"fields": {}}
    mig.jira_client.batch_get_issues.return_value["DEMO-2"]["fields"]["parent"] = {"key": "DEMO-3"}
    result = mig.run()
    assert result.success
    assert mig.op_client.batch_update_work_packages.call_args_list[0].args[0] == [{"id": 20, "parent_id": 30}]


def test_missing_subtask_parent_is_actionable(migration):
    mig, _ = migration
    mig.jira_client.batch_get_issues.return_value["DEMO-2"]["fields"]["parent"] = {"key": "DEMO-99"}
    result = mig.run()
    assert not result.success
    assert result.details["unresolved_parents"] == {"DEMO-2": "DEMO-99"}
    assert result.details["parent_links"] == 0


def test_recovers_missing_work_package_mapping_from_project_origin_keys(migration):
    mig, mappings = migration
    mappings["work_package"].clear()
    mig.op_client.get_project_wp_cf_snapshot.return_value = [
        {"id": 10, "jira_issue_key": "DEMO-1"},
        {"id": 20, "jira_issue_key": "DEMO-2"},
        {"id": 90, "jira_issue_key": None},
    ]
    result = mig.run()
    assert result.success
    assert result.updated == 4
    assert not mappings["work_package"]  # A key-only recovery cannot replace the shared Jira ID mapping.
    mig.op_client.get_project_wp_cf_snapshot.assert_called_once_with(4)
    mig.op_client.get_project_wp_cf_snapshot.return_value.append({"id": 11, "jira_issue_key": "DEMO-1"})
    assert not mig.run().success


def test_same_named_sprints_without_versions_keep_both_ids(migration):
    mig, mappings = migration
    mappings["sprint"].clear()
    fields = mig.jira_client.batch_get_issues.return_value["DEMO-2"]["fields"]
    fields["fixVersions"] = [{"id": 75}]
    fields["customfield_123"] = [{"name": "Sprint, one", "id": 21}, {"name": "Sprint, one", "id": 22}]
    result = mig.run()
    assert result.success
    assert result.details["version_assignments"] == 0
    assert result.details["sprint_history_updates"] == 1
    history = mig.op_client.bulk_set_wp_custom_field_values.call_args.args[0][0]["value"]
    assert json.loads(history) == [{"id": "21", "name": "Sprint, one"}, {"id": "22", "name": "Sprint, one"}]
