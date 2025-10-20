"""Migrate Jira resolutions into OpenProject via CF + audit note strategy.

Approach:
- Ensure WorkPackage CF "Resolution" exists (string or list).
- Map Jira resolution names to CF values; set on WPs for resolved issues.
- Append audit journal entry noting resolution for traceability.
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
        from typing import Any
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

        # Create CF via execute_query (string field, global)
        script = (
            f"cf = CustomField.find_by(type: 'WorkPackageCustomField', name: '{RESOLUTION_CF_NAME}'); "
            f"if !cf; cf = CustomField.new(name: '{RESOLUTION_CF_NAME}', field_format: 'string', is_required: false, is_for_all: true, type: 'WorkPackageCustomField'); cf.save; end; cf.id"
        )
        cf_id = self.op_client.execute_query(script)
        return int(cf_id) if isinstance(cf_id, int) else int(cf_id or 0)

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
        reso_by_key: dict[str, str] = (mapped.data or {}).get("resolution", {})  # type: ignore[assignment]

        updated = 0
        failed = 0
        # Batch updates: set CF and append audit note as Journal
        for jira_key, res_name in reso_by_key.items():
            entry = wp_map.get(jira_key)
            if not (isinstance(entry, dict) and entry.get("openproject_id")):
                continue
            wp_id = int(entry["openproject_id"])  # type: ignore[arg-type]
            try:
                # Set CF
                set_script = (
                    "wp = WorkPackage.find(%d); cf = CustomField.find(%d); "
                    "cv = wp.custom_value_for(cf); if cv; cv.value = '%s'; cv.save; else; wp.custom_field_values = { cf.id => '%s' }; end; wp.save!; true"
                    % (wp_id, cf_id, res_name.replace("'", "\\'"), res_name.replace("'", "\\'"))
                )
                ok = self.op_client.execute_query(set_script)
                if ok:
                    updated += 1
                # Add audit note
                note = f"Resolution: {res_name} (migrated from Jira)"
                note_esc = note.replace("'", "\\'")
                journal_script = (
                    "wp = WorkPackage.find(%d); Journal::WorkPackageJournal.create!(journaled_id: wp.id, notes: '%s', user_id: wp.author_id); true"
                    % (wp_id, note_esc)
                )
                self.op_client.execute_query(journal_script)
            except Exception:
                logger.exception("Failed to apply resolution for %s", jira_key)
                failed += 1

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


