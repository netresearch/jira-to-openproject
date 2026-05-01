"""Pydantic v2 model for OpenProject user payloads.

The shape modelled here matches what
:mod:`src.infrastructure.openproject.openproject_user_service` returns and what
:mod:`src.utils.enhanced_user_association_migrator` consumes — a flat dict
with ``login``, ``mail``, ``firstname``/``lastname`` and friends. The
``email`` alias accommodates the small number of code paths that still
use the older REST key.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.domain.ids import OpUserId


class OpUser(BaseModel):
    """Canonical representation of an OpenProject user."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: OpUserId | None = None
    """OpenProject ``users.id`` primary key."""

    login: str | None = None
    """OpenProject login (the OP equivalent of a Jira ``name``)."""

    firstname: str | None = None
    lastname: str | None = None

    mail: str | None = Field(default=None, alias="email")
    """Email address. Accepts both ``mail`` (canonical Rails attribute) and
    ``email`` (older REST shape) on input via Pydantic alias."""

    admin: bool = False
    """``True`` if this account has the global admin role."""

    language: str | None = None
    """User language preference, e.g. ``"en"``."""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> OpUser:
        """Build an :class:`OpUser` from a Rails/REST dict shape."""
        return cls.model_validate(raw)


__all__ = ["OpUser"]
