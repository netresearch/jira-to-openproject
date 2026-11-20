# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Inactive Jira accounts must not become usable OpenProject accounts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.application.components.user_migration import UserMigration
from src.models import MigrationError


def _migration(tmp_path: Path) -> UserMigration:
    migration = UserMigration(jira_client=MagicMock(), op_client=MagicMock())
    migration.data_dir = tmp_path
    return migration


def _jira(key: str, *, active: bool) -> dict[str, Any]:
    return {
        "key": key,
        "name": key,
        "emailAddress": f"{key}@example.com",
        "displayName": f"{key} User",
        "active": active,
    }


def _row(key: str, op_id: int | None) -> dict[str, Any]:
    return {
        "jira_key": key,
        "jira_name": key,
        "jira_email": f"{key}@example.com",
        "jira_display_name": f"{key} User",
        "openproject_id": op_id,
        "matched_by": "username" if op_id else "none",
    }


def _locked_ids(script: str) -> list[int]:
    match = re.search(r"ids = JSON\.parse\((\".*?(?<!\\)\")\)", script)
    assert match, script
    return json.loads(json.loads(match.group(1)))


def test_inactive_jira_user_is_created_locked(tmp_path: Path) -> None:
    migration = _migration(tmp_path)
    migration.jira_users = [_jira("gone", active=False), _jira("here", active=True)]
    migration._jira_user_index = None
    migration.user_mapping = {"gone": _row("gone", None), "here": _row("here", None)}
    migration.op_users = []
    op = migration.op_client
    op.bulk_create_records.return_value = {
        "status": "success",
        "created": [{"index": 0, "id": 101}, {"index": 1, "id": 102}],
        "errors": [],
    }
    op.execute_query_to_json_file.return_value = {"locked": [101], "missing": [], "errors": []}

    with (
        patch("src.application.components.user_migration._backfill_unmapped_users_from_op"),
        patch.object(UserMigration, "_get_user_origin_cf_ids", return_value={}),
        patch.object(UserMigration, "backfill_user_origin_metadata", return_value={}),
        patch.object(UserMigration, "extract_openproject_users", return_value=[]),
        patch.object(UserMigration, "create_user_mapping", return_value={}),
        patch.object(UserMigration, "_ensure_jira_user_details"),
    ):
        migration.create_missing_users()

    records = op.bulk_create_records.call_args.kwargs["records"]
    status_by_login = {record["login"]: record["status"] for record in records}
    assert status_by_login == {"gone": "locked", "here": "active"}


def test_create_missing_users_locks_users_from_earlier_runs(tmp_path: Path) -> None:
    migration = _migration(tmp_path)
    migration.jira_users = [_jira("gone", active=False)]
    migration._jira_user_index = None
    migration.user_mapping = {"gone": _row("gone", 5)}
    migration.op_users = [{"id": 5, "login": "gone", "status": "active", "admin": False}]
    op = migration.op_client
    op.execute_query_to_json_file.return_value = {"locked": [5], "missing": [], "errors": []}

    with (
        patch("src.application.components.user_migration._backfill_unmapped_users_from_op"),
        patch.object(UserMigration, "_get_user_origin_cf_ids", return_value={}),
        patch.object(UserMigration, "backfill_user_origin_metadata", return_value={}),
        patch.object(UserMigration, "extract_openproject_users", return_value=[]),
        patch.object(UserMigration, "create_user_mapping", return_value={}),
    ):
        result = migration.create_missing_users()

    op.bulk_create_records.assert_not_called()
    assert _locked_ids(op.execute_query_to_json_file.call_args.args[0]) == [5]
    assert result["locked"]["locked"] == 1


def test_rerun_locks_existing_user_whose_jira_account_is_inactive(tmp_path: Path) -> None:
    migration = _migration(tmp_path)
    migration.jira_users = [
        _jira("gone", active=False),
        _jira("back", active=True),
        _jira("already", active=False),
        _jira("alias-off", active=False),
        _jira("alias-on", active=True),
        _jira("boss", active=False),
    ]
    migration._jira_user_index = None
    migration.user_mapping = {
        "gone": _row("gone", 5),
        "back": _row("back", 6),
        "already": _row("already", 7),
        "alias-off": _row("alias-off", 8),
        "alias-on": _row("alias-on", 8),
        "boss": _row("boss", 9),
    }
    migration.op_users = [
        {"id": 5, "login": "gone", "status": "active", "admin": False},
        {"id": 6, "login": "back", "status": "locked", "admin": False},
        {"id": 7, "login": "already", "status": "locked", "admin": False},
        {"id": 8, "login": "alias", "status": "active", "admin": False},
        {"id": 9, "login": "boss", "status": "active", "admin": True},
    ]
    op = migration.op_client
    op.execute_query_to_json_file.return_value = {"locked": [5], "missing": [], "errors": []}

    result = migration.lock_inactive_users()

    op.execute_query_to_json_file.assert_called_once()
    script = op.execute_query_to_json_file.call_args.args[0]
    assert _locked_ids(script) == [5]
    # The script only ever locks: a hand-locked user (id 6) is never unlocked.
    assert re.findall(r"status\s*=", script) == ["status ="]
    assert "user.status = 'locked'" in script
    assert result == {"requested": 1, "locked": 1, "missing": 0, "skipped_admin": 1}


def test_unconfirmed_lock_fails_the_run(tmp_path: Path) -> None:
    migration = _migration(tmp_path)
    migration.jira_users = [_jira("gone", active=False)]
    migration._jira_user_index = None
    migration.user_mapping = {"gone": _row("gone", 5)}
    migration.op_users = [{"id": 5, "login": "gone", "status": "active", "admin": False}]
    migration.op_client.execute_query_to_json_file.return_value = {"locked": [], "missing": [], "errors": []}

    with pytest.raises(MigrationError, match="5"):
        migration.lock_inactive_users()


def test_nothing_to_lock_sends_no_script(tmp_path: Path) -> None:
    migration = _migration(tmp_path)
    unknown = _jira("unknown", active=False)
    del unknown["active"]  # no flag: not proof of inactivity
    migration.jira_users = [_jira("here", active=True), unknown]
    migration._jira_user_index = None
    migration.user_mapping = {"here": _row("here", 5), "unknown": _row("unknown", 6)}
    migration.op_users = [
        {"id": 5, "login": "here", "status": "active", "admin": False},
        {"id": 6, "login": "unknown", "status": "active", "admin": False},
    ]

    assert migration.lock_inactive_users() == {"requested": 0, "locked": 0, "missing": 0, "skipped_admin": 0}
    migration.op_client.execute_query_to_json_file.assert_not_called()
