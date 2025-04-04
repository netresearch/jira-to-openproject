#!/usr/bin/env python3
"""
Master migration script for Jira to OpenProject migration.
This script orchestrates the complete migration process.
"""

import os
import sys
import argparse
import time
import json
import shutil
import subprocess
from datetime import datetime
from typing import Dict, List, Any, Literal, TypedDict, NotRequired, Optional

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__))))

from src.config import logger, get_path, ensure_subdir, migration_config
from src.display import console
from src.migrations.user_migration import run_user_migration
from src.migrations.company_migration import run_company_migration
from src.migrations.account_migration import run_account_migration
from src.migrations.project_migration import run_project_migration
from src.migrations.custom_field_migration import run_custom_field_migration
from src.migrations.workflow_migration import run_workflow_migration
from src.migrations.link_type_migration import run_link_type_migration
from src.migrations.issue_type_migration import run_issue_type_migration
from src.clients.openproject_client import OpenProjectClient
from src.clients.jira_client import JiraClient

# Attempt to import the Rails client, but don't fail if it's not available
try:
    from src.clients.openproject_rails_client import OpenProjectRailsClient
    HAS_RAILS_CLIENT = True
except ImportError:
    logger.info("OpenProjectRailsClient not available. Direct migration via Rails console will be disabled.")
    HAS_RAILS_CLIENT = False
    OpenProjectRailsClient = None # Define it as None if not available

# PEP 695 Type aliases
type BackupDir = str | None
type ComponentStatus = Literal["success", "failed", "interrupted"]
type ComponentName = Literal[
    "users", "custom_fields", "companies", "accounts", "projects",
    "link_types", "issue_types", "work_packages"
]

# TypedDict for structured results
class ComponentResult(TypedDict):
    status: ComponentStatus
    time: NotRequired[float]
    error: NotRequired[str]
    message: NotRequired[str]
    # Additional fields that might come from component migrations
    success_count: NotRequired[int]
    failed_count: NotRequired[int]
    total_count: NotRequired[int]

class MigrationResult(TypedDict):
    components: Dict[str, ComponentResult]
    overall: Dict[str, Any]

def print_component_header(component_name: str) -> None:
    """
    Print a formatted header for a migration component.

    Args:
        component_name: Name of the component to display
    """
    print("\n" + "=" * 50)
    print(f"  RUNNING COMPONENT: {component_name}")
    print("=" * 50 + "\n")

def create_backup(backup_dir: BackupDir = None) -> BackupDir:
    """
    Create a backup of the data directory before running the migration.

    Args:
        backup_dir: Directory to store the backup. If None, a timestamp directory is created.

    Returns:
        Path to the created backup directory
    """
    # Use the centralized config for var directories
    data_dir = get_path("data")

    # If data directory doesn't exist, there's nothing to back up
    if not os.path.exists(data_dir):
        logger.warning("No data directory found, nothing to back up")
        return None

    # Create backup directory
    if not backup_dir:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_dir = os.path.join(get_path("backups"), f"backup_{timestamp}")

    os.makedirs(backup_dir, exist_ok=True)
    logger.info(f"Creating backup in: {backup_dir=}")

    # Copy all files from data directory to backup directory
    for file_name in os.listdir(data_dir):
        file_path = os.path.join(data_dir, file_name)
        if os.path.isfile(file_path):
            shutil.copy2(file_path, backup_dir)

    file_count = len(os.listdir(backup_dir))
    logger.info(f"Backup created with {file_count=} files")

    # Save migration metadata to backup
    metadata = {
        "timestamp": datetime.now().isoformat(),
        "backup_dir": backup_dir,
        "files_backed_up": os.listdir(backup_dir),
    }

    with open(os.path.join(backup_dir, "backup_metadata.json"), "w") as f:
        json.dump(metadata, f, indent=2)

    return backup_dir


def restore_backup(backup_dir: str) -> bool:
    """
    Restore data from a backup directory.

    Args:
        backup_dir: Directory containing the backup

    Returns:
        True if restoration was successful, False otherwise
    """
    # Use the centralized config for var directories
    data_dir = get_path("data")

    if not os.path.exists(backup_dir):
        logger.error(f"Backup directory not found: {backup_dir=}")
        return False

    if not os.path.exists(data_dir):
        os.makedirs(data_dir)

    logger.info(f"Restoring from backup: {backup_dir=}")

    # Check for metadata file to verify it's a valid backup
    metadata_path = os.path.join(backup_dir, "backup_metadata.json")
    if os.path.exists(metadata_path):
        try:
            with open(metadata_path, "r") as f:
                metadata = json.load(f)
            logger.info(f"Backup was created on: {metadata.get('timestamp')}")
            files_count = len(metadata.get('files_backed_up', []))
            logger.info(f"Contains {files_count=} files")
        except Exception as e:
            logger.warning(f"Could not read backup metadata: {str(e)=}")

    # Copy all files from backup to data directory
    restored_count = 0
    for file_name in os.listdir(backup_dir):
        # Skip metadata file
        if file_name == "backup_metadata.json":
            continue

        file_path = os.path.join(backup_dir, file_name)
        if os.path.isfile(file_path):
            shutil.copy2(file_path, data_dir)
            restored_count += 1

    logger.info(f"Restored {restored_count=} files from backup")
    return True


def run_migration(
    dry_run: bool = False,
    components: Optional[List[str]] = None,
    no_backup: bool = False,
    force: bool = False,
    direct_migration: bool = False,
) -> MigrationResult:
    """
    Run the migration process.

    Args:
        dry_run: If True, no changes will be made to OpenProject
        components: List of specific components to run (if None, run all)
        no_backup: If True, skip creating a backup before migration
        force: If True, force extraction of data even if it already exists
        direct_migration: If True, use direct Rails console execution for supported operations, including work packages

    Returns:
        Dictionary with migration results
    """
    # Check if we need a migration mode header
    if dry_run:
        logger.warning("Running in DRY RUN mode - no changes will be made to OpenProject", extra={"markup": True})
        time.sleep(1)  # Give the user a moment to see this warning
        mode = "DRY RUN"
    else:
        mode = "PRODUCTION"

    logger.info(f"Starting Jira to OpenProject migration - mode='{mode}'", extra={"markup": True})

    # Create a timestamp for this migration run
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_dir = os.path.join(get_path("logs"), f"migration_{timestamp}")
    logger.info(f"log_dir='{log_dir}'", extra={"markup": True})

    # Store configuration for this migration run
    migration_config.update({
        "dry_run": dry_run,
        "components": components,
        "direct_migration": direct_migration,
        "direct_work_package_creation": direct_migration,  # Use the same flag for work package creation
        "timestamp": timestamp,
        "log_dir": log_dir,
    })

    # Setup components to run
    available_components = ["users", "custom_fields", "companies", "accounts", "projects", "link_types", "issue_types", "work_packages"]

    if components:
        components_to_run = [c for c in components if c in available_components]
        if not components_to_run:
            logger.error(f"No valid components specified. Available: {', '.join(available_components)}", extra={"markup": True})
            return {
                "components": {},
                "overall": {
                    "status": "error",
                    "message": "No valid components specified",
                    "timestamp": timestamp,
                    "dry_run": dry_run
                }
            }
        logger.info(f"Running selected components: {' '.join(components_to_run)}", extra={"markup": True})
    else:
        # Run all components in the specified order
        components_to_run = available_components
        logger.info(f"Running all components: {' '.join(components_to_run)}", extra={"markup": True})

    # Create backup before migration (unless skipped or we're in dry-run mode)
    if not no_backup and not dry_run:
        create_backup(timestamp)

    # Explain migration strategy
    logger.info("Migration strategy:", extra={"markup": True})
    logger.info("1. Migrate users and custom fields first", extra={"markup": True})
    logger.info("2. Migrate Tempo companies as top-level projects", extra={"markup": True})
    logger.info("3. Create custom fields for Tempo accounts", extra={"markup": True})
    logger.info("4. Migrate Jira projects with account information as custom fields", extra={"markup": True})
    logger.info("5. Migrate work packages with proper type assignments", extra={"markup": True})
    logger.info("This approach handles the many-to-many relationship between accounts and projects", extra={"markup": True})

    # Check Rails console availability if direct migration is requested
    if direct_migration:
        if not HAS_RAILS_CLIENT:
            logger.warning("Direct migration was requested but OpenProjectRailsClient is not available.", extra={"markup": True})
            logger.warning("Some operations may not work correctly in direct migration mode.", extra={"markup": True})
        else:
            # Initialize a client to test connection
            try:
                rails_client = OpenProjectRailsClient()
                logger.info("Rails console is available for direct migration.", extra={"markup": True})
            except Exception as e:
                logger.warning(f"Direct migration was requested but could not connect to Rails console: {str(e)}", extra={"markup": True})
                logger.warning("Make sure the tmux session is started and properly configured.", extra={"markup": True})
                logger.warning("Some operations may not work correctly in direct migration mode.", extra={"markup": True})

    # Run the migration components
    results = {}
    for component in components_to_run:
        if component in available_components:
            # Get the function for this component
            migration_component = {
                "users": run_user_migration,
                "custom_fields": run_custom_field_migration,
                "companies": run_company_migration,
                "accounts": run_account_migration,
                "projects": run_project_migration,
                "link_types": run_link_type_migration,
                "issue_types": run_issue_type_migration,
            }.get(component)
            if migration_component:
                try:
                    logger.info(f"Running migration component: name='{component}'", extra={"markup": True})

                    # Based on function signatures, only pass parameters that each function accepts
                    if component == "users":
                        component_result = migration_component(dry_run=dry_run)
                    elif component == "custom_fields":
                        component_result = migration_component(
                            dry_run=dry_run,
                            force=force,
                            direct_migration=direct_migration
                        )
                    elif component == "companies":
                        component_result = migration_component(
                            dry_run=dry_run,
                            force=force
                        )
                    elif component == "accounts":
                        component_result = migration_component(dry_run=dry_run)
                    elif component == "projects":
                        component_result = migration_component(dry_run=dry_run)
                    elif component == "link_types":
                        component_result = migration_component(dry_run=dry_run)
                    elif component == "issue_types":
                        component_result = migration_component(
                            dry_run=dry_run,
                            force=force,
                            direct_migration=direct_migration
                        )
                    else:
                        # Default case, trying with all parameters
                        component_result = migration_component(
                            dry_run=dry_run,
                            force=force,
                            direct_migration=direct_migration
                        )

                    results[component] = component_result
                except Exception as e:
                    logger.error(f"Error running component {component}: {str(e)}", extra={"markup": True})
                    results[component] = {"status": "error", "message": str(e)}
            else:
                # Only show error for components other than work_packages since it's handled separately
                if component != "work_packages":
                    logger.error(f"Invalid component: {component}", extra={"markup": True})

    # Handle work_packages component separately with our new approach
    if "work_packages" in components_to_run:
        try:
            print_component_header("Work Packages")
            # Import the export function from our new script
            from export_work_packages import export_work_packages

            # Run the export which now also handles import per project
            export_result = export_work_packages(dry_run=dry_run, force=force)

            if not dry_run:
                # Get summary counts
                total_issues = export_result.get('total_issues', 0)
                total_exported = export_result.get('total_exported', 0)
                total_created = export_result.get('total_created', 0)
                total_errors = export_result.get('total_errors', 0)

                logger.success(f"Work package migration completed. Created: {total_created}/{total_exported} work packages (errors: {total_errors})", extra={"markup": True})

                # Store results
                results["work_packages"] = {
                    "status": "success",
                    "total_issues": total_issues,
                    "total_exported": total_exported,
                    "created_count": total_created,
                    "error_count": total_errors
                }
            else:
                logger.success(f"Work package export completed (dry run). Found: {export_result.get('total_issues', 0)} issues", extra={"markup": True})

                # Store results for dry run
                results["work_packages"] = {
                    "status": "success",
                    "dry_run": True,
                    "total_issues": export_result.get('total_issues', 0),
                    "message": "Dry run completed successfully"
                }
        except Exception as e:
            logger.error(f"Work package migration failed: {str(e)}", extra={"markup": True})
            logger.exception("Work package migration failed")

            # Store error
            results["work_packages"] = {
                "status": "error",
                "message": str(e)
            }

    if results:
        logger.success("Migration completed", extra={"markup": True})
    else:
        logger.error("Migration failed - no components were executed", extra={"markup": True})

    return {
        "components": results,
        "overall": {
            "status": "success" if results else "error",
            "timestamp": timestamp,
            "dry_run": dry_run,
            "components_to_run": components_to_run
        }
    }

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Run Jira to OpenProject migration")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode (no changes to OpenProject)",
    )
    parser.add_argument(
        "--components",
        nargs="+",
        help="Specific components to run (space-separated list)",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip creating a backup before migration",
    )
    parser.add_argument(
        "--restore",
        dest="backup_dir",
        help="Restore from a backup directory instead of running migration",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force extraction of data even if it already exists",
    )
    parser.add_argument(
        "--direct-migration",
        action="store_true",
        help="Use direct Rails console execution for supported operations, including work packages",
    )
    parser.add_argument(
        "--setup-tmux",
        action="store_true",
        help="Create and setup a tmux session for Rails console use",
    )
    return parser.parse_args()

def setup_tmux_session():
    """Create and set up a tmux session for Rails console."""
    from src import config  # Import config here to make it available

    session_name = config.openproject_config.get("tmux_session_name", "rails_console")

    logger.info(f"Setting up tmux session '{session_name}' for Rails console...", extra={"markup": True})

    try:
        # Check if tmux is installed
        subprocess.run(["tmux", "-V"], check=True, capture_output=True)

        # Check if session already exists
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            check=False,
            capture_output=True
        )

        if result.returncode == 0:
            logger.warning(f"tmux session '{session_name}' already exists", extra={"markup": True})
            logger.info("To attach to this session, run:", extra={"markup": True})
            logger.info(f"tmux attach -t {session_name}", extra={"markup": True})
            return True

        # Create a new session
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name],
            check=True
        )

        logger.success(f"Created tmux session '{session_name}'", extra={"markup": True})
        logger.info("To attach to this session, run:", extra={"markup": True})
        logger.info(f"tmux attach -t {session_name}", extra={"markup": True})

        # Determine if Docker is being used
        using_docker = "container" in config.openproject_config

        # Send commands to the session to set up Rails console
        if using_docker:
            container = config.openproject_config.get("container", "openproject")
            logger.info(f"Detected Docker setup with container '{container}'", extra={"markup": True})
            logger.info("Please manually run the following commands in the tmux session:", extra={"markup": True})
            logger.info(f"docker exec -it {container} bash", extra={"markup": True})
            logger.info("cd /app && bundle exec rails console", extra={"markup": True})
        else:
            server = config.openproject_config.get("server")
            if server:
                logger.info(f"Detected remote server '{server}'", extra={"markup": True})
                logger.info("Please manually run the following commands in the tmux session:", extra={"markup": True})
                logger.info(f"ssh {server}", extra={"markup": True})
                logger.info("cd /opt/openproject && bundle exec rails console", extra={"markup": True})
            else:
                logger.info("Please manually run the following command in the tmux session:", extra={"markup": True})
                logger.info("cd /path/to/openproject && bundle exec rails console", extra={"markup": True})

        logger.info("After running Rails console, you can use the direct migration features.", extra={"markup": True})

        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        logger.error("tmux is not installed or not available in PATH", extra={"markup": True})
        logger.info("Please install tmux first:", extra={"markup": True})
        logger.info("  On Ubuntu/Debian: sudo apt-get install tmux", extra={"markup": True})
        logger.info("  On CentOS/RHEL: sudo yum install tmux", extra={"markup": True})
        logger.info("  On macOS with Homebrew: brew install tmux", extra={"markup": True})
        return False

def main():
    """Run the migration tool."""
    args = parse_args()

    # Handle tmux setup if requested
    if args.setup_tmux:
        setup_tmux_session()
        return

    # Handle backup restoration if requested
    if args.backup_dir:
        restore_backup(args.backup_dir)
        return

    # Run migration with provided arguments
    run_migration(
        dry_run=args.dry_run,
        components=args.components,
        no_backup=args.no_backup,
        force=args.force,
        direct_migration=args.direct_migration,
    )

if __name__ == "__main__":
    main()
