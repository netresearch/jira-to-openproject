#!/usr/bin/env python3
"""
Main entry point for Jira to OpenProject migration tool.

This script provides a unified interface for running both migration
and export operations from a single command-line tool.
"""

import argparse
import sys

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.clients.openproject_rails_client import OpenProjectRailsClient
from src.config import logger, migration_config
from src.export import export_work_packages, import_work_packages_to_rails

# Import migration functions from the new modules
from src.migration import restore_backup, run_migration, setup_tmux_session


def main():
    """
    Parse arguments and execute the appropriate command.
    """
    # Create the top-level parser
    parser = argparse.ArgumentParser(
        description="Jira to OpenProject migration tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Create the parser for the "migrate" command
    migrate_parser = subparsers.add_parser(
        "migrate", help="Run the migration process from Jira to OpenProject"
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
        help="Force extraction of data even if it already exists",
    )
    migrate_parser.add_argument(
        "--direct-migration",
        action="store_true",
        help="Use direct Rails console execution for supported operations",
    )
    migrate_parser.add_argument(
        "--restore",
        metavar="BACKUP_DIR",
        help="Restore from a backup directory instead of running migration",
    )
    migrate_parser.add_argument(
        "--tmux", action="store_true", help="Run in a tmux session for persistence"
    )

    # Create the parser for the "export" command
    export_parser = subparsers.add_parser(
        "export",
        help="Export work packages from Jira to JSON files for import into OpenProject",
    )

    # Add export arguments
    export_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without making changes to OpenProject",
    )
    export_parser.add_argument(
        "--force",
        action="store_true",
        help="Force extraction of data even if it already exists",
    )
    export_parser.add_argument(
        "--projects",
        nargs="+",
        help="Specific projects to export (by Jira project key)",
    )

    # Create the parser for the "import" command
    import_parser = subparsers.add_parser(
        "import", help="Import work packages from exported JSON files into OpenProject"
    )

    # Add import arguments
    import_parser.add_argument(
        "--project", help="Import a specific project (by Jira project key)"
    )
    import_parser.add_argument(
        "--export-dir", help="Directory containing the exported JSON files"
    )

    # Parse arguments
    args = parser.parse_args()

    # Execute the appropriate command
    if args.command == "migrate":
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
        result = run_migration(
            dry_run=args.dry_run,
            components=args.components,
            no_backup=args.no_backup,
            force=args.force,
            direct_migration=args.direct_migration,
        )

        # Exit with appropriate code
        if result["overall"]["status"] == "success":
            sys.exit(0)
        else:
            sys.exit(1)

    elif args.command == "export":
        # Initialize clients
        jira_client = JiraClient()
        op_client = OpenProjectClient()
        op_rails_client = (
            OpenProjectRailsClient()
            if migration_config.get("use_rails_console")
            else None
        )

        # Run the export
        result = export_work_packages(
            jira_client=jira_client,
            op_client=op_client,
            op_rails_client=op_rails_client,
            dry_run=args.dry_run,
            force=args.force,
            project_keys=args.projects,
        )

        # Exit with appropriate code
        if result.get("status") == "success":
            sys.exit(0)
        else:
            sys.exit(1)

    elif args.command == "import":
        # Run the import
        result = import_work_packages_to_rails(
            export_dir=args.export_dir, project_key=args.project
        )

        # Exit with appropriate code
        if result.get("status") == "success":
            sys.exit(0)
        else:
            sys.exit(1)

    else:
        # No command specified, show help
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
