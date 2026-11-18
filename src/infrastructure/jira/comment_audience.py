# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Decide which Jira comments may become ordinary OpenProject journals."""

from __future__ import annotations

import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

import requests

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class CommentSelection:
    """Verified comment copies and the number retained at source."""

    comments: list[SimpleNamespace]
    held: int


def is_comment_change(item: dict[str, Any]) -> bool:
    """Recognize historical comment revisions, including localized field names."""
    return any(str(item.get(key, "")).strip().casefold() in {"comment", "comments"} for key in ("field", "fieldId"))


def issue_without_comments(raw: dict[str, Any]) -> dict[str, Any]:
    """Omit comment text and its past revisions from persisted issue inventories."""
    clean = dict(raw)
    for field in ("fields", "renderedFields"):
        if isinstance(raw.get(field), dict):
            clean[field] = {key: value for key, value in raw[field].items() if key not in {"comment", "comments"}}
    history = raw.get("changelog")
    if isinstance(history, dict) and isinstance(history.get("histories"), list):
        clean["changelog"] = {
            **history,
            "histories": [
                {**entry, "items": items}
                for entry in history["histories"]
                if (items := [item for item in entry.get("items", []) if not is_comment_change(item)])
            ],
        }
    return clean


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
        batch = page["comments"]
        total = page["total"]
        if (
            not isinstance(batch, list)
            or type(total) is not int
            or page.get("startAt") != len(rows)
            or total < len(rows) + len(batch)
            or (expected is not None and total != expected)
            or (not batch and total != len(rows))
        ):
            msg = "Incomplete Jira comment page"
            raise ValueError(msg)
        for row in batch:
            cid = row.get("id")
            if not isinstance(cid, str) or not cid or cid in ids:
                msg = "Missing or duplicate Jira comment ID"
                raise ValueError(msg)
            ids.add(cid)
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
