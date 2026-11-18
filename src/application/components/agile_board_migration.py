# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Migrate Jira board membership into private project queries and map owned sprints to versions."""

from __future__ import annotations

from typing import Any

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult
from src.utils.change_aware_runner import ChangeAwareRunner


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

        board_payloads: list[dict[str, Any]] = []
        sprint_payloads: list[dict[str, Any]] = []

        targets = self._board_work_package_mapping()
        filters = config.migration_config.get("jira_project_filter") or []
        selected = (
            set(self.project_mapping).intersection([filters] if isinstance(filters, str) else filters)
            if filters
            else set(self.project_mapping)
        )
        read_errors = []
        for board in self._list_boards(selected if filters else None):
            board_id = board.get("id")
            if board_id is None:
                continue

            try:
                configuration = self.jira_client.get_board_configuration(board_id)
                board_sprints = [] if board.get("type") == "kanban" else self.jira_client.get_board_sprints(board_id)
                project_key, candidates = self._board_project(board, board_id)
                scoped_keys = selected.intersection(candidates)
                if filters and not scoped_keys and project_key not in selected:
                    continue
                snapshots = {key: self._board_work_package_ids(board_id, key, targets) for key in sorted(scoped_keys)}
            except Exception as exc:
                self.logger.exception("Failed reading Jira board %s", board_id)
                read_errors.append({"board_id": board_id, "error": str(exc)})
                continue

            statuses = self._column_statuses(configuration)

            query = configuration.get("filter", {}) or {}
            filter_jql = query.get("query") or query.get("queryString") or ""

            for key, ids in snapshots.items() or ([] if filters else [(project_key, None)]):
                board_payloads.append(
                    {
                        "id": board_id,
                        "name": board.get("name"),
                        "type": board.get("type"),
                        "project_key": key if snapshots else None,
                        "candidate_projects": candidates or ([project_key] if project_key else []),
                        "statuses": statuses,
                        "filter_jql": filter_jql,
                        "work_package_ids": ids,
                    },
                )

            if project_key and project_key not in selected and filters:
                continue
            for sprint in self._owned_sprints(board_sprints, board_id):
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

        inventory = {
            "id": "jira-agile-inventory",
            "read_errors": read_errors,
            "boards": board_payloads,
            "sprints": sprint_payloads,
            "total_count": len(board_payloads),
        }
        return [self._with_targets(inventory)]

    @staticmethod
    def _owned_sprints(sprints: list[dict], board_id: int) -> list[dict]:
        """Jira also lists sprints reached through the board filter."""
        return [s for s in sprints if s.get("originBoardId") is None or str(s["originBoardId"]) == str(board_id)]

    def _with_targets(self, inventory: dict[str, Any]) -> dict[str, Any]:
        return {
            **inventory,
            "sprint_targets": {str(s["id"]): self.sprint_mapping.get(str(s["id"])) for s in inventory["sprints"]},
            "project_targets": {
                str(b["project_key"]): self.project_mapping.get(b["project_key"]) for b in inventory["boards"]
            },
        }

    def run_with_change_detection(self, entity_type: str | None = None) -> ComponentResult:
        # After writing versions, refresh their target IDs on the exact source
        # inventory passed to _map. A fresh Jira read can include unapplied edits.
        return ChangeAwareRunner(self).run(entity_type, snapshot_entities=lambda: [self._with_targets(self._inventory)])

    def _extract(self) -> ComponentResult:
        """Fetch boards, configurations, and sprints from Jira."""
        try:
            data_list = self._get_current_entities_for_type("agile_boards")
            data = data_list[0]
            self._inventory = data
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

            ids = board.get("work_package_ids")
            if ids is None:
                skipped_boards.append({"board_id": board["id"], "reason": "missing_board_membership"})
                continue
            query_payloads.append(
                {
                    "name": f"[Jira Board {board['id']}] {board.get('name')}",
                    "project_id": op_project_id,
                    "is_public": False,
                    "options": {
                        "board_work_package_ids": ids,
                        "jira_board_id": board["id"],
                        "include_subprojects": False,
                    },
                },
            )

        self._map_sprints(sprints, skipped_sprints, version_payloads)

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

        skipped = mapped.data.get("skipped_boards", []) + mapped.data.get("skipped_sprints", [])
        read_errors = mapped.data.get("read_errors", [])
        errors = len(skipped) + len(read_errors)
        sprint_mapping_updates: dict[str, Any] = {}

        created_queries, query_errors = self._load_board_queries(queries)
        errors += query_errors

        created_versions, version_errors = self._load_sprint_versions(versions, sprint_mapping_updates)
        errors += version_errors

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

    def _board_work_package_mapping(self) -> dict[str, int]:
        work_packages = self.mappings.get_mapping("work_package") or {}
        targets = {}
        for outer_key, value in work_packages.items():
            key = self._inner_jira_key(outer_key, value)
            target = self._resolve_wp_id(work_packages, outer_key)
            if key and target:
                if key in targets and targets[key] != target:
                    msg = "Ambiguous Jira work package mapping"
                    raise ValueError(msg)
                targets[key] = target
        return targets

    def _board_work_package_ids(self, board_id: int, key: str, targets: dict[str, int]) -> list[int]:
        issue_keys = self.jira_client.get_board_issue_keys(board_id, key)
        ids = [targets[issue] for issue in issue_keys]
        if len(set(ids)) != len(ids):
            msg = "Distinct board issues map to the same work package"
            raise ValueError(msg)
        return sorted(ids)

    def _list_boards(self, selected: set[str] | None) -> list[dict[str, Any]]:
        if selected is None:
            return self.jira_client.get_boards()
        by_id = {b["id"]: b for key in sorted(selected) for b in self.jira_client.get_boards_for_project(key)}
        return [by_id[i] for i in sorted(by_id)]

    def _board_project(self, board: dict, board_id: int) -> tuple[str | None, list[str]]:
        """Use the filter's projects for cards, and the board's home for its sprints."""
        projects = self.jira_client.get_board_projects(board_id)
        candidates = sorted({p["key"] for p in projects if p.get("key")})
        location = board.get("location") or {}
        home = location.get("projectKey") or board.get("locationProjectKey")
        return home or (candidates[0] if len(candidates) == 1 else None), candidates

    @staticmethod
    def _column_statuses(configuration: dict) -> list[str]:
        """Collect the Jira status IDs in board column order."""
        columns = configuration.get("columnConfig", {}).get("columns", [])
        statuses: list[str] = []
        for column in columns:
            column_statuses = column.get("statuses", [])
            if not isinstance(column_statuses, list):
                continue
            for status in column_statuses:
                if isinstance(status, dict):
                    status_id = status.get("id") or status.get("name")
                else:
                    status_id = status
                if status_id:
                    statuses.append(str(status_id))
        return statuses

    def _map_sprints(self, sprints: list[dict], skipped_sprints: list[dict], version_payloads: list[dict]) -> None:
        """Assign Jira sprints to project-scoped OpenProject versions."""
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

    def _load_board_queries(self, queries: list[dict]) -> tuple[int, int]:
        """Create the saved queries and count successful creations and errors."""
        created_queries = errors = 0
        for payload in queries:
            try:
                result = self.op_client.create_or_update_query(**payload)
                if result.get("success"):
                    if result.get("created"):
                        created_queries += 1
                else:
                    self.logger.error("Could not verify board query: %s", result.get("error"))
                    errors += 1
            except Exception as exc:
                errors += 1
                self.logger.exception("Failed to create query for board %s: %s", payload.get("name"), exc)
        return created_queries, errors

    def _load_sprint_versions(self, versions: list[dict], sprint_mapping_updates: dict) -> tuple[int, int]:
        """Record OpenProject IDs for successfully migrated sprints."""
        created_versions = errors = 0
        for payload in versions:
            jira_sprint_id = payload.get("jira_sprint_id")
            try:
                result = self.op_client.ensure_project_version(
                    **{k: v for k, v in payload.items() if k != "jira_sprint_id"}
                )
                if not result.get("success"):
                    errors += 1
                    continue
                if result.get("created"):
                    created_versions += 1
                if jira_sprint_id:
                    entry = {
                        "openproject_id": result.get("id"),
                        "project_id": payload["project_id"],
                        "name": payload.get("name"),
                    }
                    sprint_mapping_updates[str(jira_sprint_id)] = entry
                    if sprint_name := payload.get("name"):
                        sprint_mapping_updates[sprint_name] = entry
            except Exception as exc:
                errors += 1
                self.logger.exception(
                    "Failed to create version for sprint %s: %s",
                    jira_sprint_id,
                    exc,
                )
        return created_versions, errors
