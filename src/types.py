

from typing import Any, Literal, Union
from dataclasses import dataclass, field


type JiraData = dict[str, Any]
type OpenProjectData = dict[str, Any]
type MappingResult = dict[str, Any]
type TypeMapping = dict[str, int]
type StatusMapping = dict[str, int]


@dataclass(slots=True)
class JiraIssueType:
    id: str
    name: str
    description: str | None = None


@dataclass(slots=True)
class OpenProjectWorkPackageType:
    name: str
    color: str = "#0000FF"
    is_milestone: bool = False
    is_default: bool = False
    position: int = 1
    is_in_roadmap: bool = True
    jira_id: str | None = None
    description: str | None = None


@dataclass(slots=True)
class JiraStatus:
    id: str
    name: str
    description: str | None = None


@dataclass(slots=True)
class OpenProjectStatus:
    name: str
    description: str | None = None
    is_closed: bool = False
    color: str = "#0000FF"
    jira_id: str | None = None


type ConfigValue = Union[str, int, bool, dict[str, Any], list[Any]]

type ConfigDict = dict[str, dict[str, ConfigValue]]

type SectionName = Literal["jira", "openproject", "migration"]

type BackupDir = str | None
type ComponentStatus = Literal["success", "failed", "interrupted"]
type ComponentName = Literal[
    "users",
    "custom_fields",
    "companies",
    "accounts",
    "projects",
    "link_types",
    "issue_types",
    "status_types",
    "work_packages",
]


# PEP 695 Type Aliases
type DirType = Literal[
    "root", "data", "logs", "output", "backups", "temp", "exports", "results"
]

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


@dataclass(slots=True)
class ConfigSection:
    """Configuration section with its settings"""

    name: str
    settings: dict[str, Any] = field(default_factory=dict)

