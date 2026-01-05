"""Master migration script for Jira to OpenProject migration.

This script orchestrates the complete migration process with performance optimization.
"""

import argparse
import asyncio
import inspect
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console

from src import config
from src.clients.docker_client import DockerClient
from src.clients.health_check_client import HealthCheckClient
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.clients.rails_console_client import RailsConsoleClient
from src.clients.ssh_client import SSHClient
from src.migrations.account_migration import AccountMigration
from src.migrations.admin_scheme_migration import AdminSchemeMigration
from src.migrations.affects_versions_migration import AffectsVersionsMigration
from src.migrations.agile_board_migration import AgileBoardMigration
from src.migrations.attachment_provenance_migration import AttachmentProvenanceMigration
from src.migrations.attachments_migration import AttachmentsMigration
from src.migrations.category_defaults_migration import CategoryDefaultsMigration
from src.migrations.company_migration import CompanyMigration
from src.migrations.components_migration import ComponentsMigration
from src.migrations.custom_field_migration import CustomFieldMigration
from src.migrations.customfields_generic_migration import CustomFieldsGenericMigration
from src.migrations.estimates_migration import EstimatesMigration
from src.migrations.group_migration import GroupMigration
from src.migrations.inline_refs_migration import InlineRefsMigration
from src.migrations.issue_type_migration import IssueTypeMigration
from src.migrations.labels_migration import LabelsMigration
from src.migrations.link_type_migration import LinkTypeMigration
from src.migrations.native_tags_migration import NativeTagsMigration
from src.migrations.priority_migration import PriorityMigration
from src.migrations.project_migration import ProjectMigration
from src.migrations.relation_migration import RelationMigration
from src.migrations.remote_links_migration import RemoteLinksMigration
from src.migrations.reporting_migration import ReportingMigration
from src.migrations.resolution_migration import ResolutionMigration
from src.migrations.security_levels_migration import SecurityLevelsMigration
from src.migrations.simpletasks_migration import SimpleTasksMigration
from src.migrations.sprint_epic_migration import SprintEpicMigration
from src.migrations.status_migration import StatusMigration
from src.migrations.story_points_migration import StoryPointsMigration
from src.migrations.time_entry_migration import TimeEntryMigration
from src.migrations.user_migration import UserMigration
from src.migrations.versions_migration import VersionsMigration
from src.migrations.votes_migration import VotesMigration
from src.migrations.watcher_migration import WatcherMigration
from src.migrations.work_package_content_migration import WorkPackageContentMigration
from src.migrations.work_package_migration import WorkPackageMigration
from src.migrations.work_package_skeleton_migration import WorkPackageSkeletonMigration
from src.migrations.workflow_migration import WorkflowMigration
from src.models import ComponentResult, MigrationResult
from src.type_definitions import BackupDir, ComponentName
from src.utils import data_handler

if TYPE_CHECKING:
    from src.migrations.base_migration import BaseMigration

console = Console()

# Two-Phase Migration Sequence for Proper Attachment URL Conversion
# ==================================================================
# Phase 1 (work_packages_skeleton): Creates WP structure without content
# Phase 2 (attachments): Uploads files and creates attachment_mapping.json
# Phase 3 (work_packages_content): Populates descriptions/comments with resolved attachment URLs
#
# This ensures !image.png! references in Jira convert to proper OP API URLs:
# /api/v3/attachments/{id}/content
#
DEFAULT_COMPONENT_SEQUENCE: list[ComponentName] = [
    # === Foundation: Users & Groups ===
    "users",
    "groups",
    # === Metadata: Field Definitions ===
    "custom_fields",
    "priorities",
    "link_types",
    "issue_types",
    "status_types",
    "resolutions",
    # === Organization: Companies & Accounts (Tempo) ===
    "companies",
    "accounts",
    # === Structure: Projects ===
    "projects",
    # === Agile: Workflows, Boards, Sprints ===
    "workflows",
    "agile_boards",
    "sprint_epic",
    # === Phase 1: Work Package Skeletons (no content) ===
    "work_packages_skeleton",
    # === Phase 2: Attachments (creates mapping for URL conversion) ===
    "attachments",
    "attachment_provenance",
    # === Phase 3: Work Package Content (with resolved attachment URLs) ===
    "work_packages_content",
    # === Post-WP Data: Versions, Components, Labels ===
    "versions",
    "components",
    "labels",
    "native_tags",
    # === WP Metadata: Estimates, Story Points, Security ===
    "story_points",
    "estimates",
    "security_levels",
    "affects_versions",
    "customfields_generic",
    # === WP Relationships ===
    "relations",
    "remote_links",
    "inline_refs",
    # === WP Engagement: Watchers, Votes ===
    "watchers",
    "votes_reactions",
    # === Time Tracking ===
    "time_entries",
    # === Finalization ===
    "category_defaults",
    "admin_schemes",
    "reporting",
]


PREDEFINED_PROFILES: dict[str, list[ComponentName]] = {
    "full": DEFAULT_COMPONENT_SEQUENCE.copy(),
    "metadata_refresh": [
        "projects",
        "issue_types",
        "status_types",
        "workflows",
        "agile_boards",
        "sprint_epic",
        "admin_schemes",
        "reporting",
    ],
}


# Helper: strictly detect any error condition in a component result
def _component_has_errors(result: ComponentResult | None) -> bool:  # noqa: C901, PLR0911
    """Return True if the component result contains any errors or failed items.

    This treats as error when:
    - result is None
    - success is False
    - errors list is non-empty or 'error' field is set
    - details.status is 'failed'/'error'
    - any failed counters are > 0 (failed_count, failed, failed_types, failed_issues)
    """
    if result is None:
        return True
    if not getattr(result, "success", False):
        return True
    if getattr(result, "errors", None) and len(result.errors) > 0:
        return True
    if getattr(result, "error", None):
        return True
    # Check details
    details = getattr(result, "details", None) or {}
    if isinstance(details, dict):
        status = str(details.get("status", "")).lower()
        if status in ("failed", "error", "errors"):
            return True
        if int(details.get("failed_count", 0)) > 0:
            return True
        if int(details.get("failed", 0)) > 0:
            return True
    # Check explicit counters on the model
    if int(getattr(result, "failed_count", 0)) > 0:
        return True
    if int(getattr(result, "failed", 0)) > 0:
        return True
    if int(getattr(result, "failed_types", 0)) > 0:
        return True
    return int(getattr(result, "failed_issues", 0)) > 0


# Helper: robustly extract success/failed/total counts for summaries
def _extract_counts(result: ComponentResult) -> tuple[int, int, int]:
    """Return (success_count, failed_count, total_count) using robust fallbacks.

    Priority:
    1) details.success_count/failed_count/total_count
    2) model fields success_count/failed_count/total_count
    3) derive from common detail/data shapes (e.g., total_created/total_issues,
       total_time_entries {migrated, failed}, generic 'total'/'error_count')
    4) final fallback: total = success + failed
    """
    try:
        details = getattr(result, "details", None) or {}

        # 1) Direct counts in details dict
        if isinstance(details, dict):
            d_succ = int(details.get("success_count", 0) or 0)
            d_fail = int(details.get("failed_count", 0) or 0)
            d_total = int(details.get("total_count", 0) or 0)
        else:
            d_succ = d_fail = d_total = 0

        # 2) Model-level fields
        m_succ = int(getattr(result, "success_count", 0) or 0)
        m_fail = int(getattr(result, "failed_count", 0) or 0)
        m_total = int(getattr(result, "total_count", 0) or 0)

        success_count = d_succ or m_succ
        failed_count = d_fail or m_fail
        total_count = d_total or m_total

        # 3) Derive from common shapes when still missing
        if (success_count == 0 and failed_count == 0) or total_count == 0:
            # Work packages: total_created / total_issues (either in details or data)
            def _find(key: str) -> int:
                try:
                    if isinstance(details, dict) and key in details:
                        return int(details.get(key, 0) or 0)
                except Exception:
                    pass
                data = getattr(result, "data", None)
                try:
                    if isinstance(data, dict) and key in data:
                        return int(data.get(key, 0) or 0)
                except Exception:
                    pass
                return 0

            total_created = _find("total_created")
            total_issues = _find("total_issues") or _find("total")

            if total_created or total_issues:
                success_count = success_count or total_created
                # If we have total_issues and no failed_count, derive failed
                if total_issues:
                    total_count = total_count or total_issues
                    if failed_count == 0 and success_count:
                        failed_count = max(total_count - success_count, 0)

            # Time entries: total_time_entries { migrated, failed }
            if (success_count == 0 and failed_count == 0) or total_count == 0:
                te = None
                if isinstance(details, dict):
                    te = details.get("total_time_entries")
                if te and isinstance(te, dict):
                    migrated = int(te.get("migrated", 0) or 0)
                    failed = int(te.get("failed", 0) or 0)
                    success_count = success_count or migrated
                    failed_count = failed_count or failed
                    total_count = total_count or (migrated + failed)

            # Generic totals: derive even if total is present but success/failed missing
            err_cnt = _find("error_count")
            tot = _find("total")
            if tot or err_cnt:
                if total_count == 0 and tot:
                    total_count = tot
                if failed_count == 0 and err_cnt:
                    failed_count = err_cnt
                if success_count == 0 and total_count >= failed_count:
                    success_count = total_count - failed_count

        # 4) Final fallback
        if total_count == 0:
            total_count = success_count + failed_count

        return int(success_count), int(failed_count), int(total_count)
    except Exception:
        # Extremely defensive fallback
        sc = int(getattr(result, "success_count", 0) or 0)
        fc = int(getattr(result, "failed_count", 0) or 0)
        tc = int(getattr(result, "total_count", 0) or (sc + fc))
        return sc, fc, tc


class Migration:
    """Main migration orchestrator class."""

    def __init__(self, components: list[ComponentName] | None = None) -> None:  # noqa: D107
        self.components = components or []

    async def run(
        self,
        *,
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


def _build_component_factories(
    jira_client: JiraClient,
    op_client: OpenProjectClient,
) -> dict[str, Callable[[], "BaseMigration"]]:
    """Return lazy factories for all available components.

    This avoids side-effects and log spam from constructing every component
    when only a subset was requested.
    """
    return {
        "users": lambda: UserMigration(jira_client=jira_client, op_client=op_client),
        "groups": lambda: GroupMigration(jira_client=jira_client, op_client=op_client),
        "custom_fields": lambda: CustomFieldMigration(jira_client=jira_client, op_client=op_client),
        "companies": lambda: CompanyMigration(jira_client=jira_client, op_client=op_client),
        "projects": lambda: ProjectMigration(jira_client=jira_client, op_client=op_client),
        "link_types": lambda: LinkTypeMigration(jira_client=jira_client, op_client=op_client),
        "issue_types": lambda: IssueTypeMigration(jira_client=jira_client, op_client=op_client),
        "status_types": lambda: StatusMigration(jira_client=jira_client, op_client=op_client),
        "work_packages": lambda: WorkPackageMigration(jira_client=jira_client, op_client=op_client),
        "work_packages_skeleton": lambda: WorkPackageSkeletonMigration(jira_client=jira_client, op_client=op_client),
        "work_packages_content": lambda: WorkPackageContentMigration(jira_client=jira_client, op_client=op_client),
        "time_entries": lambda: TimeEntryMigration(jira_client=jira_client, op_client=op_client),
        "watchers": lambda: WatcherMigration(jira_client=jira_client, op_client=op_client),
        "relations": lambda: RelationMigration(jira_client=jira_client, op_client=op_client),
        "priorities": lambda: PriorityMigration(jira_client=jira_client, op_client=op_client),
        "simpletasks": lambda: SimpleTasksMigration(jira_client=jira_client, op_client=op_client),
        "resolutions": lambda: ResolutionMigration(jira_client=jira_client, op_client=op_client),
        "labels": lambda: LabelsMigration(jira_client=jira_client, op_client=op_client),
        "versions": lambda: VersionsMigration(jira_client=jira_client, op_client=op_client),
        "components": lambda: ComponentsMigration(jira_client=jira_client, op_client=op_client),
        "attachments": lambda: AttachmentsMigration(jira_client=jira_client, op_client=op_client),
        "estimates": lambda: EstimatesMigration(jira_client=jira_client, op_client=op_client),
        "affects_versions": lambda: AffectsVersionsMigration(jira_client=jira_client, op_client=op_client),
        "security_levels": lambda: SecurityLevelsMigration(jira_client=jira_client, op_client=op_client),
        "votes_reactions": lambda: VotesMigration(jira_client=jira_client, op_client=op_client),
        "customfields_generic": lambda: CustomFieldsGenericMigration(jira_client=jira_client, op_client=op_client),
        "story_points": lambda: StoryPointsMigration(jira_client=jira_client, op_client=op_client),
        "sprint_epic": lambda: SprintEpicMigration(jira_client=jira_client, op_client=op_client),
        "remote_links": lambda: RemoteLinksMigration(jira_client=jira_client, op_client=op_client),
        "category_defaults": lambda: CategoryDefaultsMigration(jira_client=jira_client, op_client=op_client),
        "attachment_provenance": lambda: AttachmentProvenanceMigration(jira_client=jira_client, op_client=op_client),
        "inline_refs": lambda: InlineRefsMigration(jira_client=jira_client, op_client=op_client),
        "native_tags": lambda: NativeTagsMigration(jira_client=jira_client, op_client=op_client),
        "accounts": lambda: AccountMigration(jira_client=jira_client, op_client=op_client),
        "workflows": lambda: WorkflowMigration(jira_client=jira_client, op_client=op_client),
        "agile_boards": lambda: AgileBoardMigration(jira_client=jira_client, op_client=op_client),
        "admin_schemes": lambda: AdminSchemeMigration(jira_client=jira_client, op_client=op_client),
        "reporting": lambda: ReportingMigration(jira_client=jira_client, op_client=op_client),
    }


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
    data_dir_path: Path = config.get_path("data")

    backup_timestamp = datetime.now(UTC).strftime("%Y-%m-%d_%H-%M-%S")
    # Create backup directory
    if not backup_dir:
        backup_dir = Path(config.get_path("backups")) / f"backup_{backup_timestamp}"

    backup_dir.mkdir(parents=True, exist_ok=True)
    config.logger.info("Creating backup in: %s", backup_dir)

    # Copy all files from data directory to backup directory
    for file_path in data_dir_path.iterdir():
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
    data_dir_path: Path = config.get_path("data")

    if not backup_dir.exists():
        config.logger.error("Backup directory not found: %s", backup_dir)
        return False

    if not data_dir_path.exists():
        data_dir_path.mkdir(parents=True, exist_ok=True)

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
        except Exception as e:  # noqa: BLE001
            config.logger.warning("Could not read backup metadata: %s", e)

    # Copy all files from backup to data directory
    restored_count = 0
    for file_path in backup_dir.iterdir():
        # Skip metadata file
        if file_path.name == "backup_metadata.json":
            continue

        if file_path.is_file():
            shutil.copy2(file_path, data_dir_path)
            restored_count += 1

    config.logger.info("Restored %s files from backup", restored_count)
    return True


async def run_migration(  # noqa: C901, PLR0913, PLR0912, PLR0915
    *,
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
    try:
        # Check if we need a migration mode header
        if config.migration_config.get("dry_run", False):
            config.logger.warning(
                "Running in DRY RUN mode - no changes will be made to OpenProject",
            )
            import asyncio as _asyncio  # noqa: PLC0415

            await _asyncio.sleep(1)  # Give the user a moment to see this warning
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
        # Heuristic no-op plan detection (placeholder for future preflight analysis)
        plan_noop = False
        if (
            not config.migration_config.get(
                "no_backup",
                False,
            )
            and not config.migration_config.get("dry_run", False)
            and not plan_noop
        ):
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

        # Create clients - fail fast if config is missing
        ssh_client = SSHClient(
            host=str(config.openproject_config.get("server", "openproject.local")),
            user=config.openproject_config.get("user", None),
            key_file=(
                str(config.openproject_config.get("key_file", ""))
                if config.openproject_config.get("key_file")
                else None
            ),
        )

        docker_client = DockerClient(
            container_name=str(
                config.openproject_config.get(
                    "container_name",
                    config.openproject_config.get("container", "openproject"),
                ),
            ),
            ssh_client=ssh_client,
        )

        rails_client = RailsConsoleClient(
            tmux_session_name=config.openproject_config.get(
                "tmux_session_name",
                "rails_console",
            ),
        )

        # Get performance configuration from migration config
        performance_config = {
            "cache_size": config.migration_config.get("cache_size", 2000),
            "cache_ttl": config.migration_config.get("cache_ttl", 1800),
            "batch_size": batch_size,
            "max_workers": max_concurrent,
            "rate_limit": config.migration_config.get("rate_limit_per_sec", 15.0),
        }

        jira_client = JiraClient(**performance_config)

        # Adjust performance config for OpenProject (typically lower rates)
        op_performance_config = performance_config.copy()
        op_performance_config.update(
            {
                "cache_size": config.migration_config.get("op_cache_size", 1500),
                "cache_ttl": config.migration_config.get("op_cache_ttl", 2400),
                "batch_size": config.migration_config.get("op_batch_size", 50),
                "rate_limit": config.migration_config.get(
                    "op_rate_limit_per_sec",
                    12.0,
                ),
            },
        )

        op_client = OpenProjectClient(
            container_name=config.openproject_config.get("container", None),
            ssh_host=config.openproject_config.get("server", None),
            ssh_user=config.openproject_config.get("user", None),
            tmux_session_name=config.openproject_config.get(
                "tmux_session_name",
                None,
            ),
            # Reuse previously initialized clients to avoid duplicate init/logging
            ssh_client=ssh_client,
            docker_client=docker_client,
            rails_client=rails_client,
            **op_performance_config,
        )

        config.logger.success("All clients initialized successfully")

        # Initialize health check client for pre-migration and during-migration monitoring
        health_client = HealthCheckClient(
            ssh_client=ssh_client,
            docker_client=docker_client,
            container_name=config.openproject_config.get(
                "container_name",
                config.openproject_config.get("container", "openproject"),
            ),
        )

        # Run pre-migration health checks
        config.logger.info("Running pre-migration health checks...")
        health_ok, health_issues = health_client.run_pre_migration_checks()
        if not health_ok:
            config.logger.error("Pre-migration health checks failed:")
            for issue in health_issues:
                config.logger.error("  - %s", issue)
            # Optionally abort migration on critical health issues
            if any("CRITICAL" in issue.upper() for issue in health_issues):
                config.logger.error("Aborting migration due to critical health issues")
                results.overall["status"] = "failed"
                results.overall["health_check_failed"] = True
                results.overall["health_issues"] = health_issues
                return results
            config.logger.warning("Continuing migration despite health warnings")
            results.overall["health_warnings"] = health_issues
        else:
            config.logger.success("All pre-migration health checks passed")

        # Initialize mappings once via config accessor to avoid double loads
        # Accessing any attribute on config.mappings triggers lazy init via proxy
        _ = config.mappings.get_all_mappings()

        config.logger.info("Starting migration process...")

        # Define lazy factories for all migration components
        available_component_factories = _build_component_factories(
            jira_client=jira_client,
            op_client=op_client,
        )

        # If components parameter is not provided, use default component order
        if not components:
            components = DEFAULT_COMPONENT_SEQUENCE.copy()

        # Validate requested components and filter to supported ones
        requested_components = components or []
        available_names = set(available_component_factories.keys())
        unknown_components = [c for c in requested_components if c not in available_names]
        if unknown_components:
            config.logger.warning(
                "Unknown component(s) requested: %s. Valid components: %s",
                unknown_components,
                sorted(available_names),
            )
        components = [c for c in requested_components if c in available_component_factories]

        # Show which components will be run
        config.logger.info(
            "Migration will run the following components in order: %s",
            components,
        )

        # WorkPackageMigration is already constructed in available_components above
        # Avoid re-instantiation here to prevent duplicate initializer side-effects

        # Note: Order-aware preflight for work_packages moved to just-in-time before execution

        # Run each component in order
        try:
            for component_name in components:
                # Lazily construct the component instance
                factory = available_component_factories.get(component_name)
                if factory is None:
                    config.logger.warning("Unknown component '%s' - skipping", component_name)
                    continue
                component = factory()

                # Header for this component in logs
                print_component_header(component_name)

                # Track timing
                component_start_time = time.time()

                # Just-in-time preflight for work_packages before execution
                if component_name == "work_packages":
                    try:
                        # Critical prerequisites for WP migration
                        required = {
                            "project": bool(config.mappings.get_mapping("project")),
                            "user": bool(config.mappings.get_mapping("user")),
                            "issue_type": bool(config.mappings.get_mapping("issue_type")),
                            "issue_type_id": bool(config.mappings.get_mapping("issue_type_id")),
                            "status": bool(config.mappings.get_mapping("status")),
                        }
                        # Note: custom_field removed from required mappings per ADR 2025-10-20
                        # (Idempotency Requirements). Work packages migration now handles
                        # custom field mapping idempotently by querying OpenProject metadata
                        # when cache is missing, rather than blocking execution.
                        missing = [k for k, ok in required.items() if not ok]
                        if missing:
                            config.logger.error(
                                "Preflight failed: missing mappings for %s. Run prerequisite components first.",
                                ", ".join(missing),
                            )
                            # Record a failed component result and honor --stop-on-error
                            failed_result = ComponentResult(
                                success=False,
                                message=f"Missing required mappings: {', '.join(missing)}",
                                errors=[f"missing_mapping:{m}" for m in missing],
                                details={
                                    "status": "failed",
                                    "failed_reason": "missing_required_mappings",
                                    "missing": missing,
                                },
                            )
                            results.components[component_name] = failed_result
                            results.overall["status"] = "failed"
                            if stop_on_error:
                                break
                            # Skip executing this component but continue with others
                            continue
                    except Exception as e:  # noqa: BLE001
                        config.logger.warning("Preflight mapping check error: %s", e)

                # Run the component (diagnose if base run is invoked)
                try:
                    try:
                        from src.migrations.base_migration import (  # noqa: PLC0415
                            BaseMigration,  # local import to avoid cycles
                        )

                        if component.__class__.run is BaseMigration.run:
                            src_file = inspect.getsourcefile(component.__class__) or "<unknown>"
                            config.logger.warning(
                                "Component '%s' (%s) is using BaseMigration.run; override likely missing. "
                                "Class file: %s",
                                component_name,
                                component.__class__.__name__,
                                src_file,
                            )
                            try:
                                has_own = "run" in component.__class__.__dict__
                                config.logger.warning(
                                    "Class __dict__ has own 'run': %s; available keys (truncated): %s",
                                    has_own,
                                    sorted(component.__class__.__dict__.keys())[:12],
                                )
                                # Fallback: if this is the work_packages component and subclass didn't override run,
                                # directly call its migrate_work_packages() wrapper to proceed.
                                if (
                                    component_name == "work_packages"
                                    and not has_own
                                    and hasattr(component, "migrate_work_packages")
                                ):
                                    config.logger.warning(
                                        "Falling back to migrate_work_packages() because run() is not overridden",
                                    )
                                    data = component.migrate_work_packages()
                                    component_result = ComponentResult(
                                        success=(data.get("status") == "success"),
                                        details={
                                            "status": data.get("status", "unknown"),
                                            "total_count": data.get("total", 0),
                                            "failed_count": data.get("error_count", 0),
                                        },
                                        data=data,
                                    )
                                    # Store and continue to next component
                                    results.components[component_name] = component_result
                                    if (not component_result.success) or _component_has_errors(component_result):
                                        results.overall["status"] = "failed"
                                    # Skip the normal run() call
                                    continue
                            except Exception:  # noqa: BLE001, S110
                                pass
                    except Exception:  # noqa: BLE001, S110
                        pass

                    # j2o-50: Use idempotent workflow with caching by default
                    # All migrations now use run_with_change_detection() as the standard approach
                    # This provides: thread-safe caching, change detection, API call reduction

                    # Try to determine entity_type for the component
                    entity_type = None
                    try:
                        from src.migrations.base_migration import EntityTypeRegistry

                        entity_type = EntityTypeRegistry.resolve(component.__class__)
                    except (ValueError, AttributeError):
                        # If entity type can't be resolved, run_with_change_detection will fall back to run()
                        pass

                    try:
                        # Call run_with_change_detection() which provides:
                        # - Thread-safe cached entity retrieval (30-50% API reduction)
                        # - Change detection to skip unnecessary migrations (25-35% performance gain)
                        # - Automatic snapshot creation for rollback capability
                        component_result = component.run_with_change_detection(entity_type=entity_type)
                    except AttributeError:
                        # Fallback for transformation-only migrations that don't support idempotent workflow
                        config.logger.debug(
                            f"Component '{component_name}' does not implement run_with_change_detection(), "
                            f"using legacy run() method",
                        )
                        component_result = component.run()

                    if component_result:
                        # Store result in the results dictionary
                        results.components[component_name] = component_result

                        # Mapping controller now handles consistency; no ad-hoc reloads needed

                        # Update overall status if component failed OR has errors
                        if (not component_result.success) or _component_has_errors(component_result):
                            results.overall["status"] = "failed"

                        # Print component summary based on robust count extraction
                        details = component_result.details or {}
                        success_count, failed_count, total_count = _extract_counts(component_result)
                        component_time = details.get("time", details.get("duration_seconds", 0))

                        # Logging
                        had_errors = _component_has_errors(component_result)
                        if component_result.success and not had_errors:
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
                            "Component '%s' did not return a result",
                            component_name,
                        )
                        results.components[component_name] = component_result

                except KeyboardInterrupt:
                    # Handle user interruption within a component
                    config.logger.warning(
                        "Component '%s' was interrupted by user",
                        component_name,
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

                except Exception as e:  # noqa: BLE001
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
                            f"Component '{component_name}' failed with exception and "
                            f"--stop-on-error is set, aborting migration",
                        )
                        break

                # Check if component failed or has any errors and we should stop on error (before user confirmation)
                component_result_obj = results.components.get(component_name)
                if stop_on_error and _component_has_errors(component_result_obj):
                    config.logger.error(
                        f"Component '{component_name}' reported errors and --stop-on-error is set, aborting migration",
                    )
                    break

                # During-migration health check after each component
                try:
                    health_status = health_client.run_during_migration_check()
                    if not health_status["healthy"]:
                        for warning in health_status.get("warnings", []):
                            config.logger.warning("Health check: %s", warning)
                        # Auto-cleanup temp files if threshold exceeded
                        if health_status.get("temp_files_exceeded"):
                            config.logger.info("Auto-cleaning old temp files...")
                            cleanup_result = health_client.cleanup_temp_files(max_age_minutes=30)
                            if cleanup_result.success:
                                config.logger.info(
                                    "Cleaned up %d temp files, freed %s",
                                    cleanup_result.files_removed,
                                    cleanup_result.space_freed_human,
                                )
                            else:
                                config.logger.warning("Temp file cleanup failed: %s", cleanup_result.error)
                except Exception as health_err:  # noqa: BLE001
                    config.logger.debug("During-migration health check failed: %s", health_err)

                # Pause for user confirmation between components
                if (
                    component_name != components[-1] and not no_confirm
                ):  # Skip after the last component or if no_confirm is set
                    try:
                        result: str = "[dim]UNKNOWN RESULT[/dim]"
                        current_result = results.components.get(component_name)
                        if current_result:
                            if hasattr(current_result, "success"):
                                if current_result.success:
                                    result = "[bold green]SUCCEEDED[/bold green]"
                                else:
                                    result = "[bold red]FAILED[/bold red]"
                            if hasattr(current_result, "errors") and current_result.errors:
                                result = f"[bold red]FAILED[/bold red] with errors: {current_result.errors}"

                        console.rule(f"Component '{component_name}' has {result}.")

                        if results.overall["status"] != "success":
                            console.print("WARNING: Previous component had errors.")

                        next_component = components[components.index(component_name) + 1]

                        user_input = (
                            input(
                                f"\nContinue to next component: {next_component}? [Y/n]: ",
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
                    component_name,
                    ComponentResult(),
                )

                # Check if component failed - use success flag as primary indicator
                component_failed = not current_component_result.success if current_component_result else True

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

        focus_components = [
            "workflows",
            "agile_boards",
            "sprint_epic",
            "admin_schemes",
            "reporting",
        ]
        summary_payload: dict[str, Any] = {
            "timestamp": migration_timestamp,
            "overall_status": results.overall.get("status"),
            "components": {},
        }
        for component_name in focus_components:
            component_result = results.components.get(component_name)
            if not component_result:
                continue
            summary_payload["components"][component_name] = {
                "success": component_result.success,
                "message": component_result.message,
                "details": component_result.details,
            }

        if summary_payload["components"]:
            summary_file = f"migration_summary_{migration_timestamp}.json"
            data_handler.save_results(
                summary_payload,
                filename=summary_file,
            )
            config.logger.info(
                "Focused migration summary saved to %s",
                summary_file,
            )

        # Post-migration cleanup and final health check
        try:
            config.logger.info("Running post-migration cleanup...")
            cleanup_result = health_client.cleanup_temp_files(max_age_minutes=5)
            if cleanup_result.success and cleanup_result.files_removed > 0:
                config.logger.info(
                    "Post-migration cleanup: removed %d temp files, freed %s",
                    cleanup_result.files_removed,
                    cleanup_result.space_freed_human,
                )
            # Get final health snapshot for results
            final_snapshot = health_client.get_health_snapshot()
            results.overall["final_health"] = {
                "container_disk_free_mb": final_snapshot.container_disk_free_mb,
                "temp_file_count": final_snapshot.temp_file_count,
                "local_disk_free_mb": final_snapshot.local_disk_free_mb,
                "remote_disk_free_mb": final_snapshot.remote_disk_free_mb,
            }
        except Exception as cleanup_err:  # noqa: BLE001
            config.logger.debug("Post-migration cleanup failed: %s", cleanup_err)

        # Print final status
        if results.overall.get("status") == "success":
            config.logger.success(
                f"Migration completed successfully in {total_seconds:.2f} seconds.",
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
        config.logger.info("Migration complete")
        return results

    except Exception as e:  # noqa: BLE001
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
        help=(
            "Force fresh extraction and mapping re-generation (skip disk caches). "
            "Does not force re-writing into OpenProject; keeps in-run in-memory caches; "
            "also overrides pre-migration validation/security gating."
        ),
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
    from src import config  # noqa: PLC0415

    session_name = config.openproject_config.get("tmux_session_name", "rails_console")

    config.logger.info(
        "Setting up tmux session '%s' for Rails console...",
        session_name,
    )

    try:
        # Check if tmux is installed
        import shutil as _shutil  # noqa: PLC0415

        tmux_bin = _shutil.which("tmux") or "tmux"
        subprocess.run([tmux_bin, "-V"], check=True, capture_output=True)  # noqa: S603

        # Check if session already exists
        result = subprocess.run(  # noqa: S603
            [tmux_bin, "has-session", "-t", session_name],
            check=False,
            capture_output=True,
        )

        if result.returncode == 0:
            config.logger.warning("tmux session '%s' already exists", session_name)
            config.logger.info("To attach to this session, run:")
            config.logger.info("tmux attach -t %s", session_name)
            return True

        # Create a new session
        subprocess.run([tmux_bin, "new-session", "-d", "-s", session_name], check=True)  # noqa: S603

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
                    "cd /path/to/openproject && bundle exec rails console",
                )

        config.logger.info(
            "After running Rails console, you can use the direct migration features.",
        )

        return True  # noqa: TRY300
    except (subprocess.SubprocessError, FileNotFoundError):
        config.logger.error("tmux is not installed or not available in PATH")
        config.logger.info("Please install tmux first:")
        config.logger.info("  On Ubuntu/Debian: sudo apt-get install tmux")
        config.logger.info("  On CentOS/RHEL: sudo yum install tmux")
        config.logger.info("  On macOS with Homebrew: brew install tmux")
        return False


def main() -> None:  # noqa: C901, PLR0912, PLR0915
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
                    "tmux_session_name",
                    "rails_console",
                ),
            )

            # Use default performance config for main() function
            default_performance_config = {
                "cache_size": config.migration_config.get("cache_size", 2000),
                "cache_ttl": config.migration_config.get("cache_ttl", 1800),
                "batch_size": config.migration_config.get("batch_size", 100),
                "max_workers": config.migration_config.get("max_workers", 5),
                "rate_limit": config.migration_config.get("rate_limit_per_sec", 15.0),
            }

            jira_client = JiraClient(**default_performance_config)

            # Adjust performance config for OpenProject
            op_performance_config = default_performance_config.copy()
            op_performance_config.update(
                {
                    "cache_size": config.migration_config.get("op_cache_size", 1500),
                    "cache_ttl": config.migration_config.get("op_cache_ttl", 2400),
                    "batch_size": config.migration_config.get("op_batch_size", 50),
                    "rate_limit": config.migration_config.get(
                        "op_rate_limit_per_sec",
                        12.0,
                    ),
                },
            )

            op_client = OpenProjectClient(
                container_name=config.openproject_config.get("container", None),
                ssh_host=config.openproject_config.get("server", None),
                ssh_user=config.openproject_config.get("user", None),
                tmux_session_name=config.openproject_config.get(
                    "tmux_session_name",
                    None,
                ),
                **op_performance_config,
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
                            jira_client=jira_client,
                            op_client=op_client,
                        )
                        cf_mapping_update_result = custom_field_migration.update_mapping_file()
                        if cf_mapping_update_result:
                            config.logger.success(
                                "Custom field mapping updated successfully.",
                            )
                        else:
                            config.logger.warning(
                                "No updates were made to custom field mapping.",
                            )
                        break
                    if choice == "2":
                        issue_type_migration = IssueTypeMigration(
                            jira_client=jira_client,
                            op_client=op_client,
                        )
                        issue_type_mapping_update_result = issue_type_migration.update_mapping_file()
                        if issue_type_mapping_update_result:
                            config.logger.success(
                                "Issue type mapping updated successfully.",
                            )
                        else:
                            config.logger.warning(
                                "No updates were made to issue type mapping.",
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
        migration_result = asyncio.run(
            run_migration(
                components=args.components,
                stop_on_error=getattr(args, "stop_on_error", False),
                no_confirm=getattr(args, "no_confirm", False),
                batch_size=getattr(args, "batch_size", 100),
                max_concurrent=getattr(args, "max_concurrent", 5),
                enable_performance_optimization=not getattr(
                    args,
                    "no_performance_optimization",
                    False,
                ),
            ),
        )

        # Display migration results summary
        if migration_result:
            # Access overall dict's status key (overall is a dict, not an object)
            overall_status = migration_result.overall.get("status", "unknown") if hasattr(migration_result, "overall") else "unknown"

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
            component_items = migration_result.components.items() if hasattr(migration_result, "components") else {}
            for component, comp_result in component_items:
                # Access status from details
                status = comp_result.details.get("status", "unknown") if comp_result.details else "unknown"
                if status == "success":
                    config.logger.success("✓ %s: %s", component, status)
                elif status == "interrupted":
                    config.logger.warning("⚠ %s: %s", component, status)
                else:
                    config.logger.error("✗ %s: %s", component, status)

            # Show performance summary if available
            if (
                hasattr(migration_result, "overall")
                and isinstance(migration_result.overall, dict)
                and "performance_summary" in migration_result.overall
            ):
                perf_summary = migration_result.overall["performance_summary"]
                timing = perf_summary.get("timing", {})
                throughput = perf_summary.get("throughput", {})

                config.logger.info("Performance Summary:")
                config.logger.info(
                    f"  Total processing time: {timing.get('total_time_seconds', 0):.2f}s",
                )
                config.logger.info(
                    f"  Overall throughput: {throughput.get('items_per_second', 0):.1f} items/sec",
                )
                config.logger.info(
                    f"  Success rate: {throughput.get('success_rate', 0):.1%}",
                )
                config.logger.info(
                    f"  Processing efficiency: {timing.get('processing_efficiency', 0):.1%}",
                )

    except KeyboardInterrupt:
        console.print("\nMigration manually interrupted. Exiting...")
        sys.exit(0)


if __name__ == "__main__":
    main()
