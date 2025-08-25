"""Configuration module for the Jira to OpenProject migration.
Provides a centralized configuration interface using ConfigLoader.
"""

import threading
import logging
from pathlib import Path
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from src.config_loader import ConfigLoader
from src.display import configure_logging
from src.type_definitions import Config, DirType, LogLevel, SectionName

if TYPE_CHECKING:
    from src.mappings.mappings import Mappings

# Import error recovery specific config
from .error_recovery_config import ErrorRecoveryConfig, load_error_recovery_config

# Create a singleton instance of ConfigLoader
_config_loader = ConfigLoader()

# Extract configuration sections for easy access
jira_config = _config_loader.get_jira_config()
openproject_config = _config_loader.get_openproject_config()
migration_config = _config_loader.get_migration_config()
_cli_args: dict[str, Any] = {}

# Set up the var directory structure
root_dir = Path(__file__).parent.parent.parent
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

# Always keep a stable, aggregate log as before
latest_log_file = var_dirs["logs"] / "migration.log"
logger = configure_logging(LOG_LEVEL, latest_log_file)

# Additionally, attach a per-run log file handler for easier analysis/rotation
try:
    _timestamp = datetime.now(tz=UTC).strftime("%Y-%m-%d_%H-%M-%S")
    per_run_log_file = var_dirs["logs"] / f"migration_{_timestamp}.log"

    _file_formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - %(message)s",
    )
    _file_handler = logging.FileHandler(per_run_log_file)
    _file_handler.setFormatter(_file_formatter)
    _file_handler.setLevel(getattr(logging, str(LOG_LEVEL).upper(), logging.INFO))
    logging.getLogger().addHandler(_file_handler)
    logger.info("Per-run log file: %s", per_run_log_file)

    # Simple retention: keep only the most recent N per-run log files
    retention_count = int(migration_config.get("log_retention_count", 20))
    if retention_count > 0:
        per_run_logs = sorted(var_dirs["logs"].glob("migration_*.log"))
        if len(per_run_logs) > retention_count:
            to_delete = per_run_logs[: len(per_run_logs) - retention_count]
            pruned = 0
            for old_log in to_delete:
                try:
                    old_log.unlink()
                    pruned += 1
                except Exception:
                    logger.debug("Failed to remove old per-run log: %s", old_log)
            if pruned:
                logger.info(
                    "Per-run log rotation: kept %d, pruned %d",
                    retention_count,
                    pruned,
                )
except Exception:
    # Do not fail initialization if per-run handler cannot be attached
    logger.exception("Failed to attach per-run log handler")

# Now log the directory creation messages
for message in created_dirs:
    logger.debug(message)

# Export logger for use by other modules
__all__ = [
    "FALLBACK_MAIL_DOMAIN",
    "LOG_LEVEL",
    "USER_CREATION_BATCH_SIZE",
    "USER_CREATION_TIMEOUT",
    "ErrorRecoveryConfig",
    "ensure_subdir",
    "get_config",
    "get_mappings",
    "get_path",
    "get_value",
    "jira_config",
    "load_error_recovery_config",
    "logger",
    "mappings",
    "migration_config",
    "openproject_config",
    "update_from_cli_args",
    "reset_mappings",
    "update_from_cli_args",
    "validate_config",
    "var_dirs",
]


# ==================================================================================
# MAPPINGS MANAGEMENT - Expert-validated solution for compliance violations
# ==================================================================================

# Global mappings state management with thread safety
_mappings: "Mappings | None" = None
_mappings_lock = threading.Lock()


class MappingsInitializationError(Exception):
    """Raised when mappings cannot be initialized.

    This custom exception provides clear diagnostics when mappings initialization
    fails, following proper exception-based error handling patterns.
    """


def get_mappings() -> "Mappings":
    """Get or initialize the global mappings instance.

    Follows optimistic execution pattern - attempts operation directly,
    only performs diagnostics if initialization fails.

    Thread-safe implementation using double-checked locking pattern
    to prevent race conditions during initialization.

    Returns:
        Mappings: The global mappings instance

    Raises:
        MappingsInitializationError: If mappings cannot be initialized

    """
    global _mappings
    if _mappings is None:
        with _mappings_lock:  # Thread safety per expert feedback
            if _mappings is None:  # Double-check pattern
                try:
                    # Optimistic execution: attempt to create mappings directly
                    from src.mappings.mappings import Mappings

                    _mappings = Mappings(data_dir=get_path("data"))
                    logger.debug("Successfully initialized global mappings instance")
                except Exception as e:
                    # Only perform diagnostics if initialization fails
                    data_dir = get_path("data")
                    logger.exception(
                        "Failed to initialize mappings with data_dir=%s: %s",
                        data_dir,
                        e,
                    )
                    msg = f"Cannot initialize mappings: {e}"
                    raise MappingsInitializationError(
                        msg,
                    ) from e
    return _mappings


def reset_mappings() -> None:
    """Reset mappings cache for testing.

    This helper function allows tests to clear the cached mappings state
    to prevent cross-test leakage and ensure test isolation.

    Note: This function is primarily intended for test usage.
    """
    global _mappings
    with _mappings_lock:
        _mappings = None
        logger.debug("Reset mappings cache for testing")


class _MappingsProxy:
    """Proxy class for backward compatibility with config.mappings access pattern.

    This proxy automatically delegates all attribute access to the actual
    Mappings instance, providing seamless backward compatibility while
    maintaining proper lazy initialization and exception handling.
    """

    def __getattr__(self, name: str) -> Any:
        """Delegate all attribute access to the actual Mappings instance."""
        return getattr(get_mappings(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        """Delegate all attribute setting to the actual Mappings instance."""
        setattr(get_mappings(), name, value)


# Backward compatibility - seamless access for existing code using config.mappings
mappings = _MappingsProxy()


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


def update_from_cli_args(args: Any) -> None:
    """Apply CLI arguments into runtime config.

    Currently supports --jira-project-filter to limit projects.
    """
    try:
        # Reuse existing loader to set values so downstream views pick them up
        if hasattr(args, "jira_project_filter") and args.jira_project_filter:
            keys_raw = args.jira_project_filter
            keys = [k.strip() for k in str(keys_raw).split(",") if k.strip()]
            if keys:
                _config_loader.set_value("jira", "projects", keys)
                logger.info("Applied CLI Jira project filter: %s", keys)
        if hasattr(args, "disable_wpm_shim") and args.disable_wpm_shim:
            _config_loader.set_value("migration", "disable_wpm_shim", True)
            logger.info("Applied CLI: disable WorkPackageMigration runtime shim")
    except Exception as e:
        logger.warning("Failed applying CLI args to config: %s", e)


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
            "Missing required environment variables: %s",
            ", ".join(missing_vars),
        )
        return False

    return True


def update_from_cli_args(args: Any) -> None:
    """Update migration configuration from CLI arguments.

    Also applies select Jira/OpenProject overrides that must be available
    before importing heavy migration modules (e.g., project filters).

    Args:
        args: An object containing CLI arguments (typically from argparse)

    """
    # Apply Jira project filter early so downstream modules see it
    if hasattr(args, "jira_project_filter") and args.jira_project_filter:
        try:
            keys_raw = args.jira_project_filter
            keys = [k.strip() for k in str(keys_raw).split(",") if k.strip()]
            if keys:
                _config_loader.set_value("jira", "projects", keys)
                jira_config["projects"] = keys
                logger.info("Applied CLI Jira project filter: %s", keys)
        except Exception as e:
            logger.warning("Failed applying CLI jira project filter: %s", e)

    # Optional: disable WorkPackageMigration shim
    if hasattr(args, "disable_wpm_shim") and getattr(args, "disable_wpm_shim"):
        try:
            _config_loader.set_value("migration", "disable_wpm_shim", True)
            migration_config["disable_wpm_shim"] = True
            logger.info("Applied CLI: disable WorkPackageMigration runtime shim")
        except Exception as e:
            logger.warning("Failed applying CLI disable_wpm_shim: %s", e)
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


# User Migration Configuration Constants
FALLBACK_MAIL_DOMAIN = "noreply.migration.local"
USER_CREATION_TIMEOUT = 60  # seconds
USER_CREATION_BATCH_SIZE = 10
