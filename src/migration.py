#!/usr/bin/env python3
"""Master migration script for Jira to OpenProject migration.
This script orchestrates the complete migration process with performance optimization.
"""

import argparse
import asyncio
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
from src.performance.migration_performance_manager import (
    MigrationPerformanceManager,
    PerformanceConfig
)
from src.type_definitions import BackupDir, ComponentName
from src.utils import data_handler

if TYPE_CHECKING:
    from src.migrations.base_migration import BaseMigration

console = Console()


class Migration:
    """Main migration orchestrator class."""
    
    def __init__(self, components: list[ComponentName] | None = None):
        self.components = components or []
        self.performance_manager = MigrationPerformanceManager()
    
    async def run(
        self,
        stop_on_error: bool = False,
        no_confirm: bool = False,
        batch_size: int = 100,
        max_concurrent: int = 5,
        enable_performance_optimization: bool = True,
    ) -> MigrationResult:
        """Run the migration with the specified components."""
        return await run_migration(
            components=self.components,
            stop_on_error=stop_on_error,
            no_confirm=no_confirm,
            batch_size=batch_size,
            max_concurrent=max_concurrent,
            enable_performance_optimization=enable_performance_optimization,
        )


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


def create_performance_config(
    batch_size: int = 100,
    max_concurrent_batches: int = 5,
    enable_performance_tracking: bool = True
) -> PerformanceConfig:
    """Create performance configuration based on migration settings.
    
    Args:
        batch_size: Size of batches for API processing
        max_concurrent_batches: Maximum concurrent batch operations
        enable_performance_tracking: Whether to enable performance tracking
    
    Returns:
        Configured PerformanceConfig instance
    """
    # Get rate limiting settings from config or use defaults
    max_requests_per_minute = getattr(config, 'max_requests_per_minute', 100)
    
    # Determine if this is a large migration (affects performance tuning)
    is_large_migration = batch_size > 50 or max_concurrent_batches > 3
    
    return PerformanceConfig(
        # Batching configuration
        batch_size=batch_size,
        max_concurrent_batches=max_concurrent_batches,
        batch_timeout=60.0 if is_large_migration else 30.0,
        
        # Rate limiting configuration
        max_requests_per_minute=max_requests_per_minute,
        burst_size=min(20, batch_size // 5),
        adaptive_rate_limiting=True,
        
        # Retry configuration
        max_retries=3,
        base_delay=1.0,
        max_delay=60.0,
        
        # Progress tracking
        enable_progress_tracking=enable_performance_tracking,
        progress_update_interval=2.0,
        save_progress_to_file=True,
        
        # Performance tuning
        enable_parallel_processing=is_large_migration,
        memory_limit_mb=1024 if is_large_migration else 512,
        enable_streaming=is_large_migration
    )


async def run_migration(
    components: list[ComponentName] | None = None,
    stop_on_error: bool = False,
    no_confirm: bool = False,
    batch_size: int = 100,
    max_concurrent: int = 5,
    enable_performance_optimization: bool = True,
) -> MigrationResult:
    """Run the migration process with performance optimization.

    Args:
        components: List of specific components to run (if None, run all)
        stop_on_error: If True, stop migration on the first error/exception
        no_confirm: If True, skip the 'Continue to next component' prompt
        batch_size: Size of batches for API processing
        max_concurrent: Maximum concurrent batch operations
        enable_performance_optimization: Whether to enable performance optimization

    Returns:
        Dictionary with migration results

    """
    # Initialize performance manager
    performance_manager = None
    if enable_performance_optimization:
        perf_config = create_performance_config(
            batch_size=batch_size,
            max_concurrent_batches=max_concurrent,
            enable_performance_tracking=True
        )
        performance_manager = MigrationPerformanceManager(perf_config)
        config.logger.info("Performance optimization enabled")
        config.logger.info(f"Batch size: {batch_size}, Max concurrent: {max_concurrent}")
    
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
                    "batch_size": batch_size,
                    "max_concurrent": max_concurrent,
                    "performance_optimization": enable_performance_optimization,
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

        # 4. Finally, create the enhanced Jira client and OpenProject client (which uses the other clients)
        # Get performance configuration from migration config
        performance_config = {
            'cache_size': config.migration_config.get('cache_size', 2000),
            'cache_ttl': config.migration_config.get('cache_ttl', 1800),
            'batch_size': batch_size,
            'max_workers': max_concurrent,
            'rate_limit': config.migration_config.get('rate_limit_per_sec', 15.0)
        }
        
        jira_client = JiraClient(**performance_config)

        if mock_mode:
            # For mock mode, create a simplified OpenProject client that doesn't require real connections
            # Adjust performance config for OpenProject (typically lower rates)
            op_performance_config = performance_config.copy()
            op_performance_config.update({
                'cache_size': config.migration_config.get('op_cache_size', 1500),
                'cache_ttl': config.migration_config.get('op_cache_ttl', 2400),
                'batch_size': config.migration_config.get('op_batch_size', 50),
                'rate_limit': config.migration_config.get('op_rate_limit_per_sec', 12.0)
            })
            
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
                **op_performance_config
            )
        else:
            # Adjust performance config for OpenProject (typically lower rates)
            op_performance_config = performance_config.copy()
            op_performance_config.update({
                'cache_size': config.migration_config.get('op_cache_size', 1500),
                'cache_ttl': config.migration_config.get('op_cache_ttl', 2400),
                'batch_size': config.migration_config.get('op_batch_size', 50),
                'rate_limit': config.migration_config.get('op_rate_limit_per_sec', 12.0)
            })
            
            op_client = OpenProjectClient(
                container_name=config.openproject_config.get("container", None),
                ssh_host=config.openproject_config.get("server", None),
                ssh_user=config.openproject_config.get("user", None),
                tmux_session_name=config.openproject_config.get(
                    "tmux_session_name", None
                ),
                **op_performance_config
            )

        config.logger.success("All clients initialized successfully")

        # Initialize validation framework
        from src.utils.advanced_validation import ValidationFramework, validate_pre_migration
        validation_framework = ValidationFramework()
        config.logger.info("Validation framework initialized")

        # Initialize advanced configuration manager
        from src.utils.advanced_config_manager import ConfigurationManager, EnvironmentType
        config_manager = ConfigurationManager(
            config_dir=Path("config"),
            templates_dir=Path("config/templates"),
            schemas_dir=Path("config/schemas"),
            backups_dir=Path("config/backups")
        )
        config_manager.create_directories()
        config.logger.info("Advanced configuration manager initialized")

        # Initialize advanced security system
        from src.utils.advanced_security import SecurityManager, SecurityConfig, UserRole, SecurityLevel
        security_config = SecurityConfig(
            encryption_key_path=Path("config/security/encryption.key"),
            credentials_path=Path("config/security/credentials.json"),
            audit_log_path=Path("logs/security/audit.log"),
            rate_limit_requests=100,
            rate_limit_window=60,
            password_min_length=12,
            session_timeout=3600,
            max_login_attempts=5,
            lockout_duration=900
        )
        security_manager = SecurityManager(security_config)
        security_manager.initialize()
        config.logger.info("Advanced security system initialized")

        # Initialize large-scale optimizer for performance
        from src.utils.large_scale_optimizer import LargeScaleOptimizer, get_optimized_config_for_size
        large_scale_config = get_optimized_config_for_size(100000)  # Default to 100k+ optimization
        large_scale_optimizer = LargeScaleOptimizer(large_scale_config)
        config.logger.info("Large-scale optimizer initialized")

        # Initialize comprehensive logging and monitoring
        from src.utils.comprehensive_logging import start_monitoring, log_migration_start
        await start_monitoring()
        config.logger.info("Comprehensive logging and monitoring initialized")

        # Initialize automated testing suite
        from src.utils.automated_testing_suite import AutomatedTestingSuite, TestSuiteConfig, TestType
        test_config = TestSuiteConfig(
            test_types=[TestType.UNIT, TestType.INTEGRATION],
            parallel_workers=2,
            enable_coverage=True
        )
        automated_test_suite = AutomatedTestingSuite(test_config)
        config.logger.info("Automated testing suite initialized")

        # Initialize mappings
        config.mappings = Mappings(data_dir=config.get_path("data"))

        # Run pre-migration validation
        config.logger.info("Running pre-migration validation...")
        try:
            pre_migration_data = {
                "jira_config": config.jira_config,
                "openproject_config": config.openproject_config,
                "migration_config": config.migration_config,
                "mappings": config.mappings.get_all_mappings(),
                "clients": {
                    "jira_client": jira_client,
                    "op_client": op_client
                }
            }
            
            validation_context = {
                "migration_timestamp": migration_timestamp,
                "batch_size": batch_size,
                "max_concurrent": max_concurrent,
                "dry_run": config.migration_config.get("dry_run", False)
            }
            
            pre_validation_summary = await validate_pre_migration(pre_migration_data, validation_context)
            
            if pre_validation_summary.has_critical_errors():
                config.logger.error("Pre-migration validation failed with critical errors")
                config.logger.error(f"Validation summary: {pre_validation_summary.to_dict()}")
                if not config.migration_config.get("force", False):
                    raise Exception("Pre-migration validation failed. Use --force to override.")
                else:
                    config.logger.warning("Continuing despite validation errors due to --force flag")
            elif pre_validation_summary.errors > 0:
                config.logger.warning(f"Pre-migration validation completed with {pre_validation_summary.errors} errors")
                config.logger.info(f"Success rate: {pre_validation_summary.get_success_rate():.1f}%")
            else:
                config.logger.success("Pre-migration validation passed successfully")
                
            # Store validation results
            results.overall["pre_migration_validation"] = pre_validation_summary.to_dict()
            
            # Log migration start with comprehensive logging
            from src.utils.comprehensive_logging import log_migration_start
            log_migration_start(
                migration_id=migration_timestamp,
                components=components,
                batch_size=batch_size,
                max_concurrent=max_concurrent,
                stop_on_error=stop_on_error,
                dry_run=config.migration_config.get("dry_run", False)
            )
            
        except Exception as e:
            config.logger.error(f"Pre-migration validation failed: {e}")
            if not config.migration_config.get("force", False):
                raise
            else:
                config.logger.warning("Continuing despite validation failure due to --force flag")

        # Run security validation and audit logging
        config.logger.info("Running security validation and audit logging...")
        try:
            # Audit log the migration start
            security_manager.audit_logger.log_event(
                event_type="MIGRATION_START",
                user_id="system",
                details={
                    "migration_id": migration_timestamp,
                    "components": components,
                    "batch_size": batch_size,
                    "max_concurrent": max_concurrent,
                    "dry_run": config.migration_config.get("dry_run", False)
                }
            )
            
            # Validate security configuration
            security_scan_result = security_manager.security_scanner.scan_configuration(
                config_path=Path("config/config.yaml"),
                scan_type="configuration"
            )
            
            if security_scan_result.vulnerabilities:
                config.logger.warning(f"Security scan found {len(security_scan_result.vulnerabilities)} potential issues")
                for vuln in security_scan_result.vulnerabilities:
                    config.logger.warning(f"Security issue: {vuln.severity} - {vuln.description}")
            else:
                config.logger.success("Security validation passed")
                
            # Store security validation results
            results.overall["security_validation"] = {
                "scan_result": security_scan_result.to_dict(),
                "audit_events": security_manager.audit_logger.get_recent_events(10)
            }
            
        except Exception as e:
            config.logger.error(f"Security validation failed: {e}")
            if not config.migration_config.get("force", False):
                raise
            else:
                config.logger.warning("Continuing despite security validation failure due to --force flag")

        # Define all available migration components
        # Pass performance manager to components that support it
        available_components = AvailableComponents(
            users=UserMigration(
                jira_client=jira_client, 
                op_client=op_client,
                performance_manager=performance_manager
            ),
            custom_fields=CustomFieldMigration(
                jira_client=jira_client, 
                op_client=op_client,
                performance_manager=performance_manager
            ),
            companies=CompanyMigration(
                jira_client=jira_client, 
                op_client=op_client,
                performance_manager=performance_manager
            ),
            projects=ProjectMigration(
                jira_client=jira_client, 
                op_client=op_client,
                performance_manager=performance_manager
            ),
            link_types=LinkTypeMigration(
                jira_client=jira_client, 
                op_client=op_client,
                performance_manager=performance_manager
            ),
            issue_types=IssueTypeMigration(
                jira_client=jira_client, 
                op_client=op_client,
                performance_manager=performance_manager
            ),
            status_types=StatusMigration(
                jira_client=jira_client, 
                op_client=op_client,
                performance_manager=performance_manager
            ),
            work_packages=WorkPackageMigration(
                jira_client=jira_client, 
                op_client=op_client,
                performance_manager=performance_manager
            ),
            accounts=AccountMigration(
                jira_client=jira_client, 
                op_client=op_client,
                performance_manager=performance_manager
            ),
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
                performance_manager=performance_manager,
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

                        # Add performance metrics if available
                        if performance_manager:
                            perf_summary = performance_manager.get_performance_summary()
                            component_result.details = component_result.details or {}
                            component_result.details["performance_metrics"] = perf_summary

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

                        # Enhanced logging with performance metrics
                        if component_result.success:
                            log_msg = (
                                f"Component '{component_name}' completed successfully "
                                f"({success_count}/{total_count} items migrated), "
                                f"took {component_time:.2f} seconds"
                            )
                            
                            if performance_manager and "performance_metrics" in details:
                                perf = details["performance_metrics"]
                                throughput = perf.get("throughput", {})
                                items_per_sec = throughput.get("items_per_second", 0)
                                log_msg += f", throughput: {items_per_sec:.1f} items/sec"
                            
                            config.logger.success(log_msg)
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

        # Add overall performance summary
        if performance_manager:
            overall_perf_summary = performance_manager.get_performance_summary()
            results.overall["performance_summary"] = overall_perf_summary
            
            # Save detailed performance report
            perf_report_path = config.get_path("data") / f"performance_report_{migration_timestamp}.json"
            performance_manager.save_performance_report(perf_report_path)
            config.logger.info(f"Performance report saved to: {perf_report_path}")

        # Print final status with performance information
        if results.overall["status"] == "success":
            log_msg = f"Migration completed successfully in {total_seconds:.2f} seconds."
            if performance_manager:
                perf = results.overall.get("performance_summary", {})
                throughput = perf.get("throughput", {})
                overall_items_per_sec = throughput.get("items_per_second", 0)
                if overall_items_per_sec > 0:
                    log_msg += f" Overall throughput: {overall_items_per_sec:.1f} items/sec"
            config.logger.success(log_msg)
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

        # Log migration completion with comprehensive logging
        from src.utils.comprehensive_logging import log_migration_complete, stop_monitoring
        log_migration_complete(
            migration_id=migration_timestamp,
            success=results.overall["status"] == "success",
            total_components=len(results.components),
            successful_components=sum(1 for c in results.components.values() if c.get("status") == "success"),
            total_seconds=total_seconds,
            results_file=results_file
        )
        
        # Stop monitoring
        await stop_monitoring()

        # Log security audit completion
        try:
            security_manager.audit_logger.log_event(
                event_type="MIGRATION_COMPLETE",
                user_id="system",
                details={
                    "migration_id": migration_timestamp,
                    "success": results.overall["status"] == "success",
                    "total_components": len(results.components),
                    "successful_components": sum(1 for c in results.components.values() if c.get("status") == "success"),
                    "total_seconds": total_seconds
                }
            )
            config.logger.info("Security audit logging completed")
        except Exception as e:
            config.logger.warning(f"Security audit logging failed: {e}")

        return results

    except Exception as e:
        # Handle unexpected errors at the top level
        config.logger.exception(e)
        config.logger.error("Unexpected error during migration: %s", e)

        # Log security audit for migration failure
        try:
            if 'security_manager' in locals():
                security_manager.audit_logger.log_event(
                    event_type="MIGRATION_FAILED",
                    user_id="system",
                    details={
                        "migration_id": migration_timestamp,
                        "error": str(e),
                        "error_type": type(e).__name__
                    }
                )
        except Exception as audit_error:
            config.logger.warning(f"Security audit logging failed during error handling: {audit_error}")

        # Create a basic result object
        return MigrationResult(
            overall={
                "status": "failed",
                "error": str(e),
                "message": f"Unexpected error during migration: {e}",
                "timestamp": datetime.now(tz=UTC).isoformat(),
            },
        )
    finally:
        # Clean up performance manager
        if performance_manager:
            performance_manager.cleanup()


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
    # Performance optimization arguments
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Size of batches for API processing (default: 100)",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=5,
        help="Maximum concurrent batch operations (default: 5)",
    )
    parser.add_argument(
        "--no-performance-optimization",
        action="store_true",
        help="Disable performance optimization features",
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

            # Use default performance config for main() function
            default_performance_config = {
                'cache_size': config.migration_config.get('cache_size', 2000),
                'cache_ttl': config.migration_config.get('cache_ttl', 1800),
                'batch_size': config.migration_config.get('batch_size', 100),
                'max_workers': config.migration_config.get('max_workers', 5),
                'rate_limit': config.migration_config.get('rate_limit_per_sec', 15.0)
            }
            
            jira_client = JiraClient(**default_performance_config)
            
            # Adjust performance config for OpenProject
            op_performance_config = default_performance_config.copy()
            op_performance_config.update({
                'cache_size': config.migration_config.get('op_cache_size', 1500),
                'cache_ttl': config.migration_config.get('op_cache_ttl', 2400),
                'batch_size': config.migration_config.get('op_batch_size', 50),
                'rate_limit': config.migration_config.get('op_rate_limit_per_sec', 12.0)
            })
            
            op_client = OpenProjectClient(
                container_name=config.openproject_config.get("container", None),
                ssh_host=config.openproject_config.get("server", None),
                ssh_user=config.openproject_config.get("user", None),
                tmux_session_name=config.openproject_config.get(
                    "tmux_session_name", None
                ),
                **op_performance_config
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
        migration_result = asyncio.run(run_migration(
            components=args.components,
            stop_on_error=getattr(args, "stop_on_error", False),
            no_confirm=getattr(args, "no_confirm", False),
            batch_size=getattr(args, "batch_size", 100),
            max_concurrent=getattr(args, "max_concurrent", 5),
            enable_performance_optimization=not getattr(args, "no_performance_optimization", False),
        ))

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

            # Show performance summary if available
            if hasattr(migration_result, "overall") and "performance_summary" in migration_result.overall:
                perf_summary = migration_result.overall["performance_summary"]
                timing = perf_summary.get("timing", {})
                throughput = perf_summary.get("throughput", {})
                
                config.logger.info("Performance Summary:")
                config.logger.info(f"  Total processing time: {timing.get('total_time_seconds', 0):.2f}s")
                config.logger.info(f"  Overall throughput: {throughput.get('items_per_second', 0):.1f} items/sec")
                config.logger.info(f"  Success rate: {throughput.get('success_rate', 0):.1%}")
                config.logger.info(f"  Processing efficiency: {timing.get('processing_efficiency', 0):.1%}")

    except KeyboardInterrupt:
        console.print("\nMigration manually interrupted. Exiting...")
        sys.exit(0)


if __name__ == "__main__":
    main()
