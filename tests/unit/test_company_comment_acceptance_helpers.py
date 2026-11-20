# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Check the Docker-free helpers of the opt-in company comment acceptance test."""

from __future__ import annotations

from typing import Any

import pytest

from src.application.components.admin_scheme_migration import ROLE_NAME_MAPPING
from tests.integration import company_comment_load as load

pytestmark = pytest.mark.unit


def _comment(kind: str | None = None, value: str = "") -> dict[str, Any]:
    return {"id": "1", "body": "text", "visibility": {"type": kind, "value": value} if kind else None}


ACCOUNTS = {"authorized": {"name": "accauthorized"}, "other": {"name": "accother"}}


def test_restriction_roles_include_role_comments_of_every_case():
    cases = {
        "ordinary": {"comments": [_comment()]},
        "group": {"comments": [_comment("group", "staff")]},
        "role": {"comments": [_comment("role", "Developers")]},
        "security": {"comments": [_comment("role", "Service Desk Team"), _comment()]},
    }
    assert load.restriction_roles(cases) == {"Developers", "Service Desk Team"}


def test_baseline_role_avoids_every_sampled_restriction():
    assert load.baseline_role(set()) == "Users"
    assert load.baseline_role({"users"}) == "Developers"
    with pytest.raises(RuntimeError, match="no role is left"):
        load.baseline_role(set(load.MEMBER_ROLES))


def test_baseline_roles_become_openproject_memberships():
    """The unauthorized account of the group, role and security cases must still reach the work package."""
    for name in load.MEMBER_ROLES:
        assert ROLE_NAME_MAPPING[name.casefold()] == ["Project member"]


def test_unauthorized_account_holds_no_restriction_role():
    comments = [_comment("role", "Users"), _comment()]
    baseline = load.baseline_role(load.restriction_roles({"role": {"comments": comments}}))
    actors = load.role_actors("role", "admin", ACCOUNTS, comments, baseline)
    assert actors == {
        "Administrators": ["admin"],
        "Developers": ["accauthorized", "accother"],
        "Users": ["accauthorized", "admin"],
    }


def test_ordinary_unauthorized_account_has_no_role():
    actors = load.role_actors("ordinary", "admin", ACCOUNTS, [_comment()], "Users")
    assert actors == {"Administrators": ["admin"], "Users": ["accauthorized"]}


def test_only_administrators_and_baseline_roles_grant_browsing():
    class Admin:
        def __init__(self) -> None:
            self.payload: dict[str, Any] = {}

        def rest(self, method: str, path: str, **kwargs: Any) -> dict[str, int]:
            self.payload = kwargs["json"]
            return {"id": 7}

    admin = Admin()
    roles = {"Administrators": 1, "Developers": 2, "Users": 3}
    assert load._permission_scheme(admin, roles, "Developers") == 7  # type: ignore[arg-type]
    browse = {
        row["holder"]["parameter"] for row in admin.payload["permissions"] if row["permission"] == "BROWSE_PROJECTS"
    }
    assert browse == {"1", "2"}
