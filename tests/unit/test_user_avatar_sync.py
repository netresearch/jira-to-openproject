"""Tests for UserMigration avatar synchronization helpers."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.application.components.user_migration import UserMigration

pytestmark = pytest.mark.unit


def _logger_stub():
    return SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        success=lambda *args, **kwargs: None,
        notice=lambda *args, **kwargs: None,
    )


@pytest.fixture
def migration(tmp_path):
    instance = UserMigration.__new__(UserMigration)
    instance.logger = _logger_stub()
    instance.data_dir = tmp_path
    instance.avatar_cache_file = tmp_path / "user_avatar_cache.json"
    instance._avatar_cache = {}

    instance.op_client = MagicMock()
    instance.op_client.ensure_local_avatars_enabled = MagicMock()
    instance.op_client.transfer_file_to_container = MagicMock()
    instance.op_client.set_user_avatar.return_value = {"success": True}
    instance.op_client.docker_client = MagicMock()
    instance.op_client.docker_client.execute_command = MagicMock()

    instance.jira_client = MagicMock()
    instance.jira_client.download_user_avatar.return_value = (b"avatar-bytes", "image/png")

    return instance


def _job(jira_key: str, cache: dict[str, str] | None = None) -> dict[str, object]:
    return {
        "jira_key": jira_key,
        "openproject_id": 99,
        "avatar_url": "https://example.org/avatar.png",
        "cache": cache or {},
    }


def test_sync_user_avatars_uploads_and_caches(migration: UserMigration):
    result = migration._sync_user_avatars([_job("USER1")])

    assert result == {"uploaded": 1, "skipped": 0, "skip_reasons": {}}
    assert "USER1" in migration._avatar_cache
    assert migration.op_client.ensure_local_avatars_enabled.called
    migration.op_client.transfer_file_to_container.assert_called_once()
    migration.op_client.set_user_avatar.assert_called_once()
    migration.op_client.docker_client.execute_command.assert_called_once()

    # Cache file persisted
    cache_path = Path(migration.avatar_cache_file)
    assert cache_path.exists()
    cache_payload = json.loads(cache_path.read_text())
    assert cache_payload["USER1"]["url"] == "https://example.org/avatar.png"


def test_sync_user_avatars_skips_when_digest_matches(migration: UserMigration):
    from hashlib import sha256

    migration._avatar_cache = {
        "USER1": {
            "digest": sha256(b"avatar-bytes").hexdigest(),
            "url": "https://example.org/avatar.png",
        },
    }
    # Pre-populate cache file to ensure skip path still saves
    migration._save_avatar_cache()

    result = migration._sync_user_avatars(
        [_job("USER1", cache=migration._avatar_cache["USER1"])],
    )

    # Idempotent re-run: cached digest match. The ``skip_reasons``
    # breakdown lets an operator subtract this "expected" skip from
    # the aggregate when interpreting an audit run — it is NOT a
    # real loss.
    assert result == {
        "uploaded": 0,
        "skipped": 1,
        "skip_reasons": {"cached_digest_match": 1},
    }
    migration.op_client.transfer_file_to_container.assert_not_called()
    migration.op_client.set_user_avatar.assert_not_called()


def test_sync_user_avatars_set_avatar_raise_counts_once_not_twice(
    migration: UserMigration,
):
    """Pre-#190 double-count regression guard.

    The old code incremented ``skipped`` twice on a raised
    ``set_user_avatar`` call: once in the ``except`` block, then
    *again* in the post-finally ``else`` branch (because the except
    set ``result = {"success": False}``). That made the aggregate
    ``skipped`` overcount transient API failures by 2x. The
    ``api_raised`` guard in the refactored code counts each failure
    exactly once.
    """
    # Force the API call to raise — so the except path fires.
    migration.op_client.set_user_avatar.side_effect = RuntimeError("boom")

    result = migration._sync_user_avatars([_job("USER_RAISE")])

    # 1 failed avatar = 1 skip, NOT 2 (the pre-#190 bug).
    assert result["skipped"] == 1, result
    assert result["uploaded"] == 0
    # And only the "raised" bucket fires; the "returned_false"
    # bucket must stay empty so an operator can tell network/raise
    # failures from semantic-rejection failures.
    breakdown = result["skip_reasons"]
    assert breakdown.get("set_avatar_api_raised") == 1, breakdown
    assert breakdown.get("set_avatar_api_returned_false", 0) == 0, breakdown


def test_sync_user_avatars_skip_reasons_sum_to_aggregate(migration: UserMigration):
    """The breakdown's values must sum to the aggregate ``skipped``.

    Mirror of the relation/watcher sum-equality tests. Pins the
    invariant against future refactor regressions where a bare
    ``skipped += 1`` slips back in.
    """
    # Three jobs, each hitting a different bucket.
    migration.jira_client.download_user_avatar.side_effect = [
        None,  # download fail
        (b"x", "image/png"),  # success → uploaded
        (b"x", "image/png"),  # success → uploaded
    ]

    # Make the SECOND avatar's set_user_avatar raise; the third
    # returns success=False cleanly.
    migration.op_client.set_user_avatar.side_effect = [
        RuntimeError("transient"),
        {"success": False},
    ]

    jobs = [_job("U1"), _job("U2"), _job("U3")]
    result = migration._sync_user_avatars(jobs)

    breakdown = result["skip_reasons"]
    assert sum(breakdown.values()) == result["skipped"], (breakdown, result)
    # Three distinct buckets fired (each test case hit a different
    # failure mode):
    assert breakdown["download_failed"] == 1
    assert breakdown["set_avatar_api_raised"] == 1
    assert breakdown["set_avatar_api_returned_false"] == 1
