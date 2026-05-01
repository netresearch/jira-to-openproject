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
    """Coerce a value to ``str`` while preserving ``None``.

    Returns ``None`` for values that are not already strings or ``str``-able
    primitives (e.g. :class:`unittest.mock.Mock` auto-vivified attributes).
    Pydantic would otherwise reject those at validation time and abort the
    enclosing :func:`_jira_fields_payload` call — which the legacy
    attribute-walking code never did. We fall back to ``None`` for anything
    that isn't a string, ``int``, ``float`` or ``bool``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return None


def _str_attr(obj: Any, name: str) -> str | None:
    """Read ``obj.name`` and coerce to a clean ``str`` or ``None``."""
    return _str_or_none(getattr(obj, name, None))


def _jira_ref(value: Any) -> dict[str, Any] | None:
    """Pluck ``id``/``name`` off an SDK reference object."""
    if value is None:
        return None
    return {
        "id": _str_attr(value, "id"),
        "name": _str_attr(value, "name"),
    }


def _safe_iter(value: Any) -> list[Any]:
    """Return ``value`` as a list if iterable, else an empty list.

    SDK fields normally expose iterable collections (``list``, generator,
    etc.) for ``comment.comments``, ``attachment``, ``fixVersions`` and
    ``components``. Mock-based test fixtures, however, auto-vivify these
    attributes with non-iterable :class:`unittest.mock.Mock` instances —
    iterating those raises :class:`TypeError`. We tolerate both shapes
    so the typed pipeline doesn't accidentally crash on test doubles
    that the legacy attribute-walking code happily ignored.
    """
    if value is None:
        return []
    try:
        return list(value)
    except TypeError:
        return []


def _safe_user(obj: Any) -> JiraUser | None:
    """Build a :class:`JiraUser` from ``obj`` or return ``None`` on bad shapes.

    Same defensive rationale as :func:`_safe_iter`: Mock-based test
    fixtures often auto-vivify ``assignee``/``reporter`` to non-string
    Mocks that fail Pydantic validation. The migration's previous
    attribute-walking code never tried to construct a user payload from
    those, so we silently treat them as missing here.

    Dict-shaped inputs (cached payloads, integration test fixtures)
    take the :meth:`JiraUser.from_dict` path so attribute aliases
    (``accountId`` etc.) are honoured; everything else flows through
    :meth:`JiraUser.from_jira_obj` for SDK-style attribute access.
    """
    if obj is None:
        return None
    try:
        if isinstance(obj, dict):
            return JiraUser.from_dict(obj)
        return JiraUser.from_jira_obj(obj)
    except Exception:
        return None


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
    for c in _safe_iter(comments_iter):
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
    for att in _safe_iter(attachments_iter):
        attachments_payload.append(
            {
                "id": _str_or_none(getattr(att, "id", None)),
                "filename": getattr(att, "filename", None),
                "size": getattr(att, "size", None),
                "content": getattr(att, "url", None),
                "author": _safe_user(getattr(att, "author", None)),
                "created": _str_attr(att, "created"),
            },
        )

    labels = _safe_iter(getattr(sdk_fields, "labels", None))

    fix_versions_iter = getattr(sdk_fields, "fixVersions", None)
    fix_versions_payload = [_jira_ref(v) for v in _safe_iter(fix_versions_iter) if v is not None]

    affects_versions_iter = getattr(sdk_fields, "versions", None)
    affects_versions_payload = [_jira_ref(v) for v in _safe_iter(affects_versions_iter) if v is not None]

    components_iter = getattr(sdk_fields, "components", None)
    components_payload = [_jira_ref(c) for c in _safe_iter(components_iter) if c is not None]

    # ``remotelinks`` is not a standard inner ``fields`` attribute on the
    # Jira SDK — it surfaces as a per-issue dict on cached/test payloads.
    # We accept any of a handful of synonym attributes and normalise into
    # a single ``remote_links`` list of ``{title, url}`` dicts. The model
    # validates these into typed :class:`JiraRemoteLinkRef` instances.
    remote_links_payload: list[dict[str, Any]] = []
    for attr in ("remotelinks", "remote_links", "webLinks", "weblinks", "issuelinks"):
        candidates = getattr(sdk_fields, attr, None)
        if candidates is None:
            continue
        for item in _safe_iter(candidates):
            obj: Any
            if isinstance(item, dict):
                obj = item.get("object", item)
            else:
                obj = getattr(item, "object", item)
            if isinstance(obj, dict):
                url = obj.get("url")
                title = obj.get("title") or obj.get("summary")
            else:
                url = getattr(obj, "url", None)
                title = getattr(obj, "title", None) or getattr(obj, "summary", None)
            remote_links_payload.append({"title": title, "url": url})
        break

    votes_obj = getattr(sdk_fields, "votes", None)
    votes_payload: dict[str, Any] | None = None
    if votes_obj is not None:
        votes_count = getattr(votes_obj, "votes", None)
        votes_payload = {"votes": votes_count if isinstance(votes_count, int) else None}

    return {
        "summary": _str_attr(sdk_fields, "summary"),
        "description": _str_attr(sdk_fields, "description"),
        "status": _jira_ref(getattr(sdk_fields, "status", None)),
        "priority": _jira_ref(getattr(sdk_fields, "priority", None)),
        "issuetype": _jira_ref(getattr(sdk_fields, "issuetype", None)),
        "resolution": _jira_ref(getattr(sdk_fields, "resolution", None)),
        "security": _jira_ref(getattr(sdk_fields, "security", None)),
        "votes": votes_payload,
        "assignee": _safe_user(assignee_obj),
        "reporter": _safe_user(reporter_obj),
        "created": _str_attr(sdk_fields, "created"),
        "updated": _str_attr(sdk_fields, "updated"),
        "labels": [str(label) for label in labels if isinstance(label, (str, int, float))],
        "fixVersions": fix_versions_payload,
        "versions": affects_versions_payload,
        "components": components_payload,
        "remote_links": remote_links_payload,
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


class JiraResolutionRef(BaseModel):
    """Reference to a Jira resolution (``id`` + ``name``)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    id: str | None = None
    name: str | None = None


class JiraSecurityLevelRef(BaseModel):
    """Reference to a Jira issue security level (``id`` + ``name``)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    id: str | None = None
    name: str | None = None


class JiraRemoteLinkRef(BaseModel):
    """Reference to a Jira remote/web link entry on an issue.

    Jira returns remote links via several near-synonymous attributes
    (``remotelinks``, ``webLinks``, ``issuelinks``) and either as a flat
    ``{url, title}`` dict or with the payload nested under ``object``.
    The boundary helper (:func:`_jira_fields_payload`) flattens those
    shapes so this model only carries the two fields the migration
    actually needs.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    url: str | None = None
    title: str | None = None


class JiraVotesRef(BaseModel):
    """Reference to the Jira ``votes`` block on an issue.

    The Jira REST/SDK exposes ``fields.votes`` as an object that carries
    a ``votes`` integer count (and a ``hasVoted`` flag we don't need for
    migration). We model the count only.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")
    votes: int | None = None


class JiraComment(BaseModel):
    """A Jira issue comment."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str | None = None
    body: str | None = None
    author: str | None = None
    """Author display name (the cache flattens the SDK's nested author)."""
    created: str | None = None


class JiraAttachment(BaseModel):
    """A Jira issue attachment reference.

    The provenance-aware fields (``author`` and ``created``) carry the
    upload metadata the attachment-provenance migration writes back onto
    the OpenProject side. Both are optional because the SDK boundary may
    strip them in older payloads (e.g. cached fixtures captured before
    phase 7c added these fields).
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str | None = None
    filename: str | None = None
    size: int | None = None
    content: str | None = None
    """Download URL — the SDK calls this attribute ``url``."""
    author: JiraUser | None = None
    """Author of the attachment — used for provenance preservation."""
    created: str | None = None
    """ISO-8601 timestamp the attachment was uploaded, when known."""


class JiraIssueFields(BaseModel):
    """Inner ``fields`` block of a Jira issue."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    summary: str | None = None
    description: str | None = None

    status: JiraStatusRef | None = None
    priority: JiraPriorityRef | None = None
    issue_type: JiraIssueTypeRef | None = Field(default=None, alias="issuetype")
    resolution: JiraResolutionRef | None = None
    security: JiraSecurityLevelRef | None = None
    votes: JiraVotesRef | None = None

    assignee: JiraUser | None = None
    reporter: JiraUser | None = None

    created: str | None = None
    updated: str | None = None

    labels: list[str] = Field(default_factory=list)
    fix_versions: list[JiraVersionRef] = Field(default_factory=list, alias="fixVersions")
    affects_versions: list[JiraVersionRef] = Field(default_factory=list, alias="versions")
    components: list[JiraComponentRef] = Field(default_factory=list)
    remote_links: list[JiraRemoteLinkRef] = Field(default_factory=list)

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
            "versions",
            "affects_versions",
            "components",
            "remote_links",
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
    "JiraRemoteLinkRef",
    "JiraResolutionRef",
    "JiraSecurityLevelRef",
    "JiraStatusRef",
    "JiraVersionRef",
    "JiraVotesRef",
]
