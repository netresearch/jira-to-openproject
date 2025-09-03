"""Migrate Jira Issue Security Levels to OpenProject via CF fallback.

Creates/ensures a WorkPackage custom field "Security Level" (string) and writes
the Jira `fields.security.name` for mapped issues.
"""

from __future__ import annotations

from typing import Any

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import configure_logging
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

try:
    from src.config import logger as logger  # type: ignore
    from src import config
except Exception:  # noqa: BLE001
    logger = configure_logging("INFO", None)
    from src import config  # type: ignore  # noqa: PLC0415


SECURITY_LEVEL_CF_NAME = "Security Level"


@register_entity_types("security_levels")
class SecurityLevelsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)
        import src.mappings as mappings  # noqa: PLC0415

        self.mappings = mappings.Mappings()

    def _ensure_security_cf(self) -> int:
        """Ensure the Security Level CF exists; return its ID."""
        try:
            cf = self.op_client.get_custom_field_by_name(SECURITY_LEVEL_CF_NAME)
            cf_id = int(cf.get("id")) if isinstance(cf, dict) else None
            if cf_id:
                return cf_id
        except Exception:  # noqa: BLE001
            logger.info("Security Level CF not found; will create")

        # Create CF via execute_query (string field, global)
        script = (
            "cf = CustomField.find_by(type: 'WorkPackageCustomField', name: '%s'); "
            "if !cf; cf = CustomField.new(name: '%s', field_format: 'string', is_required: false, is_for_all: true, type: 'WorkPackageCustomField'); cf.save; end; cf.id"
            % (SECURITY_LEVEL_CF_NAME, SECURITY_LEVEL_CF_NAME)
        )
        cf_id = self.op_client.execute_query(script)
        return int(cf_id) if isinstance(cf_id, int) else int(cf_id or 0)

    def _extract(self) -> ComponentResult:  # noqa: D401
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

    def _map(self, extracted: ComponentResult) -> ComponentResult:  # noqa: D401
        data = extracted.data or {}
        return ComponentResult(success=True, data=data)

    def _load(self, mapped: ComponentResult) -> ComponentResult:  # noqa: D401
        cf_id = self._ensure_security_cf()
        if not cf_id:
            return ComponentResult(success=False, failed=1)

        wp_map = self.mappings.get_mapping("work_package") or {}
        sec_by_key: dict[str, str] = (mapped.data or {}).get("security", {})  # type: ignore[assignment]

        updated = 0
        failed = 0

        for jira_key, sec_name in sec_by_key.items():
            entry = wp_map.get(jira_key)
            if not (isinstance(entry, dict) and entry.get("openproject_id")):
                continue
            wp_id = int(entry["openproject_id"])  # type: ignore[arg-type]
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
            except Exception:  # noqa: BLE001
                logger.exception("Failed to apply security level for %s", jira_key)
                failed += 1

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)


