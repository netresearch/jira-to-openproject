 
"""Main entry point for Jira to OpenProject migration tool.

This script provides a unified interface for running both migration
and export operations from a single command-line tool.
"""

import argparse
import sys

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
        "--no-confirm",
        action="store_true",
        help="Skip the 'Continue to next component' prompt and run all components without pausing",
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
        from src.migration import restore_backup, run_migration, setup_tmux_session  # noqa: PLC0415

        # Check if we're restoring from a backup
        if args.restore:
            success = restore_backup(args.restore)
            if not success:
                logger.error("Failed to restore from backup: %s", args.restore)
                sys.exit(1)
            logger.success("Successfully restored from backup: %s", args.restore)
            sys.exit(0)

        # Check if we should run in tmux
        if args.tmux:
            setup_tmux_session()

        # Run the migration with new options
        import asyncio  # noqa: PLC0415

        result = asyncio.run(
            run_migration(
                components=args.components,
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
