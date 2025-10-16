"""Unit tests for UserMigration metadata helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.migrations.user_migration import UserMigration

pytestmark = pytest.mark.unit


@pytest.fixture
def logger_stub():
    """Return a minimal logger stub with the attributes used in tests."""
    return SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
        success=lambda *args, **kwargs: None,
        notice=lambda *args, **kwargs: None,
    )


def test_get_user_origin_cf_ids_ensures_fields(logger_stub):
    """Verify legacy fields are removed and new origin CFs are created once."""
    instance = UserMigration.__new__(UserMigration)
    instance.logger = logger_stub
    instance.op_client = MagicMock()
    instance._origin_cf_id_map = None

    removal_results = [
        {"removed": 1},
        {"removed": 0},
    ]
    instance.op_client.remove_custom_field.side_effect = removal_results

    ensured = [
        {"id": 11},
        {"id": 12},
        {"id": 13},
        {"id": 14},
    ]
    instance.op_client.ensure_custom_field.side_effect = ensured

    ids = instance._get_user_origin_cf_ids()

    assert ids == {
        "J2O Origin System": 11,
        "J2O User ID": 12,
        "J2O User Key": 13,
        "J2O External URL": 14,
    }
    assert instance.op_client.remove_custom_field.call_count == 2
    assert instance.op_client.ensure_custom_field.call_count == 4

    # Second call should use the cached value without calling the client again
    instance.op_client.ensure_custom_field.reset_mock()
    cached = instance._get_user_origin_cf_ids()
    assert cached is ids
    instance.op_client.ensure_custom_field.assert_not_called()


@pytest.mark.parametrize(
    ("account_id", "expected_suffix"),
    [
        ("abc-123", "accountId=abc-123"),
        (None, "name=user.name"),
    ],
)
def test_build_user_origin_metadata(logger_stub, account_id, expected_suffix, monkeypatch):
    """Ensure metadata assembly derives IDs, URLs, and avatar correctly."""
    instance = UserMigration.__new__(UserMigration)
    instance.logger = logger_stub
    instance.data_dir = Path(".")
    instance._origin_system_label_cache = "Jira Data Center 9.1"
    instance._jira_base_url_cache = "https://jira.example"
    instance._ensure_jira_user_details = lambda *args, **kwargs: None

    jira_user = {
        "accountId": account_id,
        "key": "USERKEY",
        "name": "user.name",
        "displayName": "User Example",
        "timeZone": "Europe/Berlin",
        "locale": "de_DE",
        "avatarUrls": {
            "48x48": "https://jira.example/avatars/48",
            "32x32": "https://jira.example/avatars/32",
        },
    }

    meta = instance._build_user_origin_metadata(jira_user)

    assert meta["origin_system"] == "Jira Data Center 9.1"
    assert meta["user_key"] == "USERKEY"
    assert meta["user_id"]
    assert meta["time_zone"] == "Europe/Berlin"
    assert meta["locale"] == "de_DE"
    assert meta["avatar_url"] == "https://jira.example/avatars/48"
    assert meta["external_url"].startswith("https://jira.example/secure/ViewProfile.jspa")
    assert expected_suffix in meta["external_url"]


def test_map_locale_to_language_prefers_supported(logger_stub):
    instance = UserMigration.__new__(UserMigration)
    instance.logger = logger_stub
    instance._supported_languages = {"en", "en_us", "de"}

    assert instance._map_locale_to_language("en-US") == "en_us"
    assert instance._map_locale_to_language("de") == "de"
    assert instance._map_locale_to_language("fr_FR") == ""


def test_prepare_avatar_job_uses_cache(logger_stub):
    instance = UserMigration.__new__(UserMigration)
    instance.logger = logger_stub
    instance._avatar_cache = {"key1": {"digest": "abc"}}

    job = instance._prepare_avatar_job(
        jira_user={},
        op_user=None,
        mapping={"openproject_id": 42, "jira_key": "key1"},
        meta={"avatar_url": "https://example/avatar.png", "user_key": "key1"},
    )

    assert job["jira_key"] == "key1"
    assert job["openproject_id"] == 42
    assert job["cache"] == {"digest": "abc"}

    # Missing avatar URL prevents job creation
    assert (
        instance._prepare_avatar_job(
            jira_user={},
            op_user=None,
            mapping={"openproject_id": 42, "jira_key": "key1"},
            meta={"avatar_url": "", "user_key": "key1"},
        )
        is None
    )
