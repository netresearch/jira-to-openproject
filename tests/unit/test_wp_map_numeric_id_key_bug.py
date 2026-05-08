"""Regression tests for the wp_map numeric-ID-as-key bug.

Production work_package mappings are stored with the numeric Jira *ID* as the
outer dict key and the human-readable Jira *key* (e.g. "TEST-123") inside the
value dict:

    {
        "144952": {"jira_key": "TEST-1", "openproject_id": 10},
        "144953": {"jira_key": "TEST-2", "openproject_id": 11},
    }

Before the fix, migrations that iterated ``[str(k) for k in wp_map]`` would
pass numeric strings ("144952") to ``_merge_batch_issues`` → ``batch_get_issues``,
which builds ``key in ("144952")`` JQL.  Jira rejects that with HTTP 400:
"The issue key '144952' for field 'key' is invalid."

Each test below:
1. Mounts a production-format wp_map (numeric outer keys).
2. Verifies that ``batch_get_issues`` is called with human-readable keys only
   (e.g. "TEST-1"), never with bare numeric strings.
3. Asserts that the keys present in the extracted result data (e.g.
   ``result.data["votes"]``) are the human-readable Jira keys so that
   downstream ``_load`` and ``_map`` phases can correlate back to wp_map
   via the ``jira_key`` field rather than the numeric outer key.
"""

from __future__ import annotations

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
    """Returns minimal issue stubs for batch_get_issues calls."""

    def __init__(self) -> None:
        self._issues = {
            "TEST-1": _make_issue("TEST-1"),
            "TEST-2": _make_issue("TEST-2"),
            "TEST-3": _make_issue("TEST-3"),
        }

    def batch_get_issues(self, keys: list[str]) -> dict:
        result = {}
        for k in keys:
            issue = self._issues.get(k)
            if issue is not None:
                result[k] = issue
        return result

    def get_priorities(self):
        return []


class _SimpleRef:
    """Minimal Jira ref stub with a real ``name`` attribute (not a MagicMock name)."""

    def __init__(self, name: str, ref_id: str = "1") -> None:
        self.name = name
        self.id = ref_id


class _VotesRef:
    def __init__(self, count: int) -> None:
        self.votes = count


def _make_issue(key: str):
    """Build a minimal Jira issue stub whose fields survive JiraIssueFields.from_issue_any.

    Uses plain objects for resolution/security/priority/votes so that
    _str_attr can return real strings (not None from MagicMock internals).
    """
    issue = MagicMock()
    issue.key = key
    issue.id = key  # simplified
    fields = MagicMock()
    # Use real string-attribute objects so _jira_ref produces non-None names
    fields.votes = _VotesRef(3)
    fields.resolution = _SimpleRef("Fixed")
    fields.security = _SimpleRef("Internal")
    fields.labels = ["tag-a"]
    fields.priority = _SimpleRef("High")
    fields.fixVersions = []
    fields.versions = []
    fields.components = []
    fields.customfield_10016 = None  # story points
    fields.customfield_10020 = None  # sprint
    fields.customfield_10014 = None  # epic link
    fields.remoteLinks = []
    issue.fields = fields
    return issue


# ── VotesMigration ──────────────────────────────────────────────────────────


def test_votes_migration_extract_uses_jira_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """VotesMigration._extract must pass human-readable keys and produce usable result data.

    Verifies:
    - batch_get_issues is called with human-readable keys (not numeric IDs).
    - The extracted ``votes`` dict is keyed by human-readable Jira keys, enabling
      downstream _load to correlate entries back to wp_map via the jira_key field.
    """
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
    # Verify the extracted data keys are human-readable so _load can look them
    # up in wp_map via the jira_key field.
    votes: dict = (result.data or {}).get("votes", {})
    assert votes, "No votes data extracted"
    for k in votes:
        assert not str(k).isdigit(), (
            f"Extracted votes dict has numeric key {k!r} — _load would fail to correlate it back to wp_map entries."
        )


# ── ResolutionMigration ─────────────────────────────────────────────────────


def test_resolution_migration_extract_uses_jira_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """ResolutionMigration._extract must pass human-readable keys and produce usable result data."""
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
    resolution: dict = (result.data or {}).get("resolution", {})
    assert resolution, "No resolution data extracted"
    for k in resolution:
        assert not str(k).isdigit(), (
            f"Extracted resolution dict has numeric key {k!r} — _load would fail to "
            "correlate it back to wp_map entries."
        )


# ── LabelsMigration ─────────────────────────────────────────────────────────


def test_labels_migration_extract_uses_jira_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """LabelsMigration._extract must pass human-readable keys and produce usable result data."""
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
    labels: dict = (result.data or {}).get("labels", {})
    assert labels, "No labels data extracted"
    for k in labels:
        assert not str(k).isdigit(), (
            f"Extracted labels dict has numeric key {k!r} — _load would fail to correlate it back to wp_map entries."
        )


# ── NativeTagsMigration ─────────────────────────────────────────────────────


def test_native_tags_migration_extract_uses_jira_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """NativeTagsMigration._extract must pass human-readable keys and produce usable result data."""
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
    by_key: dict = (result.data or {}).get("by_key", {})
    assert by_key, "No by_key data extracted"
    for k in by_key:
        assert not str(k).isdigit(), (
            f"Extracted by_key dict has numeric key {k!r} — _map would fail to correlate it back to wp_map entries."
        )


# ── SecurityLevelsMigration ─────────────────────────────────────────────────


def test_security_levels_migration_extract_uses_jira_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """SecurityLevelsMigration._extract must pass human-readable keys and produce usable result data."""
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
    security: dict = (result.data or {}).get("security", {})
    assert security, "No security data extracted"
    for k in security:
        assert not str(k).isdigit(), (
            f"Extracted security dict has numeric key {k!r} — _load would fail to correlate it back to wp_map entries."
        )
