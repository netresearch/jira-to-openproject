# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Snapshot each migrated project's Jira boards as native basic boards in the configured private archive."""

from __future__ import annotations

from typing import Any

from src import config
from src.application.components.base_migration import BaseMigration
from src.infrastructure.openproject.native_board_writer import save_layout
from src.models import ComponentResult
from src.native_boards import capture

ARCHIVE_SETTING = "native_board_archive_project"


class NativeBoardsMigration(BaseMigration):
    """Run the ``j2o-boards`` import for every migrated project when an archive project is configured."""

    def run(self) -> ComponentResult:
        """Create new board revisions, verify unchanged ones and count each board's outcome."""
        archive = config.migration_config.get(ARCHIVE_SETTING)
        if not archive:
            return self._skipped("native_board_archive_project_not_configured")
        projects = self._selected_projects()
        if not projects:
            return self._skipped("no_migrated_projects")
        created = verified = 0
        errors: list[str] = []
        for key, entry in projects.items():
            target = entry.get("openproject_identifier") if isinstance(entry, dict) else None
            if not target:
                errors.append(f"{key}: project mapping has no OpenProject identifier")
                continue
            try:
                boards = self.jira_client.get_boards_for_project(key)
            except Exception as exc:  # one project's listing failure must not hide the others
                self.logger.warning("Listing Jira boards of %s failed: %s", key, exc)
                errors.append(f"{key}: board listing failed: {exc}")
                continue
            for board in boards:
                try:
                    result = save_layout(self.op_client, capture(self.jira_client, board, key), target, archive)
                except Exception as exc:  # one board's failure must not hide the others
                    self.logger.warning("Native board %s of %s failed: %s", board.get("id"), key, exc)
                    errors.append(f"{key} board {board.get('id')}: {exc}")
                    continue
                if result.get("created"):
                    created += 1
                else:
                    verified += 1
        imported = created + verified
        return ComponentResult(
            success=not errors,
            message=f"Native boards: {created} created, {verified} verified, {len(errors)} failed",
            details={"created": created, "verified": verified, "archive_project": archive},
            errors=errors,
            total_count=imported + len(errors),
            success_count=imported,
            failed_count=len(errors),
        )

    def _selected_projects(self) -> dict[str, Any]:
        mapping = self.mappings.get_mapping("project") or {}
        configured = config.migration_config.get("jira_project_filter")
        if isinstance(configured, str):
            configured = [configured]
        selected = set(configured) if configured else None
        return {key: entry for key, entry in sorted(mapping.items()) if selected is None or key in selected}

    def _skipped(self, reason: str) -> ComponentResult:
        self.logger.info("Skipping native boards: %s", reason)
        return ComponentResult(
            success=True,
            message=f"Native boards skipped: {reason}",
            details={"status": "skipped", "reason": reason},
        )
