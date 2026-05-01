"""Models package for data structures used in the application."""

from src.models.component_results import ComponentResult
from src.models.jira import (
    JiraAttachment,
    JiraComment,
    JiraComponentLead,
    JiraComponentRef,
    JiraIssue,
    JiraIssueFields,
    JiraIssueTypeRef,
    JiraPriority,
    JiraPriorityRef,
    JiraProject,
    JiraProjectCategoryRef,
    JiraProjectComponent,
    JiraRemoteLinkRef,
    JiraResolutionRef,
    JiraSecurityLevelRef,
    JiraStatusRef,
    JiraUser,
    JiraVersionRef,
    JiraVotesRef,
    JiraWatcher,
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
    "JiraComponentLead",
    "JiraComponentRef",
    "JiraIssue",
    "JiraIssueFields",
    "JiraIssueTypeRef",
    "JiraPriority",
    "JiraPriorityRef",
    "JiraProject",
    "JiraProjectCategoryRef",
    "JiraProjectComponent",
    "JiraRemoteLinkRef",
    "JiraResolutionRef",
    "JiraSecurityLevelRef",
    "JiraStatusRef",
    "JiraUser",
    "JiraVersionRef",
    "JiraVotesRef",
    "JiraWatcher",
    "MigrationError",
    "MigrationResult",
    "OpCustomField",
    "OpProject",
    "OpUser",
    "OpWorkPackage",
    "WorkPackageMappingEntry",
]
