# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Migrate Jira fixVersions to OpenProject Versions and assign to work packages.

Strategy:
- Extract: collect fixVersion names per Jira project from issues in work_package mapping.
- Map: ensure Versions exist per OP project (create missing via bulk_create_records('Version')).
- Load: assign the complete Jira fixVersions set, with read-back and legacy compatibility.
"""

from __future__ import annotations

from typing import Any

from src.application.components.base_migration import BaseMigration, register_entity_types
from src.application.components.project_named_records import apply_work_package_updates, map_project_records, project_id
from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult
from src.models.jira import JiraIssueFields


@register_entity_types("versions")
class VersionsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities for transformation.

        This migration performs data transformation on work package mappings
        rather than fetching directly from Jira. It operates on already-migrated
        work packages to extract and assign fixVersions to Versions.

        Args:
            entity_type: The type of entities requested (e.g., "versions")

        Returns:
            Empty list (this migration doesn't fetch from Jira directly)

        Raises:
            ValueError: Always, as this migration doesn't support idempotent workflow

        """
        msg = (
            "VersionsMigration is a transformation-only migration and does not "
            "support idempotent workflow. It operates on work package mappings."
        )
        raise ValueError(msg)

    def _extract(self) -> ComponentResult:
        """Extract fixVersion names per Jira project from known work package issues.

        Issues come back from :meth:`_merge_batch_issues` in either the
        SDK shape (attribute access on ``issue.fields.fixVersions``) or
        the cached dict shape. :meth:`JiraIssueFields.from_issue_any`
        normalises both, giving us a typed list of
        :class:`JiraVersionRef` to iterate.
        """
        wp_map = self.mappings.get_mapping("work_package") or {}
        jira_keys = self._jira_keys_from_wp_map(wp_map)
        if not jira_keys:
            return ComponentResult(success=True, extracted=0, data={"by_project": {}})

        issues: dict[str, Any] = self._merge_batch_issues(jira_keys)

        by_project: dict[str, set[str]] = {}
        for key, issue in issues.items():
            pj = self._issue_project_key(key)
            try:
                fields = JiraIssueFields.from_issue_any(issue)
                if not fields.fix_versions:
                    continue
                for ref in fields.fix_versions:
                    name = ref.name
                    if name and name.strip():
                        by_project.setdefault(pj, set()).add(name.strip())
            except Exception:
                continue

        materialized = {k: sorted(v) for k, v in by_project.items() if v}
        # Cache issues for reuse in _load() to avoid second Jira API call
        return ComponentResult(
            success=True,
            extracted=sum(len(v) for v in materialized.values()),
            data={"by_project": materialized, "issues": issues},
        )

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        return map_project_records(self, extracted, "Version")

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        version_map: dict[str, dict[str, int]] = (mapped.data or {}).get("version_map", {}) if mapped.data else {}
        if not version_map and not (mapped.data or {}).get("by_project"):
            return ComponentResult(success=True, updated=0)

        wp_map = self.mappings.get_mapping("work_package") or {}
        proj_map = self.mappings.get_mapping("project") or {}

        # Use cached issues from extract phase to avoid second Jira API call
        issues: dict[str, Any] = (mapped.data or {}).get("issues", {}) if mapped.data else {}
        if not issues:
            logger.warning("No cached issues available, fetching from Jira (this may cause timeout)")
            jira_keys = self._jira_keys_from_wp_map(wp_map)
            issues = self._merge_batch_issues(jira_keys)

        updates: list[dict[str, Any]] = []
        failed = 0
        for key, wp_entry in wp_map.items():
            try:
                assignment = self._version_assignment(key, wp_entry, issues, proj_map, version_map)
                if assignment:
                    updates.append(assignment)
            except Exception:
                failed += 1

        return apply_work_package_updates(self, updates, failed)

    def _version_assignment(self, key: str, entry: Any, issues: dict, projects: dict, versions: dict) -> dict | None:
        if not isinstance(entry, (dict, int)):
            return None
        wp_id = entry.get("openproject_id") if isinstance(entry, dict) else entry
        if not wp_id:
            return None
        jira_key = self._inner_jira_key(key, entry) or key
        issue = issues.get(jira_key)
        if not issue:
            return None
        fields = JiraIssueFields.from_issue_any(issue)
        if not fields.fix_versions:
            return None
        proj_entry = projects.get(self._issue_project_key(jira_key))
        op_pid = project_id(proj_entry)
        records = versions.get(str(op_pid), {})
        # All references must resolve before writing anything for this work package.
        target_ids = list(dict.fromkeys(int(records[(ref.name or "").strip()]) for ref in fields.fix_versions))
        return {"id": int(wp_id), "target_version_ids": target_ids}

    def run(self) -> ComponentResult:
        """Execute versions migration pipeline."""
        self.logger.info("Starting versions migration")

        extracted = self._extract()
        if not extracted.success:
            self.logger.error("Versions extraction failed: %s", extracted.message or extracted.error)
            return extracted

        mapped = self._map(extracted)
        if not mapped.success:
            self.logger.error("Versions mapping failed: %s", mapped.message or mapped.error)
            return mapped

        loaded = self._load(mapped)
        if loaded.success:
            self.logger.info("Versions migration completed (updated=%s, failed=%s)", loaded.updated, loaded.failed)
        else:
            self.logger.error("Versions migration encountered failures (failed=%s)", loaded.failed)
        return loaded
