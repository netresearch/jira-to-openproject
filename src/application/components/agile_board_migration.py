# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Migrate Jira Software boards and sprints into OpenProject equivalents.

Phase 7d note
-------------
This migration is intentionally left structurally unchanged in the
typed-pipeline sweep. It does not consume the ``work_package`` mapping
(no ``wp_map`` polymorphic ladder to retire here), and the Jira-side
input is uniform REST dicts whose keys are well-defined — there is no
polymorphic ladder of the kind phase 7 targets. The ``project`` mapping
``isinstance(..., dict)`` check is for an unrelated polymorphic shape
and is left as-is. Modelling boards/sprints as Pydantic types would be
mostly cosmetic at this site, so it is deferred until a downstream
caller benefits.
"""

from __future__ import annotations

from typing import Any

from src.application.components.base_migration import BaseMigration, register_entity_types
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult


@register_entity_types("agile_boards", "sprints")
class AgileBoardMigration(BaseMigration):
    """Create OpenProject queries for Jira boards and map sprints to versions."""

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)
        self.project_mapping = self.mappings.get_mapping("project") or {}
        self.sprint_mapping = self.mappings.get_mapping("sprint") or {}

    # ------------------------------------------------------------------ #
    # BaseMigration overrides                                            #
    # ------------------------------------------------------------------ #

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities from Jira for a specific type.

        This method enables idempotent workflow caching by providing a standard
        interface for entity retrieval. Called by run_with_change_detection() to fetch data
        with automatic thread-safe caching.

        Args:
            entity_type: The type of entities to retrieve (e.g., "agile_boards", "sprints")

        Returns:
            List containing aggregated board and sprint data

        Raises:
            ValueError: If entity_type is not supported by this migration

        """
        # Check if this is one of the entity types we handle
        if entity_type not in ("agile_boards", "sprints"):
            msg = (
                f"AgileBoardMigration does not support entity type: {entity_type}. "
                f"Supported types: ['agile_boards', 'sprints']"
            )
            raise ValueError(msg)

        boards = self.jira_client.get_boards()
        board_payloads: list[dict[str, Any]] = []
        sprint_payloads: list[dict[str, Any]] = []

        read_errors = []
        for board in boards:
            board_id = board.get("id")
            if board_id is None:
                continue

            candidates: list[str] = []
            try:
                configuration = self.jira_client.get_board_configuration(board_id)
                board_sprints = [] if board.get("type") == "kanban" else self.jira_client.get_board_sprints(board_id)
                location = board.get("location") or {}
                project_key = location.get("projectKey") or board.get("locationProjectKey")
                if not project_key:
                    # A cross-project filter does not identify a single owning project.
                    projects = self.jira_client.get_board_projects(board_id)
                    candidates = sorted({p["key"] for p in projects if p.get("key")})
                    if len(candidates) == 1:
                        project_key = candidates[0]
            except Exception as exc:
                self.logger.exception("Failed reading Jira board %s", board_id)
                read_errors.append({"board_id": board_id, "error": str(exc)})
                continue

            columns = configuration.get("columnConfig", {}).get("columns", [])
            statuses: list[str] = []
            for column in columns:
                column_statuses = column.get("statuses", [])
                if isinstance(column_statuses, list):
                    for status in column_statuses:
                        if isinstance(status, dict):
                            status_id = status.get("id") or status.get("name")
                        else:
                            status_id = status
                        if status_id:
                            statuses.append(str(status_id))

            query = configuration.get("filter", {}) or {}
            filter_jql = query.get("query") or query.get("queryString") or ""

            board_payloads.append(
                {
                    "id": board_id,
                    "name": board.get("name"),
                    "type": board.get("type"),
                    "project_key": project_key,
                    "candidate_projects": candidates,
                    "statuses": statuses,
                    "filter_jql": filter_jql,
                },
            )

            for sprint in board_sprints:
                sprint_payloads.append(
                    {
                        "board_id": board_id,
                        "project_key": project_key,
                        "id": sprint.get("id"),
                        "name": sprint.get("name"),
                        "goal": sprint.get("goal"),
                        "state": sprint.get("state"),
                        "startDate": sprint.get("startDate"),
                        "endDate": sprint.get("endDate"),
                    },
                )

        # Return aggregated data structure
        return [
            {
                "id": "jira-agile-inventory",
                "read_errors": read_errors,
                "sprint_targets": {str(s["id"]): self.sprint_mapping.get(str(s["id"])) for s in sprint_payloads},
                "project_targets": {
                    str(b["project_key"]): self.project_mapping.get(b["project_key"]) for b in board_payloads
                },
                "boards": board_payloads,
                "sprints": sprint_payloads,
                "total_count": len(board_payloads),
            },
        ]

    def _extract(self) -> ComponentResult:
        """Fetch boards, configurations, and sprints from Jira."""
        try:
            data_list = self._get_current_entities_for_type("agile_boards")
            data = data_list[0] if data_list else {}
            return ComponentResult(
                success=True,
                data={
                    "boards": data.get("boards", []),
                    "sprints": data.get("sprints", []),
                    "read_errors": data.get("read_errors", []),
                },
                total_count=data.get("total_count", 0),
            )
        except Exception as exc:
            return ComponentResult(
                success=False,
                message=f"Failed to fetch Jira boards: {exc}",
                error=str(exc),
            )

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        """Convert board and sprint data into OpenProject payloads."""
        if not extracted.success or not isinstance(extracted.data, dict):
            return ComponentResult(
                success=False,
                message="Agile board extraction failed",
                error=extracted.message or "extract phase returned no data",
            )

        boards: list[dict[str, Any]] = extracted.data.get("boards", [])
        sprints: list[dict[str, Any]] = extracted.data.get("sprints", [])

        query_payloads: list[dict[str, Any]] = []
        version_payloads: list[dict[str, Any]] = []
        skipped_boards: list[dict[str, Any]] = []
        skipped_sprints: list[dict[str, Any]] = []

        for board in boards:
            project_key = board.get("project_key")
            project_entry = self.project_mapping.get(project_key) if project_key else None
            op_project_id = int(project_entry.get("openproject_id", 0)) if isinstance(project_entry, dict) else 0

            if op_project_id <= 0:
                skipped_boards.append(
                    {
                        "reason": "missing_project_mapping",
                        "board_id": board.get("id"),
                        "candidate_projects": board.get("candidate_projects", []),
                        "project_key": project_key,
                    },
                )
                continue

            description_parts = [
                f"Imported from Jira board '{board.get('name')}' ({board.get('type')})",
            ]
            if board.get("filter_jql"):
                description_parts.append(f"Original JQL: {board['filter_jql']}")
            if board.get("statuses"):
                description_parts.append(
                    "Columns / statuses: " + ", ".join(board["statuses"]),
                )

            query_payloads.append(
                {
                    "name": f"[Board] {board.get('name')}",
                    "description": "\n".join(description_parts),
                    "project_id": op_project_id,
                    "is_public": None,  # private on creation; respect subsequent operator sharing decisions
                    "options": {
                        "filters": [],
                        "columns": [],
                    },
                },
            )

        for sprint in sprints:
            project_key = sprint.get("project_key")
            project_entry = self.project_mapping.get(project_key) if project_key else None
            op_project_id = int(project_entry.get("openproject_id", 0)) if isinstance(project_entry, dict) else 0
            if op_project_id <= 0:
                skipped_sprints.append(
                    {
                        "reason": "missing_project_mapping",
                        "sprint_id": sprint.get("id"),
                        "project_key": project_key,
                    },
                )
                continue

            state = str(sprint.get("state", "")).lower()
            status = "closed" if state == "closed" else "open"

            version_payloads.append(
                {
                    "project_id": op_project_id,
                    "jira_sprint_id": sprint.get("id"),
                    "name": sprint.get("name") or f"Sprint {sprint.get('id')}",
                    "description": sprint.get("goal"),
                    "start_date": sprint.get("startDate"),
                    "due_date": sprint.get("endDate"),
                    "status": status,
                },
            )

        mapped = {
            "read_errors": extracted.data.get("read_errors", []),
            "queries": query_payloads,
            "versions": version_payloads,
            "skipped_boards": skipped_boards,
            "skipped_sprints": skipped_sprints,
        }

        return ComponentResult(
            success=True,
            data=mapped,
            total_count=len(query_payloads) + len(version_payloads),
            details={
                "queries": len(query_payloads),
                "versions": len(version_payloads),
                "skipped_boards": len(skipped_boards),
                "skipped_sprints": len(skipped_sprints),
            },
        )

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        """Create queries and versions in OpenProject and persist sprint mapping."""
        if not mapped.success or not isinstance(mapped.data, dict):
            return ComponentResult(
                success=False,
                message="Agile board mapping failed",
                error=mapped.message or "map phase returned no data",
            )

        queries: list[dict[str, Any]] = mapped.data.get("queries", [])
        versions: list[dict[str, Any]] = mapped.data.get("versions", [])

        created_queries = 0
        created_versions = 0
        skipped = mapped.data.get("skipped_boards", []) + mapped.data.get("skipped_sprints", [])
        read_errors = mapped.data.get("read_errors", [])
        errors = len(skipped) + len(read_errors)
        sprint_mapping_updates: dict[str, Any] = {}

        for payload in queries:
            try:
                result = self.op_client.create_or_update_query(**payload)
                if result.get("success"):
                    if result.get("created"):
                        created_queries += 1
                else:
                    errors += 1
            except Exception as exc:
                errors += 1
                self.logger.exception("Failed to create query for board %s: %s", payload.get("name"), exc)

        for payload in versions:
            jira_sprint_id = payload.get("jira_sprint_id")
            try:
                result = self.op_client.ensure_project_version(
                    **{k: v for k, v in payload.items() if k != "jira_sprint_id"}
                )
                if result.get("success"):
                    if result.get("created"):
                        created_versions += 1
                    if jira_sprint_id:
                        entry = {
                            "openproject_id": result.get("id"),
                            "project_id": payload["project_id"],
                            "name": payload.get("name"),
                        }
                        sprint_mapping_updates[str(jira_sprint_id)] = entry
                        sprint_name = payload.get("name")
                        if sprint_name:
                            sprint_mapping_updates[sprint_name] = entry
                else:
                    errors += 1
            except Exception as exc:
                errors += 1
                self.logger.exception(
                    "Failed to create version for sprint %s: %s",
                    jira_sprint_id,
                    exc,
                )

        if sprint_mapping_updates:
            updated_mapping = dict(self.sprint_mapping)
            updated_mapping.update(sprint_mapping_updates)
            self.mappings.set_mapping("sprint", updated_mapping)
            self.sprint_mapping = updated_mapping

        return ComponentResult(
            success=errors == 0,
            message="Agile boards and sprints migrated",
            success_count=created_queries + created_versions,
            failed_count=errors,
            details={
                "queries_created": created_queries,
                "versions_created": created_versions,
                "errors": errors,
                "unresolved_projects": skipped,
                "board_read_errors": read_errors,
                "skipped_boards": len(mapped.data.get("skipped_boards", [])),
                "skipped_sprints": len(mapped.data.get("skipped_sprints", [])),
            },
        )

    def run(self) -> ComponentResult:
        """Execute the agile board migration pipeline."""
        self.logger.info("Starting agile board and sprint migration")

        extracted = self._extract()
        if not extracted.success:
            self.logger.error(
                "Agile board extraction failed: %s",
                extracted.message or extracted.error,
            )
            return extracted

        mapped = self._map(extracted)
        if not mapped.success:
            self.logger.error(
                "Agile board mapping failed: %s",
                mapped.message or mapped.error,
            )
            return mapped

        result = self._load(mapped)
        if result.success:
            self.logger.info(
                "Agile migration complete (queries=%s, versions=%s)",
                result.details.get("queries_created", 0),
                result.details.get("versions_created", 0),
            )
        else:
            self.logger.error(
                "Agile migration errors encountered: %s",
                result.details.get("errors", 0),
            )
        return result
