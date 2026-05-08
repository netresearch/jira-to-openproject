"""Regression tests for the wp_map numeric-ID-as-key bug.

Production work_package mappings are stored with the numeric Jira *ID* as the
outer dict key and the human-readable Jira *key* (e.g. "TEST-123") inside the
value dict:

    {
        "144952": {"jira_key": "TEST-1", "openproject_id": 10},
        "144953": {"jira_key": "TEST-2", "openproject_id": 11},
    }

Before the fix, migrations that iterated ``[str(k) for k in wp_map]`` would
pass numeric strings ("144952") to ``_merge_batch_issues`` → ``batch_get_issues``
→ ``_fetch_issues_batch``, which builds ``key in ("144952")`` JQL. Jira
rejects that with HTTP 400: "The issue key '144952' for field 'key' is
invalid." — because Jira issue *keys* look like "TEST-123", not bare numbers.

Each test below:
1. Mounts a production-format wp_map (numeric outer keys).
2. Captures what JQL string ``search_issues`` is called with.
3. Asserts the JQL never contains a bare numeric value after ``key in (``
   — i.e. only real keys like "TEST-1" appear.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest

import src.config as cfg

# ── shared mapping fixture ──────────────────────────────────────────────────

NUMERIC_ID_WP_MAP = {
    # outer key = str(jira_issue.id)  (numeric Jira ID)
    # value     = dict with jira_key + openproject_id
    "144952": {"jira_key": "TEST-1", "openproject_id": 10},
    "144953": {"jira_key": "TEST-2", "openproject_id": 11},
    "144954": {"jira_key": "TEST-3", "openproject_id": 12},
}


class _DummyMappings:
    def __init__(self, wp_map: dict) -> None:
        self._m = {"work_package": wp_map}

    def get_mapping(self, name: str):
        return self._m.get(name, {})

    def set_mapping(self, name: str, mapping):
        self._m[name] = mapping


# ── helper to assert no numeric IDs sneak into JQL ─────────────────────────

_NUMERIC_ID_PATTERN = re.compile(r"key\s+in\s+\(([^)]+)\)", re.IGNORECASE)


def _assert_no_numeric_keys_in_jql(jql: str) -> None:
    """Assert that no bare numeric value appears in the ``key in (...)`` clause."""
    match = _NUMERIC_ID_PATTERN.search(jql)
    if not match:
        return  # no ``key in (...)`` clause at all — acceptable
    clause = match.group(1)
    for token in clause.split(","):
        token = token.strip().strip('"').strip("'")
        assert not token.isdigit(), (
            f"Numeric ID {token!r} found in JQL 'key in (...)' clause — "
            f"this will cause HTTP 400 from Jira. Full JQL: {jql!r}"
        )


# ── DummyOp used by most migration tests ───────────────────────────────────


class _DummyOp:
    def get_custom_field_by_name(self, name: str):
        raise Exception("not found")

    def execute_query(self, script: str):
        return True

    def bulk_set_wp_custom_field_values(self, values):
        return {"updated": len(values), "failed": 0}

    def ensure_wp_custom_field_id(self, name: str, field_format: str = "text") -> int:
        return 999

    def enable_custom_field_for_projects(self, cf_id, project_ids, cf_name=None):
        return None


# ── DummyJira: records JQL calls and returns stub issues ───────────────────


class _DummyJira:
    """Captures JQL strings passed to search_issues and returns minimal stubs."""

    def __init__(self) -> None:
        self.jql_calls: list[str] = []
        self._issues = {
            "TEST-1": _make_issue("TEST-1"),
            "TEST-2": _make_issue("TEST-2"),
            "TEST-3": _make_issue("TEST-3"),
        }

    def search_issues(self, jql: str, maxResults: int = 50, expand: str = ""):
        self.jql_calls.append(jql)
        # Check the JQL isn't using numeric IDs as keys
        _assert_no_numeric_keys_in_jql(jql)
        # Return issues whose keys appear in the JQL
        return [v for k, v in self._issues.items() if k in jql]

    def batch_get_issues(self, keys: list[str]) -> dict:
        result = {}
        for k in keys:
            issue = self._issues.get(k)
            if issue is not None:
                result[k] = issue
        return result

    def get_priorities(self):
        return []


def _make_issue(key: str):
    """Build a minimal Jira issue stub."""
    issue = MagicMock()
    issue.key = key
    issue.id = key  # simplified
    fields = MagicMock()
    fields.votes = MagicMock(votes=3)
    fields.resolution = MagicMock(name="Fixed")
    fields.labels = ["tag-a"]
    fields.priority = MagicMock(name="High")
    fields.fixVersions = []
    fields.components = []
    fields.customfield_10016 = None  # story points
    fields.customfield_10020 = None  # sprint
    fields.customfield_10014 = None  # epic link
    fields.remoteLinks = []
    issue.fields = fields
    return issue


# ── VotesMigration ──────────────────────────────────────────────────────────


def test_votes_migration_extract_uses_jira_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """VotesMigration._extract must NOT pass numeric IDs to batch_get_issues."""
    from src.application.components.votes_migration import VotesMigration

    monkeypatch.setattr(cfg, "mappings", _DummyMappings(NUMERIC_ID_WP_MAP), raising=False)

    captured_keys: list[list[str]] = []

    class CapturingJira(_DummyJira):
        def batch_get_issues(self, keys):
            captured_keys.append(list(keys))
            for k in keys:
                assert not k.isdigit(), (
                    f"Numeric Jira ID {k!r} passed to batch_get_issues — "
                    "should be a key like 'TEST-1', not a bare number."
                )
            return super().batch_get_issues(keys)

    mig = VotesMigration(jira_client=CapturingJira(), op_client=_DummyOp())  # type: ignore[arg-type]
    result = mig._extract()
    assert result.success
    assert captured_keys, "batch_get_issues was never called"
    all_keys = [k for batch in captured_keys for k in batch]
    assert all_keys, "No keys were fetched"
    for k in all_keys:
        assert not k.isdigit(), f"Numeric ID {k!r} slipped through to batch_get_issues"


# ── ResolutionMigration ─────────────────────────────────────────────────────


def test_resolution_migration_extract_uses_jira_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """ResolutionMigration._extract must NOT pass numeric IDs to batch_get_issues."""
    from src.application.components.resolution_migration import ResolutionMigration

    monkeypatch.setattr(cfg, "mappings", _DummyMappings(NUMERIC_ID_WP_MAP), raising=False)

    captured_keys: list[list[str]] = []

    class CapturingJira(_DummyJira):
        def batch_get_issues(self, keys):
            captured_keys.append(list(keys))
            for k in keys:
                assert not k.isdigit(), f"Numeric Jira ID {k!r} passed to batch_get_issues"
            return super().batch_get_issues(keys)

    mig = ResolutionMigration(jira_client=CapturingJira(), op_client=_DummyOp())  # type: ignore[arg-type]
    result = mig._extract()
    assert result.success
    assert captured_keys, "batch_get_issues was never called"
    for k in [k for batch in captured_keys for k in batch]:
        assert not k.isdigit(), f"Numeric ID {k!r} passed to batch_get_issues"


# ── LabelsMigration ─────────────────────────────────────────────────────────


def test_labels_migration_extract_uses_jira_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """LabelsMigration._extract must NOT pass numeric IDs to batch_get_issues."""
    from src.application.components.labels_migration import LabelsMigration

    monkeypatch.setattr(cfg, "mappings", _DummyMappings(NUMERIC_ID_WP_MAP), raising=False)

    captured_keys: list[list[str]] = []

    class CapturingJira(_DummyJira):
        def batch_get_issues(self, keys):
            captured_keys.append(list(keys))
            for k in keys:
                assert not k.isdigit(), f"Numeric Jira ID {k!r} passed to batch_get_issues"
            return super().batch_get_issues(keys)

    mig = LabelsMigration(jira_client=CapturingJira(), op_client=_DummyOp())  # type: ignore[arg-type]
    result = mig._extract()
    assert result.success
    assert captured_keys, "batch_get_issues was never called"
    for k in [k for batch in captured_keys for k in batch]:
        assert not k.isdigit(), f"Numeric ID {k!r} passed to batch_get_issues"


# ── NativeTagsMigration ─────────────────────────────────────────────────────


def test_native_tags_migration_extract_uses_jira_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """NativeTagsMigration._extract must NOT pass numeric IDs to batch_get_issues."""
    from src.application.components.native_tags_migration import NativeTagsMigration

    monkeypatch.setattr(cfg, "mappings", _DummyMappings(NUMERIC_ID_WP_MAP), raising=False)

    captured_keys: list[list[str]] = []

    class CapturingJira(_DummyJira):
        def batch_get_issues(self, keys):
            captured_keys.append(list(keys))
            for k in keys:
                assert not k.isdigit(), f"Numeric Jira ID {k!r} passed to batch_get_issues"
            return super().batch_get_issues(keys)

    mig = NativeTagsMigration(jira_client=CapturingJira(), op_client=_DummyOp())  # type: ignore[arg-type]
    result = mig._extract()
    assert result.success
    assert captured_keys, "batch_get_issues was never called"
    for k in [k for batch in captured_keys for k in batch]:
        assert not k.isdigit(), f"Numeric ID {k!r} passed to batch_get_issues"


# ── SecurityLevelsMigration ─────────────────────────────────────────────────


def test_security_levels_migration_extract_uses_jira_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """SecurityLevelsMigration._extract must NOT pass numeric IDs to batch_get_issues."""
    from src.application.components.security_levels_migration import SecurityLevelsMigration

    monkeypatch.setattr(cfg, "mappings", _DummyMappings(NUMERIC_ID_WP_MAP), raising=False)

    captured_keys: list[list[str]] = []

    class CapturingJira(_DummyJira):
        def batch_get_issues(self, keys):
            captured_keys.append(list(keys))
            for k in keys:
                assert not k.isdigit(), f"Numeric Jira ID {k!r} passed to batch_get_issues"
            return super().batch_get_issues(keys)

    mig = SecurityLevelsMigration(jira_client=CapturingJira(), op_client=_DummyOp())  # type: ignore[arg-type]
    result = mig._extract()
    assert result.success
    assert captured_keys, "batch_get_issues was never called"
    for k in [k for batch in captured_keys for k in batch]:
        assert not k.isdigit(), f"Numeric ID {k!r} passed to batch_get_issues"
