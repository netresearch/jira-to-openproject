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
ROLES = ("Administrators", "Developers", "Users")


def _role_ids(admin: JiraAdmin, needed: set[str]) -> dict[str, int]:
    existing = {row["name"]: row["id"] for row in admin.rest("GET", "/rest/api/2/role")}
    for name in sorted(needed - existing.keys()):
        existing[name] = admin.rest("POST", "/rest/api/2/role", json={"name": name})["id"]
    return existing


def _permission_scheme(admin: JiraAdmin, roles: dict[str, int]) -> int:
    """Grant browsing only through project roles. Anonymous users and application access get nothing."""
    grants = [
        {"holder": {"type": "projectRole", "parameter": str(roles[role])}, "permission": "BROWSE_PROJECTS"}
        for role in roles
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
    role_names = {row["visibility"]["value"] for row in cases["role"]["comments"] if row["visibility"]}
    roles = _role_ids(admin, set(ROLES) | role_names)
    scheme = _permission_scheme(admin, roles)
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
        actors = {"Administrators": [admin.user], "Users": [accounts["authorized"]["name"]]}
        if case != "ordinary":
            actors["Users"].append(accounts["other"]["name"])
        visibilities = [row["visibility"] for row in source["comments"] if row["visibility"]]
        for visibility in visibilities:
            if visibility["type"] == "group":
                _group(admin, visibility["value"], [admin.user, accounts["authorized"]["name"]])
            else:
                actors.setdefault(visibility["value"], []).extend([admin.user, accounts["authorized"]["name"]])
        for role, users in actors.items():
            admin.rest(
                "POST",
                f"/rest/api/2/project/{project}/role/{roles[role]}",
                json={"user": sorted(set(users))},
            )
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
