"""
Configuration module for Jira to OpenProject migration.
Handles loading and accessing configuration settings.
"""

import os
import yaml
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Any, Union, Literal, Optional, TypeVar, Generic
from dotenv import load_dotenv

# Set up basic logging for configuration loading phase
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s"
)
config_logger = logging.getLogger("config_loader")

# Type aliases
T = TypeVar("T")
ConfigValue = Union[str, int, bool, Dict[str, Any], List[Any]]
ConfigDict = Dict[str, Dict[str, ConfigValue]]
SectionName = Literal["jira", "openproject", "migration"]

@dataclass(slots=True)
class ConfigSection:
    """Configuration section with its settings"""
    name: str
    settings: dict[str, Any] = field(default_factory=dict)

class ConfigLoader:
    """
    Loads and provides access to configuration settings from YAML files and environment variables.
    """

    def __init__(self, config_file_path="config/config.yaml"):
        """
        Initialize the configuration loader.

        Args:
            config_file_path (str): Path to the YAML configuration file
        """
        # Load environment variables from .env file (default values)
        load_dotenv()

        # Load environment variables from .env.local file (custom values that override defaults)
        load_dotenv(".env.local", override=True)

        # Load YAML configuration
        self.config = self._load_yaml_config(config_file_path)

        # Initialize default structure if not present
        if "jira" not in self.config:
            self.config["jira"] = {}
        if "openproject" not in self.config:
            self.config["openproject"] = {}
        if "migration" not in self.config:
            self.config["migration"] = {}

        # Override with environment variables
        self._apply_environment_overrides()

    def _load_yaml_config(self, config_file_path: str) -> ConfigDict:
        """
        Load configuration from YAML file.

        Args:
            config_file_path (str): Path to the YAML configuration file

        Returns:
            dict: Configuration settings
        """
        try:
            with open(config_file_path, "r") as config_file:
                return yaml.safe_load(config_file)
        except FileNotFoundError:
            config_logger.error(f"{config_file_path=} not found")
            return {}

    def _apply_environment_overrides(self) -> None:
        """
        Override configuration settings with environment variables.
        """
        # Use pattern matching to organize environment variable processing
        for env_var, env_value in os.environ.items():
            if not env_var.startswith("J2O_"):
                continue

            match env_var.split("_"):
                case ["J2O", "JIRA", *rest] if rest:
                    key = "_".join(rest).lower()
                    self.config["jira"][key] = self._convert_value(env_value)
                    config_logger.info(f"Applied Jira config: {key}={env_value}")

                case ["J2O", "OPENPROJECT", *rest] if rest:
                    key = "_".join(rest).lower()
                    self.config["openproject"][key] = self._convert_value(env_value)
                    config_logger.info(f"Applied OpenProject config: {key}={env_value}")

                case ["J2O", "LOG", "LEVEL"]:
                    # Make sure log level is valid - our custom levels are handled by display.py
                    valid_levels = ["DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "SUCCESS"]
                    if env_value.upper() in valid_levels:
                        self.config["migration"]["log_level"] = env_value.upper()
                        config_logger.info(f"Applied log level: {env_value.upper()}")
                    else:
                        config_logger.warning(f"Invalid log level: {env_value}. Using INFO instead.")
                        self.config["migration"]["log_level"] = "INFO"

                case ["J2O", "BATCH", "SIZE"]:
                    self.config["migration"]["batch_size"] = int(env_value)
                    config_logger.info(f"Applied batch size: {env_value=}")

                case ["J2O", "RATE", "LIMIT", "REQUESTS"]:
                    self.config["migration"]["rate_limit_requests"] = int(env_value)
                    config_logger.info(f"Applied rate limit requests: {env_value=}")

                case ["J2O", "RATE", "LIMIT", "PERIOD"]:
                    self.config["migration"]["rate_limit_period"] = int(env_value)
                    config_logger.info(f"Applied rate limit period: {env_value=}")

                case ["J2O", "SSL", "VERIFY"]:
                    ssl_verify = env_value.lower() not in ("false", "0", "no", "n", "f")
                    self.config["migration"]["ssl_verify"] = ssl_verify
                    config_logger.info(f"Applied SSL verify: {ssl_verify=}")

    def _convert_value(self, value: str) -> ConfigValue:
        """Convert string value to appropriate type"""
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

    def get_config(self) -> ConfigDict:
        """
        Get the complete configuration dictionary.

        Returns:
            dict: Configuration settings
        """
        return self.config

    def get_jira_config(self) -> ConfigDict:
        """
        Get Jira-specific configuration.

        Returns:
            dict: Jira configuration settings
        """
        return self.config.get("jira", {})

    def get_openproject_config(self) -> ConfigDict:
        """
        Get OpenProject-specific configuration.

        Returns:
            dict: OpenProject configuration settings
        """
        return self.config.get("openproject", {})

    def get_migration_config(self) -> ConfigDict:
        """
        Get migration-specific configuration.

        Returns:
            dict: Migration configuration settings
        """
        return self.config.get("migration", {})

    def get_value(self, section: SectionName, key: str, default: Any = None) -> Any:
        """
        Get a specific configuration value.

        Args:
            section (str): Configuration section (jira, openproject, migration)
            key (str): Configuration key
            default: Default value if not found

        Returns:
            Configuration value or default if not found
        """
        return self.config.get(section, {}).get(key, default)
