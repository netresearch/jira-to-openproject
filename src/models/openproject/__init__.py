"""OpenProject-side Pydantic models (ADR-002 phase 3a)."""

from __future__ import annotations

from src.models.openproject.custom_field import OpCustomField
from src.models.openproject.project import OpProject
from src.models.openproject.user import OpUser
from src.models.openproject.work_package import OpWorkPackage

__all__ = [
    "OpCustomField",
    "OpProject",
    "OpUser",
    "OpWorkPackage",
]
