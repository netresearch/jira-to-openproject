#!/usr/bin/env python3
"""Master migration script for Jira to OpenProject migration.
This script orchestrates the complete migration process.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from rich.console import Console

from src import config
from src.clients.docker_client import DockerClient
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.clients.rails_console_client import RailsConsoleClient
from src.clients.ssh_client import SSHClient
from src.mappings.mappings import Mappings
from src.migrations.account_migration import AccountMigration
from src.migrations.company_migration import CompanyMigration
from src.migrations.custom_field_migration import CustomFieldMigration
from src.migrations.issue_type_migration import IssueTypeMigration
from src.migrations.link_type_migration import LinkTypeMigration
from src.migrations.project_migration import ProjectMigration
from src.migrations.status_migration import StatusMigration
from src.migrations.user_migration import UserMigration
from src.migrations.work_package_migration import WorkPackageMigration
from src.models import ComponentResult, MigrationResult
from src.type_definitions import BackupDir, ComponentName
from src.utils import data_handler

if TYPE_CHECKING:
    from src.migrations.base_migration import BaseMigration

console = Console()


class AvailableComponents(TypedDict):
    """Available components for the migration."""

    users: UserMigration
    custom_fields: CustomFieldMigration
    companies: CompanyMigration
    projects: ProjectMigration
    link_types: LinkTypeMigration
    issue_types: IssueTypeMigration
    status_types: StatusMigration
    work_packages: WorkPackageMigration
    accounts: AccountMigration


def print_component_header(component_name: str) -> None:
    """Print a formatted header for a migration component.

    Args:
        component_name: Name of the component to display

    """
    console.rule(f"RUNNING COMPONENT: {component_name}")


def create_backup(backup_dir: BackupDir | None = None) -> BackupDir:
    """Create a backup of the data directory before running the migration.

    Args:
        backup_dir: Directory to store the backup.
        If None, a timestamp directory is created.

    Returns:
        Path to the created backup directory

    """
    # Use the centralized config for var directories
    data_dir: Path = config.get_path("data")

    backup_timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
    # Create backup directory
    if not backup_dir:
        backup_dir = Path(config.get_path("backups")) / f"backup_{backup_timestamp}"

    backup_dir.mkdir(parents=True, exist_ok=True)
    config.logger.info("Creating backup in: %s", backup_dir)

    # Copy all files from data directory to backup directory
    for file_path in data_dir.iterdir():
        if file_path.is_file():
            shutil.copy2(file_path, backup_dir)

    backup_files = list(backup_dir.iterdir())
    file_count = len(backup_files)
    config.logger.info("Backup created with %s files", file_count)

    # Save migration metadata to backup
    metadata = {
        "timestamp": backup_timestamp,
        "backup_dir": str(backup_dir),
        "files_backed_up": [file.name for file in backup_files],
    }

    with (backup_dir / "backup_metadata.json").open("w") as f:
        json.dump(metadata, f, indent=2)

    return backup_dir


def restore_backup(backup_dir: Path) -> bool:
    """Restore data from a backup directory.

    Args:
        backup_dir: Directory containing the backup

    Returns:
        True if restoration was successful, False otherwise

    """
    # Use the centralized config for var directories
    data_dir: Path = config.get_path("data")

    if not backup_dir.exists():
        config.logger.error("Backup directory not found: %s", backup_dir)
        return False

    if not data_dir.exists():
        data_dir.mkdir(parents=True, exist_ok=True)

    config.logger.info("Restoring from backup: %s", backup_dir)

    # Check for metadata file to verify it's a valid backup
    metadata_path = backup_dir / "backup_metadata.json"
    if metadata_path.exists():
        try:
            with metadata_path.open("r") as f:
                metadata = json.load(f)
            config.logger.info("Backup was created on: %s", metadata.get("timestamp"))
            files_count = len(metadata.get("files_backed_up", []))
            config.logger.info("Contains %s files", files_count)
        except Exception as e:
            config.logger.warning("Could not read backup metadata: %s", e)

    # Copy all files from backup to data directory
    restored_count = 0
    for file_path in backup_dir.iterdir():
        # Skip metadata file
        if file_path.name == "backup_metadata.json":
            continue

        if file_path.is_file():
            shutil.copy2(file_path, data_dir)
            restored_count += 1

    config.logger.info("Restored %s files from backup", restored_count)
    return True


def run_migration(
    components: list[ComponentName] | None = None,
    stop_on_error: bool = False,
    no_confirm: bool = False,
) -> MigrationResult:
    """Run the migration process.

    Args:
        components: List of specific components to run (if None, run all)
        stop_on_error: If True, stop migration on the first error/exception
        no_confirm: If True, skip the 'Continue to next component' prompt

    Returns:
        Dictionary with migration results

    """
    try:
        # Check if we need a migration mode header
        if config.migration_config.get("dry_run", False):
            config.logger.warning(
                "Running in DRY RUN mode - no changes will be made to OpenProject",
            )
            time.sleep(1)  # Give the user a moment to see this warning
            mode = "DRY RUN"
        else:
            mode = "PRODUCTION"

        config.logger.info(
            f"Starting Jira to OpenProject migration - mode='{mode}'",
        )

        # Create a timestamp for this migration run
        migration_timestamp = datetime.now(tz=UTC).strftime("%Y-%m-%d_%H-%M-%S")

        # Results object
        results = MigrationResult(
            overall={
                "timestamp": migration_timestamp,
                "status": "success",  # Will be updated if any component fails
                "start_time": datetime.now(tz=UTC).isoformat(),
                "input_params": {
                    "dry_run": config.migration_config.get("dry_run", False),
                    "components": components,
                    "no_backup": config.migration_config.get("no_backup", False),
                    "force": config.migration_config.get("force", False),
                },
                "confirm_after_component": True,  # Enable confirmation between components
            },
        )

        # Create a backup if not disabled
        backup_path = None
        if not config.migration_config.get(
            "no_backup", False
        ) and not config.migration_config.get("dry_run", False):
            config.logger.info("Creating backup before migration...")
            backup_path = create_backup()
            if backup_path:
                results.overall["backup_path"] = backup_path
                config.logger.success("Backup created at: %s", backup_path)
            else:
                config.logger.warning("No backup created (no data to back up)")

        # Initialize clients
        config.logger.info("Initializing API clients...")

        # Debug: print available config keys to help identify the correct ones
        config.logger.info(
            "Available OpenProject config keys: %s",
            list(config.openproject_config.keys()),
        )

        # Check if we're running in mock mode
        mock_mode = os.environ.get("J2O_TEST_MOCK_MODE", "false").lower() == "true"
        if mock_mode:
            config.logger.info(
                "Running in MOCK MODE - using mock clients instead of real connections"
            )

        # Create clients in the correct hierarchical order
        if mock_mode:
            # Create mock clients that don't require real connections
            from tests.integration.test_file_transfer_chain import (
                MockDockerClient,
                MockRailsConsoleClient,
                MockSSHClient,
            )

            ssh_client = MockSSHClient()
            docker_client = MockDockerClient()
            rails_client = MockRailsConsoleClient()

            config.logger.info("Mock clients initialized successfully")
        else:
            # 1. First, create the SSH client which is the foundation
            ssh_client = SSHClient(
                host=str(
                    config.openproject_config.get("server", "openproject.local"),
                ),
                user=config.openproject_config.get("user", None),
                key_file=(
                    str(config.openproject_config.get("key_file", ""))
                    if config.openproject_config.get("key_file")
                    else None
                ),
            )

            # 2. Next, create the Docker client using the SSH client
            docker_client = DockerClient(
                container_name=str(
                    config.openproject_config.get(
                        "container_name",
                        config.openproject_config.get("container", "openproject"),
                    ),
                ),
                ssh_client=ssh_client,
            )

            # 3. Create the Rails console client
            rails_client = RailsConsoleClient(
                tmux_session_name=config.openproject_config.get(
                    "tmux_session_name", "rails_console"
                ),
            )

        # 4. Finally, create the Jira client and OpenProject client (which uses the other clients)
        jira_client = JiraClient()

        if mock_mode:
            # For mock mode, create a simplified OpenProject client that doesn't require real connections
            op_client = OpenProjectClient(
                container_name=config.openproject_config.get(
                    "container", "mock_container"
                ),
                ssh_host=config.openproject_config.get("server", "mock_server"),
                ssh_user=config.openproject_config.get("user", "mock_user"),
                tmux_session_name=config.openproject_config.get(
                    "tmux_session_name", "mock_session"
                ),
                ssh_client=ssh_client,
                docker_client=docker_client,
                rails_client=rails_client,
            )
        else:
            op_client = OpenProjectClient(
                container_name=config.openproject_config.get("container", None),
                ssh_host=config.openproject_config.get("server", None),
                ssh_user=config.openproject_config.get("user", None),
                tmux_session_name=config.openproject_config.get(
                    "tmux_session_name", None
                ),
            )

        config.logger.success("All clients initialized successfully")

        # Initialize mappings
        config.mappings = Mappings(data_dir=config.get_path("data"))

        # Define all available migration components
        # Initialize work_packages with a dummy instance that will be replaced later
        available_components = AvailableComponents(
            users=UserMigration(jira_client=jira_client, op_client=op_client),
            custom_fields=CustomFieldMigration(
                jira_client=jira_client, op_client=op_client
            ),
            companies=CompanyMigration(jira_client=jira_client, op_client=op_client),
            projects=ProjectMigration(jira_client=jira_client, op_client=op_client),
            link_types=LinkTypeMigration(jira_client=jira_client, op_client=op_client),
            issue_types=IssueTypeMigration(
                jira_client=jira_client, op_client=op_client
            ),
            status_types=StatusMigration(jira_client=jira_client, op_client=op_client),
            work_packages=WorkPackageMigration(
                jira_client=jira_client, op_client=op_client
            ),  # Will be re-initialized if needed
            accounts=AccountMigration(jira_client=jira_client, op_client=op_client),
        )

        # If components parameter is not provided, use default component order
        if not components:
            default_components: list[ComponentName] = [
                "users",
                "custom_fields",
                "companies",
                "projects",
                "link_types",
                "issue_types",
                "status_types",
                "work_packages",
            ]

            # Add accounts only if it's available
            if "accounts" in available_components:
                # Add accounts after companies and before projects
                default_components.insert(3, "accounts")

            components = default_components

        # Filter to keep only supported components
        components = [c for c in components if c in available_components]

        # Show which components will be run
        config.logger.info(
            "Migration will run the following components in order: %s", components
        )

        # Initialize work package migration if it's in the components list
        if "work_packages" in components:
            available_components["work_packages"] = WorkPackageMigration(
                jira_client=jira_client,
                op_client=op_client,
            )

        # Run each component in order
        try:
            for component_name in components:
                # Get the component instance
                component: BaseMigration = available_components.get(component_name)

                # Header for this component in logs
                print_component_header(component_name)

                # Track timing
                component_start_time = time.time()

                # Run the component
                try:
                    component_result = component.run()

                    if component_result:
                        # Add timing information (if not already present)
                        if "time" not in component_result:
                            component_result["time"] = (
                                time.time() - component_start_time
                            )

                        # Store result in the results dictionary
                        results.components[component_name] = component_result

                        # Update overall status if component failed
                        if not component_result.success:
                            results.overall["status"] = "failed"

                        # Print component summary based on details dictionary
                        details = component_result.details or {}
                        success_count = details.get("success_count", 0)
                        failed_count = details.get("failed_count", 0)
                        total_count = details.get("total_count", 0)
                        component_time = details.get("time", 0)

                        if component_result.success:
                            config.logger.success(
                                f"Component '{component_name}' completed successfully "
                                f"({success_count}/{total_count} items migrated), "
                                f"took {component_time:.2f} seconds",
                            )
                        else:
                            config.logger.error(
                                f"Component '{component_name}' failed or had errors "
                                f"({success_count}/{total_count} items migrated, {failed_count} failed), "
                                f"took {component_time:.2f} seconds",
                            )
                    else:
                        # Handle case where component didn't return a result (should not happen with dataclass)
                        config.logger.warning(
                            "Component '%s' did not return a result", component_name
                        )
                        results.components[component_name] = component_result

                except KeyboardInterrupt:
                    # Handle user interruption within a component
                    config.logger.warning(
                        "Component '%s' was interrupted by user", component_name
                    )
                    # Create a basic result reflecting interruption
                    interrupted_result = ComponentResult(
                        success=False,
                        message="Component was interrupted by user",
                        details={
                            "status": "interrupted",
                            "time": time.time() - component_start_time,
                        },
                    )
                    results.components[component_name] = interrupted_result
                    results.overall["status"] = "interrupted"
                    break

                except Exception as e:
                    # Handle unexpected errors during component execution
                    config.logger.exception(
                        f"Error during '{component_name}' migration: {e}",
                    )
                    # Create a basic result reflecting the error
                    error_result = ComponentResult(
                        success=False,
                        message=f"Error during component execution: {e}",
                        errors=[str(e)],
                        details={
                            "status": "failed",
                            "time": time.time() - component_start_time,
                            "error": str(e),  # Keep error in details for compatibility
                        },
                    )
                    results.components[component_name] = error_result
                    results.overall["status"] = "failed"

                    # Check if we should stop on error immediately after an exception
                    if stop_on_error:
                        config.logger.error(
                            f"Component '{component_name}' failed with exception and --stop-on-error is set, aborting migration",
                        )
                        break

                # Check if component failed and we should stop on error (before user confirmation)
                component_result_obj = results.components.get(component_name)
                if (
                    component_result_obj
                    and not component_result_obj.success
                    and stop_on_error
                ):
                    config.logger.error(
                        f"Component '{component_name}' failed and --stop-on-error is set, aborting migration",
                    )
                    break

                # Pause for user confirmation between components
                if (
                    component_name != components[-1] and not no_confirm
                ):  # Skip after the last component or if no_confirm is set
                    try:
                        result: str = "\033[1;90mUNKNOWN RESULT\033[0m"
                        current_result = results.components.get(component_name)
                        if current_result:
                            if hasattr(current_result, "success"):
                                if current_result.success:
                                    result = "\033[1;32mSUCCEEDED\033[0m"
                                else:
                                    result = "\033[1;31mFAILED\033[0m"
                            if (
                                hasattr(current_result, "errors")
                                and current_result.errors
                            ):
                                result = f"\033[1;31mFAILED\033[0m with errors: {current_result.errors}"

                        console.rule(f"Component '{component_name}' has {result}.")

                        if results.overall["status"] != "success":
                            console.print("WARNING: Previous component had errors.")

                        next_component = components[
                            components.index(component_name) + 1
                        ]

                        user_input = (
                            input(
                                f"\nContinue to next component: {next_component}? [Y/n]: "
                            )
                            .strip()
                            .lower()
                        )
                        if user_input and user_input not in ("y", "yes"):
                            config.logger.warning("Migration paused by user")
                            results.overall["status"] = "interrupted"
                            results.overall["message"] = "Migration was paused by user"
                            break
                    except KeyboardInterrupt:
                        config.logger.warning("Migration interrupted by user")
                        results.overall["status"] = "interrupted"
                        results.overall["message"] = "Migration was interrupted by user"
                        break

                # Break out if critical components failed
                component_result_data = results.components
                current_component_result = component_result_data.get(
                    component_name, ComponentResult()
                )

                # Check if component failed - use success flag as primary indicator
                component_failed = (
                    not current_component_result.success
                    if current_component_result
                    else True
                )

                # Stop for critical components regardless of stop_on_error flag
                if component_failed and component_name in ["users", "projects"]:
                    config.logger.error(
                        f"Critical component '{component_name}' failed, aborting migration",
                    )
                    break

        except KeyboardInterrupt:
            # Handle user interruption at the top level
            config.logger.warning("Migration interrupted by user")
            results.overall["status"] = "interrupted"
            results.overall["message"] = "Migration was interrupted by user"

        # Add end time to results
        results.overall["end_time"] = datetime.now(tz=UTC).isoformat()

        # Calculate total time
        start_time = datetime.fromisoformat(results.overall["start_time"])
        end_time = datetime.fromisoformat(results.overall["end_time"])
        total_seconds = (end_time - start_time).total_seconds()
        results.overall["total_time_seconds"] = total_seconds

        # Print final status
        if results.overall["status"] == "success":
            config.logger.success(
                "Migration completed successfully in %.2f seconds.", total_seconds
            )
        else:
            config.logger.error(
                "Migration completed with status '%s' in %.2f seconds.",
                results.overall["status"],
                total_seconds,
            )

        # Save results to file
        results_file = f"migration_results_{migration_timestamp}.json"
        data_handler.save_results(
            results,
            filename=results_file,
        )

        config.logger.info("Migration results saved to %s", results_file)

        return results

    except Exception as e:
        # Handle unexpected errors at the top level
        config.logger.exception(e)
        config.logger.error("Unexpected error during migration: %s", e)

        # Create a basic result object
        return MigrationResult(
            overall={
                "status": "failed",
                "error": str(e),
                "message": f"Unexpected error during migration: {e}",
                "timestamp": datetime.now(tz=UTC).isoformat(),
            },
        )


def parse_args() -> argparse.Namespace:
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
        "--setup-tmux",
        action="store_true",
        help="Create and setup a tmux session for Rails console use",
    )
    parser.add_argument(
        "--update-mapping",
        action="store_true",
        help="Update custom field mapping after manual Ruby script execution",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop the migration on the first error or exception encountered",
    )
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="Skip the 'Continue to next component' prompt and run all components without pausing",
    )
    return parser.parse_args()


def setup_tmux_session() -> bool:
    """Create and set up a tmux session for Rails console."""
    from src import config  # Import config here to make it available

    session_name = config.openproject_config.get("tmux_session_name", "rails_console")

    config.logger.info(
        "Setting up tmux session '%s' for Rails console...", session_name
    )

    try:
        # Check if tmux is installed
        subprocess.run(["tmux", "-V"], check=True, capture_output=True)

        # Check if session already exists
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            check=False,
            capture_output=True,
        )

        if result.returncode == 0:
            config.logger.warning("tmux session '%s' already exists", session_name)
            config.logger.info("To attach to this session, run:")
            config.logger.info("tmux attach -t %s", session_name)
            return True

        # Create a new session
        subprocess.run(["tmux", "new-session", "-d", "-s", session_name], check=True)

        config.logger.success("Created tmux session '%s'", session_name)
        config.logger.info("To attach to this session, run:")
        config.logger.info("tmux attach -t %s", session_name)

        # Determine if Docker is being used
        using_docker = "container" in config.openproject_config

        # Send commands to the session to set up Rails console
        if using_docker:
            container = config.openproject_config.get("container", "openproject")
            config.logger.info("Detected Docker setup with container '%s'", container)
            config.logger.info(
                "Please manually run the following commands in the tmux session:",
            )
            config.logger.info("docker exec -it %s bash", container)
            config.logger.info("cd /app && bundle exec rails console")
        else:
            server = config.openproject_config.get("server")
            if server:
                config.logger.info("Detected remote server '%s'", server)
                config.logger.info(
                    "Please manually run the following commands in the tmux session:",
                )
                config.logger.info("ssh %s", server)
                config.logger.info("cd /opt/openproject && bundle exec rails console")
            else:
                config.logger.info(
                    "Please manually run the following command in the tmux session:",
                )
                config.logger.info(
                    "cd /path/to/openproject && bundle exec rails console"
                )

        config.logger.info(
            "After running Rails console, you can use the direct migration features."
        )

        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        config.logger.error("tmux is not installed or not available in PATH")
        config.logger.info("Please install tmux first:")
        config.logger.info("  On Ubuntu/Debian: sudo apt-get install tmux")
        config.logger.info("  On CentOS/RHEL: sudo yum install tmux")
        config.logger.info("  On macOS with Homebrew: brew install tmux")
        return False


def main() -> None:
    """Run the migration tool."""
    # Parse command-line arguments
    args = parse_args()

    try:
        if args.setup_tmux:
            setup_tmux_session()
            config.logger.success("tmux session setup complete.")
            return

        if args.backup_dir:
            success = restore_backup(args.backup_dir)
            if success:
                config.logger.success("Backup restoration completed successfully.")
            else:
                config.logger.error("Backup restoration failed.")
            return

        if args.update_mapping:
            # Initialize clients properly using dependency injection
            # Create clients in the correct hierarchical order

            # Debug: print available config keys
            config.logger.info(
                "Available OpenProject config keys: %s",
                list(config.openproject_config.keys()),
            )

            ssh_client = SSHClient(
                host=str(
                    config.openproject_config.get("server", "openproject.local"),
                ),
                user=config.openproject_config.get("user", None),
                key_file=config.openproject_config.get("key_file", None),
            )

            DockerClient(
                container_name=str(
                    config.openproject_config.get(
                        "container_name",
                        config.openproject_config.get("container", "openproject"),
                    ),
                ),
                ssh_client=ssh_client,
            )

            RailsConsoleClient(
                tmux_session_name=config.openproject_config.get(
                    "tmux_session_name", "rails_console"
                ),
            )

            jira_client = JiraClient()
            op_client = OpenProjectClient(
                container_name=config.openproject_config.get("container", None),
                ssh_host=config.openproject_config.get("server", None),
                ssh_user=config.openproject_config.get("user", None),
                tmux_session_name=config.openproject_config.get(
                    "tmux_session_name", None
                ),
            )

            # List options to choose which mapping to update
            console.print("\nSelect mapping to update:")
            console.print("1. Custom Field mapping")
            console.print("2. Issue Type mapping")

            while True:
                try:
                    choice = input("Enter choice (1-2): ")
                    if choice == "1":
                        custom_field_migration = CustomFieldMigration(
                            jira_client=jira_client, op_client=op_client
                        )
                        cf_mapping_update_result = (
                            custom_field_migration.update_mapping_file()
                        )
                        if cf_mapping_update_result:
                            config.logger.success(
                                "Custom field mapping updated successfully."
                            )
                        else:
                            config.logger.warning(
                                "No updates were made to custom field mapping."
                            )
                        break
                    if choice == "2":
                        issue_type_migration = IssueTypeMigration(
                            jira_client=jira_client, op_client=op_client
                        )
                        issue_type_mapping_update_result = (
                            issue_type_migration.update_mapping_file()
                        )
                        if issue_type_mapping_update_result:
                            config.logger.success(
                                "Issue type mapping updated successfully."
                            )
                        else:
                            config.logger.warning(
                                "No updates were made to issue type mapping."
                            )
                        break
                    config.logger.error("Invalid choice. Please enter 1 or 2.")
                except KeyboardInterrupt:
                    config.logger.warning("Operation cancelled by user.")
                    return
            return

        # dump args
        config.logger.debug("Args: %s", args)

        # Update configuration with CLI arguments
        config.update_from_cli_args(args)

        # Run migration with provided arguments
        migration_result = run_migration(
            components=args.components,
            stop_on_error=getattr(args, "stop_on_error", False),
            no_confirm=getattr(args, "no_confirm", False),
        )

        # Display migration results summary
        if migration_result:
            # Fix: Access MigrationResult properties correctly (it's an object, not a dict)
            overall_status = (
                migration_result.overall.get("status", "unknown")
                if hasattr(migration_result, "overall")
                else "unknown"
            )

            # Show summary header based on status
            if overall_status == "success":
                config.logger.success("Migration completed successfully")
            elif overall_status == "interrupted":
                config.logger.warning("Migration was interrupted before completion")
            else:
                config.logger.error("Migration completed with errors")

            # Print component results
            config.logger.info("Component results:")
            # Fix: Access components property correctly
            component_items = (
                migration_result.components.items()
                if hasattr(migration_result, "components")
                else {}
            )
            for component, comp_result in component_items:
                # Access status from details
                status = (
                    comp_result.details.get("status", "unknown")
                    if comp_result.details
                    else "unknown"
                )
                if status == "success":
                    config.logger.success("✓ %s: %s", component, status)
                elif status == "interrupted":
                    config.logger.warning("⚠ %s: %s", component, status)
                else:
                    config.logger.error("✗ %s: %s", component, status)

    except KeyboardInterrupt:
        console.print("\nMigration manually interrupted. Exiting...")
        sys.exit(0)


if __name__ == "__main__":
    main()
