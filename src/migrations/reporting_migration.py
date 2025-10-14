"""Migrate Jira filters and dashboards into OpenProject queries and wiki summaries."""

from __future__ import annotations

from typing import Any

from src import config
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult


REPORTING_PROJECT_IDENTIFIER_DEFAULT = "j2o-reporting"
REPORTING_PROJECT_NAME_DEFAULT = "Jira Dashboards"


@register_entity_types("reporting")
class ReportingMigration(BaseMigration):
    """Create OpenProject artefacts representing Jira saved filters and dashboards."""

    def __init__(self, jira_client, op_client) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)
        self.project_mapping = config.mappings.get_mapping("project") or {}

    def _extract(self) -> ComponentResult:
        """Fetch Jira filters and dashboards."""

        try:
            filters = self.jira_client.get_filters()
        except Exception as exc:  # noqa: BLE001
            return ComponentResult(
                success=False,
                message=f"Failed to fetch Jira filters: {exc}",
                error=str(exc),
            )

        try:
            dashboards = self.jira_client.get_dashboards()
        except Exception as exc:  # noqa: BLE001
            dashboards = []
            self.logger.exception("Failed to fetch Jira dashboards: %s", exc)

        dashboard_details: list[dict[str, Any]] = []
        for dashboard in dashboards:
            dash_id = dashboard.get("id")
            if dash_id is None:
                continue
            try:
                detail = self.jira_client.get_dashboard_details(int(dash_id))
            except Exception:  # noqa: BLE001
                detail = dashboard
            dashboard_details.append(detail)

        return ComponentResult(
            success=True,
            data={
                "filters": filters,
                "dashboards": dashboard_details,
            },
            total_count=len(filters) + len(dashboard_details),
        )

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        """Convert filters and dashboard metadata to OpenProject payloads."""

        if not extracted.success or not isinstance(extracted.data, dict):
            return ComponentResult(
                success=False,
                message="Reporting extraction failed",
                error=extracted.message or "extract phase returned no data",
            )

        filters: list[dict[str, Any]] = extracted.data.get("filters", [])
        dashboards: list[dict[str, Any]] = extracted.data.get("dashboards", [])

        query_payloads: list[dict[str, Any]] = []
        wiki_payloads: list[dict[str, Any]] = []
        skipped_dashboards: list[dict[str, Any]] = []

        explanation_lines = [
            "_This page was generated automatically during the Jira→OpenProject migration to preserve the original dashboard metadata._",
            "_It contains pointers back to the Jira dashboard so you can rebuild or replace it using OpenProject components._",
            "",
        ]

        for filt in filters:
            name = filt.get("name") or f"Filter {filt.get('id')}"
            jql = filt.get("jql") or filt.get("query")
            owner = filt.get("owner", {}) or {}
            description_lines = [
                f"Imported from Jira filter '{name}'",
            ]
            if jql:
                description_lines.append(f"Original JQL:\n```\n{jql}\n```")
            if owner:
                owner_name = owner.get("displayName") or owner.get("name")
                if owner_name:
                    description_lines.append(f"Owner: {owner_name}")
            if filt.get("viewUrl"):
                description_lines.append(f"Source URL: {filt.get('viewUrl')}")
            query_payloads.append(
                {
                    "name": f"[Jira Filter] {name}",
                    "description": "\n\n".join(description_lines),
                    "project_id": None,
                    "is_public": True,
                    "options": {},
                },
            )

        # Determine fallback project for wiki content
        fallback_project_id = None
        preferred_project_key = (
            str(config.openproject_config.get("reporting_wiki_project")).strip()
            if config.openproject_config.get("reporting_wiki_project")
            else ""
        )

        reporting_identifier = preferred_project_key or REPORTING_PROJECT_IDENTIFIER_DEFAULT
        reporting_name = (
            str(config.openproject_config.get("reporting_wiki_project_name")).strip()
            if config.openproject_config.get("reporting_wiki_project_name")
            else REPORTING_PROJECT_NAME_DEFAULT
        )

        try:
            reporting_project_id = self.op_client.ensure_reporting_project(
                reporting_identifier,
                reporting_name,
            )
            fallback_project_id = reporting_project_id
        except Exception:  # noqa: BLE001
            self.logger.exception(
                "Failed to ensure dedicated reporting project '%s'; dashboards without explicit share will be skipped",
                reporting_identifier,
            )
            fallback_project_id = 0

        def _lookup_project_id(project_key: str | None) -> int:
            if not project_key:
                return 0
            entry = self.project_mapping.get(project_key)
            if isinstance(entry, dict) and entry.get("openproject_id"):
                try:
                    return int(entry["openproject_id"])
                except (TypeError, ValueError):
                    return 0
            return 0

        if preferred_project_key:
            fallback_project_id = _lookup_project_id(preferred_project_key)
            if not fallback_project_id:
                self.logger.warning(
                    "Configured reporting_wiki_project=%s but no mapping was found; falling back to first mapped project",
                    preferred_project_key,
                )

        if not fallback_project_id and self.project_mapping:
            first_entry = next(iter(self.project_mapping.values()))
            if isinstance(first_entry, dict):
                fallback_project_id = int(first_entry.get("openproject_id", 0) or 0)

        if not fallback_project_id:
            self.logger.warning(
                "Unable to resolve a reporting wiki project; dashboards without explicit project will be skipped",
            )

        for dashboard in dashboards:
            dash_name = dashboard.get("name") or f"Dashboard {dashboard.get('id')}"
            share_permissions = dashboard.get("sharePermissions") or []
            project_id = fallback_project_id

            for perm in share_permissions:
                proj = perm.get("project")
                if isinstance(proj, dict):
                    project_key = proj.get("key")
                    pid = _lookup_project_id(project_key) if project_key else 0
                    if pid:
                        project_id = pid
                        break

            if not project_id:
                skipped_dashboards.append(
                    {
                        "dashboard_id": dashboard.get("id"),
                        "reason": "no_project_mapping",
                    },
                )
                continue

            gadgets = dashboard.get("gadgets") or dashboard.get("widgets") or []
            gadget_lines = []
            for gadget in gadgets:
                title = gadget.get("title") or gadget.get("name") or "Gadget"
                gadget_lines.append(f"- {title}")

            content_lines = [
                f"## Imported Jira Dashboard: {dash_name}",
                "",
                f"Source URL: {dashboard.get('viewUrl') or dashboard.get('self')}",
            ]

            content_lines = explanation_lines + content_lines
            description = dashboard.get("description")
            if description:
                content_lines.extend(["", description])
            if gadget_lines:
                content_lines.extend(["", "### Gadgets", *gadget_lines])

            wiki_payloads.append(
                {
                    "project_id": project_id,
                    "title": f"Jira Dashboard - {dash_name}",
                    "content": "\n".join(content_lines),
                },
            )

        mapped = {
            "queries": query_payloads,
            "wiki_pages": wiki_payloads,
            "skipped_dashboards": skipped_dashboards,
        }

        return ComponentResult(
            success=True,
            data=mapped,
            total_count=len(query_payloads) + len(wiki_payloads),
            details={
                "filters": len(query_payloads),
                "dashboards": len(wiki_payloads),
                "skipped_dashboards": len(skipped_dashboards),
            },
        )

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        """Create OpenProject queries and wiki pages."""

        if not mapped.success or not isinstance(mapped.data, dict):
            return ComponentResult(
                success=False,
                message="Reporting mapping failed",
                error=mapped.message or "map phase returned no data",
            )

        queries: list[dict[str, Any]] = mapped.data.get("queries", [])
        wiki_pages: list[dict[str, Any]] = mapped.data.get("wiki_pages", [])

        updated = 0
        failed = 0

        for payload in queries:
            try:
                result = self.op_client.create_or_update_query(**payload)
                if result.get("success"):
                    updated += 1
                else:
                    failed += 1
                    self.logger.error(
                        "Query creation failed for '%s': %s",
                        payload.get("name"),
                        result.get("error") or result,
                    )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self.logger.exception(
                    "Failed to create query for filter '%s': %s",
                    payload.get("name"),
                    exc,
                )

        for payload in wiki_pages:
            try:
                result = self.op_client.create_or_update_wiki_page(**payload)
                if result.get("success"):
                    updated += 1
                else:
                    failed += 1
                    self.logger.error(
                        "Wiki page creation failed for '%s': %s",
                        payload.get("title"),
                        result.get("error") or result,
                    )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self.logger.exception(
                    "Failed to create wiki page '%s': %s",
                    payload.get("title"),
                    exc,
                )

        return ComponentResult(
            success=failed == 0,
            message="Reporting artefacts migrated",
            success_count=updated,
            failed_count=failed,
            details={
                "queries": len(queries),
                "wiki_pages": len(wiki_pages),
                "skipped_dashboards": len(mapped.data.get("skipped_dashboards", [])),
            },
        )

    def run(self) -> ComponentResult:
        """Execute the reporting migration pipeline."""

        self.logger.info("Starting reporting artefact migration")

        extracted = self._extract()
        if not extracted.success:
            self.logger.error(
                "Reporting extraction failed: %s",
                extracted.message or extracted.error,
            )
            return extracted

        mapped = self._map(extracted)
        if not mapped.success:
            self.logger.error(
                "Reporting mapping failed: %s",
                mapped.message or mapped.error,
            )
            return mapped

        result = self._load(mapped)
        if result.success:
            self.logger.info(
                "Reporting migration complete (artefacts=%s)",
                result.success_count,
            )
        else:
            self.logger.error(
                "Reporting migration encountered errors (failed=%s)",
                result.failed_count,
            )
        return result
