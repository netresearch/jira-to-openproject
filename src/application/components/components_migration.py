# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Migrate Jira components to OpenProject Categories and assign to work packages.

Phase 7d converts the work-package side of this migration to the typed
pipeline: issues are parsed at the boundary via
:meth:`JiraIssueFields.from_issue_any`, and the legacy ``wp_map``
``dict | int`` ladder is normalised through
:meth:`WorkPackageMappingEntry.from_legacy`. The ``project`` mapping
ladder uses a separate, unrelated polymorphic shape and is intentionally
left as-is — phase 7 targets the ``wp_map`` polymorphism only.
"""

from __future__ import annotations

from typing import Any

from src.application.components.base_migration import BaseMigration, register_entity_types
from src.application.components.project_named_records import apply_work_package_updates, map_project_records, project_id
from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult, WorkPackageMappingEntry
from src.models.jira import JiraIssueFields


@register_entity_types("components")
class ComponentsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities for transformation.

        This migration performs data transformation on work package mappings
        rather than fetching directly from Jira. It operates on already-migrated
        work packages to extract and assign components to Categories.

        Args:
            entity_type: The type of entities requested (e.g., "components")

        Returns:
            Empty list (this migration doesn't fetch from Jira directly)

        Raises:
            ValueError: Always, as this migration doesn't support idempotent workflow

        """
        msg = (
            "ComponentsMigration is a transformation-only migration and does not "
            "support idempotent workflow. It operates on work package mappings."
        )
        raise ValueError(msg)

    def _extract(self) -> ComponentResult:
        """Collect component names per Jira project from migrated issues.

        Issues are normalised through :meth:`JiraIssueFields.from_issue_any`
        so the rest of the pipeline reads ``fields.components`` as a typed
        list of :class:`JiraComponentRef`.
        """
        wp_map = self.mappings.get_mapping("work_package") or {}
        # Production wp_map is keyed by str(jira_id) (numeric) outer with
        # the human-readable ``jira_key`` stored inside. Prefer the inner
        # ``jira_key``; fall back to the outer key for legacy/test layouts.
        jira_keys = [self._component_issue_key(key, entry) for key, entry in wp_map.items()]
        if not jira_keys:
            return ComponentResult(success=True, extracted=0, data={"by_project": {}})

        issues: dict[str, Any] = self._merge_batch_issues(jira_keys)

        by_project: dict[str, set[str]] = {}
        for key, issue in issues.items():
            pj = self._issue_project_key(key)
            try:
                fields = JiraIssueFields.from_issue_any(issue)
            except Exception:
                continue
            for comp in fields.components:
                name = comp.name
                if name and name.strip():
                    by_project.setdefault(pj, set()).add(name.strip())

        materialized = {k: sorted(v) for k, v in by_project.items() if v}
        # Cache issues for reuse in _load() to avoid second Jira API call
        return ComponentResult(
            success=True,
            extracted=sum(len(v) for v in materialized.values()),
            data={"by_project": materialized, "issues": issues},
        )

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        return map_project_records(self, extracted, "Category")

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        category_map: dict[str, dict[str, int]] = (mapped.data or {}).get("category_map", {})
        if not category_map:
            return ComponentResult(success=True, updated=0)

        wp_map = self.mappings.get_mapping("work_package") or {}
        proj_map = self.mappings.get_mapping("project") or {}

        # Use cached issues from extract phase to avoid second Jira API call.
        # Cache keys match the ``jira_key`` form used during ``_extract``
        # (inner ``jira_key`` when present, outer key otherwise).
        issues: dict[str, Any] = (mapped.data or {}).get("issues", {})
        if not issues:
            logger.warning("No cached issues available, fetching from Jira (this may cause timeout)")
            fallback_keys = [self._component_issue_key(key, entry) for key, entry in wp_map.items()]
            issues = self._merge_batch_issues(fallback_keys)

        updates: list[dict[str, Any]] = []
        failed = 0
        for outer_key, raw_entry in wp_map.items():
            jira_key = self._component_issue_key(outer_key, raw_entry)
            try:
                entry = WorkPackageMappingEntry.from_legacy(jira_key, raw_entry)
            except ValueError:
                continue
            wp_id = int(entry.openproject_id)

            issue = issues.get(jira_key)
            if not issue:
                continue
            try:
                fields = JiraIssueFields.from_issue_any(issue)
            except Exception:
                failed += 1
                continue
            assignment = self._category_assignment(jira_key, wp_id, fields, proj_map, category_map)
            if assignment:
                updates.append(assignment)

        return apply_work_package_updates(self, updates, failed)

    @staticmethod
    def _component_issue_key(outer_key: str, entry: Any) -> str:
        return str(entry.get("jira_key") or outer_key) if isinstance(entry, dict) else str(outer_key)

    def _category_assignment(
        self, key: str, wp_id: int, fields: JiraIssueFields, projects: dict, categories: dict
    ) -> dict | None:
        if not fields.components or not fields.components[0].name:
            return None
        pid = project_id(projects.get(self._issue_project_key(key)))
        if not pid:
            return None
        category_id = categories.get(str(pid), {}).get(str(fields.components[0].name))
        return {"id": wp_id, "category_id": int(category_id)} if category_id else None

    def run(self) -> ComponentResult:
        """Execute component migration pipeline."""
        self.logger.info("Starting components migration")

        extracted = self._extract()
        if not extracted.success:
            self.logger.error("Components extraction failed: %s", extracted.message or extracted.error)
            return extracted

        mapped = self._map(extracted)
        if not mapped.success:
            self.logger.error("Components mapping failed: %s", mapped.message or mapped.error)
            return mapped

        loaded = self._load(mapped)
        if loaded.success:
            self.logger.info("Components migration completed (updated=%s, failed=%s)", loaded.updated, loaded.failed)
        else:
            self.logger.error("Components migration encountered failures (failed=%s)", loaded.failed)
        return loaded
