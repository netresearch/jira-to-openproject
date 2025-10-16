"""Migrate Jira votes count to OpenProject via integer CF fallback.

Creates/ensures a WorkPackage custom field "Votes" (int) and writes the
Jira `fields.votes.votes` count for mapped issues.
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

VOTES_CF_NAME = "Votes"


@register_entity_types("votes_reactions")
class VotesMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)

        self.mappings = config.mappings

    def _ensure_votes_cf(self) -> int:
        """Ensure the Votes CF exists; return its ID."""
        try:
            cf = self.op_client.get_custom_field_by_name(VOTES_CF_NAME)
            cf_id = int(cf.get("id")) if isinstance(cf, dict) else None
            if cf_id:
                return cf_id
        except Exception:  # noqa: BLE001
            logger.info("Votes CF not found; will create")

        # Create CF via execute_query (int field, global)
        script = (
            f"cf = CustomField.find_by(type: 'WorkPackageCustomField', name: '{VOTES_CF_NAME}'); "
            f"if !cf; cf = CustomField.new(name: '{VOTES_CF_NAME}', field_format: 'int', is_required: false, is_for_all: true, type: 'WorkPackageCustomField'); cf.save; end; cf.id"
        )
        cf_id = self.op_client.execute_query(script)
        return int(cf_id) if isinstance(cf_id, int) else int(cf_id or 0)

    def _extract(self) -> ComponentResult:
        """Extract Jira votes count per issue mapped to a WP."""
        wp_map = self.mappings.get_mapping("work_package") or {}
        keys = [str(k) for k in wp_map.keys()]
        issues = self.jira_client.batch_get_issues(keys)

        votes_by_key: dict[str, int] = {}
        for k, issue in issues.items():
            try:
                fields = getattr(issue, "fields", None)
                votes_obj = getattr(fields, "votes", None)
                count = getattr(votes_obj, "votes", None)
                if isinstance(count, int):
                    votes_by_key[k] = count
            except Exception:  # noqa: BLE001
                continue
        return ComponentResult(success=True, data={"votes": votes_by_key})

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        data = extracted.data or {}
        return ComponentResult(success=True, data=data)

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        cf_id = self._ensure_votes_cf()
        if not cf_id:
            return ComponentResult(success=False, failed=1)

        wp_map = self.mappings.get_mapping("work_package") or {}
        votes_by_key: dict[str, int] = (mapped.data or {}).get("votes", {})  # type: ignore[assignment]

        updated = 0
        failed = 0

        for jira_key, count in votes_by_key.items():
            entry = wp_map.get(jira_key)
            if not (isinstance(entry, dict) and entry.get("openproject_id")):
                continue
            wp_id = int(entry["openproject_id"])  # type: ignore[arg-type]
            try:
                # Set CF value (store integer as string per OP conventions)
                set_script = (
                    "wp = WorkPackage.find(%d); cf = CustomField.find(%d); "
                    "cv = wp.custom_value_for(cf); if cv; cv.value = '%s'; cv.save; else; wp.custom_field_values = { cf.id => '%s' }; end; wp.save!; true"
                    % (wp_id, cf_id, str(count), str(count))
                )
                ok = self.op_client.execute_query(set_script)
                if ok:
                    updated += 1
            except Exception:
                logger.exception("Failed to apply votes for %s", jira_key)
                failed += 1

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run votes migration using ETL pattern."""
        logger.info("Starting votes migration...")
        try:
            extracted = self._extract()
            if not extracted.success:
                return ComponentResult(
                    success=False,
                    message="Votes extraction failed",
                    errors=extracted.errors or ["votes extraction failed"],
                )

            mapped = self._map(extracted)
            if not mapped.success:
                return ComponentResult(
                    success=False,
                    message="Votes mapping failed",
                    errors=mapped.errors or ["votes mapping failed"],
                )

            result = self._load(mapped)
            logger.info(
                "Votes migration completed: success=%s, updated=%s, failed=%s",
                result.success,
                result.updated,
                result.failed,
            )
            return result
        except Exception as e:
            logger.exception("Votes migration failed")
            return ComponentResult(
                success=False,
                message=f"Votes migration failed: {e}",
                errors=[str(e)],
            )


