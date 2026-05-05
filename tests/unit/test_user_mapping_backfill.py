"""Bug A2: back-fill ``openproject_id`` for users still ``matched_by='none'``.

After the initial probe in ``create_user_mapping`` builds the mapping,
some users that DO exist in OP can still end up with
``matched_by='none'`` and ``openproject_id: None`` (sample sizes,
custom-field probe failures, race vs the user-creation phase, prior runs
left the disk file in this state, etc.). On the live NRS / TEST data,
228 of 438 mapping entries were stuck this way — even high-traffic
real users like ``sebastian.mendel`` whose OP record was clearly there
(``id=352``).

The fix is a defensive back-fill: walk the mapping, and for every entry
that's still ``matched_by='none'``, do a final ``User.find_by(login:)``
/ ``find_by(mail:)`` lookup against OP. If found, write the id and
matched_by="backfill_op_lookup". This unblocks downstream consumers
(work-package assignee mapping, time-entry author mapping) without
needing a wipe + full re-run.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.application.components.user_migration import (
    _backfill_unmapped_users_from_op,
)


def test_backfills_user_found_by_login() -> None:
    user_mapping = {
        "sebastian.mendel": {
            "jira_name": "sebastian.mendel",
            "jira_email": "sebastian.mendel@netresearch.de",
            "matched_by": "none",
            "openproject_id": None,
        },
    }
    op = MagicMock()
    op.get_user.return_value = {
        "id": 352,
        "login": "sebastian.mendel",
        "mail": "sebastian.mendel@netresearch.de",
    }
    logger = MagicMock()

    n = _backfill_unmapped_users_from_op(user_mapping, op, logger)

    assert n == 1
    e = user_mapping["sebastian.mendel"]
    assert e["openproject_id"] == 352
    assert e["openproject_login"] == "sebastian.mendel"
    assert e["openproject_email"] == "sebastian.mendel@netresearch.de"
    assert e["matched_by"] == "backfill_op_lookup"


def test_falls_back_to_email_when_login_lookup_fails() -> None:
    """Some Jira logins don't survive into OP (e.g. truncated, normalised);
    fall back to email lookup.
    """
    user_mapping = {
        "x": {
            "jira_name": "weird+name",
            "jira_email": "real@example.com",
            "matched_by": "none",
            "openproject_id": None,
        },
    }
    op = MagicMock()
    op.get_user.side_effect = [None, {"id": 99, "login": "real", "mail": "real@example.com"}]
    n = _backfill_unmapped_users_from_op(user_mapping, op, MagicMock())
    assert n == 1
    assert user_mapping["x"]["openproject_id"] == 99


def test_skips_already_mapped_entries() -> None:
    user_mapping = {
        "alice": {"jira_name": "alice", "openproject_id": 1, "matched_by": "j2o_user_key_cf"},
    }
    op = MagicMock()
    n = _backfill_unmapped_users_from_op(user_mapping, op, MagicMock())
    assert n == 0
    op.get_user.assert_not_called()


def test_skips_when_neither_login_nor_email_in_op() -> None:
    user_mapping = {
        "ghost": {
            "jira_name": "ghost",
            "jira_email": "ghost@x",
            "matched_by": "none",
            "openproject_id": None,
        },
    }
    op = MagicMock()
    op.get_user.return_value = None
    n = _backfill_unmapped_users_from_op(user_mapping, op, MagicMock())
    assert n == 0
    assert user_mapping["ghost"]["openproject_id"] is None


def test_handles_op_get_user_raising() -> None:
    """OP probe errors must not crash the back-fill — keep going."""
    user_mapping = {
        "alice": {
            "jira_name": "alice",
            "jira_email": "a@x",
            "matched_by": "none",
            "openproject_id": None,
        },
        "bob": {
            "jira_name": "bob",
            "jira_email": "b@x",
            "matched_by": "none",
            "openproject_id": None,
        },
    }
    op = MagicMock()
    op.get_user.side_effect = [
        Exception("boom"),
        Exception("boom"),
        {"id": 7, "login": "bob", "mail": "b@x"},
        Exception("boom"),
    ]
    n = _backfill_unmapped_users_from_op(user_mapping, op, MagicMock())
    assert n == 1
    assert user_mapping["bob"]["openproject_id"] == 7
    assert user_mapping["alice"]["openproject_id"] is None


def test_skips_entries_with_matched_by_other_than_none() -> None:
    """Only ``matched_by='none'`` rows get back-filled. Other reasons —
    even if the entry has ``openproject_id: None`` — are intentional
    (e.g. dropped) and not for back-fill.
    """
    user_mapping = {
        "x": {
            "jira_name": "x",
            "jira_email": "x@x",
            "matched_by": "explicitly_dropped",
            "openproject_id": None,
        },
    }
    op = MagicMock()
    op.get_user.return_value = {"id": 99, "login": "x"}
    n = _backfill_unmapped_users_from_op(user_mapping, op, MagicMock())
    assert n == 0
    op.get_user.assert_not_called()
