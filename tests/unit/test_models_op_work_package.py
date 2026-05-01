"""Tests for :class:`src.models.openproject.OpWorkPackage`."""

from __future__ import annotations

from src.models.openproject import OpWorkPackage


def test_from_dict_happy_path() -> None:
    raw = {
        "id": 1234,
        "subject": "Hello",
        "description": "World",
        "type_id": 1,
        "status_id": 2,
        "priority_id": 3,
        "project_id": 7,
        "assigned_to_id": 42,
        "responsible_id": 43,
        "start_date": "2026-01-01",
        "due_date": "2026-01-31",
        "parent_id": 1233,
        "done_ratio": 50,
        "created_at": "2026-01-01T12:00:00Z",
        "updated_at": "2026-01-02T12:00:00Z",
    }
    wp = OpWorkPackage.from_dict(raw)

    assert wp.id == 1234
    assert wp.subject == "Hello"
    assert wp.description == "World"
    assert wp.type_id == 1
    assert wp.status_id == 2
    assert wp.priority_id == 3
    assert wp.project_id == 7
    assert wp.assigned_to_id == 42
    assert wp.responsible_id == 43
    assert wp.start_date == "2026-01-01"
    assert wp.due_date == "2026-01-31"
    assert wp.parent_id == 1233
    assert wp.done_ratio == 50
    assert wp.created_at == "2026-01-01T12:00:00Z"
    assert wp.updated_at == "2026-01-02T12:00:00Z"


def test_from_dict_missing_optional_fields_default_to_none() -> None:
    wp = OpWorkPackage.from_dict({})
    assert wp.id is None
    assert wp.subject is None
    assert wp.description is None
    assert wp.type_id is None
    assert wp.status_id is None
    assert wp.priority_id is None
    assert wp.project_id is None
    assert wp.assigned_to_id is None
    assert wp.responsible_id is None
    assert wp.start_date is None
    assert wp.due_date is None
    assert wp.parent_id is None
    assert wp.done_ratio is None
    assert wp.created_at is None
    assert wp.updated_at is None


def test_from_dict_extra_fields_are_ignored() -> None:
    wp = OpWorkPackage.from_dict(
        {
            "id": 1,
            "subject": "Hi",
            "_links": {"self": {"href": "/api/v3/work_packages/1"}},
            "lockVersion": 0,
            "author_id": 5,
        },
    )
    assert wp.id == 1
    assert wp.subject == "Hi"
    dump = wp.model_dump()
    assert "_links" not in dump
    assert "lockVersion" not in dump
    # ``author_id`` is intentionally not modelled in 3α — it's used by the
    # service layer but not by the migration's read/update payloads.
    assert "author_id" not in dump
