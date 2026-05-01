"""Configuration module for the Jira to OpenProject migration.

Provides a centralized configuration interface using ``ConfigLoader``.

Phase 6a of ADR-002: this module is **inert at import time**. It computes
path constants, registers extended-logger level names, and exposes
``Settings``-style accessors — but does not touch the filesystem, attach
``FileHandler`` instances, or rotate log files. Those side effects live
in :mod:`src.config._bootstrap` and are triggered explicitly by CLI
entry points via :func:`bootstrap`.

Rationale: import-time ``logging.FileHandler`` failed with
``PermissionError`` on container UID mismatches, making the package
un-importable in some CI environments. Phase 6a removes the root cause.

The ``_MappingsProxy`` shim and ``get_mappings()`` lazy initializer remain
unchanged — tests rely on ``monkeypatch.setattr(cfg, "mappings", ...)``
to replace the proxy without touching the underlying singleton. Per
ADR-002, this proxy is removed in Phase 7.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from src.config_loader import ConfigLoader
from src.type_definitions import Config, ConfigValue, DirType, LogLevel, SectionName

if TYPE_CHECKING:
    from src.display import ExtendedLogger
    from src.mappings.mappings import Mappings

# Import error recovery specific config
# Re-export bootstrap so callers can do ``from src.config import bootstrap``.
from ._bootstrap import bootstrap, is_bootstrapped, reset_bootstrap_state
from .error_recovery_config import ErrorRecoveryConfig, load_error_recovery_config

# Create a singleton instance of ConfigLoader
_config_loader = ConfigLoader()

# Extract configuration sections for easy access
jira_config = _config_loader.get_jira_config()
openproject_config = _config_loader.get_openproject_config()
migration_config = _config_loader.get_migration_config()
_cli_args: dict[str, Any] = {}

# Set up the var directory structure (pure path computation — no I/O).
root_dir = Path(__file__).parent.parent.parent
var_dir = root_dir / "var"

# Define all var directories. Path objects are values, not side effects;
# directories are created by ``bootstrap()``, not at import.
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

# Logging level used by ``bootstrap()`` to configure handlers. Read here
# so the constant is available to callers that want to introspect it
# without bootstrapping.
LOG_LEVEL: LogLevel = migration_config.get("log_level", "DEBUG")  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Custom log level registration (no I/O)
# ---------------------------------------------------------------------------
# The migration codebase uses ``logger.success(...)`` and ``logger.notice(...)``
# extensively. These extended methods are normally installed by
# ``configure_logging`` — but that function also attaches handlers, which is
# a side effect we want to defer to ``bootstrap()``. To keep
# ``from src.config import logger`` honest (callers can use
# ``logger.success`` immediately), we register *only* the level names and
# methods at import time. Handler attachment still waits for ``bootstrap()``.
# This is pure in-memory state — no filesystem access, no
# ``logging.basicConfig`` call.

logging.addLevelName(25, "SUCCESS")
logging.addLevelName(21, "NOTICE")


def _success(
    self: logging.Logger,
    message: str,
    *args: object,
    **kwargs: object,
) -> None:
    """Bound ``Logger.success`` method — INFO < SUCCESS < WARNING."""
    if self.isEnabledFor(25):
        existing_extra = kwargs.get("extra")
        extra_mapping: dict[str, object] = {}
        if isinstance(existing_extra, dict):
            extra_mapping.update(existing_extra)  # type: ignore[arg-type]
        extra_mapping["markup"] = True
        self._log(25, f"[success]{message}[/]", args, extra=extra_mapping, stacklevel=2)


def _notice(
    self: logging.Logger,
    message: str,
    *args: object,
    **kwargs: object,
) -> None:
    """Bound ``Logger.notice`` method — DEBUG < NOTICE < INFO."""
    if self.isEnabledFor(21):
        existing_extra = kwargs.get("extra")
        extra_mapping: dict[str, object] = {}
        if isinstance(existing_extra, dict):
            extra_mapping.update(existing_extra)  # type: ignore[arg-type]
        extra_mapping["markup"] = True
        self._log(21, message, args, extra=extra_mapping, stacklevel=2)


# Attach to the Logger class so every logger gets the extended methods.
# Idempotent — re-assigning the same function on re-import is a no-op.
logging.Logger.success = _success  # type: ignore[attr-defined,assignment]
logging.Logger.notice = _notice  # type: ignore[attr-defined,assignment]


# Logger is configured lazily by ``bootstrap()``. Until then, it is a plain
# Python logger with no file handler — output goes to the root logger's
# default handlers (typically stderr). This avoids import-time
# ``PermissionError`` when the log file is not writable (e.g., container
# UID mismatch in CI).
logger: ExtendedLogger = cast("ExtendedLogger", logging.getLogger("migration"))


# Export logger for use by other modules
__all__ = [
    "FALLBACK_MAIL_DOMAIN",
    "LOG_LEVEL",
    "USER_CREATION_BATCH_SIZE",
    "USER_CREATION_TIMEOUT",
    "ErrorRecoveryConfig",
    "bootstrap",
    "ensure_subdir",
    "get_config",
    "get_mappings",
    "get_path",
    "get_value",
    "is_bootstrapped",
    "jira_config",
    "load_error_recovery_config",
    "logger",
    "mappings",
    "migration_config",
    "openproject_config",
    "reset_bootstrap_state",
    "reset_mappings",
    "update_from_cli_args",
    "validate_config",
    "var_dirs",
]


# ==================================================================================
# MAPPINGS MANAGEMENT - Expert-validated solution for compliance violations
# ==================================================================================

# Global mappings state management with thread safety
_mappings: Mappings | None = None
_mappings_lock = threading.Lock()


class MappingsInitializationError(Exception):
    """Raised when mappings cannot be initialized.

    This custom exception provides clear diagnostics when mappings initialization
    fails, following proper exception-based error handling patterns.
    """


def get_mappings() -> Mappings:
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
    global _mappings  # noqa: PLW0603
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
                        "Failed to initialize mappings with data_dir=%s",
                        data_dir,
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
    global _mappings  # noqa: PLW0603
    with _mappings_lock:
        _mappings = None
        logger.debug("Reset mappings cache for testing")


class _MappingsProxy:
    """Proxy class for backward compatibility with config.mappings access pattern.

    This proxy automatically delegates all attribute access to the actual
    Mappings instance, providing seamless backward compatibility while
    maintaining proper lazy initialization and exception handling.
    """

    def __getattr__(self, name: str) -> object:
        """Delegate all attribute access to the actual Mappings instance."""
        return getattr(get_mappings(), name)

    def __setattr__(self, name: str, value: object) -> None:
        """Delegate all attribute setting to the actual Mappings instance."""
        setattr(get_mappings(), name, value)


# Backward compatibility - seamless access for existing code using config.mappings
mappings = _MappingsProxy()


# Expose the function to get the full config
def get_config() -> Config:
    """Get the complete configuration object."""
    return _config_loader.get_config()


def get_value(
    section: SectionName,
    key: str,
    default: ConfigValue | None = None,
) -> ConfigValue | None:
    """Get a specific configuration value."""
    return _config_loader.get_value(section, key, default)


def get_path(path_type: DirType) -> Path:
    """Get a specific path from var_dirs."""
    if path_type not in var_dirs:
        msg = f"Invalid path type: {path_type}"
        raise ValueError(msg)

    return var_dirs[path_type]


# Removed duplicate `update_from_cli_args` (see below) to avoid F811


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
        # Use a generic mapping type to avoid inconsistent TypedDict union issues
        from collections.abc import Mapping  # local import to avoid top pollution
        from typing import Any as _Any

        match section:
            case "jira":
                config_section: Mapping[str, _Any] = jira_config
                prefix = "J2O_JIRA_"
            case "openproject":
                config_section = openproject_config
                prefix = "J2O_OPENPROJECT_"
                # Special handling for OpenProject authentication
                if not (config_section.get("api_token") or config_section.get("api_key")):
                    missing_vars.append(f"{prefix}API_TOKEN or {prefix}API_KEY")
                continue
            case _:
                continue

        missing_vars.extend(f"{prefix}{key.upper()}" for key in required_keys if not config_section.get(key))

    if missing_vars:
        logger.error(
            "Missing required environment variables: %s",
            ", ".join(missing_vars),
        )
        return False

    return True


def update_from_cli_args(args: object) -> None:
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
        except Exception:
            logger.warning("Failed applying CLI jira project filter")

    # Optional: disable WorkPackageMigration shim
    if hasattr(args, "disable_wpm_shim") and args.disable_wpm_shim:
        try:
            _config_loader.set_value("migration", "disable_wpm_shim", value=True)
            migration_config["disable_wpm_shim"] = True
            logger.info("Applied CLI: disable WorkPackageMigration runtime shim")
        except Exception:
            logger.warning("Failed applying CLI disable_wpm_shim")
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

    if hasattr(args, "reset_wp_checkpoints") and args.reset_wp_checkpoints:
        migration_config["reset_wp_checkpoints"] = True
        logger.debug("Setting reset_wp_checkpoints=True from CLI arguments")

    if hasattr(args, "no_confirm") and args.no_confirm:
        migration_config["no_confirm"] = True
        logger.debug("Setting no_confirm=True from CLI arguments")

    # Add any other CLI arguments that should affect configuration here


# User Migration Configuration Constants
FALLBACK_MAIL_DOMAIN = "noreply.migration.local"
USER_CREATION_TIMEOUT = 60  # seconds
USER_CREATION_BATCH_SIZE = 10
