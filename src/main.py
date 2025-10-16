
"""Main entry point for Jira to OpenProject migration tool.

This script provides a unified interface for running both migration
and export operations from a single command-line tool.
"""

import argparse
import atexit
import os
import sys
from pathlib import Path

from src.config import logger, update_from_cli_args

# Import migration functions from the new modules
"""
Main CLI entry for J2O. We intentionally avoid importing the heavy migration
module at top-level so that CLI flags (e.g., project filters, shim toggles)
can be applied to config before modules execute import-time logic.
"""


def validate_database_configuration() -> None:
    """Validate database configuration is properly set.

    Raises:
        SystemExit: If database configuration is invalid

    """
    try:
        from src.config_loader import ConfigLoader  # noqa: PLC0415

        # ConfigLoader initialization will raise RuntimeError if POSTGRES_PASSWORD is missing
        # or empty, so we don't need additional validation here
        ConfigLoader()
        logger.debug("Database configuration validated successfully")

    except RuntimeError as e:
        logger.error("Database configuration failed: %s", e)
        logger.error(
            "Please ensure POSTGRES_PASSWORD is set in your .env file "
            "or as a Docker secret at /run/secrets/postgres_password",
        )
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        logger.error("Unexpected error validating database configuration: %s", e)
        sys.exit(1)


def _pid_is_running(pid: int) -> bool:
    """Return True if a process with PID is running (and accessible)."""
    try:
        if pid <= 0:
            return False
        # On POSIX, signal 0 checks existence without sending a signal
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we may not have permission; treat as running
        return True
    except Exception:
        return False


def _ensure_singleton_lock(lock_file: Path) -> None:
    """Ensure only one migration runs at a time using a PID lock file.

    If a lock exists and the PID is alive, exit. If the PID is stale, remove it.
    The lock is removed automatically on process exit when possible.
    """
    if os.environ.get("J2O_DISABLE_LOCK") in {"1", "true", "True"}:
        logger.warning("Singleton lock disabled via J2O_DISABLE_LOCK=1")
        return

    lock_file.parent.mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()

    if lock_file.exists():
        try:
            existing = int(lock_file.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            existing = 0

        if existing and _pid_is_running(existing):
            logger.error(
                "Another migration instance is running (pid=%s). Lock: %s",
                existing,
                str(lock_file),
            )
            logger.error(
                "If this is stale, remove the lock or set J2O_DISABLE_LOCK=1 to override (not recommended).",
            )
            sys.exit(1)
        else:
            # Stale lock
            try:
                lock_file.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    # Create the lock exclusively if possible
    try:
        # Use 'x' mode to fail if file suddenly appears between exists() and here
        with lock_file.open("x", encoding="utf-8") as f:
            f.write(str(current_pid))
    except FileExistsError:
        # Another process raced us
        try:
            existing = int(lock_file.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            existing = 0
        logger.error(
            "Concurrent migration detected (pid=%s). Lock: %s",
            existing,
            str(lock_file),
        )
        sys.exit(1)

    def _cleanup_lock() -> None:
        try:
            # Only remove if the file still contains our PID to avoid clobbering
            content = lock_file.read_text(encoding="utf-8").strip()
            if content == str(current_pid):
                lock_file.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    atexit.register(_cleanup_lock)


def main() -> None:
    """Parse arguments and execute the appropriate command."""
    # Validate database configuration early to fail fast
    validate_database_configuration()

    # Create the top-level parser
    parser = argparse.ArgumentParser(
        description="Jira to OpenProject migration tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Create the parser for the "migrate" command
    migrate_parser = subparsers.add_parser(
        "migrate",
        help="Run the migration process from Jira to OpenProject",
    )

    # Add migration arguments
    migrate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without making changes to OpenProject",
    )
    migrate_parser.add_argument(
        "--components",
        nargs="+",
        help="Specific components to run (users, custom_fields, projects, etc.)",
    )
    migrate_parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip creating a backup before migration",
    )
    migrate_parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Force fresh extraction and mapping re-generation (skip disk caches). "
            "Does not force re-writing into OpenProject; keeps in-run in-memory caches; "
            "also overrides pre-migration validation/security gating."
        ),
    )
    migrate_parser.add_argument(
        "--restore",
        metavar="BACKUP_DIR",
        help="Restore from a backup directory instead of running migration",
    )
    migrate_parser.add_argument(
        "--tmux",
        action="store_true",
        help="Run in a tmux session for persistence",
    )
    migrate_parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop the migration on the first error or exception encountered",
    )
    migrate_parser.add_argument(
        "--reset-wp-checkpoints",
        action="store_true",
        help="Clear the work package fast-forward checkpoint store before running",
    )
    migrate_parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="Skip the 'Continue to next component' prompt and run all components without pausing",
    )
    migrate_parser.add_argument(
        "--profile",
        help=(
            "Named component profile to run (e.g., 'full', 'metadata_refresh'). "
            "Profiles expand to predefined component sequences."
        ),
    )

    # Project filtering (limit migration to specific Jira project keys)
    migrate_parser.add_argument(
        "--jira-project-filter",
        "--jira-project_filter",
        dest="jira_project_filter",
        metavar="KEYS",
        help=(
            "Comma-separated Jira project keys to migrate (e.g., 'NRBARCAMP,ADIC'). "
            "If omitted, all mapped projects are processed."
        ),
    )

    # Disable the WorkPackageMigration runtime shim
    migrate_parser.add_argument(
        "--disable-wpm-shim",
        action="store_true",
        dest="disable_wpm_shim",
        help="Disable the WorkPackageMigration runtime shim (require class run() to execute)",
    )

    # Parse arguments
    args = parser.parse_args()

    # Execute the appropriate command
    if args.command == "migrate":
        # Update configuration with CLI arguments BEFORE importing migration code
        update_from_cli_args(args)

        # Lazily import migration helpers now that config is updated
        from src.migration import (  # noqa: PLC0415
            PREDEFINED_PROFILES,
            restore_backup,
            run_migration,
            setup_tmux_session,
        )

        # Check if we're restoring from a backup
        if args.restore:
            success = restore_backup(args.restore)
            if not success:
                logger.error("Failed to restore from backup: %s", args.restore)
                sys.exit(1)
            logger.success("Successfully restored from backup: %s", args.restore)
            sys.exit(0)

        # Singleton lock: prevent concurrent runs (cleanup on exit)
        try:
            _ensure_singleton_lock(Path("var/run/j2o_migrate.pid"))
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to acquire singleton lock: %s", e)
            sys.exit(1)

        # Check if we should run in tmux
        if args.tmux:
            setup_tmux_session()

        profile_components: list[str] | None = None
        if args.profile:
            profile_key = args.profile.lower()
            if profile_key not in PREDEFINED_PROFILES:
                logger.error(
                    "Unknown profile '%s'. Available profiles: %s",
                    args.profile,
                    ", ".join(sorted(PREDEFINED_PROFILES.keys())),
                )
                sys.exit(1)
            profile_components = PREDEFINED_PROFILES[profile_key].copy()
            logger.info(
                "Using component profile '%s' with %d components",
                profile_key,
                len(profile_components),
            )

        components_to_run = args.components
        if profile_components:
            if components_to_run:
                ordered: list[str] = []
                for name in profile_components + components_to_run:
                    if name not in ordered:
                        ordered.append(name)
                components_to_run = ordered
            else:
                components_to_run = profile_components

        # Run the migration with new options
        import asyncio  # noqa: PLC0415

        result = asyncio.run(
            run_migration(
                components=components_to_run,
                stop_on_error=getattr(args, "stop_on_error", False),
                no_confirm=getattr(args, "no_confirm", False),
            ),
        )

        # Exit with appropriate code
        if hasattr(result, "overall"):
            # MigrationResult object
            if result.overall.get("status") == "success":
                sys.exit(0)
            else:
                sys.exit(1)
        # Plain dict (from tests)
        elif result.get("status") == "success":
            sys.exit(0)
        else:
            sys.exit(1)

    else:
        # No command specified, show help
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Migration interrupted by user")
        sys.exit(1)
    except (FileNotFoundError, PermissionError) as e:
        logger.error("File system error: %s", e)
        sys.exit(1)
    except (ConnectionError, TimeoutError) as e:
        logger.error("Network connectivity error: %s", e)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        logger.exception("Unexpected error occurred during migration: %s", e)
        sys.exit(1)
