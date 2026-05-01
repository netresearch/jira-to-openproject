"""Tests for :class:`src.models.jira.JiraIssue` and friends."""

from __future__ import annotations

from types import SimpleNamespace

from src.models.jira import JiraIssue, JiraIssueFields


def _nested_dict_shape() -> dict[str, object]:
    return {
        "key": "ABC-123",
        "id": "10001",
        "fields": {
            "summary": "Hello",
            "description": "World",
            "status": {"id": "1", "name": "Open"},
            "priority": {"id": "3", "name": "Medium"},
            "issuetype": {"id": "10", "name": "Bug"},
            "assignee": {
                "accountId": "557058:abc",
                "displayName": "Jane",
                "emailAddress": "jane@example.com",
            },
            "reporter": {
                "accountId": "557058:def",
                "displayName": "John",
            },
            "created": "2026-01-01T12:00:00.000+0000",
            "updated": "2026-01-02T12:00:00.000+0000",
            "labels": ["bug", "urgent"],
            "fixVersions": [{"id": "1", "name": "v1.0"}],
            "components": [{"id": "5", "name": "Backend"}],
        },
    }


def test_from_dict_nested_shape() -> None:
    issue = JiraIssue.from_dict(_nested_dict_shape())

    assert issue.key == "ABC-123"
    assert issue.id == "10001"
    assert isinstance(issue.fields, JiraIssueFields)
    assert issue.fields.summary == "Hello"
    assert issue.fields.description == "World"
    assert issue.fields.status is not None
    assert issue.fields.status.name == "Open"
    assert issue.fields.priority is not None
    assert issue.fields.priority.name == "Medium"
    assert issue.fields.issue_type is not None
    assert issue.fields.issue_type.name == "Bug"
    assert issue.fields.assignee is not None
    assert issue.fields.assignee.display_name == "Jane"
    assert issue.fields.assignee.email_address == "jane@example.com"
    assert issue.fields.reporter is not None
    assert issue.fields.reporter.display_name == "John"
    assert issue.fields.labels == ["bug", "urgent"]
    assert len(issue.fields.fix_versions) == 1
    assert issue.fields.fix_versions[0].name == "v1.0"
    assert len(issue.fields.components) == 1
    assert issue.fields.components[0].name == "Backend"


def test_from_dict_flattened_shape_assembles_fields_block() -> None:
    flattened: dict[str, object] = {
        "id": "10001",
        "key": "ABC-123",
        "summary": "Hello",
        "description": "World",
        # Flattened shape uses snake_case for issue_type.
        "issue_type": {"id": "10", "name": "Bug"},
        "status": {"id": "1", "name": "Open"},
        "created": "2026-01-01T12:00:00.000+0000",
        "updated": "2026-01-02T12:00:00.000+0000",
        "assignee": {"name": "jdoe", "display_name": "Jane"},
        "reporter": {"name": "jrep", "display_name": "John"},
        "comments": [
            {"id": "100", "body": "first", "author": "Jane", "created": "2026-01-01T12:00"},
        ],
        "attachments": [
            {"id": "200", "filename": "f.txt", "size": 12, "content": "https://x"},
        ],
    }
    issue = JiraIssue.from_dict(flattened)

    assert issue.key == "ABC-123"
    assert issue.id == "10001"
    assert issue.fields.summary == "Hello"
    assert issue.fields.issue_type is not None
    assert issue.fields.issue_type.name == "Bug"
    assert issue.fields.status is not None
    assert issue.fields.status.name == "Open"
    assert len(issue.fields.comments) == 1
    assert issue.fields.comments[0].body == "first"
    assert issue.fields.comments[0].author == "Jane"
    assert len(issue.fields.attachments) == 1
    assert issue.fields.attachments[0].filename == "f.txt"
    assert issue.fields.attachments[0].size == 12


def test_from_dict_minimal_shape_defaults_empty_fields() -> None:
    issue = JiraIssue.from_dict({"key": "ABC-1", "id": "1"})
    assert issue.fields.summary is None
    assert issue.fields.labels == []
    assert issue.fields.fix_versions == []
    assert issue.fields.components == []
    assert issue.fields.comments == []
    assert issue.fields.attachments == []


def test_from_dict_extra_fields_are_ignored() -> None:
    issue = JiraIssue.from_dict(
        {
            "key": "ABC-1",
            "id": "1",
            "fields": {"summary": "x", "votes": {"votes": 2}},
            "expand": "names,renderedFields",
        },
    )
    assert issue.key == "ABC-1"
    assert issue.fields.summary == "x"
    assert "expand" not in issue.model_dump()


def test_from_jira_obj_happy_path() -> None:
    assignee = SimpleNamespace(
        accountId="557058:abc",
        displayName="Jane",
        emailAddress="jane@example.com",
    )
    reporter = SimpleNamespace(accountId="557058:def", displayName="John")
    status = SimpleNamespace(id="1", name="Open")
    priority = SimpleNamespace(id="3", name="Medium")
    issuetype = SimpleNamespace(id="10", name="Bug")
    fix_version = SimpleNamespace(id="1", name="v1.0")
    component = SimpleNamespace(id="5", name="Backend")

    comment_obj = SimpleNamespace(
        id="100",
        body="first",
        author=SimpleNamespace(displayName="Jane"),
        created="2026-01-01T12:00",
    )
    comment_block = SimpleNamespace(comments=[comment_obj])

    attachment_obj = SimpleNamespace(
        id="200",
        filename="f.txt",
        size=12,
        url="https://x.example.com/f.txt",
    )

    fields = SimpleNamespace(
        summary="Hello",
        description="World",
        status=status,
        priority=priority,
        issuetype=issuetype,
        assignee=assignee,
        reporter=reporter,
        created="2026-01-01T12:00:00.000+0000",
        updated="2026-01-02T12:00:00.000+0000",
        labels=["bug"],
        fixVersions=[fix_version],
        components=[component],
        comment=comment_block,
        attachment=[attachment_obj],
    )
    obj = SimpleNamespace(id="10001", key="ABC-123", fields=fields)

    issue = JiraIssue.from_jira_obj(obj)

    assert issue.key == "ABC-123"
    assert issue.id == "10001"
    assert issue.fields.summary == "Hello"
    assert issue.fields.status is not None
    assert issue.fields.status.name == "Open"
    assert issue.fields.priority is not None
    assert issue.fields.priority.name == "Medium"
    assert issue.fields.issue_type is not None
    assert issue.fields.issue_type.name == "Bug"
    assert issue.fields.assignee is not None
    assert issue.fields.assignee.display_name == "Jane"
    assert issue.fields.reporter is not None
    assert issue.fields.reporter.display_name == "John"
    assert issue.fields.labels == ["bug"]
    assert len(issue.fields.fix_versions) == 1
    assert issue.fields.fix_versions[0].name == "v1.0"
    assert len(issue.fields.components) == 1
    assert issue.fields.components[0].name == "Backend"
    assert len(issue.fields.comments) == 1
    assert issue.fields.comments[0].author == "Jane"
    assert issue.fields.comments[0].body == "first"
    assert len(issue.fields.attachments) == 1
    assert issue.fields.attachments[0].content == "https://x.example.com/f.txt"


def test_from_jira_obj_missing_assignee_and_reporter_default_to_none() -> None:
    fields = SimpleNamespace(
        summary="Hello",
        description=None,
        status=None,
        priority=None,
        issuetype=None,
        assignee=None,
        reporter=None,
        created=None,
        updated=None,
        labels=None,
        fixVersions=None,
        components=None,
        comment=None,
        attachment=None,
    )
    obj = SimpleNamespace(id="10001", key="ABC-123", fields=fields)

    issue = JiraIssue.from_jira_obj(obj)

    assert issue.fields.assignee is None
    assert issue.fields.reporter is None
    assert issue.fields.status is None
    assert issue.fields.priority is None
    assert issue.fields.issue_type is None
    assert issue.fields.labels == []
    assert issue.fields.fix_versions == []
    assert issue.fields.components == []
    assert issue.fields.comments == []
    assert issue.fields.attachments == []
