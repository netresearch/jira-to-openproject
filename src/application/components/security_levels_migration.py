"""Migrate Jira Issue Security Levels to OpenProject via CF fallback.

Creates/ensures a WorkPackage custom field "Security Level" (string) and writes
the Jira `fields.security.name` for mapped issues.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.application.components.base_migration import BaseMigration, register_entity_types
from src.clients.openproject_client import escape_ruby_single_quoted
from src.config import logger
from src.models import ComponentResult

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient

SECURITY_LEVEL_CF_NAME = "Security Level"


@register_entity_types("security_levels")
class SecurityLevelsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities for change detection.

        SecurityLevelsMigration is a transformation-only component that operates on
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
        """Extract Jira security level names per issue mapped to a WP."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        keys = [str(k) for k in wp_map]
        issues = self._merge_batch_issues(keys)

        sec_by_key: dict[str, str] = {}
        for k, issue in issues.items():
            try:
                fields = getattr(issue, "fields", None)
                sec = getattr(fields, "security", None)
                name = getattr(sec, "name", None)
                if name:
                    sec_by_key[k] = str(name)
            except Exception:
                continue
        return ComponentResult(success=True, data={"security": sec_by_key})

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        cf_id = self._ensure_wp_custom_field(SECURITY_LEVEL_CF_NAME, "string")
        if not cf_id:
            return ComponentResult(success=False, failed=1)

        wp_map = self.mappings.get_mapping("work_package") or {}
        sec_by_key: dict[str, str] = (mapped.data or {}).get("security", {})  # type: ignore[assignment]

        updated = 0
        failed = 0
        projects_with_values: set[int] = set()

        for jira_key, sec_name in sec_by_key.items():
            if not sec_name:
                continue
            entry = wp_map.get(jira_key)
            if not (isinstance(entry, dict) and entry.get("openproject_id")):
                continue
            wp_id = int(entry["openproject_id"])  # type: ignore[arg-type]
            # Track project for selective enablement
            project_id = entry.get("openproject_project_id")
            if project_id:
                projects_with_values.add(int(project_id))
            try:
                # Set CF value
                set_script = (
                    "wp = WorkPackage.find(%d); cf = CustomField.find(%d); "
                    "cv = wp.custom_value_for(cf); if cv; cv.value = '%s'; cv.save; else; wp.custom_field_values = { cf.id => '%s' }; end; wp.save!; true"
                    % (wp_id, cf_id, escape_ruby_single_quoted(sec_name), escape_ruby_single_quoted(sec_name))
                )
                ok = self.op_client.execute_query(set_script)
                if ok:
                    updated += 1
            except Exception:
                logger.exception("Failed to apply security level for %s", jira_key)
                failed += 1

        # Enable CF only for projects that have values
        if projects_with_values:
            self._enable_cf_for_projects(cf_id, projects_with_values, cf_name=SECURITY_LEVEL_CF_NAME)

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run Security levels migration."""
        return self._run_etl_pipeline("Security levels")
