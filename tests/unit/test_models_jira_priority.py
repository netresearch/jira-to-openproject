"""Tests for :class:`src.models.jira.JiraPriority`."""

from __future__ import annotations

from types import SimpleNamespace

from src.models.jira import JiraPriority


def test_from_dict_happy_path() -> None:
    priority = JiraPriority.from_dict(
        {
            "id": "1",
            "name": "Critical",
            "status": "ACTIVE",
        },
    )

    assert priority.id == "1"
    assert priority.name == "Critical"
    assert priority.status == "ACTIVE"


def test_from_dict_missing_optional_fields_default_to_none() -> None:
    priority = JiraPriority.from_dict({"name": "Normal"})

    assert priority.name == "Normal"
    assert priority.id is None
    assert priority.status is None


def test_from_dict_extra_fields_are_ignored() -> None:
    priority = JiraPriority.from_dict(
        {
            "id": "2",
            "name": "High",
            "iconUrl": "https://jira.example.com/icons/high.png",
            "self": "https://jira.example.com/rest/api/2/priority/2",
        },
    )

    assert priority.id == "2"
    assert priority.name == "High"
    # Extras silently dropped.
    assert "iconUrl" not in priority.model_dump()
    assert "icon_url" not in priority.model_dump()


def test_from_jira_obj_happy_path() -> None:
    obj = SimpleNamespace(id="3", name="Medium", status="ACTIVE")

    priority = JiraPriority.from_jira_obj(obj)

    assert priority.id == "3"
    assert priority.name == "Medium"
    assert priority.status == "ACTIVE"


def test_from_jira_obj_coerces_int_id_to_str() -> None:
    """SDK Priority instances expose ``id`` as the string Jira returns,
    but a few server-side mocks expose it as ``int`` — coerce defensively.
    """
    obj = SimpleNamespace(id=4, name="Low")

    priority = JiraPriority.from_jira_obj(obj)

    assert priority.id == "4"
    assert priority.name == "Low"
    assert priority.status is None


def test_from_jira_obj_missing_attrs_default_to_none() -> None:
    priority = JiraPriority.from_jira_obj(SimpleNamespace())

    assert priority.id is None
    assert priority.name is None
    assert priority.status is None
