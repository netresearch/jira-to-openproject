"""Tests for UserMigration avatar synchronization helpers."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.migrations.user_migration import UserMigration

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

    assert result == {"uploaded": 1, "skipped": 0}
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

    assert result == {"uploaded": 0, "skipped": 1}
    migration.op_client.transfer_file_to_container.assert_not_called()
    migration.op_client.set_user_avatar.assert_not_called()
