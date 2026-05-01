"""Tests for :class:`src.models.openproject.OpProject`."""

from __future__ import annotations

from src.models.openproject import OpProject


def test_from_dict_happy_path() -> None:
    raw = {
        "id": 7,
        "name": "Demo",
        "identifier": "demo",
        "description": "Demo project",
        "parent_id": None,
        "public": False,
        "status": "on_track",
        "enabled_module_names": ["work_package_tracking", "wiki"],
    }
    project = OpProject.from_dict(raw)
    assert project.id == 7
    assert project.name == "Demo"
    assert project.identifier == "demo"
    assert project.description == "Demo project"
    assert project.parent_id is None
    assert project.public is False
    assert project.status == "on_track"
    assert project.enabled_module_names == ["work_package_tracking", "wiki"]


def test_from_dict_accepts_enabled_modules_alias() -> None:
    project = OpProject.from_dict(
        {
            "name": "Demo",
            "identifier": "demo",
            "enabled_modules": ["work_package_tracking"],
        },
    )
    assert project.enabled_module_names == ["work_package_tracking"]


def test_from_dict_missing_optional_fields_use_defaults() -> None:
    project = OpProject.from_dict({"name": "Demo", "identifier": "demo"})
    assert project.id is None
    assert project.description == ""
    assert project.parent_id is None
    assert project.public is False
    assert project.status is None
    assert project.enabled_module_names == []


def test_from_dict_extra_fields_are_ignored() -> None:
    project = OpProject.from_dict(
        {
            "name": "Demo",
            "identifier": "demo",
            "active": True,
            "status_code": 1,
            "j2o_origin_system": "jira",
        },
    )
    assert project.identifier == "demo"
    dump = project.model_dump()
    assert "active" not in dump
    assert "status_code" not in dump
    assert "j2o_origin_system" not in dump
