"""Mapping-side Pydantic models and legacy mapping helpers (ADR-002 phase 3)."""

from __future__ import annotations

from src.models.mapping.jira_to_op import JiraToOPMapping
from src.models.mapping.work_package_entry import WorkPackageMappingEntry

__all__ = [
    "JiraToOPMapping",
    "WorkPackageMappingEntry",
]
