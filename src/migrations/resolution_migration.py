"""Migrate Jira resolutions into OpenProject via custom field strategy.

Approach:
- Ensure WorkPackage CF "Resolution" exists (string field).
- Map Jira resolution names to CF values; set on WPs for resolved issues.

Note: Resolution change history is captured by the audit trail migration from
the Jira changelog. This migration only sets the current CF value to avoid
duplicate journal entries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.clients.openproject_client import escape_ruby_single_quoted
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient

from src.config import logger

RESOLUTION_CF_NAME = "Resolution"


@register_entity_types("resolutions")
class ResolutionMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities for change detection.

        ResolutionMigration is a transformation-only component that operates on
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
        """Extract Jira resolution per migrated issue (via work_package mapping)."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        keys = [str(k) for k in wp_map]
        issues = self._merge_batch_issues(keys)
        reso_by_key: dict[str, str] = {}
        for k, issue in issues.items():
            try:
                fields = getattr(issue, "fields", None)
                res = getattr(fields, "resolution", None)
                name = getattr(res, "name", None)
                if name:
                    reso_by_key[k] = str(name)
            except Exception:
                continue
        return ComponentResult(success=True, data={"resolution": reso_by_key})

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        cf_id = self._ensure_wp_custom_field(RESOLUTION_CF_NAME, "string")
        if not cf_id:
            return ComponentResult(success=False, failed=1)

        wp_map = self.mappings.get_mapping("work_package") or {}
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
                    % (wp_id, cf_id, escape_ruby_single_quoted(res_name), escape_ruby_single_quoted(res_name))
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
            self._enable_cf_for_projects(cf_id, projects_with_values, cf_name=RESOLUTION_CF_NAME)

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run Resolution migration."""
        return self._run_etl_pipeline("Resolution")
