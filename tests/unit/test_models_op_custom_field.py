"""Tests for :class:`src.models.openproject.OpCustomField`."""

from __future__ import annotations

from src.models.openproject import OpCustomField


def test_from_dict_happy_path() -> None:
    raw = {
        "id": 99,
        "name": "Origin Key",
        "field_format": "string",
        "is_required": False,
        "is_filter": True,
        "is_for_all": True,
        "searchable": True,
        "editable": True,
        "visible": True,
        "multi_value": False,
        "default_value": None,
        "min_length": 0,
        "max_length": 255,
        "regexp": None,
        "possible_values": [],
    }
    cf = OpCustomField.from_dict(raw)
    assert cf.id == 99
    assert cf.name == "Origin Key"
    assert cf.field_format == "string"
    assert cf.is_required is False
    assert cf.is_filter is True
    assert cf.is_for_all is True
    assert cf.searchable is True
    assert cf.editable is True
    assert cf.visible is True
    assert cf.multi_value is False
    assert cf.default_value is None
    assert cf.min_length == 0
    assert cf.max_length == 255
    assert cf.regexp is None
    assert cf.possible_values == []


def test_from_dict_minimal_uses_documented_defaults() -> None:
    cf = OpCustomField.from_dict({"name": "X", "field_format": "list"})
    assert cf.id is None
    assert cf.is_required is False
    assert cf.is_filter is False
    assert cf.is_for_all is False
    assert cf.searchable is False
    # Editable/visible default to True — these match Rails' factory
    # defaults for new CFs.
    assert cf.editable is True
    assert cf.visible is True
    assert cf.multi_value is False
    assert cf.default_value is None
    assert cf.possible_values == []


def test_from_dict_with_enumerated_values() -> None:
    cf = OpCustomField.from_dict(
        {
            "name": "Severity",
            "field_format": "list",
            "possible_values": ["Low", "Medium", "High"],
            "multi_value": False,
        },
    )
    assert cf.field_format == "list"
    assert cf.possible_values == ["Low", "Medium", "High"]


def test_from_dict_extra_fields_are_ignored() -> None:
    cf = OpCustomField.from_dict(
        {
            "name": "X",
            "field_format": "string",
            "type": "WorkPackageCustomField",
            "position": 12,
            "created_at": "2026-01-01",
        },
    )
    assert cf.name == "X"
    dump = cf.model_dump()
    assert "type" not in dump
    assert "position" not in dump
    assert "created_at" not in dump
