"""Migrate Jira labels to OpenProject using a custom field fallback.

Preferred path would be native tags; fallback implemented here uses a
WorkPackage custom field named "Labels" storing a comma-separated list.
"""

from __future__ import annotations

from src.display import configure_logging
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

try:
    from src.config import logger  # type: ignore
except Exception:  # noqa: BLE001
    logger = configure_logging("INFO", None)

from typing import TYPE_CHECKING

from src import config

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient

LABELS_CF_NAME = "Labels"


@register_entity_types("labels")
class LabelsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)

        self.mappings = config.mappings

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities for change detection.

        LabelsMigration is a transformation-only component that operates on
        already-migrated work packages. It doesn't fetch source data from Jira,
        so this returns an empty list to indicate no changes to detect.

        Args:
            entity_type: Type of entities

        Returns:
            Empty list (transformation-only, no source entities)

        """
        return []

    def _ensure_labels_cf(self) -> int:
        """Ensure the Labels CF exists; return its ID."""
        try:
            cf = self.op_client.get_custom_field_by_name(LABELS_CF_NAME)
            cf_id = int(cf.get("id")) if isinstance(cf, dict) else None
            if cf_id:
                return cf_id
        except Exception:  # noqa: BLE001
            logger.info("Labels CF not found; will create")

        # Create CF with is_for_all: false (selective project enablement)
        script = (
            f"cf = CustomField.find_by(type: 'WorkPackageCustomField', name: '{LABELS_CF_NAME}'); "
            f"if !cf; cf = CustomField.new(name: '{LABELS_CF_NAME}', field_format: 'text', is_required: false, is_for_all: false, type: 'WorkPackageCustomField'); cf.save; end; cf.id"
        )
        cf_id = self.op_client.execute_query(script)
        return int(cf_id) if isinstance(cf_id, int) else int(cf_id or 0)

    def _enable_cf_for_projects(self, cf_id: int, project_ids: set[int]) -> None:
        """Enable custom field for specific projects only."""
        if not project_ids:
            return
        project_ids_str = ", ".join(str(pid) for pid in sorted(project_ids))
        script = f"""
cf = CustomField.find({cf_id})
[{project_ids_str}].each do |pid|
  begin
    project = Project.find(pid)
    CustomFieldsProject.find_or_create_by!(custom_field: cf, project: project)
  rescue ActiveRecord::RecordNotFound
  end
end
true
""".strip()
        try:
            self.op_client.execute_query(script)
            logger.info("Enabled %s CF for %d projects", LABELS_CF_NAME, len(project_ids))
        except Exception:  # noqa: BLE001
            logger.warning("Failed to enable CF for some projects")

    def _extract(self) -> ComponentResult:
        """Extract Jira labels for all migrated issues."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        keys = [str(k) for k in wp_map.keys()]
        issues = self.jira_client.batch_get_issues(keys)
        labels_by_key: dict[str, list[str]] = {}
        for k, issue in issues.items():
            try:
                fields = getattr(issue, "fields", None)
                labels = getattr(fields, "labels", None)
                if isinstance(labels, list) and labels:
                    labels_by_key[k] = [str(x) for x in labels if isinstance(x, str) and x.strip()]
            except Exception:  # noqa: BLE001
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
        cf_id = self._ensure_labels_cf()
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
            cf_values_to_set.append({
                "work_package_id": wp_id,
                "custom_field_id": cf_id,
                "value": text,
            })

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
            self._enable_cf_for_projects(cf_id, projects_with_values)

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run labels migration using ETL pattern."""
        logger.info("Starting labels migration...")
        try:
            extracted = self._extract()
            if not extracted.success:
                return ComponentResult(
                    success=False,
                    message="Labels extraction failed",
                    errors=extracted.errors or ["labels extraction failed"],
                )

            mapped = self._map(extracted)
            if not mapped.success:
                return ComponentResult(
                    success=False,
                    message="Labels mapping failed",
                    errors=mapped.errors or ["labels mapping failed"],
                )

            result = self._load(mapped)
            logger.info(
                "Labels migration completed: success=%s, updated=%s, failed=%s",
                result.success,
                result.updated,
                result.failed,
            )
            return result
        except Exception as e:
            logger.exception("Labels migration failed")
            return ComponentResult(
                success=False,
                message=f"Labels migration failed: {e}",
                errors=[str(e)],
            )
