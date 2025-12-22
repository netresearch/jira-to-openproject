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

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict]:
        """Get current entities for change detection.

        VotesMigration is a transformation-only component that operates on
        already-migrated work packages. It doesn't fetch source data from Jira,
        so this returns an empty list to indicate no changes to detect.

        Args:
            entity_type: Type of entities

        Returns:
            Empty list (transformation-only, no source entities)

        """
        return []

    def _ensure_votes_cf(self) -> int:
        """Ensure the Votes CF exists; return its ID."""
        try:
            cf = self.op_client.get_custom_field_by_name(VOTES_CF_NAME)
            cf_id = int(cf.get("id")) if isinstance(cf, dict) else None
            if cf_id:
                return cf_id
        except Exception:  # noqa: BLE001
            logger.info("Votes CF not found; will create")

        # Create CF with is_for_all: false (selective project enablement)
        script = (
            f"cf = CustomField.find_by(type: 'WorkPackageCustomField', name: '{VOTES_CF_NAME}'); "
            f"if !cf; cf = CustomField.new(name: '{VOTES_CF_NAME}', field_format: 'int', is_required: false, is_for_all: false, type: 'WorkPackageCustomField'); cf.save; end; cf.id"
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
            logger.info("Enabled %s CF for %d projects", VOTES_CF_NAME, len(project_ids))
        except Exception:  # noqa: BLE001
            logger.warning("Failed to enable CF for some projects")

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

        # Collect all CF values for bulk update
        cf_values_to_set: list[dict] = []
        projects_with_values: set[int] = set()

        for jira_key, count in votes_by_key.items():
            # Only track non-zero votes (meaningful values)
            if count == 0:
                continue
            entry = wp_map.get(jira_key)
            if not (isinstance(entry, dict) and entry.get("openproject_id")):
                continue
            wp_id = int(entry["openproject_id"])  # type: ignore[arg-type]
            # Track project for selective enablement
            project_id = entry.get("openproject_project_id")
            if project_id:
                projects_with_values.add(int(project_id))
            cf_values_to_set.append({
                "work_package_id": wp_id,
                "custom_field_id": cf_id,
                "value": str(count),
            })

        # Bulk set all CF values in single Rails call
        updated = 0
        failed = 0
        if cf_values_to_set:
            logger.info("Bulk setting %d votes values...", len(cf_values_to_set))
            bulk_result = self.op_client.bulk_set_wp_custom_field_values(cf_values_to_set)
            updated = bulk_result.get("updated", 0)
            failed = bulk_result.get("failed", 0)
            logger.info("Bulk votes: updated=%d, failed=%d", updated, failed)

        # Enable CF only for projects that have values
        if projects_with_values:
            self._enable_cf_for_projects(cf_id, projects_with_values)

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
