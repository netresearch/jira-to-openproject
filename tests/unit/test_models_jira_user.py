"""Tests for :class:`src.models.jira.JiraUser`."""

from __future__ import annotations

from types import SimpleNamespace

from src.models.jira import JiraUser


def test_from_dict_happy_path() -> None:
    raw = {
        "key": "jdoe",
        "accountId": "557058:abc",
        "name": "jdoe",
        "displayName": "Jane Doe",
        "emailAddress": "jane@example.com",
        "active": True,
        "timeZone": "Europe/Berlin",
        "locale": "en_US",
        "self": "https://jira.example.com/rest/api/2/user?username=jdoe",
        "avatarUrls": {"48x48": "https://avatars.example.com/jdoe-48.png"},
    }
    user = JiraUser.from_dict(raw)

    assert user.key == "jdoe"
    assert user.account_id == "557058:abc"
    assert user.name == "jdoe"
    assert user.display_name == "Jane Doe"
    assert user.email_address == "jane@example.com"
    assert user.active is True
    assert user.time_zone == "Europe/Berlin"
    assert user.locale == "en_US"
    assert user.self_url == "https://jira.example.com/rest/api/2/user?username=jdoe"
    assert user.avatar_urls == {"48x48": "https://avatars.example.com/jdoe-48.png"}


def test_from_dict_missing_optional_fields_default_to_none() -> None:
    user = JiraUser.from_dict({"accountId": "557058:abc"})

    assert user.account_id == "557058:abc"
    assert user.key is None
    assert user.display_name is None
    assert user.email_address is None
    assert user.time_zone is None
    assert user.locale is None
    assert user.self_url is None
    assert user.avatar_urls is None
    # Default for missing ``active`` is True.
    assert user.active is True


def test_from_dict_extra_fields_are_ignored() -> None:
    user = JiraUser.from_dict(
        {
            "accountId": "557058:abc",
            "displayName": "Jane",
            "applicationRoles": {"size": 1, "items": []},
            "groups": {"size": 0, "items": []},
        },
    )
    assert user.account_id == "557058:abc"
    assert user.display_name == "Jane"
    # Extras silently dropped.
    assert "application_roles" not in user.model_dump()
    assert "applicationRoles" not in user.model_dump()


def test_alias_accepts_both_camel_and_snake() -> None:
    via_alias = JiraUser(accountId="X", displayName="Y")  # type: ignore[call-arg]
    via_field = JiraUser(account_id="X", display_name="Y")

    assert via_alias.account_id == via_field.account_id == "X"
    assert via_alias.display_name == via_field.display_name == "Y"


def test_from_jira_obj_happy_path() -> None:
    obj = SimpleNamespace(
        key="jdoe",
        accountId="557058:abc",
        name="jdoe",
        displayName="Jane Doe",
        emailAddress="jane@example.com",
        active=True,
        timeZone="Europe/Berlin",
        locale="en_US",
        self="https://jira.example.com/rest/api/2/user?username=jdoe",
        avatarUrls={"48x48": "https://avatars.example.com/jdoe-48.png"},
    )
    user = JiraUser.from_jira_obj(obj)

    assert user.key == "jdoe"
    assert user.account_id == "557058:abc"
    assert user.display_name == "Jane Doe"
    assert user.time_zone == "Europe/Berlin"
    assert user.avatar_urls == {"48x48": "https://avatars.example.com/jdoe-48.png"}


def test_from_jira_obj_falls_back_to_lowercase_timezone() -> None:
    """Some SDK versions expose ``timezone`` instead of ``timeZone``."""
    obj = SimpleNamespace(
        accountId="557058:abc",
        displayName="Jane",
        timezone="Europe/Berlin",  # lowercase
    )
    user = JiraUser.from_jira_obj(obj)
    assert user.time_zone == "Europe/Berlin"


def test_from_jira_obj_missing_attrs_default_to_none() -> None:
    obj = SimpleNamespace()
    user = JiraUser.from_jira_obj(obj)

    assert user.key is None
    assert user.account_id is None
    assert user.display_name is None
    assert user.email_address is None
    assert user.time_zone is None
    assert user.avatar_urls is None
    # ``active`` defaults to True when the attribute is missing.
    assert user.active is True


def test_from_jira_obj_avatar_urls_non_dictlike_falls_back_to_none() -> None:
    obj = SimpleNamespace(accountId="X", avatarUrls=12345)
    user = JiraUser.from_jira_obj(obj)
    assert user.avatar_urls is None


def test_from_jira_obj_avatar_urls_filters_falsy_values() -> None:
    obj = SimpleNamespace(
        accountId="X",
        avatarUrls={"48x48": "url", "24x24": "", "16x16": None},
    )
    user = JiraUser.from_jira_obj(obj)
    assert user.avatar_urls == {"48x48": "url"}
