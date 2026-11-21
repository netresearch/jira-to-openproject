# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Per-issue field components and the level work packages (ADR-009, step 5).

A field component reads each issue of the ``work_package`` mapping with ``_merge_batch_issues``
and writes one value into its work package. With ``migration.security_level_work_packages`` on,
``LevelFieldsMigration`` resolves the level work packages before any write, drops the ordinary
entries of every issue of a level search or of the archive (refused on both sides), reads the
selected level issues through one level search per Jira project, which refuses an issue whose
level changed or that the search omits, and offers them to the component as entries of its
work-package map. That map is built per run and never written back: level work packages stay out
of ``work_package_mapping.json``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.application.components import level_issues, level_versions
from src.application.components.base_migration import BaseMigration
from src.application.components.security_level_projects_migration import LevelProjectError
from src.models import ComponentResult

# A field component reads arbitrary fields of an issue; the level search reads all of them, once per project.
_FIELDS = "*all"


class LevelFields:
    """The level work packages of one field-component run and their issues as the level search returned them."""

    def __init__(self, level: level_issues.LevelWorkPackages) -> None:
        """Keep the resolver's result; ``prepare`` fills issues and entries."""
        self.level = level
        self.issues: dict[str, Any] = {}
        self.entries: dict[str, dict[str, Any]] = {}

    def prepare(self, component: BaseMigration, wp_map: dict[str, Any]) -> LevelFields:
        """Refuse the ordinary entries of level issues, then read the selected issues once per project."""
        keys = [component._inner_jira_key(str(outer), raw) or str(outer) for outer, raw in wp_map.items()]
        self.level.without_level_issues(dict.fromkeys(keys, 0))
        for project, wps in sorted(self.level.by_project().items()):
            try:
                found = level_versions.selected_level_issues(self.level, component.jira_client, project, wps, _FIELDS)
            except Exception:
                component.logger.exception("Could not read the level issues of %s", project)
                self.level.refused.extend(f"{key}: the level search failed" for key in sorted(wps))
                continue
            for key, issue in found.items():
                self.issues[key] = issue
                self.entries[str(issue.id)] = {
                    "jira_key": key,
                    "openproject_id": wps[key],
                    "project_key": project,
                    "openproject_project_id": self.level.targets[key].project_id,
                }
        return self

    def view(self, component: BaseMigration, wp_map: dict[str, Any]) -> dict[str, Any]:
        """The ordinary entries without level issues, and the entries of the level work packages read now."""
        kept = {
            outer: raw
            for outer, raw in wp_map.items()
            if (component._inner_jira_key(str(outer), raw) or str(outer)) not in self.level.level_keys
        }
        return kept | self.entries


class LevelFieldsMigration(BaseMigration):
    """A per-issue field component that also writes the level work packages of its run (ADR-009)."""

    _level_fields: LevelFields | None = None

    def _with_level_fields(self, run: Callable[[], ComponentResult]) -> ComponentResult:
        """Resolve the level work packages before any write, then ``run``; count the refusals on every return."""
        self._level_fields = None
        if level_issues.enabled():
            try:
                level = level_issues.level_work_packages(self.jira_client, self.op_client, self.mappings, self.logger)
            except LevelProjectError as exc:
                self.logger.error("Level work package fields stopped: %s", exc)
                return ComponentResult(success=False, message=str(exc), errors=["security_level"])
            self._level_fields = LevelFields(level).prepare(self, self.mappings.get_mapping("work_package") or {})
        result = run()
        return result if self._level_fields is None else self._level_fields.level.report(result, self.logger)

    def _work_package_map(self) -> dict[str, Any]:
        """The work-package map of this run; with the level switch on, see ``LevelFields.view``."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        return wp_map if self._level_fields is None else self._level_fields.view(self, wp_map)

    def _level_work_package_ids(self) -> set[int]:
        """The level work packages of this run."""
        return set() if self._level_fields is None else set(self._level_fields.level.work_packages.values())

    def _merge_batch_issues(self, keys: list[str]) -> dict[str, Any]:
        """Ordinary issues from the batch read; level issues only as the level search returned them."""
        if self._level_fields is None:
            return super()._merge_batch_issues(keys)
        level = self._level_fields
        ordinary = [key for key in keys if key not in level.level.level_keys]
        issues = super()._merge_batch_issues(ordinary) if ordinary else {}
        issues.update({key: level.issues[key] for key in keys if key in level.issues})
        return issues
