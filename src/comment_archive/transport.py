# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Authenticated, origin-bound reads for the archive migration."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, urlsplit

import requests
from tenacity import (
    RetryError,
    Retrying,
    retry_if_exception_type,
    retry_if_result,
    stop_after_attempt,
    wait_exponential,
)

from src.infrastructure.jira.comment_audience import _pages


class ArchiveError(ValueError):
    """An unverified source, identity or destination blocks the cutover."""


def require(condition: object, message: str) -> None:
    """Stop before treating an inconclusive read as proof."""
    if not condition:
        raise ArchiveError(message)


def verified_denial(jira: Endpoint, response: requests.Response, resource_path: str) -> None:
    """A hidden issue/request must itself be inaccessible, not merely its comment endpoint."""

    def jira_error(reply: requests.Response) -> bool:
        value = reply.json()
        return isinstance(value, dict) and bool(
            value.get("errorMessages") or value.get("message") or value.get("i18nErrorMessage")
        )

    require(jira_error(response), "Unrecognized Jira denial response")
    resource = jira.request("GET", resource_path, denial=True)
    require(
        resource.status_code in (403, 404) and jira_error(resource), "Comment denial disagrees with resource access"
    )


class Endpoint:
    """One principal and one HTTPS origin; never forward credentials on redirects."""

    def __init__(self, url: str, token: str, *, jira: bool, ca: str | bool = True) -> None:
        root = urlsplit(url)
        require(
            root.scheme == "https" and root.hostname and root.username is None and not root.query and not root.fragment,
            "A credential-free HTTPS base URL is required",
        )
        require(bool(token), "Missing authentication token")
        self.url = url.rstrip("/")
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = ca
        if jira:
            self.session.headers["Authorization"] = f"Bearer {token}"
        else:
            self.session.auth = ("apikey", token)

    def request(self, method: str, path: str, *, denial: bool = False, **kwargs: Any) -> requests.Response:
        require(path.startswith("/") and not path.startswith("//"), "Invalid API path")

        def send() -> requests.Response:
            self.session.cookies.clear()
            return self.session.request(method, self.url + path, timeout=(10, 90), allow_redirects=False, **kwargs)

        if method == "GET":
            retry_reads = Retrying(
                stop=stop_after_attempt(3),
                wait=wait_exponential(min=1, max=4),
                retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError))
                | retry_if_result(lambda reply: reply.status_code in (429, 502, 503, 504)),
                reraise=True,
            )
            try:
                response = retry_reads(send)
            except RetryError as exhausted:
                response = exhausted.last_attempt.result()
        else:
            response = send()
        if denial and response.status_code in (403, 404):
            return response
        require(response.status_code in (200, 201), f"{method} {path} returned HTTP {response.status_code}")
        return response

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs).json()


def project_kind(jira: Endpoint, project: str) -> str:
    """Identify the actual Jira project model before choosing a reader API."""
    require(re.fullmatch(r"[A-Z][A-Z0-9_]*", project), "Invalid Jira project key")
    data = jira.get(f"/rest/api/2/project/{project}")
    kind = data.get("projectTypeKey")
    require(
        data.get("key") == project and kind in ("software", "business", "service_desk"),
        "Unrecognized Jira project type or key",
    )
    return kind


def core_comments(jira: Endpoint, issue: str, *, reader: bool) -> list[dict]:
    """Read the exact comment collection visible to this principal, across all pages."""
    path = f"/rest/api/2/issue/{quote(issue, safe='')}/comment"
    first = jira.request("GET", path, denial=reader, params={"startAt": 0, "maxResults": 100, "expand": "properties"})
    if first.status_code in (403, 404):
        verified_denial(jira, first, path.removesuffix("/comment"))
        return []
    return _pages(lambda params: first if params["startAt"] == 0 else jira.request("GET", path, params=params))


def request_comments(jira: Endpoint, issue: str, *, reader: bool) -> list[dict]:
    """Use Service Management's own agent/customer checks for service requests."""
    path = f"/rest/servicedeskapi/request/{quote(issue, safe='')}/comment"
    rows: list[dict] = []
    seen = set()
    while True:
        response = jira.request("GET", path, denial=reader and not rows, params={"start": len(rows), "limit": 100})
        if response.status_code in (403, 404):
            verified_denial(jira, response, path.removesuffix("/comment"))
            return []
        page = response.json()
        batch = page["values"]
        require(
            isinstance(batch, list)
            and page["start"] == len(rows)
            and page["size"] == len(batch)
            and type(page["isLastPage"]) is bool
            and (batch or page["isLastPage"]),
            "Incomplete Service Management comment page",
        )
        for row in batch:
            require(isinstance(row.get("id"), str) and row["id"] not in seen, "Duplicate or missing request comment ID")
            seen.add(row["id"])
        rows.extend(batch)
        if page["isLastPage"]:
            return rows


def service_visibility_flags(row: dict, *, service: bool) -> list[bool]:
    """Normalize all supported service-desk flags before comparing the two APIs."""
    public_flags = []
    for flag in ("jsdPublic", "public", "internal", "isInternal"):
        if flag in row:
            require(service and type(row[flag]) is bool, "Unexpected service-specific comment metadata")
            public_flags.append(not row[flag] if flag in ("internal", "isInternal") else row[flag])
    properties = row.get("properties", [])
    require(isinstance(properties, list), "Malformed comment property list")
    for prop in properties:
        require(
            service
            and isinstance(prop, dict)
            and prop.get("key") == "sd.public.comment"
            and isinstance(prop.get("value"), dict)
            and "internal" in prop["value"],
            "Unverified Jira comment property; check the original audience",
        )
        internal = prop["value"]["internal"]
        require(type(internal) is bool or internal in ("true", "false"), "Unverified internal comment flag")
        public_flags.append(internal in (False, "false"))
    return public_flags


def check_core_metadata(rows: list[dict], *, service: bool, request_view: dict | None = None) -> set[str]:
    """Refuse unknown restrictions and service-specific comments outside the request API."""
    restricted = set()
    for row in rows:
        visibility = row.get("visibility")
        if visibility is not None:
            require(
                isinstance(visibility, dict)
                and visibility.get("type") in {"role", "group"}
                and (visibility.get("value") or visibility.get("identifier")),
                "Unrecognized Jira comment visibility",
            )
            restricted.add(row["id"])
        public_flags = service_visibility_flags(row, service=service)
        if request_view is not None:
            cid = row["id"]
            require(
                cid in request_view and all(flag is request_view[cid]["public"] for flag in public_flags),
                "Jira and service desk reader access disagree about comment visibility",
            )
    # Internal notes always require corroborating access through the authenticated agent API.
    if request_view is not None:
        restricted.update(cid for cid, row in request_view.items() if not row["public"])
    return restricted


def visible_comments(jira: Endpoint, issue: str, kind: str, *, reader: bool) -> dict[str, dict]:
    """Preserve only content and display metadata actually returned for this reader."""
    rows = (
        request_comments(jira, issue, reader=reader)
        if kind == "service_desk"
        else core_comments(jira, issue, reader=reader)
    )
    if kind != "service_desk":
        check_core_metadata(rows, service=False)
    else:
        require(all(type(row.get("public")) is bool for row in rows), "Unverified service request comment visibility")
    out = {}
    for row in rows:
        key = row.get("id")
        require(
            isinstance(key, str) and key.isdecimal() and isinstance(row.get("body"), str),
            "Malformed Jira comment identity or body",
        )
        created = row.get("created")
        if isinstance(created, dict):
            created = created.get("iso8601")
        author = row.get("author") or {}
        comment = {"id": key, "body": row["body"], "created": created, "author": author.get("displayName")}
        if kind == "service_desk":
            comment["public"] = row["public"]
        out[str(key)] = comment
    return out


ARCHIVE_NAME = "jira-protected-comments.j2o"


def attachments(op: Endpoint, wp: int) -> list[dict]:
    """List native attachments without trusting credential-bearing links from the server."""
    page = op.get(f"/api/v3/work_packages/{wp}/attachments")
    files = page["_embedded"]["elements"]
    require(isinstance(files, list) and len(files) == page["total"], "Incomplete target attachment collection")
    return [item for item in files if item.get("fileName") == ARCHIVE_NAME]


def download(op: Endpoint, attachment: dict) -> bytes:
    """Fetch local storage content through the authorized OpenProject endpoint."""
    return op.request("GET", f"/api/v3/attachments/{int(attachment['id'])}/content").content


def publish(op: Endpoint, wp: int, data: bytes | None) -> dict | None:
    """Resume an existing identical upload. A differing or duplicate copy blocks cutover."""
    files = attachments(op, wp)
    if data is not None and not files:
        op.request(
            "POST",
            f"/api/v3/work_packages/{wp}/attachments",
            files={
                "metadata": (None, json.dumps({"fileName": ARCHIVE_NAME}), "application/json"),
                "file": (ARCHIVE_NAME, data, "application/octet-stream"),
            },
        )
        files = attachments(op, wp)
    require(len(files) == int(data is not None), f"Unexpected existing archive count on work package {wp}")
    if files:
        require(download(op, files[0]) == data, f"Stored archive differs on work package {wp}; keep target closed")
        return files[0]
    return None
