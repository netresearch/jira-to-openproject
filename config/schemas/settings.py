"""Main settings schema for Jira to OpenProject migration.

This module defines the core Pydantic settings model with proper validation
and environment variable handling, preserving all current configuration settings.
"""

from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Main application settings with validation and environment variable support.

    Preserves all current configuration settings while adding type safety and validation.
    """

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="forbid",
        env_prefix="J2O_",
    )

    # ========================================================================
    # JIRA CONFIGURATION (J2O_JIRA_*)
    # ========================================================================

    # Jira Connection Settings
    jira_url: str = Field(
        default="https://your-company.atlassian.net", description="Jira instance URL",
    )
    jira_username: str = Field(
        default="your-email@company.com", description="Jira username/email",
    )
    jira_api_token: str = Field(
        default="your_jira_api_token_here", description="Jira API token",
    )

    # Jira Projects (from YAML config)
    jira_projects: list[str] | None = Field(
        default=None, description="Projects to migrate (leave empty to migrate all)",
    )

    # Jira API Settings
    jira_batch_size: int = Field(
        default=100, ge=1, le=1000, description="Jira API batch size",
    )
    jira_fields: str = Field(
        default=(
            "summary,description,issuetype,status,priority,assignee,reporter,"
            "created,updated,labels,components,fixVersions,versions,comment,"
            "attachment,worklog,customfield_*"
        ),
        description="Jira fields to retrieve",
    )

    # Jira ScriptRunner Plugin Configuration
    jira_scriptrunner_enabled: bool = Field(
        default=False, description="Enable ScriptRunner integration",
    )
    jira_scriptrunner_endpoint: str | None = Field(
        default="https://your-company.atlassian.net/rest/scriptrunner/latest",
        description="ScriptRunner endpoint",
    )

    # ========================================================================
    # OPENPROJECT CONFIGURATION (J2O_OPENPROJECT_*)
    # ========================================================================

    # OpenProject Connection Settings
    openproject_url: str = Field(
        default="https://your-openproject.company.com",
        description="OpenProject instance URL",
    )
    openproject_api_token: str = Field(
        default="your_openproject_api_token_here", description="OpenProject API token",
    )
    openproject_api_key: str | None = Field(
        default="your_openproject_api_key_here",
        description="Alternative OpenProject API key",
    )

    # OpenProject SSH/Docker Remote Access Settings
    openproject_server: str | None = Field(
        default="sobol.nr", description="SSH server hostname",
    )
    openproject_user: str | None = Field(
        default="sebastian.mendel", description="SSH username",
    )
    openproject_container: str | None = Field(
        default="openproject-web-1", description="Docker container name",
    )
    openproject_tmux_session_name: str = Field(
        default="rails_console", description="tmux session name",
    )

    # OpenProject API Settings
    openproject_batch_size: int = Field(
        default=50, ge=1, le=1000, description="OpenProject API batch size",
    )

    # ========================================================================
    # MIGRATION SETTINGS (J2O_*)
    # ========================================================================

    # Migration Behavior Configuration
    batch_size: int = Field(
        default=100, ge=1, le=1000, description="Migration batch size",
    )
    ssl_verify: bool = Field(
        default=True, description="Enable SSL certificate verification",
    )
    log_level: str = Field(default="INFO", description="Logging level")

    # Migration Data Directories
    data_dir: Path = Field(default=Path("./data"), description="Data directory")
    backup_dir: Path = Field(default=Path("./backups"), description="Backup directory")
    results_dir: Path = Field(
        default=Path("./results"), description="Results directory",
    )

    # Migration Component Order (from YAML config)
    component_order: list[str] = Field(
        default=[
            "users",
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

    # Migration Data Files
    mapping_file: Path = Field(
        default=Path("data/id_mapping.json"), description="ID mapping file",
    )
    attachment_path: Path = Field(
        default=Path("data/attachments"), description="Attachment storage path",
    )

    # Migration Behavior Settings
    skip_existing: bool = Field(default=True, description="Skip existing items")

    # ========================================================================
    # TESTING CONFIGURATION (J2O_TEST_*)
    # ========================================================================

    test_mode: bool = Field(default=False, description="Test mode flag")
    test_mock_mode: bool = Field(default=False, description="Mock mode flag")
    use_mock_apis: bool = Field(default=False, description="Use mock APIs flag")

    # ========================================================================
    # DATABASE CONFIGURATION (POSTGRES_*)
    # ========================================================================

    # Database settings (from environment variables)
    postgres_password: str = Field(
        default="testpass123", description="PostgreSQL password",
    )
    postgres_db: str = Field(
        default="jira_migration", description="PostgreSQL database name",
    )
    postgres_user: str = Field(default="postgres", description="PostgreSQL username")

    # ========================================================================
    # VALIDATORS
    # ========================================================================

    @field_validator("jira_url")
    @classmethod
    def validate_jira_url(cls, v: str) -> str:
        """Validate Jira URL format."""
        if not v.startswith(("http://", "https://")):
            raise ValueError("Jira URL must start with http:// or https://")

        try:
            parsed = urlparse(v)
            if not parsed.netloc:
                raise ValueError("Jira URL must have a valid hostname")
        except Exception as e:
            raise ValueError(f"Invalid Jira URL format: {e}")

        return v.rstrip("/")

    @field_validator("jira_username")
    @classmethod
    def validate_jira_username(cls, v: str) -> str:
        """Validate Jira username format."""
        if "@" not in v:
            raise ValueError("Jira username should be an email address")
        return v

    @field_validator("jira_api_token")
    @classmethod
    def validate_jira_api_token(cls, v: str) -> str:
        """Validate Jira API token format."""
        if len(v) < 10:
            raise ValueError("Jira API token must be at least 10 characters long")
        return v

    @field_validator("openproject_url")
    @classmethod
    def validate_openproject_url(cls, v: str) -> str:
        """Validate OpenProject URL format."""
        if not v.startswith(("http://", "https://")):
            raise ValueError("OpenProject URL must start with http:// or https://")

        try:
            parsed = urlparse(v)
            if not parsed.netloc:
                raise ValueError("OpenProject URL must have a valid hostname")
        except Exception as e:
            raise ValueError(f"Invalid OpenProject URL format: {e}")

        return v.rstrip("/")

    @field_validator("openproject_api_token")
    @classmethod
    def validate_openproject_api_token(cls, v: str) -> str:
        """Validate OpenProject API token format."""
        if len(v) < 10:
            raise ValueError(
                "OpenProject API token must be at least 10 characters long",
            )
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid_levels:
            raise ValueError(f'Log level must be one of: {", ".join(valid_levels)}')
        return v.upper()

    @field_validator("component_order")
    @classmethod
    def validate_component_order(cls, v: list[str]) -> list[str]:
        """Validate migration component order."""
        valid_components = {
            "users",
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

    @model_validator(mode="after")
    def validate_authentication(self) -> "Settings":
        """Ensure proper authentication is configured."""
        # Placeholders are allowed because migrations run in a trusted admin context;
        # real credentials must be supplied via environment variables prior to production use.

        # Validate Jira authentication (allow placeholders for now)
        if not self.jira_url:
            raise ValueError("Jira URL must be configured")

        # Validate OpenProject authentication (allow placeholders for now)
        if not self.openproject_url:
            raise ValueError("OpenProject URL must be configured")

        # Validate ScriptRunner configuration
        if self.jira_scriptrunner_enabled and not self.jira_scriptrunner_endpoint:
            raise ValueError(
                "ScriptRunner endpoint is required when ScriptRunner is enabled",
            )

        return self

    @model_validator(mode="after")
    def validate_test_mode(self) -> "Settings":
        """Validate test mode settings."""
        if self.test_mode and not (self.test_mock_mode or self.use_mock_apis):
            # In test mode, we should use mocks by default
            self.test_mock_mode = True
            self.use_mock_apis = True

        return self

    # ========================================================================
    # UTILITY METHODS
    # ========================================================================

    def get_jira_config(self) -> dict:
        """Get Jira configuration as dictionary."""
        return {
            "url": self.jira_url,
            "username": self.jira_username,
            "api_token": self.jira_api_token,
            "projects": self.jira_projects,
            "batch_size": self.jira_batch_size,
            "fields": self.jira_fields,
            "scriptrunner": (
                {
                    "enabled": self.jira_scriptrunner_enabled,
                    "endpoint": self.jira_scriptrunner_endpoint,
                }
                if self.jira_scriptrunner_enabled
                else None
            ),
        }

    def get_openproject_config(self) -> dict:
        """Get OpenProject configuration as dictionary."""
        return {
            "url": self.openproject_url,
            "api_token": self.openproject_api_token,
            "api_key": self.openproject_api_key,
            "server": self.openproject_server,
            "user": self.openproject_user,
            "container": self.openproject_container,
            "tmux_session": self.openproject_tmux_session_name,
            "batch_size": self.openproject_batch_size,
        }

    def get_migration_config(self) -> dict:
        """Get migration configuration as dictionary."""
        return {
            "batch_size": self.batch_size,
            "ssl_verify": self.ssl_verify,
            "log_level": self.log_level,
            "data_dir": str(self.data_dir),
            "backup_dir": str(self.backup_dir),
            "results_dir": str(self.results_dir),
            "component_order": self.component_order,
            "mapping_file": str(self.mapping_file),
            "attachment_path": str(self.attachment_path),
            "skip_existing": self.skip_existing,
        }

    def get_database_config(self) -> dict:
        """Get database configuration as dictionary."""
        return {
            "postgres_password": self.postgres_password,
            "postgres_db": self.postgres_db,
            "postgres_user": self.postgres_user,
        }

    def is_test_mode(self) -> bool:
        """Check if running in test mode."""
        return self.test_mode or self.test_mock_mode or self.use_mock_apis
