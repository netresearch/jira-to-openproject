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
from src.display import console, ProgressTracker, process_with_progress
from src.migrations.user_migration import UserMigration
from src.migrations.company_migration import CompanyMigration
from src.migrations.account_migration import AccountMigration
from src.migrations.project_migration import ProjectMigration
from src.migrations.custom_field_migration import CustomFieldMigration
from src.migrations.workflow_migration import WorkflowMigration
from src.migrations.link_type_migration import LinkTypeMigration
from src.migrations.issue_type_migration import IssueTypeMigration
from src.migrations.status_migration import StatusMigration
from src.clients.openproject_client import OpenProjectClient
from src.clients.jira_client import JiraClient
from src.mappings.mappings import Mappings

from src.clients.openproject_rails_client import OpenProjectRailsClient

# PEP 695 Type aliases
type BackupDir = str | None
type ComponentStatus = Literal["success", "failed", "interrupted"]
type ComponentName = Literal[
    "users", "custom_fields", "companies", "accounts", "projects",
    "link_types", "issue_types", "status_types", "work_packages"
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
    try:
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
            "force": force,
            "no_backup": no_backup,
        })

        # Setup components to run
        available_components = ["users", "custom_fields", "companies", "accounts", "projects", "link_types", "issue_types", "status_types", "work_packages"]

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

        # Initialize results and clients
        results = {}
        current_component = None

        # Instantiate clients
        jira_client = JiraClient()
        op_client = OpenProjectClient()

        # Create mappings object
        mappings = Mappings(
            data_dir=get_path("data"),
            jira_client=jira_client,
            op_client=op_client
        )

        # Connect to Rails console if direct migration is enabled
        rails_client = OpenProjectRailsClient()
        logger.info("Connected to Rails console", extra={"markup": True})

        # Run each component in order
        for component_name in components_to_run:
            # Print component header
            print_component_header(component_name)

            # Track the current component
            current_component = component_name

            # Find the appropriate class for the component
            MigrationClass = {
                "users": UserMigration,
                "custom_fields": CustomFieldMigration,
                "companies": CompanyMigration,
                "accounts": AccountMigration,
                "projects": ProjectMigration,
                "link_types": LinkTypeMigration,
                "issue_types": IssueTypeMigration,
                "status_types": StatusMigration,
                #"workflow": WorkflowMigration
            }.get(component_name)

            if MigrationClass:
                try:
                    logger.info(f"Running migration component: name='{component_name}'", extra={"markup": True})

                    # --- Instantiate and Run Component --- #
                    component_result = { "status": "error", "message": "Execution logic incomplete" } # Default

                    # Instantiate the class, passing required clients.
                    # Config flags (dry_run, force) are accessed via migration_config within methods.
                    if component_name == "users":
                        migration_instance = MigrationClass(
                            jira_client=jira_client,
                            op_client=op_client
                        )
                        # Call methods on the instance
                        migration_instance.extract_jira_users()
                        migration_instance.extract_openproject_users()
                        migration_instance.create_user_mapping()
                        if not migration_config.get("dry_run", False):
                            migration_instance.create_missing_users()
                        component_result = migration_instance.analyze_user_mapping()

                    elif component_name == "companies":
                        migration_instance = MigrationClass(
                            jira_client=jira_client,
                            op_client=op_client
                        )
                        migration_instance.extract_tempo_companies()
                        migration_instance.extract_openproject_projects()
                        migration_instance.create_company_mapping()
                        migration_instance.migrate_companies()
                        component_result = migration_instance.analyze_company_mapping()

                    elif component_name == "accounts":
                        migration_instance = MigrationClass(
                            jira_client=jira_client,
                            op_client=op_client,
                            op_rails_client=rails_client
                        )
                        migration_instance.extract_tempo_accounts()
                        migration_instance.extract_openproject_projects()
                        migration_instance.load_company_mapping()
                        migration_instance.create_account_mapping()
                        migration_instance.migrate_accounts()
                        component_result = migration_instance.analyze_account_mapping()

                    elif component_name == "projects":
                        migration_instance = MigrationClass(
                            jira_client=jira_client,
                            op_client=op_client
                        )
                        migration_instance.extract_jira_projects()
                        migration_instance.extract_openproject_projects()
                        migration_instance.load_account_mapping()
                        migration_instance.extract_project_account_mapping()
                        migration_instance.migrate_projects()
                        component_result = migration_instance.analyze_project_mapping()

                    elif component_name == "custom_fields":
                        migration_instance = MigrationClass(
                            jira_client=jira_client,
                            op_client=op_client,
                            rails_console=rails_client
                        )
                        migration_instance.create_custom_field_mapping(force=migration_config.get("force", False))
                        migration_instance.migrate_custom_fields(direct_migration=migration_config.get("direct_migration", False))
                        component_result = migration_instance.analyze_custom_field_mapping()

                    elif component_name == "link_types":
                        migration_instance = MigrationClass(
                            jira_client=jira_client,
                            op_client=op_client,
                        )
                        migration_instance.extract_jira_link_types()
                        migration_instance.extract_openproject_relation_types()
                        migration_instance.create_link_type_mapping()
                        migration_instance.migrate_link_types()
                        component_result = migration_instance.analyze_link_type_mapping()

                    elif component_name == "issue_types":
                        migration_instance = MigrationClass(
                            jira_client=jira_client,
                            op_client=op_client,
                            rails_console=rails_client
                        )
                        migration_instance.extract_jira_issue_types()
                        migration_instance.extract_openproject_work_package_types()
                        mapping = migration_instance.create_issue_type_mapping()
                        if migration_config.get("direct_migration", False):
                            migration_instance.migrate_issue_types_via_rails(window=0, pane=0)
                        elif not migration_config.get("dry_run", False):
                            types_to_create = {n: d for n, d in mapping.items() if d.get("openproject_id") is None}
                            if types_to_create:
                                logger.warning(f"IMPORTANT: {len(types_to_create)} work package types need to be created.")
                                try:
                                    script_path = migration_instance.generate_ruby_script(types_to_create)
                                    logger.warning(f"Run the generated script in Rails console: {script_path}")
                                except AttributeError:
                                    logger.warning("IssueTypeMigration missing generate_ruby_script method? Manual creation required or use --direct-migration.")
                        component_result = migration_instance.analyze_issue_type_mapping()

                    elif component_name == "status_types":
                        migration_instance = MigrationClass(
                            jira_client=jira_client,
                            op_client=op_client,
                            op_rails_client=rails_client,
                            mappings=mappings,
                            data_dir=get_path("data")
                        )
                        migration_instance.extract_jira_statuses()
                        migration_instance.extract_status_categories()
                        migration_instance.get_openproject_statuses()
                        migration_instance.create_status_mapping()
                        migration_result = migration_instance.migrate_statuses()
                        component_result = migration_instance.analyze_status_mapping()

                    elif component_name == "workflow":
                        migration_instance = MigrationClass(
                            jira_client=jira_client,
                            op_client=op_client
                        )
                        migration_instance.extract_jira_workflows()
                        migration_instance.extract_jira_statuses()
                        migration_instance.extract_openproject_statuses()
                        migration_instance.create_status_mapping()
                        migration_instance.migrate_statuses()
                        migration_instance.create_workflow_configuration()
                        component_result = migration_instance.analyze_status_mapping()
                    elif component_name == "work_packages":
                        component_result = {
                            "status": "delayed",
                            "message": "Work packages migration delayed",
                            "total_issues": 0,
                            "total_exported": 0,
                            "created_count": 0,
                            "error_count": 0
                        }
                    else:
                        # Fallback logic removed as all components should be handled explicitly now
                        logger.error(f"Execution logic for component '{component_name}' is not defined.")
                        component_result = {"status": "error", "message": f"Unknown component: {component_name}"}

                    results[component_name] = component_result

                except Exception as e:
                    logger.error(f"Error running component {component_name}: {str(e)}", extra={"markup": True})
                    logger.exception(f"Component {component_name} failed")
                    results[component_name] = {"status": "error", "message": str(e)}
            else:
                 logger.error(f"Invalid component specified or class not found: {component_name}", extra={"markup": True})

        # Handle work_packages component separately
        if "work_packages" in components_to_run:
            try:
                print_component_header("Work Packages")
                # Track the current component
                current_component = "work_packages"

                # Import the export function from our new script
                from export_work_packages import export_work_packages

                # Run the export which now also handles import per project
                export_result = export_work_packages(
                    jira_client=jira_client,
                    op_client=op_client,
                    op_rails_client=rails_client,
                    dry_run=dry_run,
                    force=force
                )

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

        # Check for any failed components
        has_failures = any(r.get("status") == "error" for r in results.values())

        return {
            "components": results,
            "overall": {
                "status": "error" if has_failures else "success",
                "timestamp": timestamp,
                "dry_run": dry_run,
                "components_to_run": components_to_run
            }
        }
    except KeyboardInterrupt:
        print("\nMigration interrupted by user.")

        # Set in_progress components to interrupted
        if 'current_component' in locals() and 'results' in locals():
            if current_component not in results:
                results[current_component] = {
                    "status": "interrupted",
                    "time": time.time() - start_time if 'start_time' in locals() else 0
                }

        # Return processed results so far with interrupted status
        return {
            "components": results if 'results' in locals() else {},
            "overall": {
                "status": "interrupted",
                "timestamp": timestamp if 'timestamp' in locals() else datetime.now().strftime("%Y-%m-%d_%H-%M-%S"),
                "dry_run": dry_run,
                "components_to_run": components_to_run if 'components_to_run' in locals() else []
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

    try:
        # Handle tmux setup if requested
        if args.setup_tmux:
            setup_tmux_session()
            return

        # Handle backup restoration if requested
        if args.backup_dir:
            restore_backup(args.backup_dir)
            return


        # dump args
        logger.debug(f"Args: {args}", extra={"markup": True})

        # Run migration with provided arguments
        result = run_migration(
            dry_run=args.dry_run,
            components=args.components,
            no_backup=args.no_backup,
            force=args.force,
            direct_migration=args.direct_migration,
        )

        # Display migration results summary
        if result:
            overall_status = result.get("overall", {}).get("status", "unknown")

            # Show summary header based on status
            if overall_status == "success":
                logger.success("Migration completed successfully", extra={"markup": True})
            elif overall_status == "interrupted":
                logger.warning("Migration was interrupted before completion", extra={"markup": True})
            else:
                logger.error("Migration completed with errors", extra={"markup": True})

            # Print component results
            logger.info("Component results:", extra={"markup": True})
            for component, comp_result in result.get("components", {}).items():
                status = comp_result.get("status", "unknown")
                if status == "success":
                    logger.success(f"✓ {component}: {status}", extra={"markup": True})
                elif status == "interrupted":
                    logger.warning(f"⚠ {component}: {status}", extra={"markup": True})
                else:
                    logger.error(f"✗ {component}: {status}", extra={"markup": True})

    except KeyboardInterrupt:
        print("\nMigration manually interrupted. Exiting...")
        sys.exit(0)

if __name__ == "__main__":
    main()
