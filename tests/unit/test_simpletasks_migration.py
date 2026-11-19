# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import config
from src.application.components.simpletasks_migration import SimpleTasksMigration
from src.infrastructure.jira.sidebar_tasks import issue_task_records
from src.migration import _invoke_component


def _render(identifier="42", key="J-1", checked=True):
    flag = " checked" if checked else ""
    return (
        f'<div class="task" data-issue="{key}" data-task-id="{identifier}"><div>'
        f'<input type="checkbox" class="task-input" data-task-id="{identifier}"{flag}/></div></div>'
    )


def _raw(source, rendered):
    return {"key": "J-1", "fields": {"description": source}, "renderedFields": {"description": rendered}}


@pytest.mark.parametrize(
    ("source", "rendered"),
    [
        ("{task:id=42}title{task}", _render(key="X-1")),
        ("{task:id=42}title{task}", _render("41")),
        ("{task:id=42}title{task}", _render() * 2),
        ("{task:id=42}one{task}{task:id=42}two{task}", _render()),
        ("{task:type=other}title{task}", _render()),
        ("{task}title{task}", _render()),
        ("{task:id=42}title", _render()),
    ],
)
def test_inconsistent_description_tasks_abort_before_writing(source, rendered):
    from src.infrastructure.jira.description_tasks import description_tasks

    raw = _raw(source, rendered)
    with pytest.raises(ValueError):
        description_tasks("J-1", raw)


def test_missing_description_tasks_and_issue_identity():
    from src.infrastructure.jira.description_tasks import description_tasks

    assert description_tasks("J-1", _raw(None, None)) == []
    other = _raw(None, None)
    with pytest.raises(ValueError, match="different issue"):
        description_tasks("J-2", other)


def test_issue_property_only_falls_back_on_missing():
    from unittest.mock import Mock

    from src.infrastructure.jira.jira_client import JiraApiError, JiraResourceNotFoundError
    from src.infrastructure.jira.jira_field_service import JiraFieldService

    client = Mock()
    fields = JiraFieldService(client)
    client._make_request.side_effect = JiraApiError("unavailable")
    with pytest.raises(JiraApiError, match="unavailable"):
        fields.get_issue_property("J-1", "example/key")
    client._make_request.assert_called_with("/rest/api/2/issue/J-1/properties/example%2Fkey")
    client._make_request.side_effect = JiraResourceNotFoundError("missing")
    assert fields.get_issue_property("J-1", "example") is None
    sdk_error = Exception("missing")
    sdk_error.status_code = 404
    wrapped = JiraApiError("upstream request failed")
    wrapped.__cause__ = sdk_error
    client._make_request.side_effect = wrapped
    assert fields.get_issue_property("J-1", "example") is None
    client._make_request.side_effect = None
    client._make_request.return_value.json.return_value = {"value": []}
    assert fields.get_issue_property("J-1", "example") == []
    client._make_request.return_value.json.return_value = "bad"
    with pytest.raises(JiraApiError, match="Expected an object"):
        fields.get_issue_property("J-1", "example")


def test_checklist_writer_requires_a_verified_result():
    from unittest.mock import Mock

    from src.infrastructure.openproject.openproject_work_package_content_service import (
        OpenProjectWorkPackageContentService,
    )

    client = Mock()
    service = OpenProjectWorkPackageContentService(client)
    client.execute_script_with_data.return_value = {"status": "success", "data": {"verified": True}}
    assert service.set_checklist_section(12, "example")
    script, data = client.execute_script_with_data.call_args.args
    assert "example" not in script
    assert data == {"id": 12, "markdown": "example"}
    client.execute_script_with_data.return_value = {"status": "success"}
    assert not service.set_checklist_section(12, "example")


def _project_issue():
    raw = _raw("{task:id=42}inline{task}", _render())
    raw.update(id="90")
    raw["fields"].update({"project": {"id": "7", "key": "J", "projectTypeKey": "software"}, "customfield_42": "3.0"})
    return raw


def _rows():
    common = {"issue": "J-1", "issueId": 90, "projectId": 7, "checked": True, "manuallyCreated": False}
    return [
        {**common, "id": 42, "text": "inline", "ordinal": 4, "mandatory": True, "lastChangedBy": "example"},
        {**common, "id": 43, "text": "direct task", "ordinal": 2, "manuallyCreated": True, "checked": False},
        {**common, "id": 44, "text": "restricted comment", "ordinal": 3},
    ]


@pytest.fixture
def stage(monkeypatch):
    jira = Mock(base_url="https://jira.example.invalid")
    jira.get_custom_fields.return_value = [
        {"id": "customfield_42", "schema": {"custom": "com.topshelfsolution.simpletasklists:all-tasks-count-cftype"}}
    ]
    jira.jira.server_info.return_value = {"deploymentType": "Data Center"}
    raw = _project_issue()
    raw["fields"]["comment"] = {
        "comments": [
            {
                "id": "555",
                "visibility": {"type": "role", "value": "Reviewers"},
                "body": "{task:id=44}restricted comment{task}",
            }
        ]
    }
    issue = SimpleNamespace(key="J-1", id="90", raw=raw, fields=raw["fields"])
    jira.jira.search_issues.side_effect = lambda jql, **kwargs: [issue] if kwargs["startAt"] == 0 else []
    jira.jira.issue.return_value.raw = raw
    jira.get_issue_property.return_value = {"tasks": [{"id": 44, "text": "sensitive property"}]}
    jira.get_issue_simpletasks.return_value = _rows()
    target = Mock(return_value={"updated": 1, "tasks": 2})
    monkeypatch.setattr("src.application.components.simpletasks_migration.apply_tasklists", target)
    monkeypatch.setitem(config.jira_config, "projects", ["J"])
    migration = SimpleTasksMigration(jira_client=jira, op_client=Mock())
    migration.mappings = Mock()
    migration.mappings.get_mapping.side_effect = {
        "work_package": {"90": {"jira_key": "J-1", "openproject_id": 101}, "OTHER-2": 44},
        "project": {"J": 77},
    }.get
    return migration, jira, target


def test_regular_stage_preserves_the_confirmed_task_order_and_withholds_comment_and_property(stage):
    migration, jira, target = stage
    result = _invoke_component("simpletasks", migration)
    assert result.success
    assert result.updated == 1
    assert result.details["tasks"] == 2
    assert result.details["unverified_task_audience"] == 1
    assert jira.jira.search_issues.call_args.args[0] == 'project = "J" AND level IS EMPTY ORDER BY id'
    [row] = target.call_args.args[1]["rows"]
    assert row["wp"] == 101
    assert [t["id"] for t in row["tasks"]] == ["43", "42"]
    assert row["tasks"][1]["lastChangedBy"] == "example"
    assert row["markdown"] == "- [ ] ` direct task `\n- [x] ` inline ` (mandatory)"
    assert "sensitive" not in str(row)
    assert "restricted" not in str(row)


def test_personal_or_comment_task_is_never_published_as_a_direct_sidebar_task(stage):
    migration, jira, target = stage
    jira.get_issue_simpletasks.return_value[1]["commentId"] = 555
    result = migration.run()
    assert result.details["unverified_task_audience"] == 2
    [row] = target.call_args.args[1]["rows"]
    assert [t["id"] for t in row["tasks"]] == ["42"]


def test_cloud_sidebar_tasks_are_held_for_a_separate_audience_check(stage):
    migration, jira, target = stage
    jira.jira.server_info.return_value = {"deploymentType": "Cloud"}
    result = migration.run()
    assert result.details["unverified_task_audience"] == 2
    assert [t["id"] for t in target.call_args.args[1]["rows"][0]["tasks"]] == ["42"]
    jira.get_issue_simpletasks.assert_not_called()


def test_property_cannot_publish_task_content_or_modify_confirmed_labels(stage):
    migration, jira, target = stage
    raw = jira.jira.issue.return_value.raw
    raw["fields"].pop("customfield_42")
    jira.get_issue_property.return_value = [{"id": 42, "text": "private source body"}]
    result = migration.run()
    assert result.success
    [row] = target.call_args.args[1]["rows"]
    assert row["markdown"] == "- [x] ` inline `"
    assert "private source body" not in str(row)


def test_ordinary_scope_requires_empty_level_and_excludes_customer_projects(stage):
    migration, jira, target = stage
    jira.jira.issue.return_value.raw["fields"]["security"] = {"id": "10"}
    result = migration.run()
    assert result.success
    assert result.details["unconfirmed_issues"] == 1
    target.assert_not_called()
    del jira.jira.issue.return_value.raw["fields"]["security"]
    jira.jira.issue.return_value.raw["fields"]["project"]["projectTypeKey"] = "service_desk"
    assert migration.run().details["unconfirmed_issues"] == 1
    target.assert_not_called()
    jira.get_issue_property.assert_not_called()


@pytest.mark.parametrize("count", ["NaN", "1.2", "-1", "4"])
def test_source_mismatch_aborts_before_first_write(stage, count):
    migration, jira, target = stage
    jira.jira.issue.return_value.raw["fields"]["customfield_42"] = count
    assert not migration.run().success
    target.assert_not_called()


@pytest.mark.parametrize(
    ("index", "change"),
    [
        (0, {"manuallyCreated": True}),
        (0, {"text": "changed"}),
        (0, {"id": 61}),
        (1, {"id": 42}),
        (1, {"projectId": 71}),
        (1, {"issue": "J-2"}),
        (1, {"issueId": 91}),
        (1, {"manuallyCreated": "yes"}),
        (1, {"id": ""}),
        (1, {"text": None}),
    ],
)
def test_inconsistent_sidebar_identity_aborts_the_write(index, change):
    rows = _rows()
    rows[index].update(change)
    issue = _project_issue()
    with pytest.raises((ValueError, KeyError)):
        issue_task_records(issue, [{"id": "42", "title": "inline", "checked": True}], rows, 3)
