#!/usr/bin/env python3
"""Time entry (work log) migration as a standalone component.

This component reads migrated work packages from the mapping file and then
invokes the `TimeEntryMigrator` to migrate Jira work logs (and optional Tempo
entries) into OpenProject time entries.

Note on dict access patterns kept here
--------------------------------------
The bulk of this component delegates to
:class:`src.utils.time_entry_migrator.TimeEntryMigrator`, which owns
the worklog/Tempo wire shapes (``id``/``started``/``timeSpentSeconds``
/``author`` plus tenant-specific Tempo metadata). That helper has its
own boundary; rewiring it is out of scope for phase 7c. The only
polymorphic ladder this file used to carry was the work-package
mapping read in :meth:`_load_migrated_work_packages`, which is now
normalised through :class:`WorkPackageMappingEntry.from_legacy`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult, WorkPackageMappingEntry
from src.utils.time_entry_migrator import TimeEntryMigrator


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

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities for transformation.

        This migration performs data transformation on work package mappings
        rather than fetching directly from Jira. It delegates time entry fetching
        to TimeEntryMigrator which operates on already-migrated work packages.

        Args:
            entity_type: The type of entities requested (e.g., "time_entries", "work_logs")

        Returns:
            Empty list (this migration doesn't fetch from Jira directly)

        Raises:
            ValueError: Always, as this migration doesn't support idempotent workflow

        """
        msg = (
            "TimeEntryMigration is a transformation-only migration and does not "
            "support idempotent workflow. It operates on work package mappings "
            "and delegates to TimeEntryMigrator."
        )
        raise ValueError(msg)

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

        # Optional project filter from CLI (applied earlier into config.jira_config['projects'])
        allowed_projects = [
            str(p).upper().strip() for p in (config.jira_config.get("projects") or []) if str(p).strip()
        ]

        # Note: ``work_package_mapping.json`` is keyed by ``str(jira_id)``
        # (numeric Jira id) at the outer level, while the inner record
        # carries the human-readable ``jira_key`` (e.g. "PROJ-123"). We
        # build a typed :class:`WorkPackageMappingEntry` from the inner
        # ``jira_key`` so the rest of this file flows through validated
        # types without inverting that on-disk layout.
        migrated: list[dict[str, Any]] = []
        for raw_entry in raw.values():
            inner_key = raw_entry.get("jira_key") if isinstance(raw_entry, dict) else None
            if not inner_key:
                continue
            try:
                entry = WorkPackageMappingEntry.from_legacy(str(inner_key), raw_entry)
            except ValueError:
                continue
            jira_key = str(entry.jira_key)
            if allowed_projects:
                key_u = jira_key.upper()
                if not any(key_u.startswith(f"{proj}-") for proj in allowed_projects):
                    continue
            migrated.append(
                {
                    "jira_key": jira_key,
                    "work_package_id": int(entry.openproject_id),
                    # Optional project id if present in mapping
                    "project_id": (
                        int(entry.openproject_project_id) if entry.openproject_project_id is not None else None
                    ),
                },
            )

        logger.info(
            "Loaded %d migrated work packages%s",
            len(migrated),
            f" for projects {allowed_projects}" if allowed_projects else "",
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
            int(__import__("os").environ.get("J2O_TIME_ENTRY_BATCH_SIZE") or "200")
        except Exception:
            pass

        try:
            result = self.time_entry_migrator.migrate_time_entries_for_issues(
                migrated_wps,
            )
            success = result.get("status") in ("success", "partial_success")
            errors: list[str] = []
            if result.get("status") == "failed":
                # Collect top-level errors
                errors.append(result.get("error") or "time entry migration failed")

            total_migrated = result.get("total_time_entries", {}).get("migrated", 0)
            total_failed = result.get("total_time_entries", {}).get("failed", 0)
            total_discovered = result.get("jira_work_logs", {}).get("discovered", 0) + result.get(
                "tempo_time_entries",
                {},
            ).get("discovered", 0)
            # Entries accounted for by known-benign skip classes:
            #   • unmappable: dropped at the transformer (no user + no WP in mapping tables)
            #   • skipped: already present in OP (provenance dedup)
            # These are NOT real failures; subtracting them from the denominator
            # prevents the zero-created gate from tripping on an otherwise clean re-run.
            total_unmappable = int(result.get("unmappable", 0))
            total_skipped = int(result.get("skipped", 0))
            net_actionable = total_discovered - total_unmappable - total_skipped

            # Zero-created gating: only fail loud when there are real (actionable)
            # entries that were neither migrated nor accounted for.
            if net_actionable > 0 and total_migrated == 0:
                return ComponentResult(
                    success=False,
                    message=("Time entry migration discovered entries but created zero; failed loud"),
                    details={
                        "status": "failed",
                        "reason": "zero_created_with_input",
                        "total_discovered": total_discovered,
                        "unmappable": total_unmappable,
                        "skipped": total_skipped,
                        "net_actionable": net_actionable,
                        "time": (datetime.now(tz=UTC) - start_time).total_seconds(),
                    },
                    success_count=0,
                    failed_count=total_failed,
                    total_count=total_failed,
                    errors=errors,
                )

            return ComponentResult(
                success=success,
                message=("Time entry migration completed" if success else "Time entry migration completed with errors"),
                details={
                    "status": result.get("status", "unknown"),
                    "jira_work_logs": result.get("jira_work_logs", {}),
                    "tempo_time_entries": result.get("tempo_time_entries", {}),
                    "total_time_entries": result.get("total_time_entries", {}),
                    "unmappable": total_unmappable,
                    "skipped": total_skipped,
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
