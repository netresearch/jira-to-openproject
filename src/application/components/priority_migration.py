# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Priority migration: map Jira priorities to OpenProject IssuePriority and set on WPs."""

from __future__ import annotations

from typing import Any

from src.application.components.base_migration import BaseMigration, register_entity_types
from src.config import logger
from src.domain.repositories import MappingRepository
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult, JiraPriority
from src.models.jira import JiraIssueFields


@register_entity_types("priorities")
class PriorityMigration(BaseMigration):
    """Migrate priorities and set on work packages."""

    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
        *,
        mapping_repo: MappingRepository | None = None,
    ) -> None:
        super().__init__(
            jira_client=jira_client,
            op_client=op_client,
            mapping_repo=mapping_repo,
        )

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities from Jira for a specific type.

        This method enables idempotent workflow caching by providing a standard
        interface for entity retrieval. Called by run_with_change_detection() to fetch data
        with automatic thread-safe caching.

        Args:
            entity_type: The type of entities to retrieve (e.g., "priorities")

        Returns:
            List of entity dictionaries from Jira API

        Raises:
            ValueError: If entity_type is not supported by this migration

        """
        # Check if this is the entity type we handle
        if entity_type == "priorities":
            return self.jira_client.get_priorities()

        # Raise error for unsupported types
        msg = f"PriorityMigration does not support entity type: {entity_type}. Supported types: ['priorities']"
        raise ValueError(msg)

    def run(self) -> ComponentResult:
        """Execute the extract → map → load pipeline for priorities."""
        return self._run_etl_pipeline("Priorities")

    def _extract(self) -> ComponentResult:
        """Extract Jira priorities (names and order).

        We parse at the boundary: each raw dict returned from
        ``get_priorities`` is validated into a :class:`JiraPriority`
        instance so the rest of the pipeline can rely on attribute access
        rather than ``dict.get`` lookups.
        """
        try:
            raw_priorities = self.jira_client.get_priorities()  # expected: list of {name, id}
        except Exception:
            logger.exception("Failed to extract Jira priorities")
            raw_priorities = []

        priorities: list[JiraPriority] = [JiraPriority.from_dict(p) for p in raw_priorities if p]
        return ComponentResult(success=True, extracted=len(priorities), data={"priorities": priorities})

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        """Map Jira priority names to OP IssuePriority records, create missing."""
        priorities: list[JiraPriority] = (extracted.data or {}).get("priorities", []) if extracted.data else []
        created = 0
        mapping: dict[str, int] = {}

        # Fetch existing OP priorities
        existing = self.op_client.get_issue_priorities()
        name_to_id = {p.get("name"): int(p.get("id")) for p in existing if p and p.get("id")}

        for pr in priorities:
            name = pr.name
            if not name:
                continue
            op_id = name_to_id.get(name)
            if not op_id:
                op_id = self._create_priority(name)
                if op_id:
                    name_to_id[name] = op_id
                    created += 1
            if op_id:
                mapping[name] = op_id

        # Persist mapping
        self.mappings.set_mapping("priority", mapping)
        return ComponentResult(success=True, created_types=created, data={"mapping": mapping})

    def _create_priority(self, name: str) -> int | None:
        try:
            record = self.op_client.create_issue_priority(name)
            return int(record["id"]) if record.get("id") else None
        except Exception:
            logger.exception("Failed creating IssuePriority %s", name)
            return None

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        """Set priority on work packages using the mapping if missing or different."""
        mapping: dict[str, int] = (mapped.data or {}).get("mapping", {}) if mapped.data else {}
        if not mapping:
            return ComponentResult(success=True, updated=0)

        # Load work package mapping and Jira issues with priorities
        wp_map = self.mappings.get_mapping("work_package") or {}
        updated = 0
        failed = 0

        # Build a minimal batch of updates: [{'id': x, 'priority_id': y}]
        updates: list[dict[str, Any]] = []

        # Get Jira issues by keys present in wp_map
        jira_keys = self._jira_keys_from_wp_map(wp_map)
        if not jira_keys:
            return ComponentResult(success=True, updated=0)

        iss_map: dict[str, Any] = self._merge_batch_issues(jira_keys)

        for key, wp_entry in wp_map.items():
            try:
                assignment = self._priority_assignment(key, wp_entry, iss_map, mapping)
                if assignment:
                    updates.append(assignment)
            except Exception:
                failed += 1

        if not updates:
            return ComponentResult(success=True, updated=0)

        try:
            res = self.op_client.batch_update_work_packages(updates)
            updated = int(res.get("updated", 0)) if isinstance(res, dict) else 0
        except Exception:
            logger.exception("Failed to batch update priorities on work packages")
            failed += len(updates)

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def _priority_assignment(
        self, key: str, entry: Any, issues: dict, priorities: dict[str, int]
    ) -> dict[str, int] | None:
        if not isinstance(entry, (dict, int)):
            return None
        wp_id = entry.get("openproject_id") if isinstance(entry, dict) else entry
        if not wp_id:
            return None
        issue = issues.get(self._inner_jira_key(key, entry) or key)
        if not issue:
            return None
        fields = JiraIssueFields.from_issue_any(issue)
        if not fields.priority or not fields.priority.name:
            return None
        priority_id = priorities.get(fields.priority.name)
        return {"id": int(wp_id), "priority_id": int(priority_id)} if priority_id else None
