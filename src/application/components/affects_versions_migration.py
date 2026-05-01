"""Migrate Jira Affects Versions (versions field) to OpenProject via CF fallback.

Creates/ensures a WorkPackage custom field "Affects Versions" (text) and writes
comma-separated version names from Jira `versions` (distinct from `fixVersions`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.application.components.base_migration import BaseMigration, register_entity_types
from src.clients.openproject_client import escape_ruby_single_quoted
from src.models import ComponentResult

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient

from src.config import logger

AFFECTS_VERSIONS_CF_NAME = "Affects Versions"


@register_entity_types("affects_versions")
class AffectsVersionsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict]:
        """Get current entities for change detection.

        AffectsVersionsMigration is a transformation-only component that operates on
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
        wp_map = self.mappings.get_mapping("work_package") or {}
        keys = [str(k) for k in wp_map]
        if not keys:
            return ComponentResult(success=True, data={"versions": {}})
        issues = self._merge_batch_issues(keys)
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
            except Exception:
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
        cf_id = self._ensure_wp_custom_field(AFFECTS_VERSIONS_CF_NAME, "text")
        if not cf_id:
            return ComponentResult(success=False, failed=1)

        wp_map = self.mappings.get_mapping("work_package") or {}
        data = mapped.data or {}
        text_by_key: dict[str, str] = data.get("affects_versions_text", {}) if isinstance(data, dict) else {}

        updated = 0
        failed = 0
        projects_with_values: set[int] = set()

        for jira_key, text in text_by_key.items():
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
            try:
                val = escape_ruby_single_quoted(text)
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

        # Enable CF only for projects that have values
        if projects_with_values:
            self._enable_cf_for_projects(cf_id, projects_with_values, cf_name=AFFECTS_VERSIONS_CF_NAME)

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run Affects versions migration."""
        return self._run_etl_pipeline("Affects versions")
