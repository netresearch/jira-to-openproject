"""Migrate Jira labels to OpenProject using a custom field fallback.

Preferred path would be native tags; fallback implemented here uses a
WorkPackage custom field named "Labels" storing a comma-separated list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.config import logger
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult
from src.models.jira import JiraIssueFields

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient

LABELS_CF_NAME = "Labels"


@register_entity_types("labels")
class LabelsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities for change detection.

        LabelsMigration is a transformation-only component that operates on
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
        """Extract Jira labels for all migrated issues.

        We parse at the boundary: each issue returned by the Jira client
        is normalised through :meth:`JiraIssueFields.from_issue_any` so
        the rest of the pipeline can read ``fields.labels`` as a typed
        ``list[str]``.
        """
        wp_map = self.mappings.get_mapping("work_package") or {}
        keys = [str(k) for k in wp_map]
        issues = self._merge_batch_issues(keys)
        labels_by_key: dict[str, list[str]] = {}
        for k, issue in issues.items():
            try:
                fields = JiraIssueFields.from_issue_any(issue)
                if fields.labels:
                    labels_by_key[k] = [label for label in fields.labels if label.strip()]
            except Exception:
                continue
        return ComponentResult(success=True, data={"labels": labels_by_key})

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        data = extracted.data or {}
        raw: dict[str, list[str]] = data.get("labels", {}) if isinstance(data, dict) else {}

        # Normalize: sort unique labels; join with ", "
        norm: dict[str, str] = {}
        for key, labels in raw.items():
            unique_sorted = sorted({l.strip() for l in labels if l and isinstance(l, str)})
            if unique_sorted:
                norm[key] = ", ".join(unique_sorted)
        return ComponentResult(success=True, data={"labels_markdown": norm})

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        cf_id = self._ensure_wp_custom_field(LABELS_CF_NAME, "text")
        if not cf_id:
            return ComponentResult(success=False, failed=1)

        wp_map = self.mappings.get_mapping("work_package") or {}
        data = mapped.data or {}
        labels_text: dict[str, str] = data.get("labels_markdown", {}) if isinstance(data, dict) else {}

        # Collect all CF values for bulk update
        cf_values_to_set: list[dict] = []
        projects_with_values: set[int] = set()

        for jira_key, text in labels_text.items():
            if not text:
                continue
            entry = wp_map.get(jira_key)
            if not (isinstance(entry, dict) and entry.get("openproject_id")):
                continue
            wp_id = int(entry["openproject_id"])  # type: ignore[arg-type]
            # Track project for selective enablement
            project_id = entry.get("openproject_project_id")
            if project_id:
                projects_with_values.add(int(project_id))
            cf_values_to_set.append(
                {
                    "work_package_id": wp_id,
                    "custom_field_id": cf_id,
                    "value": text,
                },
            )

        # Bulk set all CF values in single Rails call
        updated = 0
        failed = 0
        if cf_values_to_set:
            logger.info("Bulk setting %d labels values...", len(cf_values_to_set))
            bulk_result = self.op_client.bulk_set_wp_custom_field_values(cf_values_to_set)
            updated = bulk_result.get("updated", 0)
            failed = bulk_result.get("failed", 0)
            logger.info("Bulk labels: updated=%d, failed=%d", updated, failed)

        # Enable CF only for projects that have values
        if projects_with_values:
            self._enable_cf_for_projects(cf_id, projects_with_values, cf_name=LABELS_CF_NAME)

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run Labels migration."""
        return self._run_etl_pipeline("Labels")
