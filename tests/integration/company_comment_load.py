# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Recreate sampled Jira issues and their comment audiences in a disposable local Jira.

Each case gets its own project, one authorized and one unauthorized ordinary account and a
permission scheme that grants project access only through project roles. Comments keep their
original body and visibility. Jira assigns new comment IDs, an author and a creation date.
"""

from __future__ import annotations

import secrets
from typing import Any

from tests.integration.company_comment_stack import JiraAdmin

PROJECTS = {"ordinary": "ACCORD", "group": "ACCGRP", "role": "ACCROLE", "security": "ACCSEC"}
SECURITY_GROUP = "j2o-accept-security"
# Jira role names that j2o's admin_schemes component turns into an OpenProject "Project member"
# membership (ROLE_NAME_MAPPING). Both accounts need that membership to reach the work package.
MEMBER_ROLES = ("Users", "Developers", "Members", "Team Members")


def restriction_roles(cases: dict[str, Any]) -> set[str]:
    """Name every project role that a sampled comment of any case is restricted to."""
    return {
        row["visibility"]["value"]
        for source in cases.values()
        for row in source["comments"]
        if row["visibility"] and row["visibility"]["type"] == "role"
    }


def baseline_role(restricted: set[str]) -> str:
    """Pick the role that grants project access and that no sampled comment is restricted to."""
    taken = {name.casefold() for name in restricted}
    for name in MEMBER_ROLES:
        if name.casefold() not in taken:
            return name
    msg = "Every member role is a sampled comment restriction; no role is left for baseline access"
    raise RuntimeError(msg)


def role_actors(
    case: str, admin_user: str, accounts: dict[str, dict[str, str]], comments: list[dict], baseline: str
) -> dict[str, list[str]]:
    """Assign project roles: baseline access for both accounts, restriction roles for the authorized one only.

    The unauthorized account of the ordinary case gets no role at all.
    """
    authorized, other = accounts["authorized"]["name"], accounts["other"]["name"]
    actors = {"Administrators": {admin_user}, baseline: {authorized}}
    if case != "ordinary":
        actors[baseline].add(other)
    for row in comments:
        visibility = row["visibility"]
        if visibility and visibility["type"] == "role":
            actors.setdefault(visibility["value"], set()).update({admin_user, authorized})
    return {role: sorted(users) for role, users in actors.items()}


def _role_ids(admin: JiraAdmin, needed: set[str]) -> dict[str, int]:
    existing = {row["name"]: row["id"] for row in admin.rest("GET", "/rest/api/2/role")}
    for name in sorted(needed - existing.keys()):
        existing[name] = admin.rest("POST", "/rest/api/2/role", json={"name": name})["id"]
    return existing


def _permission_scheme(admin: JiraAdmin, roles: dict[str, int], baseline: str) -> int:
    """Grant browsing only through the administrator and baseline roles.

    Restriction roles grant nothing by themselves, and anonymous users and application access get nothing.
    """
    grants = [
        {"holder": {"type": "projectRole", "parameter": str(roles[role])}, "permission": "BROWSE_PROJECTS"}
        for role in ("Administrators", baseline)
    ]
    grants += [
        {"holder": {"type": "projectRole", "parameter": str(roles["Administrators"])}, "permission": permission}
        for permission in (
            "ADMINISTER_PROJECTS",
            "CREATE_ISSUES",
            "EDIT_ISSUES",
            "ADD_COMMENTS",
            "SET_ISSUE_SECURITY",
            "ASSIGNABLE_USER",
            "ASSIGN_ISSUES",
            "RESOLVE_ISSUES",
            "TRANSITION_ISSUES",
            "SCHEDULE_ISSUES",
        )
    ]
    scheme = admin.rest("POST", "/rest/api/2/permissionscheme", json={"name": "j2o acceptance", "permissions": grants})
    return scheme["id"]


def _user(admin: JiraAdmin, name: str, label: str) -> dict[str, str]:
    password = secrets.token_urlsafe(24)
    admin.rest(
        "POST",
        "/rest/api/2/user",
        json={
            "name": name,
            "password": password,
            "emailAddress": f"{name}@example.invalid",
            "displayName": label,
            "applicationKeys": ["jira-software"],
        },
    )
    me = admin.rest("GET", "/rest/api/2/myself", auth=(name, password))
    token = admin.rest(
        "POST", "/rest/pat/latest/tokens", auth=(name, password), json={"name": "acceptance", "expirationDuration": 2}
    )["rawToken"]
    return {"name": name, "key": me["key"], "password": password, "token": token, "email": f"{name}@example.invalid"}


def _group(admin: JiraAdmin, name: str, members: list[str]) -> None:
    if admin.rest("GET", "/rest/api/2/group/member", params={"groupname": name}, ok=(200, 404)) is None:
        admin.rest("POST", "/rest/api/2/group", json={"name": name})
    current = {
        row["name"] for row in admin.rest("GET", "/rest/api/2/group/member", params={"groupname": name})["values"]
    }
    for member in members:
        if member not in current:
            admin.rest("POST", "/rest/api/2/group/user", params={"groupname": name}, json={"name": member})


def load(admin: JiraAdmin, fixture: dict[str, Any]) -> dict[str, Any]:
    """Create every case and return the private state that the verification reads."""
    admin.enable_group_comment_visibility()
    cases = fixture["cases"]
    restricted = restriction_roles(cases)
    baseline = baseline_role(restricted)
    roles = _role_ids(admin, {"Administrators", baseline} | restricted)
    scheme = _permission_scheme(admin, roles, baseline)
    state: dict[str, Any] = {"cases": {}}
    for case, source in cases.items():
        project = PROJECTS[case]
        accounts = {
            side: _user(admin, f"acc{case}{side}", f"Acceptance {case} {side}") for side in ("authorized", "other")
        }
        admin.rest(
            "POST",
            "/rest/api/2/project",
            json={
                "key": project,
                "name": f"Acceptance {case}",
                "projectTypeKey": "software",
                "projectTemplateKey": "com.pyxis.greenhopper.jira:gh-scrum-template",
                "lead": admin.user,
            },
        )
        admin.rest("PUT", f"/rest/api/2/project/{project}/permissionscheme", json={"id": scheme})
        for row in source["comments"]:
            if row["visibility"] and row["visibility"]["type"] == "group":
                _group(admin, row["visibility"]["value"], [admin.user, accounts["authorized"]["name"]])
        for role, users in role_actors(case, admin.user, accounts, source["comments"], baseline).items():
            admin.rest("POST", f"/rest/api/2/project/{project}/role/{roles[role]}", json={"user": users})
        fields: dict[str, Any] = {
            "project": {"key": project},
            "summary": source["summary"],
            "issuetype": {"name": "Task"},
        }
        if source["security"]:
            _group(admin, SECURITY_GROUP, [admin.user, accounts["authorized"]["name"]])
            level = admin.security_level("j2o acceptance", source["security"], SECURITY_GROUP, project)
            fields["security"] = {"id": level}
        issue = admin.rest("POST", "/rest/api/2/issue", json={"fields": fields})
        comments = {}
        for row in source["comments"]:
            payload = {"body": row["body"]}
            if row["visibility"]:
                payload["visibility"] = {"type": row["visibility"]["type"], "value": row["visibility"]["value"]}
            created = admin.rest("POST", f"/rest/api/2/issue/{issue['key']}/comment", json=payload)
            comments[row["id"]] = created["id"]
        state["cases"][case] = {
            "project": project,
            "issue": issue["key"],
            "issue_id": issue["id"],
            "comments": comments,
            "restricted": [row["id"] for row in source["comments"] if row["visibility"]],
            "security": source["security"],
            "accounts": accounts,
        }
    return state
