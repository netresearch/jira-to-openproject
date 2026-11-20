# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Migrate Jira filters and dashboards into OpenProject queries and wiki summaries.

The authenticated source catalogs identify candidate records. Only report content
returned to an anonymous reader may appear in a shared target query or wiki.
"""

from __future__ import annotations

import re
from typing import Any

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.jira.jira_public_reporting import anonymous_verify, public_report
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult

REPORTING_PROJECT_IDENTIFIER_DEFAULT = "j2o-reporting"
REPORTING_PROJECT_NAME_DEFAULT = "Jira Dashboards"


@register_entity_types("reporting")
class ReportingMigration(BaseMigration):
    """Create OpenProject artefacts representing Jira saved filters and dashboards."""

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)
        self.project_mapping = config.mappings.get_mapping("project") or {}

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities from Jira for a specific type.

        This method enables idempotent workflow caching by providing a standard
        interface for entity retrieval. Called by run_with_change_detection() to fetch data
        with automatic thread-safe caching.

        Args:
            entity_type: The type of entities to retrieve (e.g., "reporting")

        Returns:
            List containing aggregated reporting entities (filters + dashboards)

        Raises:
            ValueError: If entity_type is not supported by this migration

        """
        # Check if this is the entity type we handle
        if entity_type != "reporting":
            msg = f"ReportingMigration does not support entity type: {entity_type}. Supported types: ['reporting']"
            raise ValueError(msg)

        records = {}
        for kind, listing in (("filter", self.jira_client.get_filters), ("dashboard", self.jira_client.get_dashboards)):
            public, held = self._public_records(kind, listing())
            records[kind + "s"] = public + [{"id": identifier} for identifier in held]
        total = len(records["filters"]) + len(records["dashboards"])
        return [{"id": "jira-reporting-inventory", **records, "total_count": total}]

    def _extract(self) -> ComponentResult:
        """Fetch Jira filters and dashboards."""
        try:
            data_list = self._get_current_entities_for_type("reporting")
            data = data_list[0] if data_list else {}
            filters = data.get("filters", [])
            dashboards = data.get("dashboards", [])
            return ComponentResult(
                success=True,
                data={
                    "filters": filters,
                    "dashboards": dashboards,
                },
                total_count=len(filters) + len(dashboards),
            )
        except Exception as exc:
            return ComponentResult(
                success=False,
                message=f"Failed to fetch Jira filters: {exc}",
                error=str(exc),
            )

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        """Convert filters and dashboard metadata to OpenProject payloads."""
        if not extracted.success or not isinstance(extracted.data, dict):
            return ComponentResult(
                success=False,
                message="Reporting extraction failed",
                error=extracted.message or "extract phase returned no data",
            )

        filters, held_filters = self._public_records("filter", extracted.data.get("filters", []))
        dashboards, held_dashboards = self._public_records("dashboard", extracted.data.get("dashboards", []))
        query_payloads, filter_pages = self._filter_payloads(filters)
        wiki_payloads, skipped_dashboards = self._dashboard_payloads(dashboards, held_dashboards)
        wiki_payloads = [*wiki_payloads, *filter_pages]
        mapped = {
            "queries": query_payloads,
            "wiki_pages": wiki_payloads,
            "skipped_filters": held_filters,
            "skipped_dashboards": skipped_dashboards,
        }

        return ComponentResult(
            success=True,
            data=mapped,
            total_count=len(query_payloads) + len(wiki_payloads),
            details={
                "filters": len(query_payloads),
                "skipped_filters": len(held_filters),
                "dashboards": len(wiki_payloads) - len(filter_pages),
                "filter_pages": len(filter_pages),
                "skipped_dashboards": len(skipped_dashboards),
            },
        )

    def _filter_payloads(self, filters):
        queries, pages = [], []
        fallback = None
        for row in filters:
            jql = row.get("jql") or row.get("query") or ""
            project_key = self._jql_project(jql)
            project_id = self._mapped_project(project_key)
            if project_id:
                queries.append(
                    {
                        "name": f"Jira Filter {row['id']}",
                        "project_id": project_id,
                        "is_public": True,
                        "options": {"include_subprojects": False, "filters": []},
                    }
                )
            elif fallback is None:
                fallback = self._reporting_project()
            pages.append(
                {
                    "title": f"Jira Filter {row['id']}",
                    "project_id": project_id or fallback,
                    "content": self._filter_text(row),
                }
            )
        return queries, pages

    @staticmethod
    def _jql_project(jql: str) -> str | None:
        match = re.fullmatch(
            r"""\s*project\s*=\s*("[A-Z][A-Z0-9_]*"|'[A-Z][A-Z0-9_]*'|[A-Z][A-Z0-9_]*)\s*""",
            jql,
            re.IGNORECASE | re.ASCII,
        )
        return match[1].strip("\"'").upper() if match else None

    def _dashboard_payloads(self, dashboards, held):
        pages = []
        skipped = [{"dashboard_id": identifier, "reason": "not_public"} for identifier in held]
        fallback = None
        for dashboard in dashboards:
            project_id = self._shared_project(dashboard)
            if not project_id:
                if fallback is None:
                    fallback = self._reporting_project()
                project_id = fallback
            if project_id:
                pages.append(self._wiki_payload(dashboard, project_id))
            else:
                skipped.append({"dashboard_id": dashboard["id"], "reason": "no_project_mapping"})
        return pages, skipped

    def _public_records(self, kind, catalog):
        public = []
        held = []
        for row in catalog:
            identifier = row.get("id")
            base_url = self.jira_client.base_url
            record = public_report(base_url, kind, identifier, verify=anonymous_verify(self.jira_client, base_url))
            if record is None:
                held.append(identifier)
            else:
                public.append(record)
        return public, held

    @staticmethod
    def _filter_text(row: dict[str, Any]) -> str:
        name = row.get("name") or f"Filter {row['id']}"
        lines = [f"Imported from Jira filter '{name}'"]
        jql = row.get("jql") or row.get("query")
        if jql:
            lines.append(f"Original JQL:\n```\n{jql}\n```")
        owner = row.get("owner") or {}
        owner_name = owner.get("displayName") or owner.get("name")
        if owner_name:
            lines.append(f"Owner: {owner_name}")
        if row.get("viewUrl"):
            lines.append(f"Source URL: {row['viewUrl']}")
        return "\n\n".join(lines)

    def _shared_project(self, row):
        for permission in row.get("sharePermissions") or []:
            project = permission.get("project") or {}
            target = self._mapped_project(project.get("key"))
            if target:
                return target
        return 0

    def _mapped_project(self, key):
        row = self.project_mapping.get(key) or {}
        try:
            return int(row.get("openproject_id") or 0)
        except TypeError, ValueError:
            return 0

    def _reporting_project(self):
        preferred = str(config.openproject_config.get("reporting_wiki_project") or "").strip()
        mapped = self._mapped_project(preferred)
        if mapped:
            return mapped
        name = config.openproject_config.get("reporting_wiki_project_name") or REPORTING_PROJECT_NAME_DEFAULT
        return self.op_client.ensure_reporting_project(preferred or REPORTING_PROJECT_IDENTIFIER_DEFAULT, name)

    @staticmethod
    def _wiki_payload(dashboard: dict[str, Any], project_id: int) -> dict[str, Any]:
        name = dashboard.get("name") or f"Dashboard {dashboard['id']}"
        lines = [
            f"## Imported Jira Dashboard: {name}",
            "",
            f"Source URL: {dashboard.get('viewUrl') or dashboard.get('self')}",
        ]
        if dashboard.get("description"):
            lines.extend(["", dashboard["description"]])
        gadgets = dashboard.get("gadgets") or dashboard.get("widgets") or []
        if gadgets:
            lines.extend(["", "### Gadgets"])
            lines.extend(f"- {item.get('title') or item.get('name') or 'Gadget'}" for item in gadgets)
        return {
            "project_id": project_id,
            "title": f"Jira Dashboard {dashboard['id']}",
            "content": "\n".join(lines),
        }

    def _write_reports(self, reports, write):
        updated = 0
        for payload in reports:
            try:
                result = write(**payload)
                if result.get("success"):
                    updated += 1
                else:
                    self.logger.error("OpenProject rejected a reporting record")
            except Exception:
                self.logger.exception("Failed to save a reporting record")
        return updated

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

        updated = self._write_reports(queries, self.op_client.create_or_update_query)
        updated += self._write_reports(wiki_pages, self.op_client.create_or_update_wiki_page)
        failed = len(queries) + len(wiki_pages) - updated

        return ComponentResult(
            success=failed == 0,
            message="Reporting artefacts migrated",
            success_count=updated,
            failed_count=failed,
            details={
                "queries": len(queries),
                "skipped_filters": mapped.data.get("skipped_filters", []),
                "held_dashboard_ids": [r["dashboard_id"] for r in mapped.data.get("skipped_dashboards", [])],
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

        try:
            mapped = self._map(extracted)
        except Exception:
            self.logger.exception("Could not verify the public reporting inventory")
            return ComponentResult(success=False, message="Could not verify the public reporting inventory")
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
