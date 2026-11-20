# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Check the Docker-free helpers of the opt-in company comment acceptance test."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from src.application.components.admin_scheme_migration import ROLE_NAME_MAPPING
from tests.integration import company_comment_load as load
from tests.integration import company_comment_sample as sample
from tests.integration import test_company_comment_acceptance as acceptance

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
    every_role = set(load.MEMBER_ROLES)
    with pytest.raises(RuntimeError, match="no role is left"):
        load.baseline_role(every_role)


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


def test_fixture_replaces_a_readable_file_with_an_owner_only_one(tmp_path: Path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    output = private / "fixture.json"
    output.write_text("old")
    output.chmod(0o644)
    sample.write_private(sample.private_output(output), {"cases": {}})
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(output.read_text()) == {"cases": {}}
    assert sorted(path.name for path in private.iterdir()) == ["fixture.json"]


def test_fixture_replaces_a_symlink_without_writing_its_target(tmp_path: Path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = tmp_path / "elsewhere.json"
    target.write_text("keep")
    (private / "fixture.json").symlink_to(target)
    sample.write_private(private / "fixture.json", {})
    assert target.read_text() == "keep"
    assert not (private / "fixture.json").is_symlink()


def test_fixture_refuses_a_directory_others_can_read(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    with pytest.raises(SystemExit, match="only you can access"):
        sample.private_output(shared / "fixture.json")


def test_fixture_creates_a_missing_directory_owner_only(tmp_path: Path):
    output = tmp_path / "new" / "fixture.json"
    assert sample.private_output(output) == output
    assert os.stat(output.parent).st_mode & 0o777 == 0o700


def test_fixture_refuses_the_checkout():
    with pytest.raises(SystemExit, match="outside the checkout"):
        sample.private_output(sample.CHECKOUT / "var" / "fixture.json")


def test_down_removes_the_state_of_the_destroyed_stack(tmp_path: Path):
    state = tmp_path / "state"
    (state / "var" / "state" / "checkpoints").mkdir(parents=True)
    for name in ("loaded.json", "migration-credentials.json", "stack.json", "archive-role-0a1b.sqlite", "fixture.json"):
        (state / name).write_text("{}")
    (state / "compose-project").write_text("j2o-accept-test\n")
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "docker").write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$DOCKER_CALLS"\n')
    (stub / "docker").chmod(0o700)
    calls = tmp_path / "docker-calls"
    env = {
        **os.environ,
        "PATH": f"{stub}:{os.environ['PATH']}",
        "DOCKER_CALLS": str(calls),
        "J2O_COMPANY_COMMENT_FIXTURE": str(state / "fixture.json"),
        "J2O_COMPANY_COMMENT_STATE": str(state),
        "J2O_ACCEPT_JIRA_LICENSE": str(tmp_path / "license"),
    }
    script = Path(sample.__file__).with_name("run_company_comment_acceptance.sh")
    subprocess.run(["bash", str(script), "down"], env=env, check=True, timeout=60)
    assert (
        calls.read_text() == f"compose -p j2o-accept-test -f {state.resolve()}/compose.yml down -v --remove-orphans\n"
    )
    assert sorted(path.name for path in state.iterdir()) == ["fixture.json"]


def test_container_lookup_uses_the_generated_compose_file(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def check_output(args: list[str], **kwargs: Any) -> str:
        calls.append(args)
        return "abc\n" if args[1] == "compose" else "/j2o-accept-test-op-1\n"

    monkeypatch.setattr(acceptance.subprocess, "check_output", check_output)
    assert acceptance._container("j2o-accept-test", "/private/compose.yml") == "j2o-accept-test-op-1"
    assert calls[0] == ["docker", "compose", "-p", "j2o-accept-test", "-f", "/private/compose.yml", "ps", "-q", "op"]
