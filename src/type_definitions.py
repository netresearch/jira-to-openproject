"""Type definitions for Jira to OpenProject migration.

This module contains data classes and type definitions used throughout
the migration process.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

type JiraData = dict[str, Any]
type OpenProjectData = dict[str, Any]
type MappingResult = dict[str, Any]
type TypeMapping = dict[str, int]
type StatusMapping = dict[str, int]


@dataclass(slots=True)
class JiraIssueType:
    """Represents a Jira issue type."""

    id: str
    name: str
    description: str | None = None


@dataclass(slots=True)
class OpenProjectWorkPackageType:
    """Represents an OpenProject work package type."""

    name: str
    color: str = "#0000FF"
    is_default: bool = False
    is_milestone: bool = False
    position: int = 1
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(slots=True)
class JiraStatus:
    """Represents a Jira status."""

    id: str
    name: str
    statusCategory: dict[str, str] | None = None


@dataclass(slots=True)
class OpenProjectStatus:
    """Represents an OpenProject status."""

    name: str
    description: str | None = None
    color: str = "#0000FF"
    is_closed: bool = False
    is_default: bool = False
    is_readonly: bool = False
    default_done_ratio: int | None = None
    position: int = 1


type ConfigValue = str | int | bool | dict[str, Any] | list[Any]

type ConfigDict = dict[str, dict[str, ConfigValue]]


class OpenProjectConfig(TypedDict, total=False):
    """Configuration for OpenProject connection."""

    url: str
    server: str
    port: NotRequired[int]
    user: NotRequired[str]
    container_name: NotRequired[str]
    project_path: NotRequired[str]
    api_key: NotRequired[str]
    api_token: NotRequired[str]
    tmux_session_name: NotRequired[str]


class JiraConfig(TypedDict):
    """Configuration for the Jira client."""

    url: str
    username: str
    api_token: str
    verify_ssl: bool
    scriptrunner: dict[str, Any]
    projects: NotRequired[list[str]]


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
    stop_on_error: bool
    no_confirm: bool
    disable_wpm_shim: NotRequired[bool]
    log_retention_count: NotRequired[int]


class DatabaseConfig(TypedDict, total=False):
    """Database configuration populated from environment/secrets."""

    postgres_password: str
    postgres_db: str
    postgres_user: str


class Config(TypedDict):
    """Configuration for the config loader."""

    jira: JiraConfig
    openproject: OpenProjectConfig
    migration: MigrationConfig
    # Optional database section populated at runtime
    database: NotRequired[DatabaseConfig]


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
