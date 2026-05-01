"""Domain layer for j2o (ADR-002 phase 3a).

This package will host the framework-agnostic core of the migration tool —
branded identifier types today; richer value objects and domain services in
subsequent phases. Adoption is intentionally gradual: nothing here changes
runtime behaviour until call sites are updated.
"""

from __future__ import annotations

from src.domain.ids import (
    JiraAccountId,
    JiraIssueKey,
    JiraProjectKey,
    JiraUserKey,
    OpCustomFieldId,
    OpPriorityId,
    OpProjectId,
    OpStatusId,
    OpTypeId,
    OpUserId,
    OpWorkPackageId,
)

__all__ = [
    "JiraAccountId",
    "JiraIssueKey",
    "JiraProjectKey",
    "JiraUserKey",
    "OpCustomFieldId",
    "OpPriorityId",
    "OpProjectId",
    "OpStatusId",
    "OpTypeId",
    "OpUserId",
    "OpWorkPackageId",
]
