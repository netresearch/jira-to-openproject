"""Priority migration: map Jira priorities to OpenProject IssuePriority and set on WPs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src import mappings
from src.display import configure_logging
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient

try:
    from src import config
    from src.config import logger  # type: ignore
except Exception:  # noqa: BLE001
    logger = configure_logging("INFO", None)
    from src import config  # type: ignore


@register_entity_types("priorities")
class PriorityMigration(BaseMigration):
    """Migrate priorities and set on work packages."""

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)
        try:
            self.mappings = config.mappings
        except Exception:  # noqa: BLE001
            self.mappings = mappings.Mappings(data_dir=config.get_path("data"))

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
        msg = (
            f"PriorityMigration does not support entity type: {entity_type}. "
            f"Supported types: ['priorities']"
        )
        raise ValueError(msg)

    def run(self) -> ComponentResult:
        """Execute the extract → map → load pipeline for priorities."""
        extracted = self._extract()
        if not extracted.success:
            return extracted

        mapped = self._map(extracted)
        if not mapped.success:
            return mapped

        loaded = self._load(mapped)
        return loaded

    def _extract(self) -> ComponentResult:
        """Extract Jira priorities (names and order)."""
        try:
            priorities = self.jira_client.get_priorities()  # expected: list of {name, id}
        except Exception:
            logger.exception("Failed to extract Jira priorities")
            priorities = []
        return ComponentResult(success=True, extracted=len(priorities), data={"priorities": priorities})

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        """Map Jira priority names to OP IssuePriority records, create missing."""
        priorities = (extracted.data or {}).get("priorities", []) if extracted.data else []
        created = 0
        mapping: dict[str, int] = {}

        # Fetch existing OP priorities
        existing = self.op_client.get_issue_priorities()
        name_to_id = {p.get("name"): int(p.get("id")) for p in existing if p and p.get("id")}

        for pr in priorities:
            name = (pr or {}).get("name")
            if not name:
                continue
            op_id = name_to_id.get(name)
            if not op_id:
                try:
                    created_rec = self.op_client.create_issue_priority(name)
                    op_id = int(created_rec.get("id")) if created_rec.get("id") else None
                    if op_id:
                        name_to_id[name] = op_id
                        created += 1
                except Exception:
                    logger.exception("Failed creating IssuePriority %s", name)
                    continue
            if op_id:
                mapping[name] = op_id

        # Persist mapping
        self.mappings.set_mapping("priority", mapping)
        return ComponentResult(success=True, created_types=created, data={"mapping": mapping})

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
        jira_keys = [str(k) for k in wp_map.keys()]
        if not jira_keys:
            return ComponentResult(success=True, updated=0)

        # Prefer using JiraClient's batch_get_issues directly to simplify testing
        iss_map: dict[str, Any] = {}
        try:
            batch_get = getattr(self.jira_client, "batch_get_issues", None)
            if callable(batch_get):
                iss_map = batch_get(jira_keys)
        except Exception:
            logger.exception("Failed to batch-get Jira issues for priority application")
            iss_map = {}

        for key, wp_entry in wp_map.items():
            try:
                wp_id = None
                if isinstance(wp_entry, dict):
                    wp_id = wp_entry.get("openproject_id")
                elif isinstance(wp_entry, int):
                    wp_id = wp_entry
                if not wp_id:
                    continue

                issue = iss_map.get(key)
                if not issue:
                    continue
                fields = getattr(issue, "fields", None)
                pr = getattr(fields, "priority", None)
                pr_name = getattr(pr, "name", None)
                if not pr_name:
                    continue
                pr_id = mapping.get(pr_name)
                if not pr_id:
                    continue

                updates.append({"id": int(wp_id), "priority_id": int(pr_id)})
            except Exception:  # noqa: BLE001
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
