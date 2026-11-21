# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Snapshot each migrated project's Jira boards as native basic boards in the configured private archive."""

from __future__ import annotations

from collections import Counter
from typing import Any

from src import config
from src.application.components import level_issues
from src.application.components.base_migration import BaseMigration
from src.application.components.security_level_projects_migration import LevelProjectError
from src.infrastructure.openproject.native_board_writer import save_layout
from src.models import ComponentResult
from src.native_boards import capture

ARCHIVE_SETTING = "native_board_archive_project"


class NativeBoardsMigration(BaseMigration):
    """Run the ``j2o-boards`` import for every migrated project when an archive project is configured.

    With ``migration.security_level_work_packages`` on, every issue of a level search or of the
    archive is left out of the snapshots (ADR-009): a level sub-project gets no board, and the
    archive, which serves the parent projects, never names a level card.
    """

    def run(self) -> ComponentResult:
        """Create new board revisions, verify unchanged ones and count each board's outcome."""
        archive = config.migration_config.get(ARCHIVE_SETTING)
        if not archive:
            return self._skipped("native_board_archive_project_not_configured")
        projects = self._selected_projects()
        if not projects:
            return self._skipped("no_migrated_projects")
        level = None
        if level_issues.enabled():
            try:
                level = level_issues.level_work_packages(self.jira_client, self.op_client, self.mappings, self.logger)
            except LevelProjectError as exc:
                self.logger.error("Level work package components stopped: %s", exc)
                return ComponentResult(success=False, message=str(exc), errors=["security_level"])
        self._withheld = frozenset() if level is None else frozenset(level.level_keys)
        outcomes: Counter[str] = Counter()
        errors: list[str] = []
        for key, entry in projects.items():
            self._import_project(key, entry, archive, outcomes, errors)
        created, verified = outcomes["created"], outcomes["verified"]
        imported = created + verified
        result = ComponentResult(
            success=not errors,
            message=f"Native boards: {created} created, {verified} verified, {len(errors)} failed",
            details={"created": created, "verified": verified, "archive_project": archive},
            errors=errors,
            total_count=imported + len(errors),
            success_count=imported,
            failed_count=len(errors),
        )
        if level is None:
            return result
        level.report(result, self.logger)
        # A refused level issue is a failed item of this component, as a failed board is.
        result.failed_count += len(level.refused)
        result.total_count += len(level.refused)
        return result

    def _import_project(self, key: str, entry: Any, archive: str, outcomes: Counter[str], errors: list[str]) -> None:
        target = entry.get("openproject_identifier") if isinstance(entry, dict) else None
        if not target:
            errors.append(f"{key}: project mapping has no OpenProject identifier")
            return
        try:
            boards = self.jira_client.get_boards_for_project(key)
        except Exception as exc:  # one project's listing failure must not hide the others
            self.logger.warning("Listing Jira boards of %s failed: %s", key, exc)
            errors.append(f"{key}: board listing failed: {exc}")
            return
        for board in boards:
            try:
                snapshot = capture(self.jira_client, board, key, self._withheld)
                result = save_layout(self.op_client, snapshot, target, archive)
            except Exception as exc:  # one board's failure must not hide the others
                self.logger.warning("Native board %s of %s failed: %s", board.get("id"), key, exc)
                errors.append(f"{key} board {board.get('id')}: {exc}")
                continue
            outcomes["created" if result.get("created") else "verified"] += 1

    def _selected_projects(self) -> dict[str, Any]:
        mapping = self.mappings.get_mapping("project") or {}
        selected = set(config.selected_jira_projects()) or None
        return {key: entry for key, entry in sorted(mapping.items()) if selected is None or key in selected}

    def _skipped(self, reason: str) -> ComponentResult:
        self.logger.info("Skipping native boards: %s", reason)
        return ComponentResult(
            success=True,
            message=f"Native boards skipped: {reason}",
            details={"status": "skipped", "reason": reason},
        )
