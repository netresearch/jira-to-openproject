"""Configuration module for Jira to OpenProject migration.

Handles loading and accessing configuration settings.
"""

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from src.type_definitions import (
    Config,
    ConfigValue,
    JiraConfig,
    MigrationConfig,
    OpenProjectConfig,
    SectionName,
)

# Set up basic logging for configuration loading phase
logging.basicConfig(level=logging.INFO, format="%(message)s")
config_logger = logging.getLogger("config_loader")


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
    """Loads and provides access to configuration settings from YAML files and environment variables."""

    def __init__(self, config_file_path: Path = Path("config/config.yaml")) -> None:
        """Initialize the configuration loader.

        Args:
            config_file_path (Path): Path to the YAML configuration file

        """
        # Load environment variables in the correct order based on context
        self._load_environment_configuration()

        # Load YAML configuration
        self.config: Config = self._load_yaml_config(config_file_path)

        # Initialize default structure if not present
        if "jira" not in self.config:
            self.config["jira"] = {}
        if "openproject" not in self.config:
            self.config["openproject"] = OpenProjectConfig
        if "migration" not in self.config:
            self.config["migration"] = {}

        # Override with environment variables
        self._apply_environment_overrides()

        # Load and validate database configuration
        self._load_database_config()

    def _load_environment_configuration(self) -> None:
        """Load environment variables from .env files based on execution context.

        The loading order respects precedence:
        - .env (base config for all environments)
        - .env.local (local development overrides, if present)
        - .env.test (test-specific config, if in test environment)
        - .env.test.local (local test overrides, if in test environment and present)

        Later files override values from earlier files.
        """
        # Always load base configuration
        load_dotenv(".env")
        config_logger.debug("Loaded base environment from .env")

        # Check if we're in test mode
        test_mode = is_test_environment()

        if test_mode:
            config_logger.debug("Running in test environment")

            # In test mode, load both .env.local and .env.test
            # with test having higher precedence
            if Path(".env.local").exists():
                load_dotenv(".env.local", override=True)
                config_logger.debug("Loaded local overrides from .env.local")

            # Always load .env.test in test mode
            if Path(".env.test").exists():
                load_dotenv(".env.test", override=True)
                config_logger.debug("Loaded test environment from .env.test")

            # Load test-specific local overrides if they exist
            if Path(".env.test.local").exists():
                load_dotenv(".env.test.local", override=True)
                config_logger.debug("Loaded local test overrides from .env.test.local")
        else:
            config_logger.debug("Running in development/production environment")

            # In non-test mode, only load .env.local if it exists
            if Path(".env.local").exists():
                load_dotenv(".env.local", override=True)
                config_logger.debug("Loaded local overrides from .env.local")

    def _load_yaml_config(self, config_file_path: Path) -> Config:
        """Load configuration from YAML file.

        Args:
            config_file_path (Path): Path to the YAML configuration file

        Returns:
            dict: Configuration settings

        """
        try:
            with config_file_path.open("r") as config_file:
                config: Config = yaml.safe_load(config_file)
                return config
        except FileNotFoundError:
            config_logger.exception("Config file not found: %s", config_file_path)
            raise

    def _load_database_config(self) -> None:
        """Load and validate database configuration from environment or Docker secrets.
        
        Tries to load POSTGRES_PASSWORD from:
        1. POSTGRES_PASSWORD environment variable
        2. /run/secrets/postgres_password Docker secret file
        
        Raises:
            RuntimeError: If POSTGRES_PASSWORD is not found or is empty
        """
        # Initialize database section if not present
        if "database" not in self.config:
            self.config["database"] = {}
        
        # Try to load PostgreSQL password from environment first
        postgres_password = os.environ.get("POSTGRES_PASSWORD")
        
        if not postgres_password:
            # Try to load from Docker secrets
            secret_path = Path("/run/secrets/postgres_password")
            if secret_path.exists():
                try:
                    postgres_password = secret_path.read_text().strip()
                    config_logger.debug("Successfully loaded PostgreSQL password from configured source")
                except (IOError, OSError, PermissionError) as e:
                    config_logger.warning("Failed to read Docker secret: %s", e.__class__.__name__)
            else:
                config_logger.debug("Docker secret file not found: %s", secret_path)
        else:
            config_logger.debug("Successfully loaded PostgreSQL password from configured source")
        
        # Validate password is present and non-empty
        if not postgres_password or not postgres_password.strip():
            raise RuntimeError(
                "POSTGRES_PASSWORD is required but not found. "
                "Please set the POSTGRES_PASSWORD environment variable "
                "or create a Docker secret at /run/secrets/postgres_password"
            )
        
        # Store in config
        self.config["database"]["postgres_password"] = postgres_password
        
        # Also load other PostgreSQL environment variables with defaults
        self.config["database"]["postgres_db"] = os.environ.get("POSTGRES_DB", "jira_migration")
        self.config["database"]["postgres_user"] = os.environ.get("POSTGRES_USER", "postgres")
        
        config_logger.debug("Database configuration loaded successfully")

    def _apply_environment_overrides(self) -> None:
        """Override configuration settings with environment variables."""
        # Use pattern matching to organize environment variable processing
        for env_var, env_value in os.environ.items():
            if not env_var.startswith("J2O_"):
                continue

            match env_var.split("_"):
                case ["J2O", "LOG", "LEVEL"]:
                    # Cast to proper log level type
                    log_level = env_value.upper()
                    if log_level in ["DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "SUCCESS"]:
                        self.config["migration"]["log_level"] = log_level
                    config_logger.debug("Applied log level: %s", log_level)

                case ["J2O", "JIRA", *rest] if rest:
                    key: str = "_".join(rest).lower()

                    # Handle ScriptRunner configuration separately
                    if key.startswith("scriptrunner_"):
                        # Initialize scriptrunner config if not present
                        if "scriptrunner" not in self.config["jira"]:
                            self.config["jira"]["scriptrunner"] = {}

                        # Extract the specific scriptrunner config key
                        sr_key = key[len("scriptrunner_") :]
                        self.config["jira"]["scriptrunner"][sr_key] = (
                            self._convert_value(env_value)
                        )
                        config_logger.debug(
                            "Applied Jira ScriptRunner config: %s=%s",
                            sr_key,
                            env_value,
                        )
                    else:
                        # Regular Jira config
                        self.config["jira"][key] = self._convert_value(env_value)
                        config_logger.debug(
                            "Applied Jira config: %s=%s", key, env_value,
                        )

                case ["J2O", "OPENPROJECT", *rest] if rest:
                    key = "_".join(rest).lower()
                    self.config["openproject"][key] = self._convert_value(env_value)
                    config_logger.debug(
                        "Applied OpenProject config: %s=%s", key, env_value,
                    )

                    # Special handling for tmux_session_name
                    if key == "tmux_session_name":
                        config_logger.debug(
                            "Configured OpenProject tmux session name: %s",
                            env_value,
                        )

                case ["J2O", "BATCH", "SIZE"]:
                    self.config["migration"]["batch_size"] = int(env_value)
                    config_logger.debug("Applied batch size: %s", env_value)

                case ["J2O", "SSL", "VERIFY"]:
                    ssl_verify = env_value.lower() not in ("false", "0", "no", "n", "f")
                    self.config["migration"]["ssl_verify"] = ssl_verify
                    config_logger.debug("Applied SSL verify: %s", ssl_verify)

    def _convert_value(self, value: str) -> ConfigValue:
        """Convert string value to appropriate type."""
        # Try to convert to int
        if value.isdigit():
            return int(value)

        # Convert boolean values
        match value.lower():
            case "true" | "yes" | "y" | "1":
                return True
            case "false" | "no" | "n" | "0":
                return False
            case _:
                return value

    def get_config(self) -> Config:
        """Get the complete configuration dictionary.

        Returns:
            dict: Configuration settings

        """
        return self.config

    def get_jira_config(self) -> JiraConfig:
        """Get Jira-specific configuration.

        Returns:
            dict: Jira configuration settings

        """
        return self.config["jira"]

    def get_openproject_config(self) -> OpenProjectConfig:
        """Get OpenProject-specific configuration.

        Returns:
            dict: OpenProject configuration settings

        """
        return self.config["openproject"]

    def get_migration_config(self) -> MigrationConfig:
        """Get migration-specific configuration.

        Returns:
            dict: Migration configuration settings

        """
        return self.config["migration"]

    def get_database_config(self) -> dict[str, str]:
        """Get database-specific configuration.

        Returns:
            dict: Database configuration settings including postgres_password

        """
        return self.config.get("database", {})

    def get_postgres_password(self) -> str:
        """Get PostgreSQL password from configuration.

        Returns:
            str: PostgreSQL password

        Raises:
            RuntimeError: If password is not configured

        """
        password = self.config.get("database", {}).get("postgres_password")
        if not password:
            raise RuntimeError("PostgreSQL password not configured")
        return password

    def get_value(self, section: SectionName, key: str, default: Any = None) -> Any:
        """Get a specific configuration value.

        Args:
            section (str): Configuration section (jira, openproject, migration)
            key (str): Configuration key
            default: Default value if not found

        Returns:
            Configuration value or default if not found

        """
        return self.config[section].get(key, default)
