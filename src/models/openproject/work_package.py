"""Pydantic v2 model for OpenProject work-package payloads.

The shape modelled here matches the dict consumed by
``WorkPackageMigration`` create/update calls and produced by the Rails
``WorkPackage`` enumeration in
:mod:`src.infrastructure.openproject.openproject_work_package_service`. We model the
attribute-style payload (``project_id``, ``type_id`` …) rather than the
``_links``-shaped HAL form; the service layer translates between the two.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from src.domain.ids import (
    OpPriorityId,
    OpProjectId,
    OpStatusId,
    OpTypeId,
    OpUserId,
    OpWorkPackageId,
)


class OpWorkPackage(BaseModel):
    """Canonical representation of an OpenProject work package."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: OpWorkPackageId | None = None
    """``work_packages.id`` primary key (``None`` for create payloads)."""

    subject: str | None = None
    description: str | None = None

    type_id: OpTypeId | None = None
    status_id: OpStatusId | None = None
    priority_id: OpPriorityId | None = None
    project_id: OpProjectId | None = None

    assigned_to_id: OpUserId | None = None
    responsible_id: OpUserId | None = None

    start_date: str | None = None
    """ISO-formatted date string. Kept as ``str`` to match the Rails dump."""

    due_date: str | None = None
    """ISO-formatted date string."""

    parent_id: OpWorkPackageId | None = None
    done_ratio: int | None = None

    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> OpWorkPackage:
        """Build an :class:`OpWorkPackage` from a Rails/REST dict shape."""
        return cls.model_validate(raw)


__all__ = ["OpWorkPackage"]
