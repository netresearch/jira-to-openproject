"""Domain layer for j2o (ADR-002 phase 3a).

This package will host the framework-agnostic core of the migration tool —
branded identifier types today; richer value objects and domain services in
subsequent phases. Adoption is intentionally gradual: nothing here changes
runtime behaviour until call sites are updated.
"""

from __future__ import annotations

from src.domain.enums import JournalEntryType
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
from src.domain.repositories import MappingRepository
from src.domain.results import (
    Failed,
    Skipped,
    StepResult,
    Success,
    from_component_result,
    to_component_result,
)

__all__ = [
    "Failed",
    "JiraAccountId",
    "JiraIssueKey",
    "JiraProjectKey",
    "JiraUserKey",
    "JournalEntryType",
    "MappingRepository",
    "OpCustomFieldId",
    "OpPriorityId",
    "OpProjectId",
    "OpStatusId",
    "OpTypeId",
    "OpUserId",
    "OpWorkPackageId",
    "Skipped",
    "StepResult",
    "Success",
    "from_component_result",
    "to_component_result",
]
