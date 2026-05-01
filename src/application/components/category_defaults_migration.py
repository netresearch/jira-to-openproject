"""Set default assignee for Categories from Jira Component leads.

For each project, fetch Jira components and their leads, map to OP users, find
matching Category by name in the corresponding OP project, and set assigned_to.
"""

from __future__ import annotations

from typing import Any

from src.application.components.base_migration import BaseMigration, register_entity_types
from src.config import logger
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient, escape_ruby_single_quoted
from src.models import ComponentResult
from src.models.jira import JiraComponentLead, JiraProjectComponent


@register_entity_types("category_defaults")
class CategoryDefaultsMigration(BaseMigration):  # noqa: D101
    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

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
        """Extract per-project component->lead mapping from Jira.

        Each component is parsed at the boundary into a typed
        :class:`JiraProjectComponent` so downstream code reads
        ``comp.name`` / ``comp.effective_lead`` rather than juggling
        dict lookups.
        """
        # Expect jira_client.get_project_components(project_key) to yield raw
        # component shapes (dicts with 'name' and 'lead', or SDK-like objects).
        proj_map = self.mappings.get_mapping("project") or {}
        components_by_project: dict[str, list[JiraProjectComponent]] = {}
        for jira_key in proj_map or {}:
            try:
                comps_raw = self.jira_client.get_project_components(jira_key)  # type: ignore[attr-defined]
            except Exception:
                continue
            if not isinstance(comps_raw, list) or not comps_raw:
                continue
            typed: list[JiraProjectComponent] = []
            for raw in comps_raw:
                try:
                    typed.append(JiraProjectComponent.from_any(raw))
                except Exception:
                    continue
            if typed:
                components_by_project[jira_key] = typed
        return ComponentResult(success=True, data={"components": components_by_project})

    def _resolve_user_id(self, lead: JiraComponentLead | None) -> int | None:
        """Map a typed component lead to an OpenProject user id.

        Probes the user mapping using (in order): ``accountId``, ``name``,
        ``key``, ``emailAddress``. ``None`` is returned when no probe
        hits — the caller skips silently.
        """
        if lead is None:
            return None
        try:
            umap = self.mappings.get_mapping("user") or {}
            for probe in (lead.account_id, lead.name, lead.key, lead.email_address):
                if not probe or probe not in umap:
                    continue
                rec = umap[probe]
                if isinstance(rec, dict) and rec.get("openproject_id"):
                    return int(rec["openproject_id"])  # type: ignore[arg-type]
        except Exception:
            return None
        return None

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        data = mapped.data or {}
        components_by_project: dict[str, list[JiraProjectComponent]] = (
            data.get("components", {}) if isinstance(data, dict) else {}
        )

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
                    name = comp.name
                    if not (isinstance(name, str) and name.strip()):
                        continue
                    user_id = self._resolve_user_id(comp.effective_lead)
                    if not user_id:
                        continue
                    # Rails script: find category by name+project, set assigned_to
                    script = (
                        "p = Project.find(%d); c = Category.find_by(project: p, name: '%s'); "
                        "if c; c.assigned_to = User.find(%d); c.save!; true; else; false; end"
                        % (project_id, escape_ruby_single_quoted(name), user_id)
                    )
                    ok = self.op_client.execute_query(script)
                    if ok:
                        updated += 1
                    else:
                        failed += 1
                except Exception:
                    logger.exception("Failed to set category default for %s/%s", jira_key, comp.name)
                    failed += 1

        return ComponentResult(success=failed == 0, updated=updated, failed=failed)

    def run(self) -> ComponentResult:
        """Run Category defaults migration."""
        return self._run_etl_pipeline("Category defaults")
