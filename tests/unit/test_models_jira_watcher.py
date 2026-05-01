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
        {"name": "dave", "self": "https://j.example.com/rest/api/2/user", "key": "ignored-extra"},
    )
    assert watcher is not None
    assert watcher.name == "dave"
