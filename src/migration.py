# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Master migration script for Jira to OpenProject migration.

This script orchestrates the complete migration process with performance optimization.
"""

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console

from src import config
from src.application.components.account_migration import AccountMigration
from src.application.components.admin_scheme_migration import AdminSchemeMigration
from src.application.components.affects_versions_migration import AffectsVersionsMigration
from src.application.components.agile_board_migration import AgileBoardMigration
from src.application.components.attachment_provenance_migration import AttachmentProvenanceMigration
from src.application.components.attachment_recovery_migration import AttachmentRecoveryMigration
from src.application.components.attachments_migration import AttachmentsMigration
from src.application.components.base_migration import BaseMigration, EntityTypeRegistry
from src.application.components.category_defaults_migration import CategoryDefaultsMigration
from src.application.components.company_migration import CompanyMigration
from src.application.components.components_migration import ComponentsMigration
from src.application.components.custom_field_migration import CustomFieldMigration
from src.application.components.customfields_generic_migration import CustomFieldsGenericMigration
from src.application.components.estimates_migration import EstimatesMigration
from src.application.components.group_migration import GroupMigration
from src.application.components.inline_refs_migration import InlineRefsMigration
from src.application.components.issue_type_migration import IssueTypeMigration
from src.application.components.labels_migration import LabelsMigration
from src.application.components.link_type_migration import LinkTypeMigration
from src.application.components.native_boards_migration import NativeBoardsMigration
from src.application.components.native_tags_migration import NativeTagsMigration
from src.application.components.priority_migration import PriorityMigration
from src.application.components.project_migration import ProjectMigration

# Re-exports for backward compatibility. Tests and src/main.py historically
# import these names from src.migration; the registry is now their canonical
# home but src.migration must keep resolving them until callers migrate.
# Only the data constants are re-exported — register_component / build_factories
# are intentionally NOT re-exported here, to keep components importing them
# from the canonical src.application.components.registry path and avoid
# circular-import risk through this legacy module.
from src.application.components.registry import (
    DEFAULT_COMPONENT_SEQUENCE,
    PREDEFINED_PROFILES,  # noqa: F401  (re-exported for src/main.py and tests)
)
from src.application.components.relation_migration import RelationMigration
from src.application.components.remote_links_migration import RemoteLinksMigration
from src.application.components.reporting_migration import ReportingMigration
from src.application.components.resolution_migration import ResolutionMigration
from src.application.components.security_levels_migration import SecurityLevelsMigration
from src.application.components.simpletasks_migration import SimpleTasksMigration
from src.application.components.sprint_epic_migration import SprintEpicMigration
from src.application.components.status_migration import StatusMigration
from src.application.components.story_points_migration import StoryPointsMigration
from src.application.components.time_entry_migration import TimeEntryMigration
from src.application.components.user_mapping_backfill_migration import UserMappingBackfillMigration
from src.application.components.user_migration import UserMigration
from src.application.components.versions_migration import VersionsMigration
from src.application.components.votes_migration import VotesMigration
from src.application.components.watcher_migration import WatcherMigration
from src.application.components.work_package_content_migration import WorkPackageContentMigration
from src.application.components.work_package_migration import WorkPackageMigration
from src.application.components.work_package_skeleton_migration import WorkPackageSkeletonMigration
from src.application.components.workflow_migration import WorkflowMigration
from src.application.components.wp_metadata_backfill_migration import WpMetadataBackfillMigration
from src.infrastructure.health_check_client import HealthCheckClient
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.docker_client import DockerClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.infrastructure.openproject.rails_console_client import RailsConsoleClient
from src.infrastructure.openproject.ssh_client import SSHClient
from src.migration_progress import MigrationRunProgress
from src.models import ComponentResult, MigrationResult
from src.type_definitions import BackupDir, ComponentName
from src.utils import data_handler

_BACKUP_METADATA_FILE = "backup_metadata.json"


console = Console()

# DEFAULT_COMPONENT_SEQUENCE and PREDEFINED_PROFILES live in
# ``src.application.components.registry`` (see Phase 1 of the
# migration.py split plan). They are re-exported above so callers that
# still import from ``src.migration`` keep working.


def _ensure_provenance_bootstrap(op_client: Any, logger: Any) -> None:
    """Pre-create the J2O provenance custom fields before any component runs.

    Without this, only ``J2O Origin Key`` survives via lazy on-demand
    creation in ``work_package_migration``; the User and TimeEntry
    provenance CFs are never created at all, breaking time-entry
    idempotency (Bug C) and round-trip recovery.

    Failures are logged but not fatal — partial provenance is better than
    no migration.
    """
    try:
        op_client.ensure_origin_custom_fields()
    except Exception as exc:
        logger.warning("Failed to ensure provenance custom fields: %s", exc)


def _backfill_user_mapping_at_startup(op_client: Any, mappings: Any, logger: Any) -> None:
    """Back-fill ``openproject_id`` for ``matched_by="none"`` users (Bug A2).

    Runs at migration startup — *before* any component executes — so it
    happens even when the ``users`` component itself gets skipped by
    change detection (e.g. ``baseline=current`` so the change-aware
    runner short-circuits to "no changes"). Without this, downstream
    WP/TimeEntry author/assignee mapping silently drops every user that
    the initial probe couldn't match.

    Failures are best-effort — degraded mapping is still better than no
    migration.
    """
    try:
        from src.application.components.user_migration import (
            _backfill_unmapped_users_from_op,
        )

        user_mapping = mappings.get_mapping("user") or {}
        if not user_mapping:
            return
        n = _backfill_unmapped_users_from_op(user_mapping, op_client, logger)
        if n:
            mappings.set_mapping("user", user_mapping)
    except Exception as exc:
        logger.warning("User-mapping startup back-fill skipped: %s", exc)


# Helper: detect a "real" error condition in a component result.
#
# Partial-success policy: a migration that returns ``success=True`` and has
# *some* successful items is treated as complete-with-warnings even if it
# also reports failures. The component's ``run()`` had final say on whether
# its work is done. Only when the component itself signals failure (via
# ``success=False`` / ``errors``-list / explicit ``error`` / failed status)
# OR when failures exist with **zero** successes is the component flagged
# as errored.
def _count_value(source: dict, key: str) -> int:
    return int(source.get(key) or 0)


def _source_count(details: dict, data: dict, key: str) -> int:
    for source in (details, data):
        if key in source:
            try:
                return _count_value(source, key)
            except TypeError, ValueError, OverflowError:
                continue
    return 0


def _component_has_errors(result: ComponentResult | None) -> bool:
    if not result or not result.success or getattr(result, "errors", None) or getattr(result, "error", None):
        return True
    details = result.details if isinstance(result.details, dict) else {}
    if str(details.get("status", "")).lower() in ("failed", "error", "errors"):
        return True
    succeeded, failed, _ = _extract_counts(result)
    if succeeded:
        return False
    legacy = sum(int(getattr(result, key, 0) or 0) for key in ("failed", "failed_types", "failed_issues"))
    return failed > 0 or legacy + _count_value(details, "failed") > 0


def _time_entry_counts(details: dict, counts: tuple[int, int, int]) -> tuple[int, int, int]:
    succeeded, failed, total = counts
    time_entries = details.get("total_time_entries")
    if isinstance(time_entries, dict) and (not total or not (succeeded or failed)):
        migrated = _count_value(time_entries, "migrated")
        errors = _count_value(time_entries, "failed")
        succeeded = succeeded or migrated
        failed = failed or errors
        total = total or migrated + errors
    return succeeded, failed, total


def _derive_counts(result: ComponentResult, counts: tuple[int, int, int], details: dict) -> tuple[int, int, int]:
    succeeded, failed, total = counts
    data = result.data if isinstance(result.data, dict) else {}
    succeeded = succeeded or _source_count(details, data, "total_created")
    issue_count = _source_count(details, data, "total_issues") or _source_count(details, data, "total")
    if issue_count:
        total = total or issue_count
        if succeeded and not failed:
            failed = max(total - succeeded, 0)
    succeeded, failed, total = _time_entry_counts(details, (succeeded, failed, total))
    generic_total = _source_count(details, data, "total")
    generic_errors = _source_count(details, data, "error_count")
    if generic_total or generic_errors:
        total = total or generic_total
        failed = failed or generic_errors
        succeeded = succeeded or max(total - failed, 0)
    return succeeded, failed, total or succeeded + failed


def _extract_counts(result: ComponentResult) -> tuple[int, int, int]:
    """Prefer explicit detail/model counters, then derive counts from older component reports."""
    details = result.details if isinstance(result.details, dict) else {}
    names = ("success_count", "failed_count", "total_count")
    model_counts = tuple(int(getattr(result, key, 0) or 0) for key in names)
    try:
        succeeded, failed, total = (
            _count_value(details, key) or count for key, count in zip(names, model_counts, strict=True)
        )
        if total and (succeeded or failed):
            return succeeded, failed, total
        return _derive_counts(result, (succeeded, failed, total), details)
    except TypeError, ValueError, OverflowError:
        succeeded, failed, total = model_counts
        return succeeded, failed, total or succeeded + failed


def _first_error_message(result: ComponentResult | None) -> str:
    """Return the most informative cause message from a result, or ''.

    Search order: ``result.error`` → ``result.errors[0]`` →
    ``details['error']`` → ``result.message``.

    Used by :func:`_format_component_outcome` to surface *why* a
    component failed inline in the per-component summary log, so users
    don't have to scroll back to find the real cause.
    """
    if result is None:
        return ""
    err = getattr(result, "error", None)
    if err:
        return str(err)
    errors = getattr(result, "errors", None)
    # Guard against ``errors=[""]`` or other falsy first elements — if the
    # list is non-empty but the first entry is empty, fall through to the
    # remaining sources rather than returning "".
    if isinstance(errors, list) and errors and errors[0]:
        return str(errors[0])
    details = getattr(result, "details", None) or {}
    if isinstance(details, dict):
        derr = details.get("error")
        if derr:
            return str(derr)
    msg = getattr(result, "message", None)
    if msg:
        return str(msg)
    return ""


def _format_component_outcome(
    name: str,
    result: ComponentResult,
    elapsed: float | None,
) -> tuple[str, str]:
    """Build the per-component summary line.

    Returns a ``(level, message)`` pair. ``level`` is ``"success"``,
    ``"warning"``, or ``"error"`` so the caller can pick the right log
    method. Partial success (``success=True`` with some failed items)
    surfaces as a *warning*, not a fake success; full failure includes
    the underlying cause inline so the user doesn't have to scroll back
    through the log to find it.
    """
    success_count, failed_count, total_count = _extract_counts(result)
    counts = f"{success_count}/{total_count} items migrated"
    if failed_count:
        counts += f", {failed_count} failed"
    # ``details.get("time")`` can return ``None`` for components that didn't
    # record a duration; ``{None:.2f}`` would crash. Use ``is not None`` so a
    # legitimate ``0`` (or ``0.0``) is preserved.
    elapsed_seconds = elapsed if elapsed is not None else 0.0
    time_part = f"took {elapsed_seconds:.2f} seconds"

    has_errors = _component_has_errors(result)
    if getattr(result, "success", False) and not has_errors:
        return "success", (f"Component '{name}' completed successfully ({counts}), {time_part}")

    cause = _first_error_message(result)
    cause_part = f": {cause}" if cause else ""

    if getattr(result, "success", False):
        return "warning", (f"Component '{name}' completed with errors{cause_part} ({counts}), {time_part}")

    return "error", (f"Component '{name}' FAILED{cause_part} ({counts}), {time_part}")


# ---------------------------------------------------------------------------
# Dry-run safety registry (issue #260, PR D)
#
# This frozenset enumerates the components that internally honour
# ``config.migration_config["dry_run"]`` and skip OpenProject writes when
# the flag is set. Each named component also declares
# ``DRY_RUN_SAFE: ClassVar[bool] = True`` on its class — the drift guard
# at ``tests/unit/test_dry_run_safety_gate.py::TestRegistryClassAttributeDriftGuard``
# keeps the two views in sync.
#
# All other components write to OpenProject regardless of ``--dry-run``;
# the startup gate aborts when any of them is in scope, unless the
# operator passes ``--allow-unsafe-dry-run`` to acknowledge the risk.
# ---------------------------------------------------------------------------
DRY_RUN_SAFE_COMPONENTS: frozenset[str] = frozenset(
    {
        "projects",
        "issue_types",
        "link_types",
        "companies",
        "status_types",
    },
)


def _dry_run_safety_partition(requested: list[str]) -> tuple[list[str], list[str]]:
    """Split requested component names into (safe, unsafe) under ``--dry-run``.

    Unknown component names fall into ``unsafe`` — fail-closed for a flag
    whose contract is "no changes to OpenProject".
    """
    safe = [name for name in requested if name in DRY_RUN_SAFE_COMPONENTS]
    unsafe = [name for name in requested if name not in DRY_RUN_SAFE_COMPONENTS]
    return safe, unsafe


def _build_dry_run_banner(
    *,
    requested: list[str],
    allow_unsafe: bool,
) -> tuple[str, list[str], bool]:
    """Build the dry-run startup banner.

    Returns ``(level, lines, abort)``:

    * ``level`` — ``"warning"`` or ``"error"``; selects the logger.
    * ``lines`` — log lines to emit, or ``[]`` for nothing to say.
    * ``abort`` — ``True`` when the orchestrator should refuse to run.
    """
    if not requested:
        return "warning", [], False

    safe, unsafe = _dry_run_safety_partition(requested)

    if not unsafe:
        return (
            "warning",
            [
                "Running in DRY RUN mode.",
                f"All requested components honour --dry-run: {', '.join(safe)}.",
                "No changes will be made to OpenProject for these components.",
            ],
            False,
        )

    if not allow_unsafe:
        return (
            "error",
            [
                (
                    "Refusing to run --dry-run: the following requested components "
                    "do NOT honour the flag and would write to OpenProject:"
                ),
                *(f"  - {name}" for name in unsafe),
                "",
                (
                    "Either restrict the run to dry-run-safe components "
                    f"({', '.join(sorted(DRY_RUN_SAFE_COMPONENTS))}), or pass "
                    "--allow-unsafe-dry-run to acknowledge that real writes will "
                    "still happen for the components above."
                ),
            ],
            True,
        )

    safe_line = f"Safe (honour --dry-run): {', '.join(safe) or 'none'}." if safe else "Safe (honour --dry-run): none."
    return (
        "warning",
        [
            "Running in DRY RUN mode with --allow-unsafe-dry-run.",
            safe_line,
            "The following components will still write to OpenProject:",
            *(f"  - {name}" for name in unsafe),
        ],
        False,
    )


class Migration:
    """Main migration orchestrator class."""

    def __init__(self, components: list[ComponentName] | None = None) -> None:
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
) -> dict[str, Callable[[], BaseMigration]]:
    """Return lazy factories for all available components.

    This avoids side-effects and log spam from constructing every component
    when only a subset was requested.
    """
    return {
        "users": lambda: UserMigration(jira_client=jira_client, op_client=op_client),
        "user_mapping_backfill": lambda: UserMappingBackfillMigration(jira_client=jira_client, op_client=op_client),
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
        "attachment_recovery": lambda: AttachmentRecoveryMigration(jira_client=jira_client, op_client=op_client),
        "wp_metadata_backfill": lambda: WpMetadataBackfillMigration(jira_client=jira_client, op_client=op_client),
        "inline_refs": lambda: InlineRefsMigration(jira_client=jira_client, op_client=op_client),
        "native_tags": lambda: NativeTagsMigration(jira_client=jira_client, op_client=op_client),
        "accounts": lambda: AccountMigration(jira_client=jira_client, op_client=op_client),
        "workflows": lambda: WorkflowMigration(jira_client=jira_client, op_client=op_client),
        "agile_boards": lambda: AgileBoardMigration(jira_client=jira_client, op_client=op_client),
        "native_boards": lambda: NativeBoardsMigration(jira_client=jira_client, op_client=op_client),
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

    with (backup_dir / _BACKUP_METADATA_FILE).open("w") as f:
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
    metadata_path = backup_dir / _BACKUP_METADATA_FILE
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
        if file_path.name == _BACKUP_METADATA_FILE:
            continue

        if file_path.is_file():
            shutil.copy2(file_path, data_dir_path)
            restored_count += 1

    config.logger.info("Restored %s files from backup", restored_count)
    return True


async def _announce_migration_mode(effective_components: list[str]) -> None:
    if config.migration_config.get("dry_run", False):
        allow_unsafe = bool(config.migration_config.get("allow_unsafe_dry_run", False))
        level, lines, abort = _build_dry_run_banner(
            requested=effective_components,
            allow_unsafe=allow_unsafe,
        )
        log = config.logger.error if level == "error" else config.logger.warning
        for line in lines:
            log(line)
        if abort:
            msg = (
                "Aborting --dry-run: requested components include "
                "unsafe migrations. See log above; pass "
                "--allow-unsafe-dry-run to acknowledge."
            )
            raise SystemExit(msg)
        if lines:
            await asyncio.sleep(1)  # Give the user a moment to see this warning
        mode = "DRY RUN"
    else:
        mode = "PRODUCTION"

    config.logger.info(
        f"Starting Jira to OpenProject migration - mode='{mode}'",
    )


def _create_migration_clients(
    batch_size: int, max_concurrent: int
) -> tuple[JiraClient, OpenProjectClient, HealthCheckClient]:
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
            str(config.openproject_config.get("key_file", "")) if config.openproject_config.get("key_file") else None
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
    return jira_client, op_client, health_client


def _check_initial_health(health_client: HealthCheckClient, results: MigrationResult) -> bool:
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
            return False
        config.logger.warning("Continuing migration despite health warnings")
        results.overall["health_warnings"] = health_issues
    else:
        config.logger.success("All pre-migration health checks passed")
    return True


def _work_package_preflight() -> ComponentResult | None:
    required = ("project", "user", "issue_type", "issue_type_id", "status")
    missing = [name for name in required if not config.mappings.get_mapping(name)]
    if not missing:
        return None
    message = f"Missing required mappings: {', '.join(missing)}"
    config.logger.error("Preflight failed: %s. Run prerequisite components first.", message)
    return ComponentResult(
        success=False,
        message=message,
        errors=[f"missing_mapping:{name}" for name in missing],
        details={"status": "failed", "failed_reason": "missing_required_mappings", "missing": missing},
    )


def _invoke_component(name: str, component: BaseMigration) -> ComponentResult:
    if name == "work_packages" and getattr(type(component), "run", None) is BaseMigration.run:
        legacy = getattr(component, "migrate_work_packages", None)
        if legacy is not None:
            data = legacy()
            return ComponentResult(
                success=data.get("status") == "success",
                data=data,
                details={
                    "status": data.get("status", "unknown"),
                    "total_count": data.get("total", 0),
                    "failed_count": data.get("error_count", 0),
                },
            )
    run_with_changes = getattr(component, "run_with_change_detection", None)
    if run_with_changes is None:
        return component.run()
    try:
        entity_type = EntityTypeRegistry.resolve(type(component))
    except ValueError, AttributeError:
        entity_type = None
    return run_with_changes(entity_type=entity_type)


def _run_component(
    name: str, component: BaseMigration, progress: MigrationRunProgress | None = None
) -> ComponentResult:
    start = time.monotonic()
    try:
        if isinstance(component, TimeEntryMigration) and progress is not None and progress.path is not None:
            component.time_entry_migrator.on_worklog_read = progress.worklog_reads
        result = _invoke_component(name, component)
        if result is None:
            return ComponentResult(success=False, message=f"Component '{name}' did not return a result")
        details = result.details or {}
        duration = details.get("time", details.get("duration_seconds", time.monotonic() - start))
        level, message = _format_component_outcome(name, result, duration)
        getattr(config.logger, level)(message)
        return result
    except KeyboardInterrupt:
        config.logger.warning("Component '%s' was interrupted by user", name)
        return ComponentResult(
            success=False,
            message="Component was interrupted by user",
            details={"status": "interrupted", "time": time.monotonic() - start},
        )
    except Exception as exc:
        config.logger.exception("Error during '%s' migration: %s", name, exc)
        return ComponentResult(
            success=False,
            errors=[str(exc)],
            message=f"Error during component execution: {exc}",
            details={"status": "failed", "time": time.monotonic() - start, "error": str(exc)},
        )


def _monitor_health(health_client: HealthCheckClient) -> None:
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
    except Exception as health_err:
        config.logger.debug("During-migration health check failed: %s", health_err)


async def _continue_after_component(name: str, next_name: str, results: MigrationResult) -> bool:
    component = results.components[name]
    outcome = "[bold green]SUCCEEDED[/bold green]" if component.success else "[bold red]FAILED[/bold red]"
    if component.errors:
        outcome = f"[bold red]FAILED[/bold red] with errors: {component.errors}"
    console.rule(f"Component '{name}' has {outcome}.")
    if results.overall["status"] != "success":
        console.print("WARNING: Previous component had errors.")
    answer = await asyncio.to_thread(input, f"\nContinue to next component: {next_name}? [Y/n]: ")
    if answer.strip().lower() in ("", "y", "yes"):
        return True
    config.logger.warning("Migration paused by user")
    results.overall.update(status="interrupted", message="Migration was paused by user")
    return False


def _component_status(result: ComponentResult) -> str:
    if result.details.get("status") == "interrupted":
        return "paused"
    if _component_has_errors(result):
        return "failed"
    if result.details.get("status") == "skipped":
        return "skipped"
    return "completed"


def _must_stop(
    name: str, result: ComponentResult, status: str, results: MigrationResult, *, stop_on_error: bool
) -> bool:
    if status == "paused":
        results.overall["status"] = "interrupted"
        return True
    if status != "failed":
        return False
    results.overall["status"] = "failed"
    if stop_on_error:
        config.logger.error("Component '%s' reported errors and --stop-on-error is set, aborting migration", name)
        return True
    if not result.success and name in ("users", "projects"):
        config.logger.error("Critical component '%s' failed, aborting migration", name)
        return True
    return False


async def _run_components(
    names: list[str],
    factories: dict[str, Callable[[], BaseMigration]],
    results: MigrationResult,
    health_client: HealthCheckClient,
    progress: MigrationRunProgress,
    *,
    stop_on_error: bool,
    no_confirm: bool,
) -> None:
    try:
        progress.plan(names)
        for index, name in enumerate(names):
            progress.start_component(index)
            print_component_header(name)
            preflight = _work_package_preflight() if name == "work_packages" else None
            result = preflight or _run_component(name, factories[name](), progress)
            results.components[name] = result
            status = _component_status(result)
            progress.end_component(name, result, status)
            if _must_stop(name, result, status, results, stop_on_error=stop_on_error):
                break
            if preflight:
                continue
            _monitor_health(health_client)
            if not no_confirm and index + 1 < len(names):
                if not await _continue_after_component(name, names[index + 1], results):
                    break
    except KeyboardInterrupt:
        config.logger.warning("Migration interrupted by user")
        results.overall.update(status="interrupted", message="Migration was interrupted by user")


def _finish_migration(results: MigrationResult, migration_timestamp: str, health_client: HealthCheckClient) -> None:
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
    except Exception as cleanup_err:
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


async def run_migration(
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
    progress = MigrationRunProgress()
    try:
        progress.start(dry_run=bool(config.migration_config.get("dry_run", False)))
        # Resolve the effective component list NOW so the dry-run gate
        # sees the same set the orchestrator will execute. Without this,
        # a bare ``--dry-run`` (no ``--components``, no ``--profile``)
        # arrives with ``components=None``; default expansion happens
        # later in this function, and the gate would see an empty list
        # and let the run proceed — re-introducing the #260 honesty bug.
        effective_components = list(components) if components else list(DEFAULT_COMPONENT_SEQUENCE)

        await _announce_migration_mode(effective_components)
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

        if not config.migration_config.get("no_backup", False) and not config.migration_config.get("dry_run", False):
            backup_path = create_backup()
            if backup_path:
                results.overall["backup_path"] = backup_path
                config.logger.success("Backup created at: %s", backup_path)

        jira_client, op_client, health_client = _create_migration_clients(batch_size, max_concurrent)
        if not _check_initial_health(health_client, results):
            progress.finish(results.overall["status"])
            return results
        # Initialize mappings once via config accessor to avoid double loads
        # Accessing any attribute on config.mappings triggers lazy init via proxy
        _ = config.mappings.get_all_mappings()

        # Bootstrap provenance custom fields once. Without this only the
        # ``J2O Origin Key`` WP CF gets created (lazily, on first WP); the
        # User and TimeEntry CFs are never created at all, breaking
        # time-entry idempotency on re-runs (Bug C) and round-trip
        # recovery from OP without local mapping files.
        _ensure_provenance_bootstrap(op_client, config.logger)

        # Bug A2: back-fill ``openproject_id`` for legacy ``matched_by="none"``
        # users. Runs here (not in the user component) so it executes
        # even when change detection skips the user component entirely.
        _backfill_user_mapping_at_startup(op_client, config.mappings, config.logger)

        config.logger.info("Starting migration process...")

        # Define lazy factories for all migration components
        available_component_factories = _build_component_factories(
            jira_client=jira_client,
            op_client=op_client,
        )
        available = set(available_component_factories)
        unknown = [name for name in effective_components if name not in available]
        if unknown:
            config.logger.warning(
                "Unknown component(s) requested: %s. Valid components: %s", unknown, sorted(available)
            )
        names = [name for name in effective_components if name in available]
        config.logger.info("Migration will run the following components in order: %s", names)
        await _run_components(
            names,
            available_component_factories,
            results,
            health_client,
            progress,
            stop_on_error=stop_on_error,
            no_confirm=no_confirm,
        )
        _finish_migration(results, migration_timestamp, health_client)
        progress.finish(results.overall["status"])
        return results

    except KeyboardInterrupt, asyncio.CancelledError:
        progress.finish("interrupted")
        raise
    except SystemExit:
        progress.finish("failed")
        raise
    except Exception as e:
        progress.finish("failed")
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
    from src import config

    session_name = config.openproject_config.get("tmux_session_name", "rails_console")

    config.logger.info(
        "Setting up tmux session '%s' for Rails console...",
        session_name,
    )

    try:
        # Check if tmux is installed
        import shutil as _shutil

        tmux_bin = _shutil.which("tmux") or "tmux"
        subprocess.run([tmux_bin, "-V"], check=True, capture_output=True)

        # Check if session already exists
        result = subprocess.run(
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
        subprocess.run([tmux_bin, "new-session", "-d", "-s", session_name], check=True)

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

        return True
    except subprocess.SubprocessError, FileNotFoundError:
        config.logger.error("tmux is not installed or not available in PATH")
        config.logger.info("Please install tmux first:")
        config.logger.info("  On Ubuntu/Debian: sudo apt-get install tmux")
        config.logger.info("  On CentOS/RHEL: sudo yum install tmux")
        config.logger.info("  On macOS with Homebrew: brew install tmux")
        return False


def _update_selected_mapping() -> None:
    jira, op, _ = _create_migration_clients(
        config.migration_config.get("batch_size", 100),
        config.migration_config.get("max_workers", 5),
    )
    choices = {"1": (CustomFieldMigration, "Custom field"), "2": (IssueTypeMigration, "Issue type")}
    console.print("\nSelect mapping to update:")
    console.print("1. Custom Field mapping")
    console.print("2. Issue Type mapping")
    try:
        while (choice := input("Enter choice (1-2): ")) not in choices:
            config.logger.error("Invalid choice. Please enter 1 or 2.")
        migration_class, label = choices[choice]
        if migration_class(jira_client=jira, op_client=op).update_mapping_file():
            config.logger.success("%s mapping updated successfully.", label)
        else:
            config.logger.warning("No updates were made to %s mapping.", label.lower())
    except KeyboardInterrupt:
        config.logger.warning("Operation cancelled by user.")


def _log_run_summary(migration_result: MigrationResult) -> None:
    overall_status = migration_result.overall.get("status", "unknown")
    if overall_status == "success":
        config.logger.success("Migration completed successfully")
    elif overall_status == "interrupted":
        config.logger.warning("Migration was interrupted before completion")
    else:
        config.logger.error("Migration completed with errors")
    config.logger.info("Component results:")
    for component, result in migration_result.components.items():
        status = (result.details or {}).get("status", "unknown")
        if status == "success":
            config.logger.success("✓ %s: %s", component, status)
        elif status == "interrupted":
            config.logger.warning("⚠ %s: %s", component, status)
        else:
            config.logger.error("✗ %s: %s", component, status)
    if "performance_summary" in migration_result.overall:
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


def main() -> None:
    """Run the migration tool."""
    # Parse command-line arguments
    args = parse_args()

    try:
        if args.setup_tmux:
            if setup_tmux_session():
                config.logger.success("tmux session setup complete.")
            else:
                sys.exit(1)
            return

        if args.backup_dir:
            success = restore_backup(args.backup_dir)
            if success:
                config.logger.success("Backup restoration completed successfully.")
            else:
                config.logger.error("Backup restoration failed.")
            return

        if args.update_mapping:
            _update_selected_mapping()
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

        if migration_result:
            _log_run_summary(migration_result)

    except KeyboardInterrupt:
        console.print("\nMigration manually interrupted. Exiting...")
        sys.exit(0)


if __name__ == "__main__":
    main()
