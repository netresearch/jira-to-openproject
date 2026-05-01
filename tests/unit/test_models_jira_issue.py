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


# ── JiraIssueFields direct constructors ──────────────────────────────────


def test_jira_issue_fields_from_dict_happy_path() -> None:
    """``JiraIssueFields.from_dict`` accepts the raw ``fields`` block alone."""
    raw = _nested_dict_shape()["fields"]
    assert isinstance(raw, dict)

    fields = JiraIssueFields.from_dict(raw)

    assert fields.summary == "Hello"
    assert fields.priority is not None
    assert fields.priority.name == "Medium"
    assert fields.labels == ["bug", "urgent"]
    assert len(fields.fix_versions) == 1
    assert fields.fix_versions[0].name == "v1.0"


def test_jira_issue_fields_from_dict_none_returns_empty() -> None:
    fields = JiraIssueFields.from_dict(None)

    assert fields.summary is None
    assert fields.labels == []
    assert fields.fix_versions == []


def test_jira_issue_fields_from_jira_obj_happy_path() -> None:
    """The classmethod accepts an SDK ``fields`` block directly — without
    a surrounding ``issue`` object — which is what the migration layer
    relies on when it only has a stand-in ``DummyIssue`` carrying fields
    but no ``key``/``id``.
    """
    sdk_fields = SimpleNamespace(
        summary="Hi",
        priority=SimpleNamespace(id="3", name="Medium"),
        labels=["a", "b"],
        fixVersions=[SimpleNamespace(id="1", name="v1.0")],
    )

    fields = JiraIssueFields.from_jira_obj(sdk_fields)

    assert fields.summary == "Hi"
    assert fields.priority is not None
    assert fields.priority.name == "Medium"
    assert fields.labels == ["a", "b"]
    assert len(fields.fix_versions) == 1
    assert fields.fix_versions[0].name == "v1.0"


def test_jira_issue_fields_from_jira_obj_none_returns_empty() -> None:
    fields = JiraIssueFields.from_jira_obj(None)

    assert fields.summary is None
    assert fields.labels == []
    assert fields.fix_versions == []


# ── JiraIssueFields.from_issue_any ───────────────────────────────────────


def test_from_issue_any_accepts_sdk_issue_with_fields() -> None:
    """SDK shape: an issue object with a ``fields`` attribute."""
    sdk_fields = SimpleNamespace(labels=["bug"], priority=SimpleNamespace(name="High"))
    issue = SimpleNamespace(key="ABC-1", id="1", fields=sdk_fields)

    fields = JiraIssueFields.from_issue_any(issue)

    assert fields.labels == ["bug"]
    assert fields.priority is not None
    assert fields.priority.name == "High"


def test_from_issue_any_accepts_test_dummy_without_key_or_id() -> None:
    """Test-only fixtures often skip ``key``/``id``; we still want fields back."""
    sdk_fields = SimpleNamespace(
        labels=["x", "y"],
        fixVersions=[SimpleNamespace(name="v1")],
    )
    dummy = SimpleNamespace(fields=sdk_fields)

    fields = JiraIssueFields.from_issue_any(dummy)

    assert fields.labels == ["x", "y"]
    assert [v.name for v in fields.fix_versions] == ["v1"]


def test_from_issue_any_accepts_nested_dict_shape() -> None:
    """Cache-restored REST shape: ``{"key": ..., "fields": {...}}``."""
    issue = {
        "key": "ABC-1",
        "id": "1",
        "fields": {
            "labels": ["a"],
            "priority": {"id": "1", "name": "Low"},
        },
    }

    fields = JiraIssueFields.from_issue_any(issue)

    assert fields.labels == ["a"]
    assert fields.priority is not None
    assert fields.priority.name == "Low"


def test_from_issue_any_accepts_flattened_dict_shape() -> None:
    """``get_issue_details`` produces a flattened dict — fields hoisted to top."""
    issue = {
        "key": "ABC-1",
        "id": "1",
        "labels": ["flat"],
        "fixVersions": [{"name": "v2"}],
    }

    fields = JiraIssueFields.from_issue_any(issue)

    assert fields.labels == ["flat"]
    assert [v.name for v in fields.fix_versions] == ["v2"]


def test_from_issue_any_none_returns_empty() -> None:
    fields = JiraIssueFields.from_issue_any(None)

    assert fields.labels == []
    assert fields.priority is None


# ── resolution / security / votes refs ───────────────────────────────────


def test_from_dict_resolution_security_and_votes() -> None:
    """The dict shape carries resolution/security/votes through to typed refs."""
    raw: dict[str, object] = {
        "key": "ABC-9",
        "id": "9",
        "fields": {
            "resolution": {"id": "1", "name": "Fixed"},
            "security": {"id": "2", "name": "Top Secret"},
            "votes": {"votes": 7},
        },
    }
    issue = JiraIssue.from_dict(raw)

    assert issue.fields.resolution is not None
    assert issue.fields.resolution.name == "Fixed"
    assert issue.fields.security is not None
    assert issue.fields.security.name == "Top Secret"
    assert issue.fields.votes is not None
    assert issue.fields.votes.votes == 7


def test_from_jira_obj_resolution_security_and_votes() -> None:
    """The SDK shape adapts resolution/security/votes the same way as status."""
    sdk_fields = SimpleNamespace(
        resolution=SimpleNamespace(id="1", name="Done"),
        security=SimpleNamespace(id="2", name="Internal"),
        votes=SimpleNamespace(votes=3),
    )
    obj = SimpleNamespace(id="42", key="ABC-42", fields=sdk_fields)

    issue = JiraIssue.from_jira_obj(obj)

    assert issue.fields.resolution is not None
    assert issue.fields.resolution.name == "Done"
    assert issue.fields.security is not None
    assert issue.fields.security.name == "Internal"
    assert issue.fields.votes is not None
    assert issue.fields.votes.votes == 3


def test_from_jira_obj_missing_resolution_security_votes_default_to_none() -> None:
    sdk_fields = SimpleNamespace(
        resolution=None,
        security=None,
        votes=None,
    )
    obj = SimpleNamespace(id="1", key="ABC-1", fields=sdk_fields)

    issue = JiraIssue.from_jira_obj(obj)

    assert issue.fields.resolution is None
    assert issue.fields.security is None
    assert issue.fields.votes is None


def test_from_jira_obj_votes_non_int_count_normalises_to_none() -> None:
    """Non-int vote counts (e.g. SDK glitches) collapse to a votes ref with None."""
    sdk_fields = SimpleNamespace(votes=SimpleNamespace(votes="not-an-int"))
    obj = SimpleNamespace(id="1", key="ABC-1", fields=sdk_fields)

    issue = JiraIssue.from_jira_obj(obj)

    assert issue.fields.votes is not None
    assert issue.fields.votes.votes is None


def test_from_jira_obj_affects_versions_via_versions_attr() -> None:
    """SDK exposes affectsVersions as ``fields.versions`` — adapter must alias it."""
    sdk_fields = SimpleNamespace(
        versions=[
            SimpleNamespace(id="100", name="1.0"),
            SimpleNamespace(id="101", name="1.1"),
        ],
    )
    obj = SimpleNamespace(id="1", key="ABC-1", fields=sdk_fields)

    issue = JiraIssue.from_jira_obj(obj)

    assert len(issue.fields.affects_versions) == 2
    assert issue.fields.affects_versions[0].name == "1.0"
    assert issue.fields.affects_versions[1].name == "1.1"


def test_from_dict_affects_versions_via_versions_key() -> None:
    """REST/cache shape uses ``versions`` key — model accepts via alias."""
    raw = {
        "key": "ABC-1",
        "id": "1",
        "fields": {
            "versions": [
                {"id": "100", "name": "1.0"},
                {"id": "101", "name": "1.1"},
            ],
        },
    }

    issue = JiraIssue.from_dict(raw)

    assert len(issue.fields.affects_versions) == 2
    assert issue.fields.affects_versions[0].name == "1.0"


def test_from_jira_obj_remote_links_synonym_attrs() -> None:
    """remote_links is populated from any of the SDK's synonym attrs."""
    sdk_fields = SimpleNamespace(
        weblinks=[
            SimpleNamespace(url="https://example.com/a", title="A"),
        ],
    )
    obj = SimpleNamespace(id="1", key="ABC-1", fields=sdk_fields)

    issue = JiraIssue.from_jira_obj(obj)

    assert len(issue.fields.remote_links) == 1
    assert issue.fields.remote_links[0].url == "https://example.com/a"
    assert issue.fields.remote_links[0].title == "A"


def test_from_dict_remote_links_canonical_key() -> None:
    """The dict-shape path consumes the canonical ``remote_links`` key.

    Synonym attribute walking (``remotelinks``/``webLinks``/…) only
    runs on the SDK path via ``_jira_fields_payload`` — the dict path
    expects the cache-canonical shape produced by ``from_jira_obj``.
    """
    raw = {
        "key": "ABC-1",
        "id": "1",
        "fields": {
            "remote_links": [
                {"url": "https://example.com/x", "title": "X"},
                {"url": "https://example.com/y", "title": "Y"},
            ],
        },
    }

    issue = JiraIssue.from_dict(raw)

    assert len(issue.fields.remote_links) == 2
    assert issue.fields.remote_links[0].url == "https://example.com/x"
    assert issue.fields.remote_links[1].url == "https://example.com/y"


def test_from_jira_obj_remote_links_nested_object_unwrap() -> None:
    """SDK-path: entries nested under ``object`` get unwrapped by the synonym walk."""
    sdk_fields = SimpleNamespace(
        remotelinks=[
            SimpleNamespace(object=SimpleNamespace(url="https://example.com/x", title="X")),
            SimpleNamespace(url="https://example.com/y", title="Y"),
        ],
    )
    obj = SimpleNamespace(id="1", key="ABC-1", fields=sdk_fields)

    issue = JiraIssue.from_jira_obj(obj)

    assert len(issue.fields.remote_links) == 2
    assert issue.fields.remote_links[0].url == "https://example.com/x"
    assert issue.fields.remote_links[1].url == "https://example.com/y"


def test_attachment_carries_author_and_created_from_jira_obj() -> None:
    """SDK-shape attachments expose ``author``/``created`` for provenance."""
    author_obj = SimpleNamespace(
        accountId="557058:author",
        name="alice",
        displayName="Alice",
        emailAddress="alice@example.com",
        active=True,
    )
    att_obj = SimpleNamespace(
        id="9001",
        filename="upload.pdf",
        size=1024,
        url="https://example.com/upload.pdf",
        author=author_obj,
        created="2026-04-01T10:00:00.000+0000",
    )
    sdk_fields = SimpleNamespace(
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
        attachment=[att_obj],
    )
    obj = SimpleNamespace(id="1", key="ABC-1", fields=sdk_fields)

    issue = JiraIssue.from_jira_obj(obj)

    assert len(issue.fields.attachments) == 1
    att = issue.fields.attachments[0]
    assert att.filename == "upload.pdf"
    assert att.created == "2026-04-01T10:00:00.000+0000"
    assert att.author is not None
    assert att.author.account_id == "557058:author"
    assert att.author.name == "alice"


def test_attachment_dict_author_uses_from_dict_path() -> None:
    """Dict-shaped attachment authors honour camelCase aliases via from_dict."""
    raw = {
        "key": "ABC-1",
        "id": "1",
        "fields": {
            "attachments": [
                {
                    "filename": "upload.pdf",
                    "created": "2026-04-01T10:00:00.000+0000",
                    "author": {"accountId": "557058:author", "displayName": "Alice"},
                },
            ],
        },
    }
    issue = JiraIssue.from_dict(raw)
    assert len(issue.fields.attachments) == 1
    att = issue.fields.attachments[0]
    assert att.author is not None
    assert att.author.account_id == "557058:author"
    assert att.author.display_name == "Alice"
