#!/usr/bin/env python3
"""Time entry (work log) migration as a standalone component.

This component reads migrated work packages from the mapping file and then
invokes the `TimeEntryMigrator` to migrate Jira work logs (and optional Tempo
entries) into OpenProject time entries.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import configure_logging
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult
from src.utils.time_entry_migrator import TimeEntryMigrator

try:
    from src.config import logger as logger  # type: ignore
except Exception:
    logger = configure_logging("INFO", None)


@register_entity_types("time_entries", "work_logs")
class TimeEntryMigration(BaseMigration):
    """Standalone migration for time entries (work logs)."""

    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
    ) -> None:
        super().__init__(jira_client, op_client)
        self.time_entry_migrator = TimeEntryMigrator(
            jira_client=jira_client,
            op_client=op_client,
            data_dir=self.data_dir,
        )

    def _load_migrated_work_packages(self) -> list[dict[str, Any]]:
        """Load migrated work packages to feed time entry migration.

        Returns a list of dicts with at least: jira_key, work_package_id, project_id (when available).
        """
        mapping_file = Path(self.data_dir) / "work_package_mapping.json"
        if not mapping_file.exists():
            logger.warning("work_package_mapping.json not found; nothing to migrate for time entries")
            return []

        try:
            with mapping_file.open("r", encoding="utf-8") as f:
                raw = json.load(f) or {}
        except Exception as e:
            logger.error("Failed to read work_package_mapping.json: %s", e)
            return []

        migrated: list[dict[str, Any]] = []
        for entry in raw.values():
            jira_key = entry.get("jira_key")
            wp_id = entry.get("openproject_id")
            if jira_key and wp_id:
                migrated.append(
                    {
                        "jira_key": jira_key,
                        "work_package_id": wp_id,
                        # Optional project id if present in mapping
                        "project_id": entry.get("openproject_project_id"),
                    },
                )

        return migrated

    def run(self) -> ComponentResult:
        """Run standalone time entry migration based on existing WP mapping."""
        logger.info("Starting time entry migration component")
        start_time = datetime.now(tz=UTC)

        # Basic preflight: ensure Rails client is present (OpenProject operations)
        if not getattr(self.op_client, "rails_client", None):
            msg = (
                "Rails client is required for time entry migration. "
                "Ensure tmux/rails console is available (use --tmux)."
            )
            logger.error(msg)
            return ComponentResult(
                success=False,
                message=msg,
                details={"status": "failed", "reason": "rails_client_missing"},
            )

        migrated_wps = self._load_migrated_work_packages()
        if not migrated_wps:
            warn = "No migrated work packages found; skipping time entry migration"
            logger.warning(warn)
            return ComponentResult(
                success=True,
                message=warn,
                details={
                    "status": "skipped",
                    "reason": "no_migrated_work_packages",
                    "time": (datetime.now(tz=UTC) - start_time).total_seconds(),
                },
                success_count=0,
                failed_count=0,
                total_count=0,
                warnings=[warn],
            )

        # Determine larger batch size for time entries (env overrides default)
        try:
            batch_size_env = int((__import__("os").environ.get("J2O_TIME_ENTRY_BATCH_SIZE") or "200"))
        except Exception:
            batch_size_env = 200

        try:
            result = self.time_entry_migrator.migrate_time_entries_for_issues(
                migrated_wps,
                batch_size=batch_size_env,
            )
            success = result.get("status") in ("success", "partial_success")
            errors: list[str] = []
            if result.get("status") == "failed":
                # Collect top-level errors
                errors.append(result.get("error") or "time entry migration failed")

            total_migrated = result.get("total_time_entries", {}).get("migrated", 0)
            total_failed = result.get("total_time_entries", {}).get("failed", 0)
            total_discovered = (
                result.get("jira_work_logs", {}).get("discovered", 0)
                + result.get("tempo_time_entries", {}).get("discovered", 0)
            )

            # Zero-created gating: discovered > 0 but migrated == 0 should fail
            if total_discovered > 0 and total_migrated == 0:
                return ComponentResult(
                    success=False,
                    message=(
                        "Time entry migration discovered entries but created zero; failing per gating policy"
                    ),
                    details={
                        "status": "failed",
                        "reason": "zero_created_with_input",
                        "total_discovered": total_discovered,
                        "time": (datetime.now(tz=UTC) - start_time).total_seconds(),
                    },
                    success_count=0,
                    failed_count=total_failed,
                    total_count=total_failed,
                    errors=errors,
                )

            return ComponentResult(
                success=success,
                message=(
                    "Time entry migration completed"
                    if success
                    else "Time entry migration completed with errors"
                ),
                details={
                    "status": result.get("status", "unknown"),
                    "jira_work_logs": result.get("jira_work_logs", {}),
                    "tempo_time_entries": result.get("tempo_time_entries", {}),
                    "total_time_entries": result.get("total_time_entries", {}),
                    "time": (datetime.now(tz=UTC) - start_time).total_seconds(),
                },
                success_count=total_migrated,
                failed_count=total_failed,
                total_count=total_migrated + total_failed,
                errors=errors,
            )

        except Exception as e:
            logger.exception("Time entry migration component failed: %s", e)
            return ComponentResult(
                success=False,
                message=f"Time entry migration failed: {e}",
                details={"status": "failed", "error": str(e)},
                errors=[str(e)],
            )


