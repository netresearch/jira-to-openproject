"""Tests for :class:`src.models.jira.JiraProject`."""

from __future__ import annotations

from types import SimpleNamespace

from src.models.jira import JiraProject, JiraProjectCategoryRef


def test_from_dict_happy_path() -> None:
    raw = {
        "key": "ABC",
        "name": "Alpha Beta Charlie",
        "id": "10000",
        "description": "Demo project",
        "lead": "jdoe",
        "lead_display": "Jane Doe",
        "browse_url": "https://jira.example.com/browse/ABC",
        "archived": False,
        "project_type_key": "software",
        "project_category": {"id": "1", "name": "Internal"},
        "avatar_url": "https://avatars.example.com/abc-128.png",
    }
    project = JiraProject.from_dict(raw)

    assert project.key == "ABC"
    assert project.name == "Alpha Beta Charlie"
    assert project.id == "10000"
    assert project.description == "Demo project"
    assert project.lead == "jdoe"
    assert project.lead_display == "Jane Doe"
    assert project.browse_url == "https://jira.example.com/browse/ABC"
    assert project.archived is False
    assert project.project_type_key == "software"
    assert isinstance(project.project_category, JiraProjectCategoryRef)
    assert project.project_category.id == "1"
    assert project.project_category.name == "Internal"
    assert project.avatar_url == "https://avatars.example.com/abc-128.png"


def test_from_dict_empty_category_normalised_to_none() -> None:
    project = JiraProject.from_dict(
        {
            "key": "ABC",
            "name": "A",
            "id": "1",
            "project_category": {},
        },
    )
    assert project.project_category is None


def test_from_dict_missing_optional_fields_use_defaults() -> None:
    project = JiraProject.from_dict({"key": "ABC", "name": "A", "id": "1"})

    assert project.description == ""
    assert project.lead is None
    assert project.lead_display is None
    assert project.browse_url is None
    assert project.archived is False
    assert project.project_type_key is None
    assert project.project_category is None
    assert project.avatar_url is None


def test_from_dict_extra_fields_are_ignored() -> None:
    project = JiraProject.from_dict(
        {
            "key": "ABC",
            "name": "A",
            "id": "1",
            "url": "ignored",
            "avatar_urls": {"48x48": "ignored"},
            "project_category_name": "ignored",
            "project_category_id": "ignored",
        },
    )
    assert project.key == "ABC"
    assert "url" not in project.model_dump()
    assert "avatar_urls" not in project.model_dump()


def test_from_jira_obj_happy_path() -> None:
    obj = SimpleNamespace(
        id="10000",
        key="ABC",
        name="Alpha",
        projectTypeKey="software",
        raw={
            "description": "Demo project",
            "lead": {"name": "jdoe", "displayName": "Jane Doe"},
            "projectCategory": {"id": 1, "name": "Internal"},
            "avatarUrls": {
                "16x16": "https://avatars.example.com/abc-16.png",
                "48x48": "https://avatars.example.com/abc-48.png",
                "128x128": "https://avatars.example.com/abc-128.png",
            },
            "projectTypeKey": "software",
            "archived": False,
        },
    )
    project = JiraProject.from_jira_obj(obj, browse_url="https://jira.example.com/browse/ABC")

    assert project.key == "ABC"
    assert project.id == "10000"
    assert project.description == "Demo project"
    assert project.lead == "jdoe"
    assert project.lead_display == "Jane Doe"
    assert project.browse_url == "https://jira.example.com/browse/ABC"
    assert project.project_type_key == "software"
    assert project.project_category is not None
    assert project.project_category.id == "1"
    assert project.project_category.name == "Internal"
    # Largest available avatar URL is preferred.
    assert project.avatar_url == "https://avatars.example.com/abc-128.png"


def test_from_jira_obj_missing_raw_falls_back_to_empty_metadata() -> None:
    obj = SimpleNamespace(id="1", key="ABC", name="A")
    project = JiraProject.from_jira_obj(obj)

    assert project.key == "ABC"
    assert project.description == ""
    assert project.lead is None
    assert project.lead_display is None
    assert project.project_category is None
    assert project.avatar_url is None
    assert project.archived is False


def test_from_jira_obj_lead_falls_back_to_key_when_name_missing() -> None:
    obj = SimpleNamespace(
        id="1",
        key="ABC",
        name="A",
        raw={"lead": {"key": "fallback-key"}},
    )
    project = JiraProject.from_jira_obj(obj)
    assert project.lead == "fallback-key"
