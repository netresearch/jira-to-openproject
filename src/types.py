from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypedDict

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


type ConfigValue = str | int | bool | dict[str, Any] | list[Any]

type ConfigDict = dict[str, dict[str, ConfigValue]]


OpenProjectConfig = TypedDict(
    "OpenProjectConfig",
    {
        "url": str,
        "server": str,
        "user": str,
        "container": str,
        "tmux_session_name": str,
        "rails_path": str,
    },
    total=False,
)


class JiraConfig(TypedDict):
    """Configuration for the Jira client."""

    url: str
    username: str
    api_token: str
    verify_ssl: bool
    scriptrunner: dict[str, Any]


type LogLevel = Literal[
    "DEBUG",
    "INFO",
    "NOTICE",
    "WARNING",
    "ERROR",
    "CRITICAL",
    "SUCCESS",
]


class MigrationConfig(TypedDict):
    """Configuration for the migration."""

    scriptrunner: dict[str, Any]
    log_level: LogLevel
    batch_size: int
    ssl_verify: bool
    dry_run: bool
    force: bool
    no_backup: bool


class Config(TypedDict):
    """Configuration for the config loader."""

    jira: JiraConfig
    openproject: OpenProjectConfig
    migration: MigrationConfig


type SectionName = Literal["jira", "openproject", "migration"]

type BackupDir = Path
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
    "backups",
    "data",
    "debug",
    "exports",
    "logs",
    "output",
    "output_test",
    "results",
    "root",
    "temp",
]


@dataclass(slots=True)
class ConfigSection:
    """Configuration section with its settings."""

    name: str
    settings: dict[str, Any] = field(default_factory=dict)
