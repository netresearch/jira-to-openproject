"""Tests for :class:`src.models.openproject.OpUser`."""

from __future__ import annotations

from src.models.openproject import OpUser


def test_from_dict_happy_path() -> None:
    raw = {
        "id": 42,
        "login": "jdoe",
        "firstname": "Jane",
        "lastname": "Doe",
        "mail": "jane@example.com",
        "admin": True,
        "language": "en",
    }
    user = OpUser.from_dict(raw)
    assert user.id == 42
    assert user.login == "jdoe"
    assert user.firstname == "Jane"
    assert user.lastname == "Doe"
    assert user.mail == "jane@example.com"
    assert user.admin is True
    assert user.language == "en"


def test_from_dict_missing_optional_fields_default_to_none() -> None:
    user = OpUser.from_dict({"login": "jdoe"})
    assert user.login == "jdoe"
    assert user.id is None
    assert user.firstname is None
    assert user.lastname is None
    assert user.mail is None
    assert user.admin is False
    assert user.language is None


def test_from_dict_extra_fields_are_ignored() -> None:
    user = OpUser.from_dict(
        {
            "id": 1,
            "login": "jdoe",
            "j2o_origin_system": "jira",
            "j2o_user_id": "557058:abc",
            "time_zone": "Europe/Berlin",
        },
    )
    assert user.login == "jdoe"
    dump = user.model_dump()
    assert "j2o_origin_system" not in dump
    assert "time_zone" not in dump


def test_email_alias_accepted_as_input() -> None:
    """Older REST shape uses ``email`` rather than the canonical ``mail``."""
    via_alias = OpUser.from_dict({"login": "jdoe", "email": "jane@example.com"})
    via_canonical = OpUser.from_dict({"login": "jdoe", "mail": "jane@example.com"})
    assert via_alias.mail == "jane@example.com"
    assert via_canonical.mail == "jane@example.com"
