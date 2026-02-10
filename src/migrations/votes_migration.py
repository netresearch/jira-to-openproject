"""Migrate Jira votes count to OpenProject via integer CF fallback.

Creates/ensures a WorkPackage custom field "Votes" (int) and writes the
Jira `fields.votes.votes` count for mapped issues.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.config import logger
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient

VOTES_CF_NAME = "Votes"


@register_entity_types("votes_reactions")
class VotesMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict]:
        """Get current entities for change detection.

        VotesMigration is a transformation-only component that operates on
        already-migrated work packages. It doesn't fetch source data from Jira,
        so change detection is not supported.

        Args:
            entity_type: Type of entities

        Raises:
            ValueError: Always, as this migration is transformation-only

        """
        msg = f"{type(self).__name__} is transformation-only and does not support change detection for entity type: {entity_type}"
        raise ValueError(msg)

    def _extract(self) -> ComponentResult:
        """Extract Jira votes count per issue mapped to a WP."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        keys = [str(k) for k in wp_map]
        issues = self._merge_batch_issues(keys)

        votes_by_key: dict[str, int] = {}
        for k, issue in issues.items():
            try:
                fields = getattr(issue, "fields", None)
                votes_obj = getattr(fields, "votes", None)
                count = getattr(votes_obj, "votes", None)
                if isinstance(count, int):
                    votes_by_key[k] = count
            except Exception:
                continue
        return ComponentResult(success=True, data={"votes": votes_by_key})

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        cf_id = self._ensure_wp_custom_field(VOTES_CF_NAME, "int")
        if not cf_id:
            return ComponentResult(success=False, failed=1)

        wp_map = self.mappings.get_mapping("work_package") or {}
        votes_by_key: dict[str, int] = (mapped.data or {}).get("votes", {})  # type: ignore[assignment]

        # Collect all CF values for bulk update
        cf_values_to_set: list[dict] = []
        projects_with_values: set[int] = set()

        for jira_key, count in votes_by_key.items():
            # Only track non-zero votes (meaningful values)
            if count == 0:
                continue
            entry = wp_map.get(jira_key)
            if not (isinstance(entry, dict) and entry.get("openproject_id")):
                continue
            wp_id = int(entry["openproject_id"])  # type: ignore[arg-type]
            # Track project for selective enablement
            project_id = entry.get("openproject_project_id")
            if project_id:
                projects_with_values.add(int(project_id))
            cf_values_to_set.append({
                "work_package_id": wp_id,
                "custom_field_id": cf_id,
                "value": str(count),
            })

        # Bulk set all CF values in single Rails call
        updated = 0
        failed = 0
        if cf_values_to_set:
            logger.info("Bulk setting %d votes values...", len(cf_values_to_set))
            bulk_result = self.op_client.bulk_set_wp_custom_field_values(cf_values_to_set)
            updated = bulk_result.get("updated", 0)
            failed = bulk_result.get("failed", 0)
            logger.info("Bulk votes: updated=%d, failed=%d", updated, failed)

        # Enable CF only for projects that have values
        if projects_with_values:
            self._enable_cf_for_projects(cf_id, projects_with_values, cf_name=VOTES_CF_NAME)

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run Votes migration."""
        return self._run_etl_pipeline("Votes")
