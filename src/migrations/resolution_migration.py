"""Migrate Jira resolutions into OpenProject via custom field strategy.

Approach:
- Ensure WorkPackage CF "Resolution" exists (string field).
- Map Jira resolution names to CF values; set on WPs for resolved issues.

Note: Resolution change history is captured by the audit trail migration from
the Jira changelog. This migration only sets the current CF value to avoid
duplicate journal entries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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


RESOLUTION_CF_NAME = "Resolution"


@register_entity_types("resolutions")
class ResolutionMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)
        self.mappings = config.mappings

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities for change detection.

        ResolutionMigration is a transformation-only component that operates on
        already-migrated work packages. It doesn't fetch source data from Jira,
        so this returns an empty list to indicate no changes to detect.

        Args:
            entity_type: Type of entities

        Returns:
            Empty list (transformation-only, no source entities)

        """
        return []

    def _ensure_resolution_cf(self) -> int:
        """Ensure the Resolution CF exists; return its ID."""
        try:
            cf = self.op_client.get_custom_field_by_name(RESOLUTION_CF_NAME)
            cf_id = int(cf.get("id")) if isinstance(cf, dict) else None
            if cf_id:
                return cf_id
        except Exception:  # noqa: BLE001
            logger.info("Resolution CF not found; will create")

        # Create CF with is_for_all: false (selective project enablement)
        # Projects are enabled individually as values are set
        script = (
            f"cf = CustomField.find_by(type: 'WorkPackageCustomField', name: '{RESOLUTION_CF_NAME}'); "
            f"if !cf; cf = CustomField.new(name: '{RESOLUTION_CF_NAME}', field_format: 'string', is_required: false, is_for_all: false, type: 'WorkPackageCustomField'); cf.save; end; cf.id"
        )
        cf_id = self.op_client.execute_query(script)
        return int(cf_id) if isinstance(cf_id, int) else int(cf_id or 0)

    def _enable_cf_for_projects(self, cf_id: int, project_ids: set[int]) -> None:
        """Enable custom field for specific projects only.

        Args:
            cf_id: Custom field ID
            project_ids: Set of project IDs to enable the field for

        """
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
            logger.info("Enabled %s CF for %d projects", RESOLUTION_CF_NAME, len(project_ids))
        except Exception:  # noqa: BLE001
            logger.warning("Failed to enable CF for some projects")

    def _extract(self) -> ComponentResult:
        """Extract Jira resolution per migrated issue (via work_package mapping)."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        keys = [str(k) for k in wp_map.keys()]
        issues = self.jira_client.batch_get_issues(keys)
        reso_by_key: dict[str, str] = {}
        for k, issue in issues.items():
            try:
                fields = getattr(issue, "fields", None)
                res = getattr(fields, "resolution", None)
                name = getattr(res, "name", None)
                if name:
                    reso_by_key[k] = str(name)
            except Exception:  # noqa: BLE001
                continue
        return ComponentResult(success=True, data={"resolution": reso_by_key})

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        data = extracted.data or {}
        return ComponentResult(success=True, data=data)

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        cf_id = self._ensure_resolution_cf()
        if not cf_id:
            return ComponentResult(success=False, failed=1)

        wp_map = self.mappings.get_mapping("work_package") or {}
        project_map = self.mappings.get_mapping("project") or {}
        reso_by_key: dict[str, str] = (mapped.data or {}).get("resolution", {})  # type: ignore[assignment]

        updated = 0
        failed = 0
        projects_with_values: set[int] = set()

        # Set CF value only - audit trail migration handles resolution history
        for jira_key, res_name in reso_by_key.items():
            entry = wp_map.get(jira_key)
            if not (isinstance(entry, dict) and entry.get("openproject_id")):
                continue
            wp_id = int(entry["openproject_id"])  # type: ignore[arg-type]

            # Track project ID for selective CF enablement
            project_id = entry.get("openproject_project_id")
            if project_id:
                projects_with_values.add(int(project_id))

            try:
                # Set CF value - do NOT create separate journal entry
                # Resolution changes are captured by audit trail migration from
                # the Jira changelog (creates "resolution: X → Y" entries)
                set_script = (
                    "wp = WorkPackage.find(%d); cf = CustomField.find(%d); "
                    "cv = wp.custom_value_for(cf); if cv; cv.value = '%s'; cv.save; else; wp.custom_field_values = { cf.id => '%s' }; end; wp.save!; wp.project_id"
                    % (wp_id, cf_id, res_name.replace("'", "\\'"), res_name.replace("'", "\\'"))
                )
                result = self.op_client.execute_query(set_script)
                if result:
                    updated += 1
                    # Also track project from WP response if not in mapping
                    if isinstance(result, int) and result > 0:
                        projects_with_values.add(result)
            except Exception:
                logger.exception("Failed to apply resolution for %s", jira_key)
                failed += 1

        # Enable CF for projects that have resolution values
        if projects_with_values:
            self._enable_cf_for_projects(cf_id, projects_with_values)

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run resolution migration using ETL pattern."""
        logger.info("Starting resolution migration...")
        try:
            extracted = self._extract()
            if not extracted.success:
                return ComponentResult(
                    success=False,
                    message="Resolution extraction failed",
                    errors=extracted.errors or ["resolution extraction failed"],
                )

            mapped = self._map(extracted)
            if not mapped.success:
                return ComponentResult(
                    success=False,
                    message="Resolution mapping failed",
                    errors=mapped.errors or ["resolution mapping failed"],
                )

            result = self._load(mapped)
            logger.info(
                "Resolution migration completed: success=%s, updated=%s, failed=%s",
                result.success,
                result.updated,
                result.failed,
            )
            return result
        except Exception as e:
            logger.exception("Resolution migration failed")
            return ComponentResult(
                success=False,
                message=f"Resolution migration failed: {e}",
                errors=[str(e)],
            )
