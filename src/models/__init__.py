"""Models package for data structures used in the application."""

from src.models.component_results import ComponentResult
from src.models.jira import (
    JiraAttachment,
    JiraComment,
    JiraComponentRef,
    JiraIssue,
    JiraIssueFields,
    JiraIssueTypeRef,
    JiraPriority,
    JiraPriorityRef,
    JiraProject,
    JiraProjectCategoryRef,
    JiraResolutionRef,
    JiraSecurityLevelRef,
    JiraStatusRef,
    JiraUser,
    JiraVersionRef,
    JiraVotesRef,
)
from src.models.mapping import WorkPackageMappingEntry
from src.models.migration_error import MigrationError
from src.models.migration_results import MigrationResult
from src.models.openproject import (
    OpCustomField,
    OpProject,
    OpUser,
    OpWorkPackage,
)

__all__ = [
    "ComponentResult",
    "JiraAttachment",
    "JiraComment",
    "JiraComponentRef",
    "JiraIssue",
    "JiraIssueFields",
    "JiraIssueTypeRef",
    "JiraPriority",
    "JiraPriorityRef",
    "JiraProject",
    "JiraProjectCategoryRef",
    "JiraResolutionRef",
    "JiraSecurityLevelRef",
    "JiraStatusRef",
    "JiraUser",
    "JiraVersionRef",
    "JiraVotesRef",
    "MigrationError",
    "MigrationResult",
    "OpCustomField",
    "OpProject",
    "OpUser",
    "OpWorkPackage",
    "WorkPackageMappingEntry",
]
