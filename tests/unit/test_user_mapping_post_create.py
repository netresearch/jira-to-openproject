"""Newly-created users must have their OP id written back to the mapping.

Bug A: ``UserMigration.create_missing_users`` calls ``bulk_create_records``
to create users in OP, gets back ``[{index:N, id:OP_ID}, ...]``, but the
code at the ``created_list`` iteration only appends to a summary list and
**never updates the corresponding ``batch[idx]`` mapping entry** with
``openproject_id``.

Result observed live: 228 of 438 Jira users were fresh-created in OP but
their mapping entries kept ``matched_by: 'none'`` and ``openproject_id:
None``. Downstream WP migration's ``_map_user`` rejects entries with
``openproject_id == None``, so 228 assignees got silently dropped from
4076 work packages.

The duplicate-resolution branch (lines 1190-1200) already does the right
thing for users that turn out to exist already. The fresh-create branch
must do the same — write back ``openproject_id`` / ``openproject_login``
/ ``openproject_email`` / ``matched_by="created"``.
"""

from __future__ import annotations

from src.application.components.user_migration import (
    _apply_created_user_ids_to_mapping,
)


def test_writes_openproject_id_back_to_mapping_entry() -> None:
    batch = [
        {"jira_name": "alice", "jira_email": "alice@x", "openproject_id": None,
         "matched_by": "none"},
        {"jira_name": "bob", "jira_email": "bob@x", "openproject_id": None,
         "matched_by": "none"},
    ]
    meta = [
        {"login": "alice", "mail": "alice@x"},
        {"login": "bob", "mail": "bob@x"},
    ]
    created_list = [
        {"index": 0, "id": 100},
        {"index": 1, "id": 101},
    ]

    n = _apply_created_user_ids_to_mapping(batch, meta, created_list)

    assert n == 2
    assert batch[0]["openproject_id"] == 100
    assert batch[0]["openproject_login"] == "alice"
    assert batch[0]["openproject_email"] == "alice@x"
    assert batch[0]["matched_by"] == "created"
    assert batch[1]["openproject_id"] == 101
    assert batch[1]["matched_by"] == "created"


def test_handles_missing_index_gracefully() -> None:
    batch = [{"jira_name": "alice", "openproject_id": None, "matched_by": "none"}]
    meta = [{"login": "alice", "mail": "alice@x"}]
    created_list = [{"id": 100}]  # missing index

    n = _apply_created_user_ids_to_mapping(batch, meta, created_list)

    assert n == 0
    assert batch[0]["openproject_id"] is None  # unchanged


def test_skips_out_of_range_index() -> None:
    batch = [{"jira_name": "alice", "openproject_id": None}]
    meta = [{"login": "alice", "mail": "alice@x"}]
    created_list = [{"index": 5, "id": 100}]  # OOB

    n = _apply_created_user_ids_to_mapping(batch, meta, created_list)

    assert n == 0


def test_skips_entries_without_op_id() -> None:
    batch = [{"jira_name": "alice", "openproject_id": None, "matched_by": "none"}]
    meta = [{"login": "alice", "mail": "alice@x"}]
    created_list = [{"index": 0}]  # no id

    n = _apply_created_user_ids_to_mapping(batch, meta, created_list)

    assert n == 0
    assert batch[0]["openproject_id"] is None


def test_idempotent_does_not_overwrite_existing_mapping() -> None:
    """If an entry was already mapped (e.g. from prior dupe resolution in
    the same run), don't trample it.
    """
    batch = [{"jira_name": "alice", "openproject_id": 999, "matched_by": "username_existing"}]
    meta = [{"login": "alice", "mail": "alice@x"}]
    created_list = [{"index": 0, "id": 100}]

    n = _apply_created_user_ids_to_mapping(batch, meta, created_list)

    # If already mapped, the new id is suspicious — leave the existing mapping alone
    assert n == 0
    assert batch[0]["openproject_id"] == 999
    assert batch[0]["matched_by"] == "username_existing"
