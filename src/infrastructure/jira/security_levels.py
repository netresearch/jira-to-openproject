# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Read Jira issue security levels and classify each restricted issue by its readers.

Jira Server 9 exposes no REST endpoint for the grants of a level. The grants come
from the project-config page ``/plugins/servlet/project-config/{KEY}/issuesecurity``.
The readers of one issue come from
``/rest/api/2/user/permission/search?permissions=BROWSE&issueKey=``, which honours
the level. ``/rest/api/2/user/viewissue/search`` ignores the level and is not used.
See ADR-009.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Protocol
from urllib.parse import quote


class SecurityLevelError(ValueError):
    """A grant, reader set or search result could not be verified."""


def _require(condition: object, message: str) -> None:
    if not condition:
        raise SecurityLevelError(message)


class JiraReader(Protocol):
    """An authenticated, origin-bound Jira reader (see ``src.comment_archive.transport.Endpoint``)."""

    url: str

    def request(self, method: str, path: str, *, denial: bool = False, **kwargs: Any) -> Any: ...

    def get(self, path: str, **kwargs: Any) -> Any: ...


# Grants that resolve to a fixed set of users.
STATIC_KINDS = frozenset({"group", "role", "lead", "user"})
# Grants that depend on the issue.
PER_ISSUE_KINDS = frozenset({"assignee", "reporter", "user_field"})

_PLAIN = {"Current assignee": "assignee", "Reporter": "reporter", "Project lead": "lead"}
_NAMED = {
    "Group": "group",
    "Project Role": "role",
    "Single user": "user",
    "User custom field value": "user_field",
}
_LABEL = re.compile(r"(?P<kind>[A-Za-z ]+?) \((?P<value>.+)\)")


@dataclass(frozen=True)
class Grant:
    """One grant of a level; ``unsupported`` grants stop the level."""

    kind: str
    value: str | None = None


@dataclass(frozen=True)
class Level:
    """A security level of a project's scheme and its grants from the project-config page."""

    id: str
    name: str
    grants: tuple[Grant, ...]

    @property
    def supported(self) -> bool:
        return all(g.kind in STATIC_KINDS | PER_ISSUE_KINDS for g in self.grants)


def parse_grant(label: str) -> Grant:
    """Map one list entry of the page; anything unknown becomes an unsupported grant."""
    text = " ".join(label.split())
    if text in _PLAIN:
        return Grant(_PLAIN[text])
    match = _LABEL.fullmatch(text)
    if match and match["kind"] in _NAMED:
        return Grant(_NAMED[match["kind"]], match["value"])
    return Grant("unsupported", text)


class _GrantPage(HTMLParser):
    """Collect level names and grant labels from the issue security table only."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found = False
        self.levels: list[tuple[str, list[str]]] = []
        self._in_table = False
        self._depth = 0
        self._capture: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "table" and values.get("id") == "project-config-issuesecurities-table":
            _require(not self.found, "Repeated issue security table")
            self.found = self._in_table = True
            self._depth = 0
            return
        if not self._in_table:
            return
        if tag == "table":
            self._depth += 1
        elif tag == "tr" and "project-config-issuesecurity" in classes:
            self.levels.append(("", []))
        elif tag == "span" and "project-config-issuesecurity-name" in classes:
            self._capture, self._text = "name", []
        elif tag == "li" and self.levels:
            self._capture, self._text = "grant", []

    def handle_endtag(self, tag: str) -> None:
        if not self._in_table:
            return
        if tag == "table":
            if self._depth == 0:
                self._in_table = False
            else:
                self._depth -= 1
        elif (tag == "span" and self._capture == "name") or (tag == "li" and self._capture == "grant"):
            _require(self.levels, "Grant outside a security level row")
            text = " ".join("".join(self._text).split())
            name, grants = self.levels[-1]
            if self._capture == "name":
                _require(not name and text, "Missing or repeated security level name")
                self.levels[-1] = (text, grants)
            else:
                grants.append(text)
            self._capture = None

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._text.append(data)


def parse_grant_page(html: str) -> dict[str, tuple[Grant, ...]]:
    """Return the grants per level name; a page without the table (login, WebSudo) raises."""
    page = _GrantPage()
    page.feed(html)
    page.close()
    _require(page.found, "Issue security table not found; expected the project-config page")
    names = [name for name, _ in page.levels]
    _require(all(names) and len(set(names)) == len(names), "Missing or duplicate security level names")
    return {name: tuple(parse_grant(label) for label in labels) for name, labels in page.levels}


def project_levels(jira: JiraReader, project: str) -> list[Level]:
    """Join the REST levels of the project's scheme with the grants of its config page."""
    _require(re.fullmatch(r"[A-Z][A-Z0-9_]*", project), "Invalid Jira project key")
    response = jira.request("GET", f"/rest/api/2/project/{project}/issuesecuritylevelscheme", denial=True)
    if response.status_code == 404:
        return []
    scheme = response.json()
    rows = scheme.get("levels")
    _require(isinstance(rows, list), "Malformed issue security scheme")
    page = jira.request("GET", f"/plugins/servlet/project-config/{project}/issuesecurity")
    grants = parse_grant_page(page.text)
    by_name = {}
    for row in rows:
        level_id, name = str(row["id"]), row["name"]
        _require(level_id.isdecimal() and isinstance(name, str) and name not in by_name, "Ambiguous security level")
        by_name[name] = level_id
    _require(set(by_name) == set(grants), f"Security levels of {project} differ between REST and the config page")
    return [Level(by_name[name], name, grants[name]) for name in sorted(by_name, key=lambda n: int(by_name[n]))]


def browse_users(jira: JiraReader, **scope: str) -> frozenset[str]:
    """Active users with Browse for a project (``projectKey``) or one issue (``issueKey``)."""
    _require(set(scope) in ({"projectKey"}, {"issueKey"}), "Expected one project or issue scope")
    names: set[str] = set()
    while True:
        page = jira.get(
            "/rest/api/2/user/permission/search",
            params={"permissions": "BROWSE", "username": "", "maxResults": 1000, "startAt": len(names), **scope},
        )
        _require(isinstance(page, list), "Malformed permission search result")
        if not page:
            return frozenset(names)
        for user in page:
            name = user.get("name")
            _require(isinstance(name, str) and name and name not in names, "Duplicate or missing user in search")
            names.add(name)


@dataclass
class Directory:
    """Per-run caches of Jira reads that do not depend on the issue."""

    jira: JiraReader
    groups: dict[str, frozenset[str]] = field(default_factory=dict)
    users: dict[str, str] = field(default_factory=dict)
    fields: dict[str, str] | None = None

    def group(self, name: str) -> frozenset[str]:
        if name not in self.groups:
            members: list[str] = []
            while True:
                page = self.jira.get(
                    "/rest/api/2/group/member",
                    params={"groupname": name, "includeInactiveUsers": "true", "startAt": len(members)},
                )
                values = page["values"]
                _require(isinstance(values, list) and page["startAt"] == len(members), "Incomplete group page")
                members.extend(str(u["name"]) for u in values)
                if page["isLast"] or not values:
                    break
            _require(len(set(members)) == len(members), "Duplicate group member")
            self.groups[name] = frozenset(members)
        return self.groups[name]

    def single_user(self, display_name: str) -> str:
        """Resolve a page display name to exactly one Jira user name, inactive users included."""
        if display_name not in self.users:
            found = self.jira.get(
                "/rest/api/2/user/search",
                params={"username": display_name, "includeInactive": "true", "maxResults": 1000},
            )
            _require(isinstance(found, list), "Malformed user search result")
            names = {str(u["name"]) for u in found if u.get("displayName") == display_name}
            _require(len(names) == 1, "A single-user grant does not resolve to exactly one Jira user")
            self.users[display_name] = names.pop()
        return self.users[display_name]

    def user_field(self, name: str) -> str:
        """Resolve a user custom field name to its single Jira field ID."""
        if self.fields is None:
            self.fields = {}
            counts: dict[str, int] = {}
            for row in self.jira.get("/rest/api/2/field"):
                custom = str((row.get("schema") or {}).get("custom", ""))
                if custom.endswith((":userpicker", ":multiuserpicker")):
                    counts[row["name"]] = counts.get(row["name"], 0) + 1
                    self.fields[row["name"]] = str(row["id"])
            for duplicate in (n for n, c in counts.items() if c > 1):
                del self.fields[duplicate]
        _require(name in self.fields, "A user custom field grant does not resolve to exactly one field")
        return self.fields[name]


@dataclass(frozen=True)
class LevelReaders:
    """Static readers of one level and how many single-user grants named the migration account."""

    level: Level
    readers: frozenset[str]
    account_grants: int


def static_readers(
    directory: Directory, project: str, level: Level, browse: frozenset[str], account: str
) -> LevelReaders:
    """Group, role, project lead and single-user grants, intersected with project Browse.

    A single-user grant that names the migration account is not counted: the
    account is granted that way only temporarily, for the inventory (ADR-009).
    """
    jira = directory.jira
    users: set[str] = set()
    ignored = 0
    roles: dict[str, str] | None = None
    for grant in level.grants:
        if grant.kind == "group":
            users |= directory.group(str(grant.value))
        elif grant.kind == "lead":
            users.add(str(jira.get(f"/rest/api/2/project/{project}")["lead"]["name"]))
        elif grant.kind == "user":
            name = directory.single_user(str(grant.value))
            if name == account:
                ignored += 1
            else:
                users.add(name)
        elif grant.kind == "role":
            if roles is None:
                roles = jira.get(f"/rest/api/2/project/{project}/role")
            url = roles.get(str(grant.value)) if isinstance(roles, dict) else None
            _require(isinstance(url, str) and url.startswith(jira.url + "/"), "Unknown project role in a grant")
            for actor in jira.get(url.removeprefix(jira.url))["actors"]:
                if actor["type"] == "atlassian-user-role-actor":
                    users.add(str(actor["name"]))
                elif actor["type"] == "atlassian-group-role-actor":
                    users |= directory.group(str(actor["name"]))
                else:
                    _require(False, "Unknown project role actor type")  # noqa: FBT003
    return LevelReaders(level, frozenset(users) & browse, ignored)


def project_issues(jira: JiraReader, project: str, levels: list[Level], fields: list[str]) -> dict[str, list[dict]]:
    """All issues with a level in the project, partitioned by level; a level outside the scheme raises.

    One search per project: Jira 9.12 answered ``level = <id>`` with HTTP 400 for a
    level of which the searching user sees no issue, so a search per level can fail.
    """
    jql = f"project = {json.dumps(project)} AND level IS NOT EMPTY ORDER BY id ASC"
    result: dict[str, list[dict]] = {level.id: [] for level in levels}
    ids: set[str] = set()
    total = None
    while True:
        page = jira.get(
            "/rest/api/2/search",
            params={"jql": jql, "fields": ",".join(["security", *fields]), "startAt": len(ids), "maxResults": 100},
        )
        rows = page["issues"]
        _require(
            isinstance(rows, list) and page["startAt"] == len(ids) and (total is None or page["total"] == total),
            f"Incomplete issue search for {project}",
        )
        total = page["total"]
        for row in rows:
            security = str((row["fields"].get("security") or {}).get("id"))
            _require(security in result, f"{project} has an issue with a level outside its scheme")
            _require(row["id"] not in ids, f"Duplicate issue in the search for {project}")
            ids.add(row["id"])
            result[security].append(row)
        if len(ids) == total:
            return result
        _require(rows and len(ids) < total, f"Issue search for {project} ended early")


def restricted_total(jira: JiraReader, project: str | None = None) -> int:
    """Count issues with any level, for one project or the whole instance."""
    jql = "level IS NOT EMPTY" if project is None else f"project = {json.dumps(project)} AND level IS NOT EMPTY"
    total = jira.get("/rest/api/2/search", params={"jql": jql, "maxResults": 0, "fields": "key"})["total"]
    _require(type(total) is int, "Malformed search total")
    return total


@dataclass(frozen=True)
class Classified:
    """Readers of one issue and its class: ``static``, ``per_issue`` or ``unexplained``."""

    issue: dict
    kind: str
    readers: frozenset[str]
    extra: frozenset[str]
    missing: frozenset[str]
    account_dropped: bool


def per_issue_grantees(issue: dict, level: Level, field_ids: dict[str, str]) -> frozenset[str]:
    """Users the level's per-issue grants name on this issue."""
    fields = issue["fields"]
    names: set[str] = set()
    for grant in level.grants:
        if grant.kind in ("assignee", "reporter"):
            user = fields.get(grant.kind)
            if user:
                names.add(str(user["name"]))
        elif grant.kind == "user_field":
            value = fields.get(field_ids[str(grant.value)])
            for user in value if isinstance(value, list) else [value] if value else []:
                names.add(str(user["name"]))
    return frozenset(names)


def classify(
    issue: dict, readers: LevelReaders, effective: frozenset[str], field_ids: dict[str, str], account: str
) -> Classified:
    """Compare the effective readers with the static readers of the level.

    The migration account is removed from the effective readers unless a grant
    other than its temporary single-user grant explains it.
    """
    explained = per_issue_grantees(issue, readers.level, field_ids)
    dropped = account in effective and account not in readers.readers | explained
    actual = effective - {account} if dropped else effective
    extra = actual - readers.readers
    missing = readers.readers - actual
    if not readers.level.supported or missing or extra - explained:
        kind = "unexplained"
    elif extra:
        kind = "per_issue"
    else:
        kind = "static"
    return Classified(issue, kind, actual, extra, missing, dropped)


@dataclass
class ProjectInventory:
    """Readers and classified issues of one project's levels."""

    project: str
    levels: list[LevelReaders]
    issues: dict[str, list[Classified]]


def scan_project(jira: JiraReader, project: str, account: str, directory: Directory | None = None) -> ProjectInventory:
    """Read levels, static readers and every restricted issue of one project."""
    directory = directory or Directory(jira)
    levels = project_levels(jira, project)
    result = ProjectInventory(project, [], {})
    if not levels:
        _require(restricted_total(jira, project) == 0, f"{project} has restricted issues but no security scheme")
        return result
    browse = browse_users(jira, projectKey=project)
    field_ids = {
        str(g.value): directory.user_field(str(g.value))
        for level in levels
        for g in level.grants
        if g.kind == "user_field"
    }
    issues = project_issues(jira, project, levels, ["assignee", "reporter", *sorted(set(field_ids.values()))])
    for level in levels:
        level_readers = static_readers(directory, project, level, browse, account)
        result.levels.append(level_readers)
        result.issues[level.id] = [
            classify(issue, level_readers, browse_users(jira, issueKey=issue["key"]), field_ids, account)
            for issue in issues[level.id]
        ]
    return result


def archived_issue(jira: JiraReader, issue_id: str) -> dict:
    """The full issue with its complete changelog, comments and worklogs."""
    _require(issue_id.isdecimal(), "Invalid issue ID")
    path = f"/rest/api/2/issue/{quote(issue_id, safe='')}"
    issue = jira.get(path, params={"fields": "*all", "expand": "changelog,names"})
    log = issue["changelog"]
    _require(log["startAt"] == 0 and log["total"] == len(log["histories"]), "Incomplete issue changelog")
    comments = issue["fields"].get("comment") or {"comments": [], "total": 0}
    _require(comments["total"] == len(comments["comments"]), "Incomplete issue comments")
    worklog = issue["fields"].get("worklog")
    if worklog and worklog["total"] != len(worklog["worklogs"]):
        worklog = jira.get(path + "/worklog")
        _require(worklog["total"] == len(worklog["worklogs"]), "Incomplete issue worklogs")
        issue["fields"]["worklog"] = worklog
    return issue
