"""Migrate Jira labels to OpenProject using a custom field fallback.

Preferred path would be native tags; fallback implemented here uses a
WorkPackage custom field named "Labels" storing a comma-separated list.
"""

from __future__ import annotations

from typing import Any

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import configure_logging
from src.mappings import Mappings
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

try:
    from src.config import logger as logger  # type: ignore
    from src import config
except Exception:  # noqa: BLE001
    logger = configure_logging("INFO", None)
    from src import config  # type: ignore  # noqa: PLC0415


LABELS_CF_NAME = "Labels"


@register_entity_types("labels")
class LabelsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)
        import src.mappings as mappings  # noqa: PLC0415

        self.mappings = mappings.Mappings()

    def _ensure_labels_cf(self) -> int:
        """Ensure the Labels CF exists; return its ID."""
        try:
            cf = self.op_client.get_custom_field_by_name(LABELS_CF_NAME)
            cf_id = int(cf.get("id")) if isinstance(cf, dict) else None
            if cf_id:
                return cf_id
        except Exception:  # noqa: BLE001
            logger.info("Labels CF not found; will create")

        script = (
            "cf = CustomField.find_by(type: 'WorkPackageCustomField', name: '%s'); "
            "if !cf; cf = CustomField.new(name: '%s', field_format: 'text', is_required: false, is_for_all: true, type: 'WorkPackageCustomField'); cf.save; end; cf.id"
            % (LABELS_CF_NAME, LABELS_CF_NAME)
        )
        cf_id = self.op_client.execute_query(script)
        return int(cf_id) if isinstance(cf_id, int) else int(cf_id or 0)

    def _extract(self) -> ComponentResult:  # noqa: D401
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

    def _map(self, extracted: ComponentResult) -> ComponentResult:  # noqa: D401
        data = extracted.data or {}
        raw: dict[str, list[str]] = data.get("labels", {}) if isinstance(data, dict) else {}

        # Normalize: sort unique labels; join with ", "
        norm: dict[str, str] = {}
        for key, labels in raw.items():
            unique_sorted = sorted({l.strip() for l in labels if l and isinstance(l, str)})
            if unique_sorted:
                norm[key] = ", ".join(unique_sorted)
        return ComponentResult(success=True, data={"labels_markdown": norm})

    def _load(self, mapped: ComponentResult) -> ComponentResult:  # noqa: D401
        cf_id = self._ensure_labels_cf()
        if not cf_id:
            return ComponentResult(success=False, failed=1)

        wp_map = self.mappings.get_mapping("work_package") or {}
        data = mapped.data or {}
        labels_text: dict[str, str] = data.get("labels_markdown", {}) if isinstance(data, dict) else {}

        updated = 0
        failed = 0
        for jira_key, text in labels_text.items():
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
            except Exception:  # noqa: BLE001
                logger.exception("Failed to set labels for %s", jira_key)
                failed += 1

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)


