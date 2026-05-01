"""Pydantic v2 model for OpenProject custom field payloads.

The shape modelled here matches the ``CustomField.as_json`` dump produced
by :mod:`src.clients.openproject_custom_field_service` and the helpers in
:mod:`src.clients.openproject_work_package_custom_field_service`. Only the
fields the migration actually relies on are modelled — the Rails
``CustomField`` record has many more attributes that we don't need to type
yet.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.domain.ids import OpCustomFieldId


class OpCustomField(BaseModel):
    """Canonical representation of an OpenProject custom field."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: OpCustomFieldId | None = None
    name: str
    field_format: str
    """Rails field format token: ``"string"``, ``"text"``, ``"int"``, ``"float"``,
    ``"list"``, ``"user"``, ``"version"``, ``"date"``, ``"bool"``, etc."""

    is_required: bool = False
    is_filter: bool = False
    is_for_all: bool = False
    """``True`` activates the CF for every project automatically."""

    searchable: bool = False
    editable: bool = True
    visible: bool = True
    multi_value: bool = False

    default_value: str | None = None
    min_length: int | None = None
    max_length: int | None = None
    regexp: str | None = None

    possible_values: list[str] = Field(default_factory=list)
    """Enumerated values for ``list``-format CFs. Empty for other formats."""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> OpCustomField:
        """Build an :class:`OpCustomField` from a Rails/REST dict shape."""
        return cls.model_validate(raw)


__all__ = ["OpCustomField"]
