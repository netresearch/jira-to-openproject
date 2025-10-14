"""Base configuration schemas for Jira to OpenProject migration.

This module defines the core Pydantic models for configuration validation.
"""

from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field, root_validator, validator


class JiraConfig(BaseModel):
    """Jira configuration settings."""

    url: str = Field(..., description="Jira instance URL")
    username: str = Field(..., description="Jira username/email")
    api_token: str = Field(..., description="Jira API token")
    projects: list[str] | None = Field(default=None, description="Projects to migrate")
    batch_size: int = Field(default=100, ge=1, le=1000, description="API batch size")
    fields: str = Field(
        default=(
            "summary,description,issuetype,status,priority,assignee,reporter,"
            "created,updated,labels,components,fixVersions,versions,comment,"
            "attachment,worklog,customfield_*"
        ),
        description="Jira fields to retrieve",
    )
    scriptrunner_enabled: bool = Field(
        default=False, description="Enable ScriptRunner integration",
    )
    scriptrunner_endpoint: str | None = Field(
        default=None, description="ScriptRunner endpoint",
    )

    @validator("url")
    def validate_url(cls, v: str) -> str:
        """Validate Jira URL format."""
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")

        try:
            parsed = urlparse(v)
            if not parsed.netloc:
                raise ValueError("URL must have a valid hostname")
        except Exception as e:
            raise ValueError(f"Invalid URL format: {e}")

        return v.rstrip("/")

    @validator("username")
    def validate_username(cls, v: str) -> str:
        """Validate username format."""
        if "@" not in v:
            raise ValueError("Username should be an email address")
        return v

    @validator("api_token")
    def validate_api_token(cls, v: str) -> str:
        """Validate API token format."""
        if len(v) < 10:
            raise ValueError("API token must be at least 10 characters long")
        return v

    @validator("scriptrunner_endpoint")
    def validate_scriptrunner_endpoint(cls, v: str | None, values: dict) -> str | None:
        """Validate ScriptRunner endpoint if enabled."""
        if values.get("scriptrunner_enabled") and not v:
            raise ValueError(
                "ScriptRunner endpoint is required when ScriptRunner is enabled",
            )
        return v


class OpenProjectConfig(BaseModel):
    """OpenProject configuration settings."""

    url: str = Field(..., description="OpenProject instance URL")
    api_token: str = Field(..., description="OpenProject API token")
    api_key: str | None = Field(default=None, description="Alternative API key")
    server: str | None = Field(default=None, description="SSH server hostname")
    user: str | None = Field(default=None, description="SSH username")
    container: str | None = Field(default=None, description="Docker container name")
    tmux_session: str = Field(default="rails_console", description="tmux session name")
    batch_size: int = Field(default=50, ge=1, le=1000, description="API batch size")

    @validator("url")
    def validate_url(cls, v: str) -> str:
        """Validate OpenProject URL format."""
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")

        try:
            parsed = urlparse(v)
            if not parsed.netloc:
                raise ValueError("URL must have a valid hostname")
        except Exception as e:
            raise ValueError(f"Invalid URL format: {e}")

        return v.rstrip("/")

    @validator("api_token")
    def validate_api_token(cls, v: str) -> str:
        """Validate API token format."""
        if len(v) < 10:
            raise ValueError("API token must be at least 10 characters long")
        return v

    @root_validator
    def validate_authentication(cls, values: dict) -> dict:
        """Ensure either API token or API key is provided."""
        api_token = values.get("api_token")
        api_key = values.get("api_key")

        if not api_token and not api_key:
            raise ValueError("Either api_token or api_key must be provided")

        return values


class DatabaseConfig(BaseModel):
    """Database configuration settings."""

    host: str = Field(default="localhost", description="Database host")
    port: int = Field(default=5432, ge=1, le=65535, description="Database port")
    name: str = Field(default="migration_db", description="Database name")
    user: str = Field(default="postgres", description="Database username")
    password: str = Field(..., description="Database password")
    ssl_mode: str = Field(default="prefer", description="SSL mode")

    @validator("ssl_mode")
    def validate_ssl_mode(cls, v: str) -> str:
        """Validate SSL mode."""
        valid_modes = [
            "disable",
            "allow",
            "prefer",
            "require",
            "verify-ca",
            "verify-full",
        ]
        if v not in valid_modes:
            raise ValueError(f'SSL mode must be one of: {", ".join(valid_modes)}')
        return v


class MigrationConfig(BaseModel):
    """Migration process configuration settings."""

    batch_size: int = Field(
        default=100, ge=1, le=1000, description="Migration batch size",
    )
    ssl_verify: bool = Field(
        default=True, description="Enable SSL certificate verification",
    )
    log_level: str = Field(
        default="INFO",
        regex="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$",
        description="Logging level",
    )
    data_dir: Path = Field(default=Path("./data"), description="Data directory")
    backup_dir: Path = Field(default=Path("./backups"), description="Backup directory")
    results_dir: Path = Field(
        default=Path("./results"), description="Results directory",
    )
    skip_existing: bool = Field(default=True, description="Skip existing items")
    component_order: list[str] = Field(
        default=[
            "users",
            "groups",
            "custom_fields",
            "projects",
            "versions",
            "issues",
            "relations",
            "worklogs",
            "attachments",
            "comments",
        ],
        description="Migration component order",
    )
    mapping_file: Path = Field(
        default=Path("data/id_mapping.json"), description="ID mapping file",
    )
    attachment_path: Path = Field(
        default=Path("data/attachments"), description="Attachment storage path",
    )
    enable_rails_meta_writes: bool = Field(
        default=True,
        description="Enable Rails meta operations for author/timestamp/audit preservation",
    )

    @validator(
        "data_dir", "backup_dir", "results_dir", "mapping_file", "attachment_path",
    )
    def validate_paths(cls, v: Path) -> Path:
        """Ensure paths are absolute or relative to current directory."""
        if not v.is_absolute():
            v = Path.cwd() / v
        return v

    @validator("component_order")
    def validate_component_order(cls, v: list[str]) -> list[str]:
        """Validate migration component order."""
        valid_components = {
            "users",
            "groups",
            "custom_fields",
            "projects",
            "versions",
            "issues",
            "relations",
            "worklogs",
            "attachments",
            "comments",
        }

        for component in v:
            if component not in valid_components:
                raise ValueError(
                    f'Invalid component: {component}. Valid components: {", ".join(valid_components)}',
                )

        return v


class AppConfig(BaseModel):
    """Main application configuration."""

    jira: JiraConfig
    openproject: OpenProjectConfig
    migration: MigrationConfig
    database: DatabaseConfig | None = Field(
        default=None, description="Database configuration",
    )
    test_mode: bool = Field(default=False, description="Test mode flag")
    mock_mode: bool = Field(default=False, description="Mock mode flag")
    use_mock_apis: bool = Field(default=False, description="Use mock APIs flag")

    class Config:
        """Pydantic configuration."""

        validate_assignment = True
        extra = "forbid"
        json_encoders = {Path: str}

    @root_validator
    def validate_test_mode(cls, values: dict) -> dict:
        """Validate test mode settings."""
        test_mode = values.get("test_mode", False)
        mock_mode = values.get("mock_mode", False)
        use_mock_apis = values.get("use_mock_apis", False)

        if test_mode and not (mock_mode or use_mock_apis):
            # In test mode, we should use mocks by default
            values["mock_mode"] = True
            values["use_mock_apis"] = True

        return values

    def get_connection_string(self) -> str:
        """Get database connection string if database config is available."""
        if not self.database:
            raise ValueError("Database configuration not available")

        return (
            f"postgresql://{self.database.user}:{self.database.password}"
            f"@{self.database.host}:{self.database.port}/{self.database.name}"
            f"?sslmode={self.database.ssl_mode}"
        )
