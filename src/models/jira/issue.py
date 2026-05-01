"""Pydantic v2 models for Jira issue payloads.

The migration code consumes Jira issues from two boundaries:

* The Jira REST/SDK pipeline — :class:`jira.Issue` instances with nested
  ``fields`` exposing ``status``, ``priority``, ``issuetype`` etc.
* The cached JSON dict shape produced by
  :mod:`src.infrastructure.jira.jira_issue_service` (``get_issue_details`` and the
  batch fetchers).

We model the structural minimum the migration actually relies on. Nested
references (status, priority, issue type, components, fix versions,
comments, attachments) get their own small Pydantic models rather than
being inlined as ``dict[str, Any]`` — this keeps the boundary checked
without exploding into a full Jira schema.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.domain.ids import JiraIssueKey
from src.models.jira.user import JiraUser


def _str_or_none(value: Any) -> str | None:
    """Coerce a value to ``str`` while preserving ``None``."""
    return None if value is None else str(value)


def _jira_ref(value: Any) -> dict[str, Any] | None:
    """Pluck ``id``/``name`` off an SDK reference object."""
    if value is None:
        return None
    return {
        "id": _str_or_none(getattr(value, "id", None)),
        "name": _str_or_none(getattr(value, "name", None)),
    }


def _jira_fields_payload(sdk_fields: Any) -> dict[str, Any]:
    """Translate a Jira SDK ``fields`` object into a validation-ready dict.

    Used by both :meth:`JiraIssueFields.from_jira_obj` and
    :meth:`JiraIssue.from_jira_obj` so the SDK adaptation logic lives in
    exactly one place.
    """
    # Importing here would be circular-free, but JiraUser is already
    # imported at module top — use it directly.
    if sdk_fields is None:
        return {}

    assignee_obj = getattr(sdk_fields, "assignee", None)
    reporter_obj = getattr(sdk_fields, "reporter", None)

    comment_block = getattr(sdk_fields, "comment", None)
    comments_iter = getattr(comment_block, "comments", None) if comment_block else None
    comments_payload: list[dict[str, Any]] = []
    if comments_iter:
        for c in comments_iter:
            author_obj = getattr(c, "author", None)
            comments_payload.append(
                {
                    "id": _str_or_none(getattr(c, "id", None)),
                    "body": getattr(c, "body", None),
                    "author": getattr(author_obj, "displayName", None) if author_obj else None,
                    "created": getattr(c, "created", None),
                },
            )

    attachments_iter = getattr(sdk_fields, "attachment", None)
    attachments_payload: list[dict[str, Any]] = []
    if attachments_iter:
        for att in attachments_iter:
            attachments_payload.append(
                {
                    "id": _str_or_none(getattr(att, "id", None)),
                    "filename": getattr(att, "filename", None),
                    "size": getattr(att, "size", None),
                    "content": getattr(att, "url", None),
                },
            )

    labels = list(getattr(sdk_fields, "labels", []) or [])

    fix_versions_iter = getattr(sdk_fields, "fixVersions", None)
    fix_versions_payload = [_jira_ref(v) for v in (fix_versions_iter or []) if v is not None]

    components_iter = getattr(sdk_fields, "components", None)
    components_payload = [_jira_ref(c) for c in (components_iter or []) if c is not None]

    return {
        "summary": getattr(sdk_fields, "summary", None),
        "description": getattr(sdk_fields, "description", None),
        "status": _jira_ref(getattr(sdk_fields, "status", None)),
        "priority": _jira_ref(getattr(sdk_fields, "priority", None)),
        "issuetype": _jira_ref(getattr(sdk_fields, "issuetype", None)),
        "assignee": JiraUser.from_jira_obj(assignee_obj) if assignee_obj else None,
        "reporter": JiraUser.from_jira_obj(reporter_obj) if reporter_obj else None,
        "created": getattr(sdk_fields, "created", None),
        "updated": getattr(sdk_fields, "updated", None),
        "labels": [str(label) for label in labels],
        "fixVersions": fix_versions_payload,
        "components": components_payload,
        "comments": comments_payload,
        "attachments": attachments_payload,
    }


class JiraStatusRef(BaseModel):
    """Reference to a Jira status (``id`` + ``name``)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    id: str | None = None
    name: str | None = None


class JiraPriorityRef(BaseModel):
    """Reference to a Jira priority (``id`` + ``name``)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    id: str | None = None
    name: str | None = None


class JiraIssueTypeRef(BaseModel):
    """Reference to a Jira issue type (``id`` + ``name``)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    id: str | None = None
    name: str | None = None


class JiraVersionRef(BaseModel):
    """Reference to a Jira version (``fixVersions`` entries)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    id: str | None = None
    name: str | None = None


class JiraComponentRef(BaseModel):
    """Reference to a Jira component."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    id: str | None = None
    name: str | None = None


class JiraComment(BaseModel):
    """A Jira issue comment."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str | None = None
    body: str | None = None
    author: str | None = None
    """Author display name (the cache flattens the SDK's nested author)."""
    created: str | None = None


class JiraAttachment(BaseModel):
    """A Jira issue attachment reference."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str | None = None
    filename: str | None = None
    size: int | None = None
    content: str | None = None
    """Download URL — the SDK calls this attribute ``url``."""


class JiraIssueFields(BaseModel):
    """Inner ``fields`` block of a Jira issue."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    summary: str | None = None
    description: str | None = None

    status: JiraStatusRef | None = None
    priority: JiraPriorityRef | None = None
    issue_type: JiraIssueTypeRef | None = Field(default=None, alias="issuetype")

    assignee: JiraUser | None = None
    reporter: JiraUser | None = None

    created: str | None = None
    updated: str | None = None

    labels: list[str] = Field(default_factory=list)
    fix_versions: list[JiraVersionRef] = Field(default_factory=list, alias="fixVersions")
    components: list[JiraComponentRef] = Field(default_factory=list)

    comments: list[JiraComment] = Field(default_factory=list)
    attachments: list[JiraAttachment] = Field(default_factory=list)

    # ── classmethod constructors ─────────────────────────────────────────

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> JiraIssueFields:
        """Build a :class:`JiraIssueFields` from a Jira REST/cache dict shape."""
        if not raw:
            return cls()
        return cls.model_validate(raw)

    @classmethod
    def from_jira_obj(cls, sdk_fields: Any) -> JiraIssueFields:
        """Build a :class:`JiraIssueFields` from a Jira SDK ``fields`` object.

        The SDK exposes the inner ``fields`` block of an issue via attribute
        access. ``sdk_fields`` may be ``None`` (when an issue was fetched
        without a fields expansion) — in that case we return an empty
        :class:`JiraIssueFields` instance.

        Test fixtures often hand in a :class:`types.SimpleNamespace` that
        only sets a subset of attributes; the helpers below tolerate
        missing attributes by falling back to ``None``/empty defaults.
        """
        return cls.model_validate(_jira_fields_payload(sdk_fields))

    @classmethod
    def from_issue_any(cls, issue: Any) -> JiraIssueFields:
        """Build a :class:`JiraIssueFields` from any issue-shaped value.

        ``_merge_batch_issues`` (in :class:`BaseMigration`) returns issues
        in two shapes depending on the underlying client path:

        * SDK ``jira.Issue`` instances exposing ``issue.fields`` via
          attribute access.
        * Cache-restored dicts of the form ``{"key": ..., "fields": {...}}``
          or the flattened ``get_issue_details`` shape.

        ``from_issue_any`` accepts either shape (plus a stand-in test
        dummy that only carries ``.fields``) and returns the typed view
        that callers actually need. ``None`` short-circuits to an empty
        :class:`JiraIssueFields`; otherwise the underlying
        :meth:`from_dict` / :meth:`from_jira_obj` may raise
        ``pydantic.ValidationError`` on truly malformed input. Callers
        that want to skip such issues should wrap the call in their own
        ``try``/``except`` (as the priority/labels/versions migrations
        do).
        """
        if issue is None:
            return cls()
        if isinstance(issue, dict):
            inner = issue.get("fields")
            if isinstance(inner, dict):
                return cls.from_dict(inner)
            # Flattened shape: hoisted fields live at the top level.
            return cls.from_dict(issue)
        return cls.from_jira_obj(getattr(issue, "fields", None))


class JiraIssue(BaseModel):
    """Canonical representation of a Jira issue."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    key: JiraIssueKey
    id: str
    fields: JiraIssueFields = Field(default_factory=JiraIssueFields)

    # ── classmethod constructors ─────────────────────────────────────────

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> JiraIssue:
        """Build a :class:`JiraIssue` from a cache/REST dict shape.

        Two dict layouts are supported:

        * The "REST-shaped" dict, where nested fields live under
          ``raw["fields"]`` (this matches ``issue.raw``).
        * The "flattened" dict produced by ``get_issue_details``, which
          hoists ``summary``, ``description``, ``status``, ``issue_type``
          (note: snake_case alias for ``issuetype``), ``created``,
          ``updated``, ``assignee``, ``reporter``, ``comments`` and
          ``attachments`` to the top level.

        We detect the flattened shape by the absence of a ``fields``
        member and assemble a synthetic ``fields`` block from the
        top-level keys.
        """
        if "fields" in raw and isinstance(raw["fields"], dict):
            return cls.model_validate(raw)

        # Flattened shape — re-pack into a nested ``fields`` block.
        fields_payload: dict[str, Any] = {}
        for key in (
            "summary",
            "description",
            "status",
            "priority",
            "created",
            "updated",
            "assignee",
            "reporter",
            "labels",
            "fixVersions",
            "fix_versions",
            "components",
            "comments",
            "attachments",
        ):
            if key in raw:
                fields_payload[key] = raw[key]
        # ``issue_type`` (snake) and ``issuetype`` (camel) are both seen.
        if "issuetype" in raw:
            fields_payload["issuetype"] = raw["issuetype"]
        elif "issue_type" in raw:
            fields_payload["issuetype"] = raw["issue_type"]

        return cls.model_validate(
            {
                "key": raw.get("key"),
                "id": raw.get("id"),
                "fields": fields_payload,
            },
        )

    @classmethod
    def from_jira_obj(cls, obj: Any) -> JiraIssue:
        """Build a :class:`JiraIssue` from a ``jira.Issue`` SDK instance."""
        sdk_fields = getattr(obj, "fields", None)
        return cls.model_validate(
            {
                "key": getattr(obj, "key", None),
                "id": _str_or_none(getattr(obj, "id", None)),
                "fields": _jira_fields_payload(sdk_fields),
            },
        )


__all__ = [
    "JiraAttachment",
    "JiraComment",
    "JiraComponentRef",
    "JiraIssue",
    "JiraIssueFields",
    "JiraIssueTypeRef",
    "JiraPriorityRef",
    "JiraStatusRef",
    "JiraVersionRef",
]
