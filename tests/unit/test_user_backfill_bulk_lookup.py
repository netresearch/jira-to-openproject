# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""The user back-fill reads every OpenProject user once instead of querying per user."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.application.components.user_migration import _backfill_unmapped_users_from_op

OP_USERS = [
    {"id": 352, "login": "alice", "mail": "Alice@Example.com"},
    {"id": 7, "login": "Bob", "mail": "bob@example.com"},
    {"id": 9, "login": "carol", "mail": None},
]


def _row(name, email):
    return {"jira_name": name, "jira_email": email, "matched_by": "none", "openproject_id": None}


def test_a_first_run_reads_the_users_once_and_queries_no_single_user():
    # The pilot case: 447 Jira users, none in OpenProject yet, took 895 single queries.
    op = MagicMock()
    op.get_users.return_value = OP_USERS
    mapping = {f"u{i}": _row(f"new{i}", f"new{i}@example.com") for i in range(50)}
    assert _backfill_unmapped_users_from_op(mapping, op, MagicMock()) == 0
    op.get_users.assert_called_once_with(refresh=True)
    op.get_user.assert_not_called()


def test_the_index_follows_the_rules_of_get_user():
    op = MagicMock()
    op.get_users.return_value = OP_USERS
    mapping = {
        "login": _row("alice", None),
        "mail any case": _row("nomatch", "ALICE@example.COM"),
        "login is exact": _row("bob", None),
        "id": _row("9", None),
        "mail in name": _row("bob@example.com", None),
    }
    assert _backfill_unmapped_users_from_op(mapping, op, MagicMock()) == 4
    found = {key: entry["openproject_id"] for key, entry in mapping.items()}
    assert found == {"login": 352, "mail any case": 352, "login is exact": None, "id": 9, "mail in name": 7}
    assert mapping["login"]["matched_by"] == "backfill_op_lookup"


def test_without_an_unmapped_user_nothing_is_read():
    op = MagicMock()
    mapping = {"done": {"jira_name": "a", "openproject_id": 1, "matched_by": "username"}}
    assert _backfill_unmapped_users_from_op(mapping, op, MagicMock()) == 0
    op.get_users.assert_not_called()


def test_an_unreadable_user_list_falls_back_to_single_lookups():
    op = MagicMock()
    op.get_users.side_effect = RuntimeError("rails down")
    op.get_user.return_value = {"id": 5, "login": "dave"}
    mapping = {"dave": _row("dave", None)}
    assert _backfill_unmapped_users_from_op(mapping, op, MagicMock()) == 1
    op.get_user.assert_called_once_with("dave")
