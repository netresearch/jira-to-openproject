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

from src import config
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
from src.clients.openproject_rails_client import OpenProjectRailsClient
from src.mappings.mappings import Mappings

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
    data_dir = config.get_path("data")

    # If data directory doesn't exist, there's nothing to back up
    if not os.path.exists(data_dir):
        config.logger.warning("No data directory found, nothing to back up")
        return None

    # Create backup directory
    if not backup_dir:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_dir = os.path.join(config.get_path("backups"), f"backup_{timestamp}")

    os.makedirs(backup_dir, exist_ok=True)
    config.logger.info(f"Creating backup in: {backup_dir=}")

    # Copy all files from data directory to backup directory
    for file_name in os.listdir(data_dir):
        file_path = os.path.join(data_dir, file_name)
        if os.path.isfile(file_path):
            shutil.copy2(file_path, backup_dir)

    file_count = len(os.listdir(backup_dir))
    config.logger.info(f"Backup created with {file_count=} files")

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
    data_dir = config.get_path("data")

    if not os.path.exists(backup_dir):
        config.logger.error(f"Backup directory not found: {backup_dir=}")
        return False

    if not os.path.exists(data_dir):
        os.makedirs(data_dir)

    config.logger.info(f"Restoring from backup: {backup_dir=}")

    # Check for metadata file to verify it's a valid backup
    metadata_path = os.path.join(backup_dir, "backup_metadata.json")
    if os.path.exists(metadata_path):
        try:
            with open(metadata_path, "r") as f:
                metadata = json.load(f)
            config.logger.info(f"Backup was created on: {metadata.get('timestamp')}")
            files_count = len(metadata.get('files_backed_up', []))
            config.logger.info(f"Contains {files_count=} files")
        except Exception as e:
            config.logger.warning(f"Could not read backup metadata: {str(e)=}")

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

    config.logger.info(f"Restored {restored_count=} files from backup")
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
            config.logger.warning("Running in DRY RUN mode - no changes will be made to OpenProject", extra={"markup": True})
            time.sleep(1)  # Give the user a moment to see this warning
            mode = "DRY RUN"
        else:
            mode = "PRODUCTION"

        config.logger.info(f"Starting Jira to OpenProject migration - mode='{mode}'", extra={"markup": True})

        # Create a timestamp for this migration run
        migration_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        # Results object
        results = MigrationResult(
            components={},
            overall={
                "timestamp": migration_timestamp,
                "status": "success",  # Will be updated if any component fails
                "start_time": datetime.now().isoformat(),
                "input_params": {
                    "dry_run": dry_run,
                    "components": components,
                    "no_backup": no_backup,
                    "force": force,
                    "direct_migration": direct_migration,
                },
            }
        )

        # Create a backup if not disabled
        backup_path = None
        if not no_backup and not dry_run:
            config.logger.info("Creating backup before migration...", extra={"markup": True})
            backup_path = create_backup()
            if backup_path:
                results["overall"]["backup_path"] = backup_path
                config.logger.success(f"Backup created at: {backup_path}", extra={"markup": True})
            else:
                config.logger.warning("No backup created (no data to back up)", extra={"markup": True})

        # Initialize clients
        config.logger.info("Initializing API clients...", extra={"markup": True})
        jira_client = JiraClient()
        op_client = OpenProjectClient()

        # Initialize Rails client if direct migration is requested
        op_rails_client = None
        if direct_migration:
            config.logger.notice("Direct migration mode enabled - using Rails console for supported operations", extra={"markup": True})
            try:
                op_rails_client = OpenProjectRailsClient()
                # Check if Rails client is working
                config.logger.info("Testing Rails console connection...", extra={"markup": True})
                if op_rails_client.test_connection():
                    config.logger.success("Rails console connection successful", extra={"markup": True})
                else:
                    config.logger.error("Rails console connection test failed", extra={"markup": True})
            except Exception as e:
                config.logger.error(f"Failed to initialize Rails client: {str(e)}", extra={"markup": True})

                # Mark overall status as failed
                results["overall"]["status"] = "failed"
                results["overall"]["error"] = f"Rails client initialization failed: {str(e)}"

                # Add end time
                results["overall"]["end_time"] = datetime.now().isoformat()
                return results

        # Initialize mappings
        mappings = Mappings(
            data_dir=config.get_path("data"),
            jira_client=jira_client,
            op_client=op_client
        )

        # Define all available migration components
        available_components = {
            "users": UserMigration(jira_client, op_client),
            "custom_fields": CustomFieldMigration(jira_client, op_client, op_rails_client),
            "companies": CompanyMigration(jira_client, op_client),
            "accounts": AccountMigration(jira_client, op_client),
            "projects": ProjectMigration(jira_client, op_client),
            "link_types": LinkTypeMigration(jira_client, op_client),
            "issue_types": IssueTypeMigration(jira_client, op_client, op_rails_client),
            "status_types": StatusMigration(jira_client, op_client, op_rails_client),
            "work_packages": None,  # Initialized later if needed
        }

        # If components parameter is not provided, use default component order
        if not components:
            components = [
                "users",
                "custom_fields",
                "companies",
                "accounts",
                "projects",
                "link_types",
                "issue_types",
                "status_types",
                "work_packages",
            ]

        # Filter to keep only supported components
        components = [c for c in components if c in available_components]

        # Show which components will be run
        config.logger.info(f"Migration will run the following components in order: {components}", extra={"markup": True})

        # Initialize work package migration if it's in the components list
        if "work_packages" in components:
            from src.migrations.work_package_migration import WorkPackageMigration
            available_components["work_packages"] = WorkPackageMigration(jira_client, op_client, op_rails_client, data_dir=config.get_path("data"))

        # Run each component in order
        try:
            for component_name in components:
                # Get the component instance
                component = available_components.get(component_name)

                if not component:
                    config.logger.warning(f"Component {component_name} not found or not initialized, skipping", extra={"markup": True})
                    continue

                # Header for this component in logs
                print_component_header(component_name)

                # Track timing
                component_start_time = time.time()

                # Run the component
                try:
                    result = component.run(
                        dry_run=dry_run,
                        force=force,
                        mappings=mappings
                    )

                    if result:
                        # Add timing information (if not already present)
                        if "time" not in result:
                            result["time"] = time.time() - component_start_time

                        # Store result in the results dictionary
                        results["components"][component_name] = result

                        # Update overall status if component failed
                        if result.get("status") == "failed":
                            results["overall"]["status"] = "failed"

                        # Print component summary based on status
                        status = result.get("status", "unknown")
                        success_count = result.get("success_count", 0)
                        failed_count = result.get("failed_count", 0)
                        total_count = result.get("total_count", 0)

                        if status == "success":
                            config.logger.success(
                                f"Component '{component_name}' completed successfully "
                                f"({success_count}/{total_count} items migrated), "
                                f"took {result.get('time', 0):.2f} seconds",
                                extra={"markup": True}
                            )
                        else:
                            config.logger.error(
                                f"Component '{component_name}' failed or had errors "
                                f"({success_count}/{total_count} items migrated, {failed_count} failed), "
                                f"took {result.get('time', 0):.2f} seconds",
                                extra={"markup": True}
                            )
                    else:
                        # Handle case where component didn't return a result
                        config.logger.warning(f"Component '{component_name}' did not return a result", extra={"markup": True})
                        results["components"][component_name] = {
                            "status": "unknown",
                            "time": time.time() - component_start_time,
                            "message": "Component did not return a result"
                        }

                except KeyboardInterrupt:
                    # Handle user interruption within a component
                    config.logger.warning(f"Component '{component_name}' was interrupted by user", extra={"markup": True})
                    results["components"][component_name] = {
                        "status": "interrupted",
                        "time": time.time() - component_start_time,
                        "message": "Component was interrupted by user"
                    }
                    results["overall"]["status"] = "interrupted"
                    break

                except Exception as e:
                    # Handle unexpected errors during component execution
                    config.logger.error(f"Error during '{component_name}' migration: {str(e)}", extra={"markup": True, "traceback": True})
                    results["components"][component_name] = {
                        "status": "failed",
                        "time": time.time() - component_start_time,
                        "error": str(e),
                        "message": f"Error during component execution: {str(e)}"
                    }
                    results["overall"]["status"] = "failed"

                # Break out if the component failed and it's critical
                if results["components"].get(component_name, {}).get("status") == "failed":
                    if component_name in ["users", "projects"]:
                        config.logger.error(f"Critical component '{component_name}' failed, aborting migration", extra={"markup": True})
                        break

        except KeyboardInterrupt:
            # Handle user interruption at the top level
            config.logger.warning("Migration interrupted by user", extra={"markup": True})
            results["overall"]["status"] = "interrupted"
            results["overall"]["message"] = "Migration was interrupted by user"

        # Add end time to results
        results["overall"]["end_time"] = datetime.now().isoformat()

        # Calculate total time
        start_time = datetime.fromisoformat(results["overall"]["start_time"])
        end_time = datetime.fromisoformat(results["overall"]["end_time"])
        total_seconds = (end_time - start_time).total_seconds()
        results["overall"]["total_time_seconds"] = total_seconds

        # Print final status
        if results["overall"]["status"] == "success":
            config.logger.success(
                f"Migration completed successfully in {total_seconds:.2f} seconds.",
                extra={"markup": True}
            )
        else:
            config.logger.error(
                f"Migration completed with status '{results['overall']['status']}' in {total_seconds:.2f} seconds.",
                extra={"markup": True}
            )

        # Save results to file
        results_dir = config.ensure_subdir(config.get_path("results"))
        results_file = os.path.join(results_dir, f"migration_results_{migration_timestamp}.json")

        with open(results_file, "w") as f:
            json.dump(results, f, indent=2)

        config.logger.info(f"Migration results saved to {results_file}", extra={"markup": True})

        return results

    except Exception as e:
        # Handle unexpected errors at the top level
        config.logger.error(f"Unexpected error during migration: {str(e)}", extra={"markup": True, "traceback": True})

        # Create a basic result object
        return MigrationResult(
            components={},
            overall={
                "status": "failed",
                "error": str(e),
                "message": f"Unexpected error during migration: {str(e)}",
                "timestamp": datetime.now().isoformat(),
            }
        )


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
    parser.add_argument(
        "--update-mapping",
        action="store_true",
        help="Update custom field mapping after manual Ruby script execution",
    )
    return parser.parse_args()

def setup_tmux_session():
    """Create and set up a tmux session for Rails console."""
    from src import config  # Import config here to make it available

    session_name = config.openproject_config.get("tmux_session_name", "rails_console")

    config.logger.info(f"Setting up tmux session '{session_name}' for Rails console...", extra={"markup": True})

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
            config.logger.warning(f"tmux session '{session_name}' already exists", extra={"markup": True})
            config.logger.info("To attach to this session, run:", extra={"markup": True})
            config.logger.info(f"tmux attach -t {session_name}", extra={"markup": True})
            return True

        # Create a new session
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name],
            check=True
        )

        config.logger.success(f"Created tmux session '{session_name}'", extra={"markup": True})
        config.logger.info("To attach to this session, run:", extra={"markup": True})
        config.logger.info(f"tmux attach -t {session_name}", extra={"markup": True})

        # Determine if Docker is being used
        using_docker = "container" in config.openproject_config

        # Send commands to the session to set up Rails console
        if using_docker:
            container = config.openproject_config.get("container", "openproject")
            config.logger.info(f"Detected Docker setup with container '{container}'", extra={"markup": True})
            config.logger.info("Please manually run the following commands in the tmux session:", extra={"markup": True})
            config.logger.info(f"docker exec -it {container} bash", extra={"markup": True})
            config.logger.info("cd /app && bundle exec rails console", extra={"markup": True})
        else:
            server = config.openproject_config.get("server")
            if server:
                config.logger.info(f"Detected remote server '{server}'", extra={"markup": True})
                config.logger.info("Please manually run the following commands in the tmux session:", extra={"markup": True})
                config.logger.info(f"ssh {server}", extra={"markup": True})
                config.logger.info("cd /opt/openproject && bundle exec rails console", extra={"markup": True})
            else:
                config.logger.info("Please manually run the following command in the tmux session:", extra={"markup": True})
                config.logger.info("cd /path/to/openproject && bundle exec rails console", extra={"markup": True})

        config.logger.info("After running Rails console, you can use the direct migration features.", extra={"markup": True})

        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        config.logger.error("tmux is not installed or not available in PATH", extra={"markup": True})
        config.logger.info("Please install tmux first:", extra={"markup": True})
        config.logger.info("  On Ubuntu/Debian: sudo apt-get install tmux", extra={"markup": True})
        config.logger.info("  On CentOS/RHEL: sudo yum install tmux", extra={"markup": True})
        config.logger.info("  On macOS with Homebrew: brew install tmux", extra={"markup": True})
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

        # Handle update mapping if requested
        if args.update_mapping:
            jira_client = JiraClient()
            op_client = OpenProjectClient()

            # Ask the user which mapping to update
            print("\nWhich mapping would you like to update?")
            print("1. Custom Fields")
            print("2. Issue Types (Work Package Types)")

            try:
                choice = input("Enter choice (1 or 2): ")
                if choice == "1":
                    custom_field_migration = CustomFieldMigration(jira_client, op_client)
                    result = custom_field_migration.update_mapping_file(force=args.force)
                    if result:
                        config.logger.success("Custom field mapping updated successfully.")
                    else:
                        config.logger.warning("No updates were made to custom field mapping.")
                elif choice == "2":
                    issue_type_migration = IssueTypeMigration(jira_client, op_client)
                    result = issue_type_migration.update_mapping_file(force=args.force)
                    if result:
                        config.logger.success("Issue type mapping updated successfully.")
                    else:
                        config.logger.warning("No updates were made to issue type mapping.")
                else:
                    config.logger.error("Invalid choice. Please enter 1 or 2.")
            except KeyboardInterrupt:
                config.logger.warning("Operation cancelled by user.")

            return

        # dump args
        config.logger.debug(f"Args: {args}", extra={"markup": True})

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
                config.logger.success("Migration completed successfully", extra={"markup": True})
            elif overall_status == "interrupted":
                config.logger.warning("Migration was interrupted before completion", extra={"markup": True})
            else:
                config.logger.error("Migration completed with errors", extra={"markup": True})

            # Print component results
            config.logger.info("Component results:", extra={"markup": True})
            for component, comp_result in result.get("components", {}).items():
                status = comp_result.get("status", "unknown")
                if status == "success":
                    config.logger.success(f"✓ {component}: {status}", extra={"markup": True})
                elif status == "interrupted":
                    config.logger.warning(f"⚠ {component}: {status}", extra={"markup": True})
                else:
                    config.logger.error(f"✗ {component}: {status}", extra={"markup": True})

    except KeyboardInterrupt:
        print("\nMigration manually interrupted. Exiting...")
        sys.exit(0)

if __name__ == "__main__":
    main()
