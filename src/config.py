"""
Configuration module for the Jira to OpenProject migration.
Provides a centralized configuration interface using ConfigLoader.
"""

import os
import logging
from typing import Any, Literal
from .config_loader import ConfigLoader, ConfigDict
from .display import configure_logging

# PEP 695 Type Aliases
type DirType = Literal["root", "data", "logs", "output", "backups", "temp"]
type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Create a singleton instance of ConfigLoader
_config_loader = ConfigLoader()

# Extract configuration sections for easy access
jira_config = _config_loader.get_jira_config()
openproject_config = _config_loader.get_openproject_config()
migration_config = _config_loader.get_migration_config()

# Set up the var directory structure
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
var_dir = os.path.join(root_dir, "var")

# Define all var directories
var_dirs: dict[DirType, str] = {
    "root": var_dir,
    "data": os.path.join(var_dir, "data"),
    "logs": os.path.join(var_dir, "logs"),
    "output": os.path.join(var_dir, "output"),
    "backups": os.path.join(var_dir, "backups"),
    "temp": os.path.join(var_dir, "temp"),
}

# Create all var directories
created_dirs = []
for dir_name, dir_path in var_dirs.items():
    # Check if directory already exists
    dir_existed = os.path.exists(dir_path)

    # Create if needed
    os.makedirs(dir_path, exist_ok=True)

    # Store appropriate message
    if not dir_existed:
        created_dirs.append(f"Created directory: {dir_path}")
    else:
        created_dirs.append(f"Using existing directory: {dir_path}")

# Set up logging with rich
LOG_LEVEL: LogLevel = migration_config.get("log_level", "DEBUG")
log_file = os.path.join(var_dirs["logs"], "migration.log")
# Configure rich logging instead of standard logging
logger = configure_logging(LOG_LEVEL, log_file)

# Now log the directory creation messages
for message in created_dirs:
    logger.debug(message, extra={"markup": True})

# Expose the function to get the full config
def get_config() -> ConfigDict:
    """Get the complete configuration object."""
    return _config_loader.get_config()

def get_value(section: str, key: str, default: Any = None) -> Any:
    """Get a specific configuration value."""
    return _config_loader.get_value(section, key, default)

def get_path(dir_type: DirType) -> str:
    """
    Get the path to a var directory.

    Args:
        dir_type: Type of directory ('data', 'logs', 'output', 'backups', 'temp')

    Returns:
        Path to the requested directory
    """
    if dir_type in var_dirs:
        return var_dirs[dir_type]
    raise ValueError(f"Unknown directory type: {dir_type=}")

def ensure_subdir(parent_dir_type: DirType, subdir_name: str) -> str:
    """
    Ensure a subdirectory exists under one of the var directories.

    Args:
        parent_dir_type: Type of parent directory ('data', 'logs', 'output', 'backups', 'temp')
        subdir_name: Name of the subdirectory to create

    Returns:
        Path to the created subdirectory
    """
    parent_dir = get_path(parent_dir_type)
    subdir_path = os.path.join(parent_dir, subdir_name)
    os.makedirs(subdir_path, exist_ok=True)
    logger.debug(f"Created subdirectory: {subdir_path=}")
    return subdir_path

# Validate required configuration
def validate_config() -> bool:
    """Validate that all required configuration variables are set."""
    missing_vars = []

    # Use pattern matching to process different config sections
    for section, required_keys in [
        ("jira", ["url", "username", "api_token"]),
        ("openproject", ["url", "api_token"]),
    ]:
        match section:
            case "jira":
                config_section = jira_config
                prefix = "J2O_JIRA_"
            case "openproject":
                config_section = openproject_config
                prefix = "J2O_OPENPROJECT_"
            case _:
                continue

        for key in required_keys:
            if not config_section.get(key):
                missing_vars.append(f"{prefix}{key.upper()}")

    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        return False

    return True
