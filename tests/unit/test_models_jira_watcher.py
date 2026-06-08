"""Tests for :class:`src.models.jira.JiraWatcher`."""

from __future__ import annotations

from types import SimpleNamespace

from src.models.jira import JiraWatcher


def test_from_any_with_dict_payload() -> None:
    raw = {
        "name": "alice",
        "accountId": "557058:abc",
        "displayName": "Alice Doe",
        "emailAddress": "alice@example.com",
        "active": True,
    }
    watcher = JiraWatcher.from_any(raw)
    assert watcher is not None
    assert watcher.name == "alice"
    assert watcher.account_id == "557058:abc"
    assert watcher.display_name == "Alice Doe"
    assert watcher.email_address == "alice@example.com"
    assert watcher.active is True


def test_from_any_with_sdk_like_object() -> None:
    obj = SimpleNamespace(
        name="bob",
        accountId="557058:xyz",
        displayName="Bob Roe",
        emailAddress="bob@example.com",
        active=False,
    )
    watcher = JiraWatcher.from_any(obj)
    assert watcher is not None
    assert watcher.name == "bob"
    assert watcher.account_id == "557058:xyz"
    assert watcher.display_name == "Bob Roe"
    assert watcher.email_address == "bob@example.com"
    assert watcher.active is False


def test_from_any_returns_none_for_none_input() -> None:
    assert JiraWatcher.from_any(None) is None


def test_from_any_returns_none_when_no_identifier() -> None:
    # Empty dict — no usable identifier means we skip silently.
    assert JiraWatcher.from_any({}) is None
    assert JiraWatcher.from_any({"active": True}) is None


def test_from_any_active_defaults_to_true_when_missing() -> None:
    watcher = JiraWatcher.from_any({"name": "carol"})
    assert watcher is not None
    assert watcher.active is True


def test_from_any_extra_keys_are_ignored() -> None:
    watcher = JiraWatcher.from_any(
        {"name": "dave", "self": "https://j.example.com/rest/api/2/user", "timeZone": "UTC"},
    )
    assert watcher is not None
    assert watcher.name == "dave"


def test_from_any_captures_server_dc_key_from_dict() -> None:
    """Jira Server/DC watcher rows carry a ``key`` (e.g. JIRAUSER18400) that
    differs from ``name`` for renamed/inactive accounts. It must be captured
    so the watcher can be resolved against a key-keyed user mapping (#260).
    """
    watcher = JiraWatcher.from_any({"key": "JIRAUSER18400", "name": "anne.geissler"})
    assert watcher is not None
    assert watcher.key == "JIRAUSER18400"
    assert watcher.name == "anne.geissler"


def test_from_any_captures_key_from_sdk_like_object() -> None:
    obj = SimpleNamespace(key="JIRAUSER18400", name="anne.geissler")
    watcher = JiraWatcher.from_any(obj)
    assert watcher is not None
    assert watcher.key == "JIRAUSER18400"


def test_from_any_resolves_when_only_key_present() -> None:
    """A row with only ``key`` is a usable identifier, not skipped."""
    watcher = JiraWatcher.from_any({"key": "JIRAUSER18400"})
    assert watcher is not None
    assert watcher.key == "JIRAUSER18400"
