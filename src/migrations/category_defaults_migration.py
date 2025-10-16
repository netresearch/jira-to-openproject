"""Set default assignee for Categories from Jira Component leads.

For each project, fetch Jira components and their leads, map to OP users, find
matching Category by name in the corresponding OP project, and set assigned_to.
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
except Exception:  # noqa: BLE001
    logger = configure_logging("INFO", None)

from src import config


@register_entity_types("category_defaults")
class CategoryDefaultsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)

        self.mappings = config.mappings

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities for transformation.

        This migration performs data transformation on project components
        rather than fetching directly from Jira. It operates on already-fetched
        project data and component information.

        Args:
            entity_type: The type of entities requested

        Returns:
            Empty list (this migration doesn't fetch from Jira directly)

        Raises:
            ValueError: Always, as this migration doesn't support idempotent workflow

        """
        msg = (
            "CategoryDefaultsMigration is a transformation-only migration and does not "
            "support idempotent workflow. It operates on data from other migrations."
        )
        raise ValueError(msg)

    def _extract(self) -> ComponentResult:
        """Extract per-project component->lead mapping from Jira."""
        # Expect jira_client.get_project_components(project_key) to yield components with 'name' and 'lead' (name/mail)
        proj_map = self.mappings.get_mapping("project") or {}
        components_by_project: dict[str, list[dict[str, Any]]] = {}
        for jira_key, entry in (proj_map or {}).items():
            try:
                comps = self.jira_client.get_project_components(jira_key)  # type: ignore[attr-defined]
                if isinstance(comps, list) and comps:
                    components_by_project[jira_key] = comps
            except Exception:  # noqa: BLE001
                continue
        return ComponentResult(success=True, data={"components": components_by_project})

    def _resolve_user_id(self, user_hint: Any) -> int | None:
        # Map via user mapping by login/mail if present
        try:
            umap = self.mappings.get_mapping("user") or {}
            if isinstance(user_hint, dict):
                key = user_hint.get("accountId") or user_hint.get("name") or user_hint.get("email") or user_hint.get("mail")
                if key and key in umap:
                    rec = umap[key]
                    if isinstance(rec, dict) and rec.get("openproject_id"):
                        return int(rec["openproject_id"])  # type: ignore[arg-type]
            elif isinstance(user_hint, str) and user_hint in umap:
                rec = umap[user_hint]
                if isinstance(rec, dict) and rec.get("openproject_id"):
                    return int(rec["openproject_id"])  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return None
        return None

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        """No transformation needed; pass through extracted data."""
        return ComponentResult(success=True, data=extracted.data)

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        data = mapped.data or {}
        components_by_project: dict[str, list[dict[str, Any]]] = data.get("components", {}) if isinstance(data, dict) else {}

        proj_map = self.mappings.get_mapping("project") or {}
        updated = 0
        failed = 0

        for jira_key, comps in components_by_project.items():
            proj_entry = proj_map.get(jira_key)
            if not (isinstance(proj_entry, dict) and proj_entry.get("openproject_id")):
                continue
            project_id = int(proj_entry["openproject_id"])  # type: ignore[arg-type]

            for comp in comps:
                try:
                    name = None
                    lead = None
                    if isinstance(comp, dict):
                        name = comp.get("name")
                        lead = comp.get("lead") or comp.get("componentLead")
                    if not (isinstance(name, str) and name.strip()):
                        continue
                    user_id = self._resolve_user_id(lead)
                    if not user_id:
                        continue
                    # Rails script: find category by name+project, set assigned_to
                    script = (
                        "p = Project.find(%d); c = Category.find_by(project: p, name: '%s'); "
                        "if c; c.assigned_to = User.find(%d); c.save!; true; else; false; end"
                        % (project_id, name.replace("'", "\\'"), user_id)
                    )
                    ok = self.op_client.execute_query(script)
                    if ok:
                        updated += 1
                    else:
                        failed += 1
                except Exception:
                    logger.exception("Failed to set category default for %s/%s", jira_key, comp)
                    failed += 1

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run category defaults migration using ETL pattern."""
        logger.info("Starting category defaults migration...")
        try:
            extracted = self._extract()
            if not extracted.success:
                return ComponentResult(
                    success=False,
                    message="Category defaults extraction failed",
                    errors=extracted.errors or ["category defaults extraction failed"],
                )

            mapped = self._map(extracted)
            if not mapped.success:
                return ComponentResult(
                    success=False,
                    message="Category defaults mapping failed",
                    errors=mapped.errors or ["category defaults mapping failed"],
                )

            result = self._load(mapped)
            logger.info(
                "Category defaults migration completed: success=%s, updated=%s, failed=%s",
                result.success,
                result.updated,
                result.failed,
            )
            return result
        except Exception as e:
            logger.exception("Category defaults migration failed")
            return ComponentResult(
                success=False,
                message=f"Category defaults migration failed: {e}",
                errors=[str(e)],
            )


