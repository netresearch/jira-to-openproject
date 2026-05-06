"""Unit tests for UserMigration metadata helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.application.components.user_migration import UserMigration

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


# --- User discovery from cached issues ---------------------------------------
# Per the live TEST audit (2026-05-06): the directory ``get_users()``
# call misses users who appear as watchers/assignees/reporters/
# comment authors but aren't in the Jira user directory (inactive,
# disabled, never-logged-in). PR #195 surfaced the distinct
# unmapped users; this PR's discovery method adds them to
# ``self.jira_users`` so they get mapped + created BEFORE
# watcher_migration tries to resolve them.


def test_discover_users_from_cached_issues_harvests_all_sources(logger_stub, tmp_path):
    """Harvest watchers + assignee + reporter + comment authors, deduped by id."""
    import json as _json

    instance = UserMigration.__new__(UserMigration)
    instance.logger = logger_stub
    instance.data_dir = tmp_path
    cache_file = tmp_path / "jira_issues_cache.json"
    cache_file.write_text(
        _json.dumps(
            {
                "PROJ-1": {
                    "fields": {
                        "watches": {
                            "watchers": [
                                {"name": "alice", "accountId": "acc-alice"},
                                {"name": "bob", "accountId": "acc-bob"},
                            ],
                        },
                        "assignee": {"name": "carol", "accountId": "acc-carol"},
                        "reporter": {"name": "dave", "accountId": "acc-dave"},
                        "comment": {
                            "comments": [
                                {"author": {"name": "eve", "accountId": "acc-eve"}},
                            ],
                        },
                    },
                },
                "PROJ-2": {
                    "fields": {
                        # Same alice — must dedup, not double-count.
                        "watches": {"watchers": [{"name": "alice", "accountId": "acc-alice"}]},
                    },
                },
            },
        ),
    )

    discovered = instance._discover_users_from_cached_issues()
    names = sorted(u["name"] for u in discovered)
    assert names == ["alice", "bob", "carol", "dave", "eve"], discovered


def test_discover_users_handles_missing_cache(logger_stub, tmp_path):
    """No cache file → empty list, no exception."""
    instance = UserMigration.__new__(UserMigration)
    instance.logger = logger_stub
    instance.data_dir = tmp_path

    assert instance._discover_users_from_cached_issues() == []


def test_discover_users_handles_malformed_cache(logger_stub, tmp_path):
    """Garbage JSON → empty list + warning, NOT a raised exception.

    Pin: discovery is best-effort. The directory pass remains the
    fallback, never block migration.
    """
    cache_file = tmp_path / "jira_issues_cache.json"
    cache_file.write_text("not valid json {{{")

    instance = UserMigration.__new__(UserMigration)
    instance.logger = logger_stub
    instance.data_dir = tmp_path

    assert instance._discover_users_from_cached_issues() == []


def test_stable_user_id_prefers_account_id_over_name():
    """Dedup key uses ``accountId`` first, then ``name``, lowercase."""
    assert UserMigration._stable_user_id({"accountId": "ACC-1", "name": "alice"}) == "acc-1"
    assert UserMigration._stable_user_id({"name": "ALICE"}) == "alice"
    assert UserMigration._stable_user_id({}) == ""


def test_discover_users_top_level_list_not_dict_returns_empty(logger_stub, tmp_path):
    """If the cache is a JSON list (not a dict), discovery returns ``[]``.

    Pin the structural assumption — the existing reader walks
    ``issues.values()`` which would fail on a list. The defensive
    ``isinstance(issues, dict)`` guard catches it and returns an
    empty list with a warning, NOT a TypeError.
    """
    import json as _json

    cache_file = tmp_path / "jira_issues_cache.json"
    cache_file.write_text(_json.dumps([{"key": "PROJ-1"}]))  # list, not dict

    instance = UserMigration.__new__(UserMigration)
    instance.logger = logger_stub
    instance.data_dir = tmp_path

    assert instance._discover_users_from_cached_issues() == []


def test_extract_jira_users_synthesizes_key_for_discovered_users(
    logger_stub,
    tmp_path,
):
    """Discovered users must have a ``key`` field after the merge.

    Caught by the Copilot review of PR #196:
    ``create_user_mapping`` line 497 SKIPS any Jira user without
    ``key``. Watcher responses carry ``accountId`` / ``name`` but
    NOT ``key``, so without a fallback the discovered users
    would map nowhere — making the entire discovery pass inert.
    """
    import json as _json

    instance = UserMigration.__new__(UserMigration)
    instance.logger = logger_stub
    instance.data_dir = tmp_path
    instance.jira_users = []
    instance.jira_client = MagicMock()
    instance.jira_client.get_users.return_value = [
        {"key": "directory-user", "name": "directory-user"},
    ]
    instance._save_to_json = lambda *args, **kwargs: None

    cache_file = tmp_path / "jira_issues_cache.json"
    cache_file.write_text(
        _json.dumps(
            {
                "PROJ-1": {
                    "fields": {
                        "watches": {
                            "watchers": [
                                {"accountId": "557058:abc", "displayName": "Cloud User"},
                                {"name": "server-user"},
                            ],
                        },
                    },
                },
            },
        ),
    )

    result = instance.extract_jira_users()

    # Every user (directory + discovered) has a truthy ``key``.
    for user in result:
        assert user.get("key"), f"User missing key after discovery: {user}"

    # Synthesized keys match the stable identity (accountId
    # preferred over name).
    keys_by_name = {u.get("name") or u.get("displayName"): u["key"] for u in result}
    assert keys_by_name["directory-user"] == "directory-user"
    assert keys_by_name["Cloud User"] == "557058:abc"
    assert keys_by_name["server-user"] == "server-user"


def test_extract_jira_users_runs_discovery_on_cache_hit(logger_stub, tmp_path):
    """Discovery runs EVEN when ``self.jira_users`` is already populated.

    Caught by the Copilot review of PR #196: the original
    arrangement returned early on cache-hit and never invoked
    discovery. On every re-run (common in production), the
    fix was inert.
    """
    import json as _json

    instance = UserMigration.__new__(UserMigration)
    instance.logger = logger_stub
    instance.data_dir = tmp_path
    # Pre-populated (mimics re-run with cached users in memory).
    instance.jira_users = [{"key": "existing", "name": "existing"}]
    instance.jira_client = MagicMock()
    instance._save_to_json = lambda *args, **kwargs: None

    cache_file = tmp_path / "jira_issues_cache.json"
    cache_file.write_text(
        _json.dumps(
            {
                "PROJ-1": {
                    "fields": {
                        "watches": {"watchers": [{"accountId": "discovered-user"}]},
                    },
                },
            },
        ),
    )

    result = instance.extract_jira_users()

    # Directory call skipped (cache hit) ...
    instance.jira_client.get_users.assert_not_called()
    # ... but discovery ran and added the watcher.
    keys = {u["key"] for u in result}
    assert "existing" in keys
    assert "discovered-user" in keys, f"Discovery did not run on cache-hit: {result}"


def test_discover_users_drops_dicts_without_account_or_name(logger_stub, tmp_path):
    """User dicts with only ``displayName`` / ``emailAddress`` are dropped.

    Documents the trade-off: ``watcher_migration._resolve_user_id``
    probes ``accountId`` then ``name`` first, so a user dict with
    only ``displayName`` couldn't be mapped downstream anyway —
    keeping it in ``self.jira_users`` would just create a
    placeholder OP user with no usable identity. The drop is
    correct policy, but PR #196's hardening logs the count so a
    future audit can quantify the residual loss.
    """
    import json as _json

    cache_file = tmp_path / "jira_issues_cache.json"
    cache_file.write_text(
        _json.dumps(
            {
                "PROJ-1": {
                    "fields": {
                        "watches": {
                            "watchers": [
                                {"accountId": "real", "name": "real-user"},
                                # Anonymized — no accountId, no name.
                                {"displayName": "Former user"},
                                # Email-only system actor.
                                {"emailAddress": "sys@example.org"},
                            ],
                        },
                    },
                },
            },
        ),
    )

    instance = UserMigration.__new__(UserMigration)
    instance.logger = logger_stub
    instance.data_dir = tmp_path

    discovered = instance._discover_users_from_cached_issues()
    # Only the user with a usable identity survives.
    assert len(discovered) == 1
    assert discovered[0]["accountId"] == "real"
