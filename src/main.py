#!/usr/bin/env python3
"""Main entry point for Jira to OpenProject migration tool.

This script provides a unified interface for running both migration
and export operations from a single command-line tool.
"""

import argparse
import sys

from src.config import logger, update_from_cli_args

# Import migration functions from the new modules
from src.migration import restore_backup, run_migration, setup_tmux_session


def main() -> None:
    """Parse arguments and execute the appropriate command.
    """
    # Create the top-level parser
    parser = argparse.ArgumentParser(
        description="Jira to OpenProject migration tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Create the parser for the "migrate" command
    migrate_parser = subparsers.add_parser("migrate", help="Run the migration process from Jira to OpenProject")

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
        help="Force extraction of data even if it already exists",
    )
    migrate_parser.add_argument(
        "--restore",
        metavar="BACKUP_DIR",
        help="Restore from a backup directory instead of running migration",
    )
    migrate_parser.add_argument("--tmux", action="store_true", help="Run in a tmux session for persistence")

    # Parse arguments
    args = parser.parse_args()

    # Execute the appropriate command
    if args.command == "migrate":
        # Update configuration with CLI arguments
        update_from_cli_args(args)

        # Check if we're restoring from a backup
        if args.restore:
            success = restore_backup(args.restore)
            if not success:
                logger.error(f"Failed to restore from backup: {args.restore}")
                sys.exit(1)
            logger.success(f"Successfully restored from backup: {args.restore}")
            sys.exit(0)

        # Check if we should run in tmux
        if args.tmux:
            setup_tmux_session()

        # Run the migration
        result = run_migration(components=args.components)

        # Exit with appropriate code
        if result["overall"]["status"] == "success":
            sys.exit(0)
        else:
            sys.exit(1)

    else:
        # No command specified, show help
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
