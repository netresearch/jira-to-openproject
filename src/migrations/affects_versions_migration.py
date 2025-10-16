"""Migrate Jira Affects Versions (versions field) to OpenProject via CF fallback.

Creates/ensures a WorkPackage custom field "Affects Versions" (text) and writes
comma-separated version names from Jira `versions` (distinct from `fixVersions`).
"""

from __future__ import annotations

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import configure_logging
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

try:
    from src import config
    from src.config import logger as logger  # type: ignore
except Exception:  # noqa: BLE001
    logger = configure_logging("INFO", None)
    from src import config  # type: ignore


AFFECTS_VERSIONS_CF_NAME = "Affects Versions"


@register_entity_types("affects_versions")
class AffectsVersionsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)
        self.mappings = config.mappings

    def _ensure_cf(self) -> int:
        try:
            cf = self.op_client.get_custom_field_by_name(AFFECTS_VERSIONS_CF_NAME)
            cf_id = int(cf.get("id")) if isinstance(cf, dict) else None
            if cf_id:
                return cf_id
        except Exception:  # noqa: BLE001
            logger.info("Affects Versions CF not found; will create")

        script = (
            "cf = CustomField.find_by(type: 'WorkPackageCustomField', name: '%s'); "
            "if !cf; cf = CustomField.new(name: '%s', field_format: 'text', is_required: false, is_for_all: true, type: 'WorkPackageCustomField'); cf.save; end; cf.id"
            % (AFFECTS_VERSIONS_CF_NAME, AFFECTS_VERSIONS_CF_NAME)
        )
        cf_id = self.op_client.execute_query(script)
        return int(cf_id) if isinstance(cf_id, int) else int(cf_id or 0)

    def _extract(self) -> ComponentResult:
        wp_map = self.mappings.get_mapping("work_package") or {}
        keys = [str(k) for k in wp_map.keys()]
        if not keys:
            return ComponentResult(success=True, data={"versions": {}})
        issues = self.jira_client.batch_get_issues(keys)
        versions_by_key: dict[str, list[str]] = {}
        for k, issue in issues.items():
            try:
                fields = getattr(issue, "fields", None)
                vers = getattr(fields, "versions", None)
                if isinstance(vers, list) and vers:
                    names = []
                    for v in vers:
                        name = getattr(v, "name", None)
                        if name and isinstance(name, str) and name.strip():
                            names.append(name.strip())
                    if names:
                        versions_by_key[k] = names
            except Exception:  # noqa: BLE001
                continue
        return ComponentResult(success=True, data={"versions": versions_by_key})

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        data = extracted.data or {}
        raw: dict[str, list[str]] = data.get("versions", {}) if isinstance(data, dict) else {}
        norm: dict[str, str] = {}
        for key, names in raw.items():
            unique_sorted = sorted({n.strip() for n in names if n and isinstance(n, str)})
            if unique_sorted:
                norm[key] = ", ".join(unique_sorted)
        return ComponentResult(success=True, data={"affects_versions_text": norm})

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        cf_id = self._ensure_cf()
        if not cf_id:
            return ComponentResult(success=False, failed=1)

        wp_map = self.mappings.get_mapping("work_package") or {}
        data = mapped.data or {}
        text_by_key: dict[str, str] = data.get("affects_versions_text", {}) if isinstance(data, dict) else {}

        updated = 0
        failed = 0
        for jira_key, text in text_by_key.items():
            if not text:
                continue
            entry = wp_map.get(jira_key)
            if not (isinstance(entry, dict) and entry.get("openproject_id")):
                continue
            wp_id = int(entry["openproject_id"])  # type: ignore[arg-type]
            try:
                val = text.replace("'", "\\'")
                script = (
                    "wp = WorkPackage.find(%d); cf = CustomField.find(%d); "
                    "cv = wp.custom_value_for(cf); if cv; cv.value = '%s'; cv.save; else; wp.custom_field_values = { cf.id => '%s' }; end; wp.save!; true"
                    % (wp_id, cf_id, val, val)
                )
                ok = self.op_client.execute_query(script)
                if ok:
                    updated += 1
                else:
                    failed += 1
            except Exception:
                logger.exception("Failed to set Affects Versions for %s", jira_key)
                failed += 1

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)
    def run(self) -> ComponentResult:
        """Execute migration pipeline."""
        self.logger.info("Starting %s migration", self.__class__.__name__)

        extracted = self._extract()
        if not extracted.success:
            self.logger.error("%s extraction failed: %s", self.__class__.__name__, extracted.message or extracted.error)
            return extracted

        mapped = self._map(extracted)
        if not mapped.success:
            self.logger.error("%s mapping failed: %s", self.__class__.__name__, mapped.message or mapped.error)
            return mapped

        loaded = self._load(mapped)
        if loaded.success:
            self.logger.info("%s migration completed (updated=%s, failed=%s)", self.__class__.__name__, loaded.updated, loaded.failed)
        else:
            self.logger.error("%s migration encountered failures (failed=%s)", self.__class__.__name__, loaded.failed)
        return loaded
