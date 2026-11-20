# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Decide which Jira comments may become ordinary OpenProject journals."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import requests

from src.infrastructure.jira.jira_public_reporting import anonymous_verify

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class CommentSelection:
    """Verified comment copies and the number retained at source."""

    comments: list[SimpleNamespace]
    held: int


# Older Server responses and caches can omit fieldId. Only built-in fields
# with known non-comment semantics or identifiable custom fields may contribute
# historical values. A translated label alone cannot establish the field type.
_HISTORY_FIELDS = frozenset(
    {
        "affects version",
        "affects versions",
        "assignee",
        "attachment",
        "component",
        "components",
        "created",
        "description",
        "due date",
        "duedate",
        "environment",
        "epic link",
        "fix version",
        "fix versions",
        "fixversion",
        "fixversions",
        "flagged",
        "issue type",
        "issuetype",
        "key",
        "labels",
        "link",
        "parent",
        "priority",
        "project",
        "rank",
        "reporter",
        "resolution",
        "resolutiondate",
        "security",
        "sprint",
        "status",
        "story points",
        "summary",
        "timeestimate",
        "timeoriginalestimate",
        "timespent",
        "updated",
        "version",
        "versions",
        "workflow",
    }
)


def _record_fields(record: object) -> Mapping[str, Any] | None:
    """Read a REST mapping or the attributes of an SDK resource without running properties."""
    if isinstance(record, Mapping):
        return record
    return getattr(record, "__dict__", None)


def _explicit_audience(fields: Mapping[str, Any]) -> bool:
    # Even a JSM "public" reply can be restricted to request participants.
    if any(key in fields for key in ("internal", "isInternal", "public", "jsdPublic")):
        return True
    scope_keys = ("visibility", "roleLevel", "groupLevel", "audience", "restrictions", "restrictedTo")
    if any(fields.get(key) is not None for key in scope_keys):
        return True
    return "properties" in fields and fields["properties"] != []


def has_comment_audience(record: object) -> bool:
    """Withhold restricted or unrecognized audience metadata on an item or its history.

    Inspect the SDK's original response and Jira's metadata envelopes. Sibling
    items have independent audiences and are checked individually by the caller.
    """
    pending = [record]
    seen = set()
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        fields = _record_fields(value)
        if fields is None or _explicit_audience(fields):
            return True
        pending.extend(fields[key] for key in ("raw", "historyMetadata", "extraData") if fields.get(key) is not None)
    return False


def may_copy_history_item(item: object, history: object | None = None) -> bool:
    """Require both a recognized field and unrestricted history and item metadata."""
    if has_comment_audience(item) or (history is not None and has_comment_audience(history)):
        return False
    fields = _record_fields(item)
    if fields is None:
        return False
    raw = _record_fields(fields.get("raw")) or {}
    if any(key in fields and key in raw and fields[key] != raw[key] for key in ("field", "fieldId", "fieldtype")):
        return False
    return _recognized_history_field({**raw, **fields})


def _recognized_history_field(item: Mapping[str, Any]) -> bool:
    field = str(item.get("field") or "").strip().casefold()
    field_id = str(item.get("fieldId") or "").strip().casefold()
    if field in {"comment", "comments"} or field_id in {"comment", "comments"}:
        return False
    key = field_id or field
    return (
        key in _HISTORY_FIELDS
        or (key.startswith("customfield_") and key.removeprefix("customfield_").isdecimal())
        or (item.get("fieldtype") == "custom" and not field_id and isinstance(item.get("field"), str) and bool(field))
    )


def _history_atoms(record: Mapping[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """Keep REST scalar values only. SDK envelopes and plugin payloads can embed comment bodies."""
    return {
        key: value
        for key in keys
        if key in record and (isinstance(value := record[key], (str, int, float, bool)) or value is None)
    }


def _cache_history(entry: object) -> dict[str, Any] | None:
    if not isinstance(entry, Mapping) or has_comment_audience(entry):
        return None
    source_items = entry.get("items")
    if not isinstance(source_items, list):
        return None
    keys = ("field", "fieldId", "fieldtype", "from", "fromString", "to", "toString")
    items = [
        _history_atoms(item, keys) for item in source_items if isinstance(item, Mapping) and may_copy_history_item(item)
    ]
    if not items:
        return None
    clean = _history_atoms(entry, ("id", "created"))
    author = entry.get("author")
    if isinstance(author, Mapping):
        clean["author"] = _history_atoms(author, ("key", "name", "accountId", "displayName", "emailAddress", "active"))
    return {**clean, "items": items}


def issue_without_comments(raw: dict[str, Any]) -> dict[str, Any]:
    """Omit comment text, unsupported history shapes and revisions from issue inventories."""
    clean = {
        key: value
        for key, value in raw.items()
        if key not in {"fields", "renderedFields", "changelog", "comment", "comments"}
    }
    for field in ("fields", "renderedFields"):
        if isinstance(raw.get(field), Mapping):
            clean[field] = {key: value for key, value in raw[field].items() if key not in {"comment", "comments"}}
    page = raw.get("changelog")
    if isinstance(page, Mapping) and isinstance(page.get("histories"), list) and not has_comment_audience(page):
        paging = {key: page[key] for key in ("startAt", "maxResults", "total") if type(page.get(key)) is int}
        if isinstance(page.get("isLast"), bool):
            paging["isLast"] = page["isLast"]
        clean["changelog"] = {
            **paging,
            "histories": [row for entry in page["histories"] if (row := _cache_history(entry))],
        }
    return clean


def _comment_page(
    page: dict[str, Any],
    offset: int,
    expected: int | None,
    ids: set[str],
) -> tuple[list[dict[str, Any]], int]:
    """Validate continuity, total and identity before accepting a page."""
    batch = page["comments"]
    total = page["total"]
    if (
        not isinstance(batch, list)
        or type(total) is not int
        or page.get("startAt") != offset
        or total < offset + len(batch)
        or (expected is not None and total != expected)
        or (not batch and total != offset)
    ):
        msg = "Incomplete Jira comment page"
        raise ValueError(msg)
    for row in batch:
        cid = row.get("id")
        if not isinstance(cid, str) or not cid or cid in ids:
            msg = "Missing or duplicate Jira comment ID"
            raise ValueError(msg)
        ids.add(cid)
    return batch, total


def _pages(get: Callable, *, anonymous: bool = False) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    ids: set[str] = set()
    expected = None
    while True:
        response = get({"startAt": len(rows), "maxResults": 100, "expand": "properties"})
        if anonymous and (response.status_code in {401, 403, 404} or response.is_redirect):
            return []
        response.raise_for_status()
        if response.status_code != 200:
            msg = "Unexpected Jira comment response"
            raise ValueError(msg)
        if anonymous and response.headers.get("X-AUSERNAME", "").lower() != "anonymous":
            return []
        page = response.json()
        batch, total = _comment_page(page, len(rows), expected, ids)
        rows.extend(batch)
        if len(rows) == total:
            return rows
        expected = total


def _eligible(row: dict[str, Any]) -> bool:
    # A Service Management customer-facing reply can itself be participant-only.
    # A positive anonymous read of the same body is required separately.
    properties = row.get("properties", [])
    return (
        isinstance(row.get("body"), str)
        and bool(row["body"])
        and row.get("visibility") is None
        and all(row.get(key, False) is False for key in ("internal", "isInternal"))
        and all(row.get(key, True) is True for key in ("jsdPublic", "public"))
        and isinstance(properties, list)
        and all(
            isinstance(prop, dict)
            and prop.get("key") == "sd.public.comment"
            and isinstance(prop.get("value"), dict)
            and prop["value"].get("internal") is False
            for prop in properties
        )
    )


def select_comments(client: Any, issue_key: str) -> CommentSelection:
    """Read the inventory with the migration account, publish only proven public copies."""
    path = f"/rest/api/2/issue/{quote(issue_key, safe='')}/comment"
    rows = _pages(lambda params: client._make_request(path, params=params))
    total = len(rows)
    if not total or os.getenv("J2O_COPY_PUBLIC_COMMENTS") != "1":
        return CommentSelection([], total)

    url = urlsplit(client.base_url)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
    ):
        return CommentSelection([], total)
    eligible = {row["id"]: row["body"] for row in rows if _eligible(row)}
    if not eligible:
        return CommentSelection([], total)

    with requests.Session() as session:
        # Ignore netrc, proxy credentials and ambient client configuration. Discard
        # cookies even between pages. Require Jira to identify every reply as anonymous.
        session.trust_env = False
        session.verify = anonymous_verify(client, client.base_url)

        def get(params: dict) -> requests.Response:
            session.cookies.clear()
            return session.get(client.base_url.rstrip("/") + path, params=params, allow_redirects=False, timeout=30)

        anonymous = _pages(get, anonymous=True)

    comments = []
    for row in anonymous:
        if _eligible(row) and eligible.get(row["id"]) == row["body"]:
            author = row.get("author")
            comments.append(
                SimpleNamespace(
                    id=row["id"],
                    body=row["body"],
                    created=row.get("created"),
                    author=SimpleNamespace(**author) if isinstance(author, dict) else None,
                )
            )
    return CommentSelection(comments, total - len(comments))
