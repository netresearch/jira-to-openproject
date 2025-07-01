"""Configuration module for the Jira to OpenProject migration.
Provides a centralized configuration interface using ConfigLoader.
"""

from pathlib import Path
from typing import Any

from src.config_loader import ConfigLoader
from src.display import configure_logging
from src.type_definitions import Config, DirType, LogLevel, SectionName

# Create a singleton instance of ConfigLoader
_config_loader = ConfigLoader()

# Extract configuration sections for easy access
jira_config = _config_loader.get_jira_config()
openproject_config = _config_loader.get_openproject_config()
migration_config = _config_loader.get_migration_config()

# Set up the var directory structure
root_dir = Path(__file__).parent.parent
var_dir = root_dir / "var"

# Define all var directories
var_dirs: dict[DirType, Path] = {
    "root": var_dir,
    "backups": var_dir / "backups",
    "data": var_dir / "data",
    "debug": var_dir / "debug",
    "exports": var_dir / "exports",
    "logs": var_dir / "logs",
    "output": var_dir / "output",
    "output_test": var_dir / "output_test",
    "results": var_dir / "results",
    "temp": var_dir / "temp",
}

# Create all var directories
created_dirs = []
for dir_path in var_dirs.values():
    # Check if directory already exists
    dir_existed = dir_path.exists()

    # Create if needed
    dir_path.mkdir(parents=True, exist_ok=True)

    # Store appropriate message
    if not dir_existed:
        created_dirs.append(f"Created directory: {dir_path}")
    else:
        created_dirs.append(f"Using existing directory: {dir_path}")

# Set up logging with rich
LOG_LEVEL: LogLevel = migration_config.get("log_level", "DEBUG")
log_file = var_dirs["logs"] / "migration.log"
# Configure rich logging instead of standard logging
logger = configure_logging(LOG_LEVEL, log_file)

# Now log the directory creation messages
for message in created_dirs:
    logger.debug(message)


# Expose the function to get the full config
def get_config() -> Config:
    """Get the complete configuration object."""
    return _config_loader.get_config()


def get_value(section: SectionName, key: str, default: Any = None) -> Any:
    """Get a specific configuration value."""
    return _config_loader.get_value(section, key, default)


def get_path(path_type: DirType) -> Path:
    """Get a specific path from var_dirs."""
    if path_type not in var_dirs:
        msg = f"Invalid path type: {path_type}"
        raise ValueError(msg)

    return var_dirs[path_type]


def ensure_subdir(parent_dir_type: DirType, subdir_name: str | None = None) -> Path:
    """Ensure a subdirectory exists under one of the var directories.

    Args:
        parent_dir_type: Type of parent directory or path to the parent directory
        subdir_name: Name of the subdirectory to create (optional if parent_dir_type is a path)

    Returns:
        Path to the created subdirectory

    """
    parent_dir = get_path(parent_dir_type)

    if subdir_name:
        subdir_path = parent_dir / subdir_name
        subdir_path.mkdir(parents=True, exist_ok=True)
        logger.debug("Created subdirectory: %s", subdir_path)
        return subdir_path
    # Just ensure the parent directory exists
    parent_dir.mkdir(parents=True, exist_ok=True)
    logger.debug("Created directory: %s", parent_dir)
    return parent_dir


# Validate required configuration
def validate_config() -> bool:
    """Validate that all required configuration variables are set."""
    missing_vars = []

    # Use pattern matching to process different config sections
    for section, required_keys in [
        ("jira", ["url", "username", "api_token"]),
        ("openproject", ["url"]),  # Allow either api_token or api_key for OpenProject
    ]:
        match section:
            case "jira":
                config_section = jira_config
                prefix = "J2O_JIRA_"
            case "openproject":
                config_section = openproject_config
                prefix = "J2O_OPENPROJECT_"
                # Special handling for OpenProject authentication
                if not (
                    config_section.get("api_token") or config_section.get("api_key")
                ):
                    missing_vars.append(f"{prefix}API_TOKEN or {prefix}API_KEY")
                continue
            case _:
                continue

        for key in required_keys:
            if not config_section.get(key):
                missing_vars.append(f"{prefix}{key.upper()}")

    if missing_vars:
        logger.error(
            "Missing required environment variables: %s", ", ".join(missing_vars),
        )
        return False

    return True


def update_from_cli_args(args: Any) -> None:
    """Update migration configuration from CLI arguments.

    Args:
        args: An object containing CLI arguments (typically from argparse)

    """
    if hasattr(args, "dry_run") and args.dry_run:
        migration_config["dry_run"] = True
        logger.debug("Setting dry_run=True from CLI arguments")

    if hasattr(args, "no_backup") and args.no_backup:
        migration_config["no_backup"] = True
        logger.debug("Setting no_backup=True from CLI arguments")

    if hasattr(args, "force") and args.force:
        migration_config["force"] = True
        logger.debug("Setting force=True from CLI arguments")

    if hasattr(args, "stop_on_error") and args.stop_on_error:
        migration_config["stop_on_error"] = True
        logger.debug("Setting stop_on_error=True from CLI arguments")

    if hasattr(args, "no_confirm") and args.no_confirm:
        migration_config["no_confirm"] = True
        logger.debug("Setting no_confirm=True from CLI arguments")

    # Add any other CLI arguments that should affect configuration here
