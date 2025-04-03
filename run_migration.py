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
from datetime import datetime
from typing import Dict, List, Any, Literal, TypedDict, NotRequired, Optional

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__))))

from src.config import logger, get_path, ensure_subdir
from src.display import console
from src.migrations.user_migration import run_user_migration
from src.migrations.company_migration import run_company_migration
from src.migrations.account_migration import run_account_migration
from src.migrations.project_migration import run_project_migration
from src.migrations.custom_field_migration import run_custom_field_migration
from src.migrations.workflow_migration import run_workflow_migration
from src.migrations.link_type_migration import run_link_type_migration
from src.migrations.work_package_migration import run_work_package_migration
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

class MigrationResult(TypedDict):
    components: Dict[str, ComponentResult]
    overall: Dict[str, Any]

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


def run_migration(dry_run: bool = True, components: List[str] | None = None, backup: bool = True, force: bool = False, direct_migration: bool = False) -> MigrationResult:
    """
    Run the complete migration process or selected components.

    Args:
        dry_run: If True, no changes will be made to OpenProject
        components: List of component names to run. If None, run all components.
        backup: Whether to create a backup before migration
        force: Whether to force extraction of data even if it already exists
        direct_migration: Whether to use direct Rails console execution for components that support it

    Returns:
        Dictionary with migration results
    """
    start_time = time.time()
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    # Use the centralized config for var directories and create a log directory for this migration run
    log_dir = ensure_subdir("logs", f"migration_{timestamp}")

    mode = "DRY RUN" if dry_run else "PRODUCTION"
    logger.info(f"Starting Jira to OpenProject migration - {mode=}")
    logger.info(f"{log_dir=}")

    # Create backup if needed
    backup_dir = None
    if backup and not dry_run:
        backup_dir = create_backup()

    if components:
        logger.info(f"Running selected components: {', '.join(components)}")
    else:
        logger.info("Running all migration components")

    # Define the migration components in the order they should be executed
    all_components: Dict[str, callable] = {
        "users": run_user_migration,
        "custom_fields": run_custom_field_migration,
        "companies": run_company_migration,
        "accounts": run_account_migration,
        "projects": run_project_migration,
        # "workflows": run_workflow_migration, # not working right now
        "link_types": run_link_type_migration,
        "issue_types": run_issue_type_migration,  # Add before work packages
        "work_packages": run_work_package_migration,  # Implemented now
        # "time_entries": run_time_entry_migration,     # Not implemented yet
    }

    # Add a comment about the migration strategy
    logger.info("Migration strategy:")
    logger.info("1. Migrate users and custom fields first")
    logger.info("2. Migrate Tempo companies as top-level projects")
    logger.info("3. Create custom fields for Tempo accounts")
    logger.info("4. Migrate Jira projects with account information as custom fields")
    logger.info("5. Migrate work packages with proper type assignments")
    logger.info("This approach handles the many-to-many relationship between accounts and projects")

    # Initialize Rails client if available and needed
    rails_client = None
    # Check if any component requiring rails is selected or if all components are running
    needs_rails = any(comp in ["custom_fields", "issue_types", "accounts"] for comp in (components or all_components.keys()))

    if HAS_RAILS_CLIENT and needs_rails:
        logger.info("Initializing OpenProject Rails Client for direct migration tasks...")
        try:
            # Initialize without the unsupported connection_timeout argument
            rails_client = OpenProjectRailsClient(session_name="rails_console")
            if not rails_client.connected:
                logger.warning("Failed to connect to Rails console during initial check. Direct migration might fail.")
            # We don't necessarily need to fail the whole migration here
            # Individual components will handle the lack of connection
        except Exception as e:
            logger.error(f"Error initializing OpenProjectRailsClient: {e}")
            logger.warning("Proceeding without Rails client. Direct migration features will be unavailable.")
            rails_client = None # Ensure it's None if initialization failed

    # Initialize API clients (Jira and OpenProject)
    # Pass the rails_client to OpenProjectClient if it was initialized
    op_client = OpenProjectClient(rails_client=rails_client)
    jira_client = JiraClient()

    # Ensure API clients are connected
    if not op_client.connected:
        logger.error("Failed to connect to OpenProject API. Aborting migration.")
        # Add placeholder results for overall status
        results: MigrationResult = {
            "components": {},
            "overall": {"start_time": timestamp, "mode": mode, "backup_dir": backup_dir, "error": "OpenProject API connection failed"},
        }
        return results
    if not jira_client.connected:
        logger.error("Failed to connect to Jira API. Aborting migration.")
        results: MigrationResult = {
            "components": {},
            "overall": {"start_time": timestamp, "mode": mode, "backup_dir": backup_dir, "error": "Jira API connection failed"},
        }
        return results

    # Determine which components to run
    components_to_run_names = list(all_components.keys()) if components is None else components
    components_to_run = {name: all_components[name] for name in components_to_run_names if name in all_components}
    if len(components_to_run_names) != len(components_to_run):
        unknown = set(components_to_run_names) - set(all_components.keys())
        logger.warning(f"Ignoring unknown components: {unknown}")

    # Run each component
    results: MigrationResult = {
        "components": {},
        "overall": {"start_time": timestamp, "mode": mode, "backup_dir": backup_dir},
    }

    has_critical_failure = False

    # Instantiate migration classes with injected clients
    # Note: We pass the rails_client even if it's None; the classes handle it.
    # Data dir is likely managed internally by run_ functions, confirm if needed.
    # Pass common clients and flags.
    migration_args = {
        "jira_client": jira_client,
        "op_client": op_client,
        "rails_console": rails_client,
        "dry_run": dry_run,
        "force": force,
    }

    for name, run_func in components_to_run.items():
        logger.info(f"Running migration component: {name=}")
        try:
            component_start = time.time()

            # Prepare arguments common to most run_ functions
            func_args = {
                "dry_run": dry_run,
                "force": force,
                # Pass the initialized clients
                "jira_client": jira_client,
                "op_client": op_client,
                "rails_console": rails_client
            }

            # Add component-specific arguments
            if name in ["custom_fields", "issue_types", "work_packages", "accounts"]:
                func_args["direct_migration"] = direct_migration

            # Remove arguments not expected by the specific run_func
            # This requires knowing the signature of each run_func or using inspect
            # For now, assume run_funcs accept extra kwargs or we only pass relevant ones
            # Example of filtering (requires import inspect):
            # sig = inspect.signature(run_func)
            # allowed_args = {k: v for k, v in func_args.items() if k in sig.parameters}

            # Call the specific run function with appropriate args
            # Simplified call structure, assuming run_funcs handle the arguments
            if name == "custom_fields":
                # run_custom_field_migration now expects clients
                run_func(
                    jira_client=jira_client,
                    op_client=op_client,
                    rails_console=rails_client,
                    dry_run=dry_run,
                    force=force,
                    direct_migration=direct_migration
                )
            elif name == "issue_types":
                # TODO: Update run_issue_type_migration similarly
                run_func(dry_run=dry_run, force=force, direct_migration=direct_migration)
            elif name == "work_packages":
                # TODO: Update run_work_package_migration similarly
                run_func(dry_run=dry_run, force=force, direct_migration=direct_migration)
            elif name == "accounts":
                # TODO: Update run_account_migration similarly
                run_func(dry_run=dry_run, force=force) # Keep original call for now until refactored
            elif name == "companies":
                # TODO: Update run_company_migration if needed
                run_func(dry_run=dry_run, force=force)
            else:
                # TODO: Update other run_ functions (users, projects, link_types)
                run_func(dry_run=dry_run)

            component_time = time.time() - component_start
            results["components"][name] = {"status": "success", "time": component_time}
            logger.info(f"Component {name} completed successfully in {component_time=:.2f} seconds")
        except KeyboardInterrupt:
            logger.error("Migration interrupted by user")
            results["components"][name] = {
                "status": "interrupted",
                "error": "Migration interrupted by user",
            }
            has_critical_failure = True
            break
        except Exception as e:
            try:
                error_type = type(e)
                error_message = str(e)
            except Exception as str_err:
                error_type = "Unknown"
                error_message = f"Failed to convert exception to string: {str_err}"
            # Use rich console to print the exception
            console.print_exception(show_locals=True)
            # Also keep a basic log message
            logger.error(f"Component {name} failed with error type {error_type}: {error_message}")
            results["components"][name] = {"status": "failed", "error": error_message}

            # Consider this a critical failure that should stop the migration
            if not dry_run:
                has_critical_failure = True
                break

    # Log summary of results
    total_time = time.time() - start_time
    results["overall"]["total_time"] = total_time

    if has_critical_failure and not dry_run and backup_dir:
        logger.warning("Critical failure detected. Initiating rollback...")
        if restore_backup(backup_dir):
            logger.info("Rollback completed successfully")
            results["overall"]["rollback"] = "success"
        else:
            logger.error("Rollback failed")
            results["overall"]["rollback"] = "failed"

    logger.info(f"Migration {mode} completed in {total_time=:.2f} seconds")

    component_results = results["components"]
    success_count = sum(
        1 for r in component_results.values() if r["status"] == "success"
    )
    failed_count = sum(1 for r in component_results.values() if r["status"] == "failed")
    interrupted_count = sum(
        1 for r in component_results.values() if r["status"] == "interrupted"
    )

    results["overall"]["success_count"] = success_count
    results["overall"]["failed_count"] = failed_count
    results["overall"]["interrupted_count"] = interrupted_count

    logger.info(f"Components completed successfully: {success_count=}/{len(components_to_run)=}")
    if failed_count > 0:
        logger.info("Failed components:")
        for name, result in component_results.items():
            if result["status"] == "failed":
                logger.info(f"  - {name}: {result.get('error', 'Unknown error')}")

    # Save results to log directory
    results_path = os.path.join(log_dir, "results.json")
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)
    logger.info(f"Results saved to: {results_path=}")

    return results


if __name__ == "__main__":
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
        metavar="BACKUP_DIR",
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
        help="Use direct Rails console execution for components that support it",
    )
    args = parser.parse_args()

    if args.restore:
        restore_backup(args.restore)
    else:
        run_migration(
            dry_run=args.dry_run, components=args.components, backup=not args.no_backup, force=args.force, direct_migration=args.direct_migration
        )
