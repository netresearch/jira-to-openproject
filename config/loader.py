"""Configuration loader for Jira to OpenProject migration.

This module provides a simple interface for loading and accessing configuration
using the Pydantic settings model while preserving all current functionality.
"""

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from .schemas.settings import Settings

logger = logging.getLogger(__name__)


def is_test_environment() -> bool:
    """Detect if code is running in a test environment.

    This checks for environment variables that would indicate pytest is running.

    Returns:
        bool: True if running in a test environment, False otherwise

    """
    # Check for pytest environment variable
    if "PYTEST_CURRENT_TEST" in os.environ:
        return True

    # Check for our custom test mode flag (set by test fixtures)
    return os.environ.get("J2O_TEST_MODE", "").lower() in ("true", "1", "yes")


class ConfigLoader:
    """Loads and provides access to configuration settings using Pydantic settings.

    Preserves all current functionality while adding type safety and validation.
    """

    def __init__(self, config_file_path: Path | None = None) -> None:
        """Initialize the configuration loader.

        Args:
            config_file_path (Path): Path to the YAML configuration file (optional)

        """
        # Load environment variables from .env.test if in test environment
        self._load_test_environment_configuration()

        # Load YAML configuration if provided
        self.yaml_config: dict[str, Any] = {}
        if config_file_path and config_file_path.exists():
            self.yaml_config = self._load_yaml_config(config_file_path)

        # Load Pydantic settings
        self.settings = Settings()

        # Override with YAML configuration
        self._apply_yaml_overrides()

        # Load and validate database configuration
        self._load_database_config()

    def _load_test_environment_configuration(self) -> None:
        """Load environment variables from .env.test if in test environment.

        In test mode, .env.test overrides the direnv configuration.
        """
        # Check if we're in test mode
        test_mode = is_test_environment()

        if test_mode:
            logger.debug("Running in test environment")

            # Load .env.test if it exists
            if Path(".env.test").exists():
                from dotenv import load_dotenv

                load_dotenv(".env.test", override=True)
                logger.debug("Loaded test environment from .env.test")

    def _load_yaml_config(self, config_file_path: Path) -> dict[str, Any]:
        """Load configuration from YAML file.

        Args:
            config_file_path (Path): Path to the YAML configuration file

        Returns:
            dict: Configuration settings

        """
        try:
            with config_file_path.open("r") as config_file:
                config: dict[str, Any] = yaml.safe_load(config_file)
                return config or {}
        except FileNotFoundError:
            logger.exception("Config file not found: %s", config_file_path)
            raise

    def _apply_yaml_overrides(self) -> None:
        """Override Pydantic settings with YAML configuration values."""
        if not self.yaml_config:
            return

        # Override Jira projects from YAML
        if "jira" in self.yaml_config and "projects" in self.yaml_config["jira"]:
            self.settings.jira_projects = self.yaml_config["jira"]["projects"]
            logger.debug(
                "Applied Jira projects from YAML: %s", self.settings.jira_projects,
            )

        # Override component order from YAML
        if (
            "migration" in self.yaml_config
            and "component_order" in self.yaml_config["migration"]
        ):
            self.settings.component_order = self.yaml_config["migration"][
                "component_order"
            ]
            logger.debug(
                "Applied component order from YAML: %s", self.settings.component_order,
            )

        # Override other YAML settings as needed
        # (Add more overrides here as needed)

    def _load_database_config(self) -> None:
        """Load and validate database configuration from environment or Docker secrets.

        Tries to load POSTGRES_PASSWORD from:
        1. POSTGRES_PASSWORD environment variable (from direnv)
        2. /run/secrets/postgres_password Docker secret file

        Raises:
            RuntimeError: If POSTGRES_PASSWORD is not found or is empty

        """
        # Try to load PostgreSQL password from environment first
        postgres_password = os.environ.get("POSTGRES_PASSWORD")

        if not postgres_password:
            # Try to load from Docker secrets
            secret_path = Path("/run/secrets/postgres_password")
            if secret_path.exists():
                try:
                    postgres_password = secret_path.read_text().strip()
                    logger.debug(
                        "Successfully loaded PostgreSQL password from Docker secret",
                    )
                except (OSError, PermissionError) as e:
                    logger.warning(
                        "Failed to read Docker secret: %s", e.__class__.__name__,
                    )
            else:
                logger.debug("Docker secret file not found: %s", secret_path)
        else:
            logger.debug(
                "Successfully loaded PostgreSQL password from environment variable",
            )

        # Update settings with database configuration
        if postgres_password:
            self.settings.postgres_password = postgres_password

        # Load other PostgreSQL environment variables
        if "POSTGRES_DB" in os.environ:
            self.settings.postgres_db = os.environ["POSTGRES_DB"]
        if "POSTGRES_USER" in os.environ:
            self.settings.postgres_user = os.environ["POSTGRES_USER"]

        logger.debug("Database configuration loaded successfully")

    def get_config(self) -> dict[str, Any]:
        """Get the complete configuration dictionary.

        Returns:
            dict: Configuration settings

        """
        return {
            "jira": self.settings.get_jira_config(),
            "openproject": self.settings.get_openproject_config(),
            "migration": self.settings.get_migration_config(),
            "database": self.settings.get_database_config(),
            "test_mode": self.settings.is_test_mode(),
        }

    def get_jira_config(self) -> dict[str, Any]:
        """Get Jira-specific configuration.

        Returns:
            dict: Jira configuration settings

        """
        return self.settings.get_jira_config()

    def get_openproject_config(self) -> dict[str, Any]:
        """Get OpenProject-specific configuration.

        Returns:
            dict: OpenProject configuration settings

        """
        return self.settings.get_openproject_config()

    def get_migration_config(self) -> dict[str, Any]:
        """Get migration-specific configuration.

        Returns:
            dict: Migration configuration settings

        """
        return self.settings.get_migration_config()

    def get_database_config(self) -> dict[str, str]:
        """Get database-specific configuration.

        Returns:
            dict: Database configuration settings including postgres_password

        """
        return self.settings.get_database_config()

    def get_postgres_password(self) -> str:
        """Get PostgreSQL password from configuration.

        Returns:
            str: PostgreSQL password

        Raises:
            RuntimeError: If password is not configured

        """
        password = self.settings.postgres_password
        if not password or password == "testpass123":
            raise RuntimeError("PostgreSQL password not configured")
        return password

    def get_value(self, section: str, key: str, default: Any = None) -> Any:
        """Get a specific configuration value.

        Args:
            section (str): Configuration section (jira, openproject, migration)
            key (str): Configuration key
            default: Default value if not found

        Returns:
            Configuration value or default if not found

        """
        config = self.get_config()
        return config.get(section, {}).get(key, default)

    def is_test_mode(self) -> bool:
        """Check if running in test mode.

        Returns:
            bool: True if running in test mode

        """
        return self.settings.is_test_mode()


# Global configuration instance
_config_loader: ConfigLoader | None = None


def get_config_loader(config_file_path: Path | None = None) -> ConfigLoader:
    """Get the global configuration loader instance.

    Args:
        config_file_path (Path): Path to the YAML configuration file (optional)

    Returns:
        ConfigLoader: Configuration loader instance

    """
    global _config_loader
    if _config_loader is None:
        _config_loader = ConfigLoader(config_file_path)
    return _config_loader


def load_settings(config_file_path: Path | None = None) -> Settings:
    """Load settings with proper precedence order.

    Args:
        config_file_path: Optional YAML configuration file

    Returns:
        Settings: Validated configuration object

    """
    try:
        loader = get_config_loader(config_file_path)
        return loader.settings
    except Exception as e:
        logger.error("Failed to load configuration: %s", e)
        raise ValueError(f"Configuration validation failed: {e}") from e
