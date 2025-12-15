"""Migrate Jira Issue Security Levels to OpenProject via CF fallback.

Creates/ensures a WorkPackage custom field "Security Level" (string) and writes
the Jira `fields.security.name` for mapped issues.
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

SECURITY_LEVEL_CF_NAME = "Security Level"


@register_entity_types("security_levels")
class SecurityLevelsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)

        self.mappings = config.mappings

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities for change detection.

        SecurityLevelsMigration is a transformation-only component that operates on
        already-migrated work packages. It doesn't fetch source data from Jira,
        so this returns an empty list to indicate no changes to detect.

        Args:
            entity_type: Type of entities

        Returns:
            Empty list (transformation-only, no source entities)

        """
        return []

    def _ensure_security_cf(self) -> int:
        """Ensure the Security Level CF exists; return its ID."""
        try:
            cf = self.op_client.get_custom_field_by_name(SECURITY_LEVEL_CF_NAME)
            cf_id = int(cf.get("id")) if isinstance(cf, dict) else None
            if cf_id:
                return cf_id
        except Exception:  # noqa: BLE001
            logger.info("Security Level CF not found; will create")

        # Create CF with is_for_all: false (selective project enablement)
        script = (
            f"cf = CustomField.find_by(type: 'WorkPackageCustomField', name: '{SECURITY_LEVEL_CF_NAME}'); "
            f"if !cf; cf = CustomField.new(name: '{SECURITY_LEVEL_CF_NAME}', field_format: 'string', is_required: false, is_for_all: false, type: 'WorkPackageCustomField'); cf.save; end; cf.id"
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
            logger.info("Enabled %s CF for %d projects", SECURITY_LEVEL_CF_NAME, len(project_ids))
        except Exception:  # noqa: BLE001
            logger.warning("Failed to enable CF for some projects")

    def _extract(self) -> ComponentResult:
        """Extract Jira security level names per issue mapped to a WP."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        keys = [str(k) for k in wp_map.keys()]
        issues = self.jira_client.batch_get_issues(keys)

        sec_by_key: dict[str, str] = {}
        for k, issue in issues.items():
            try:
                fields = getattr(issue, "fields", None)
                sec = getattr(fields, "security", None)
                name = getattr(sec, "name", None)
                if name:
                    sec_by_key[k] = str(name)
            except Exception:  # noqa: BLE001
                continue
        return ComponentResult(success=True, data={"security": sec_by_key})

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        data = extracted.data or {}
        return ComponentResult(success=True, data=data)

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        cf_id = self._ensure_security_cf()
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
                    % (wp_id, cf_id, sec_name.replace("'", "\\'"), sec_name.replace("'", "\\'"))
                )
                ok = self.op_client.execute_query(set_script)
                if ok:
                    updated += 1
            except Exception:
                logger.exception("Failed to apply security level for %s", jira_key)
                failed += 1

        # Enable CF only for projects that have values
        if projects_with_values:
            self._enable_cf_for_projects(cf_id, projects_with_values)

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run security levels migration using ETL pattern."""
        logger.info("Starting security levels migration...")
        try:
            extracted = self._extract()
            if not extracted.success:
                return ComponentResult(
                    success=False,
                    message="Security levels extraction failed",
                    errors=extracted.errors or ["security levels extraction failed"],
                )

            mapped = self._map(extracted)
            if not mapped.success:
                return ComponentResult(
                    success=False,
                    message="Security levels mapping failed",
                    errors=mapped.errors or ["security levels mapping failed"],
                )

            result = self._load(mapped)
            logger.info(
                "Security levels migration completed: success=%s, updated=%s, failed=%s",
                result.success,
                result.updated,
                result.failed,
            )
            return result
        except Exception as e:
            logger.exception("Security levels migration failed")
            return ComponentResult(
                success=False,
                message=f"Security levels migration failed: {e}",
                errors=[str(e)],
            )
