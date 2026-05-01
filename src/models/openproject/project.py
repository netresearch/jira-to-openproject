"""Pydantic v2 model for OpenProject project payloads.

The shape modelled here matches the dict produced by
:mod:`src.infrastructure.openproject.openproject_project_service` (``get_projects`` and the
single-project lookups) and the create/update payload consumed by
``ProjectMigration``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.domain.ids import OpProjectId


class OpProject(BaseModel):
    """Canonical representation of an OpenProject project."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: OpProjectId | None = None
    """OpenProject ``projects.id`` primary key (``None`` for create payloads)."""

    name: str
    """Display name of the project."""

    identifier: str
    """URL-safe project identifier (slug)."""

    description: str = ""
    """Project description. Empty string when absent — matches the Rails dump."""

    parent_id: OpProjectId | None = None
    """Parent project id for hierarchical projects."""

    public: bool = False
    """``True`` if the project is publicly visible."""

    status: str | None = None
    """Status name (``"on_track"``, ``"at_risk"``, …) — mirrors ``status&.name``."""

    enabled_module_names: list[str] = Field(default_factory=list, alias="enabled_modules")
    """Enabled OpenProject module identifiers (``"work_package_tracking"`` …).

    Accepts both ``enabled_module_names`` (the canonical OP API name) and
    ``enabled_modules`` (the alias used by the Rails JSON dump) on input.
    """

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> OpProject:
        """Build an :class:`OpProject` from a Rails/REST dict shape."""
        return cls.model_validate(raw)


__all__ = ["OpProject"]
