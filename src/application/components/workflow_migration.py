# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Workflow migration: aligns Jira workflows with OpenProject transitions.

Phase 7d note
-------------
This migration is intentionally left unchanged in the typed-pipeline
sweep. It does not consume the ``work_package`` mapping (so there is no
``wp_map`` polymorphic ladder to retire here), and the structures it
*does* touch — ``status_mapping`` and ``issue_type_mapping`` — use their
own dict-of-dict shapes that are unrelated to ADR-002 phase 3/7. Phase 7
targets the ``wp_map`` ladder specifically; retyping the workflow status
maps is deferred.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult


@dataclass
class _WorkflowMapping:
    issue_type: str
    workflow: str
    type_id: int
    statuses_by_id: dict
    statuses_by_name: dict

    def _status(self, jira_id: str, name: str, skipped: list[dict]) -> int:
        entry = self.statuses_by_id.get(jira_id) or self.statuses_by_name.get(name.lower())
        if entry:
            return int(entry.get("openproject_id", 0) or 0)
        skipped.append(
            {
                "reason": "missing_status_mapping",
                "issue_type": self.issue_type,
                "workflow": self.workflow,
                "status_id": jira_id,
            }
        )
        return 0

    def rows(self, transition: dict, skipped: list[dict]) -> Iterator[tuple[tuple[int, int, int], dict]]:
        """Map each incoming status of one transition to its target status."""
        target = transition.get("to") or {}
        to_id = self._status(str(target.get("id") or "").strip(), str(target.get("name", "")), skipped)
        if to_id <= 0:
            return
        sources = transition.get("from")
        if isinstance(sources, str):
            sources = [sources]
        if not isinstance(sources, list):
            return
        for source in sources:
            from_id = self._status(str(source), str(transition.get("name", "")), skipped)
            if from_id > 0:
                yield (
                    (self.type_id, from_id, to_id),
                    {
                        "type_id": self.type_id,
                        "from_status_id": from_id,
                        "to_status_id": to_id,
                        "jira_issue_type": self.issue_type,
                        "jira_workflow": self.workflow,
                    },
                )


@register_entity_types("workflows")
class WorkflowMigration(BaseMigration):
    """Synchronise Jira workflow transitions with OpenProject workflow records."""

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)

    # ------------------------------------------------------------------ #
    # BaseMigration overrides                                            #
    # ------------------------------------------------------------------ #

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities from Jira for a specific type.

        This method enables idempotent workflow caching by providing a standard
        interface for entity retrieval. Called by run_with_change_detection() to fetch data
        with automatic thread-safe caching.

        Args:
            entity_type: The type of entities to retrieve (e.g., "workflows")

        Returns:
            List containing aggregated workflow metadata (schemes, transitions, statuses, roles)

        Raises:
            ValueError: If entity_type is not supported by this migration

        """
        # Check if this is the entity type we handle
        if entity_type != "workflows":
            msg = f"WorkflowMigration does not support entity type: {entity_type}. Supported types: ['workflows']"
            raise ValueError(msg)

        # Fetch issue types (API call 1)
        try:
            issue_types = self.jira_client.get_issue_types()
        except Exception as exc:
            self.logger.exception("Failed to extract issue types: %s", exc)
            return []

        # Fetch workflow schemes (API call 2)
        try:
            schemes = self.jira_client.get_workflow_schemes()
        except Exception as exc:
            self.logger.exception("Failed to extract workflow schemes: %s", exc)
            return []

        # Fetch OpenProject roles (API call 3)
        try:
            roles = self.op_client.get_roles()
        except Exception as exc:
            self.logger.exception("Failed to extract OpenProject roles: %s", exc)
            roles = []

        issue_type_to_workflow, workflow_names = self._workflows_by_type(issue_types, schemes)

        # Fetch transitions and statuses for each workflow (API calls 4 & 5 per workflow)
        workflow_transitions, workflow_statuses = self._workflow_definitions(workflow_names)

        # Return aggregated data structure
        return [
            {
                "issue_type_to_workflow": issue_type_to_workflow,
                "workflow_transitions": workflow_transitions,
                "workflow_statuses": workflow_statuses,
                "roles": roles,
            },
        ]

    def _extract(self) -> ComponentResult:
        """Gather workflow schemes, transitions, and OpenProject roles."""
        try:
            data_list = self._get_current_entities_for_type("workflows")
            data = data_list[0] if data_list else {}
            return ComponentResult(
                success=True,
                data=data,
                total_count=len(data.get("workflow_transitions", {})),
            )
        except Exception as exc:
            return ComponentResult(
                success=False,
                message=f"Failed to extract workflow metadata: {exc}",
                error=str(exc),
            )

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        """Translate Jira workflows into OpenProject workflow transition payloads."""
        if not extracted.success or not isinstance(extracted.data, dict):
            return ComponentResult(
                success=False,
                message="Workflow extraction failed",
                error=extracted.message or "extract phase returned no data",
            )

        issue_type_to_workflow: dict[str, str] = extracted.data.get("issue_type_to_workflow", {})
        workflow_transitions: dict[str, list[dict[str, Any]]] = extracted.data.get(
            "workflow_transitions",
            {},
        )
        roles: list[dict[str, Any]] = extracted.data.get("roles", [])

        status_mapping = self.mappings.get_mapping("status") or {}
        issue_type_mapping = self.mappings.get_mapping("issue_type") or {}

        status_by_id = {
            str(jira_id): entry
            for jira_id, entry in status_mapping.items()
            if isinstance(jira_id, str) and isinstance(entry, dict)
        }
        status_by_name = {
            str(entry.get("jira_name", "")).lower(): entry
            for entry in status_mapping.values()
            if isinstance(entry, dict) and entry.get("jira_name")
        }

        role_ids = self._workflow_roles(roles)

        dedup_transitions: dict[tuple[int, int, int], dict[str, Any]] = {}
        skipped: list[dict[str, Any]] = []

        for issue_type_name, workflow_name in issue_type_to_workflow.items():
            mapping_entry = issue_type_mapping.get(issue_type_name)
            if not isinstance(mapping_entry, dict):
                skipped.append(
                    {
                        "reason": "missing_issue_type_mapping",
                        "issue_type": issue_type_name,
                    },
                )
                continue

            type_id = int(mapping_entry.get("openproject_id", 0) or 0)
            if type_id <= 0:
                skipped.append(
                    {
                        "reason": "invalid_openproject_type",
                        "issue_type": issue_type_name,
                    },
                )
                continue

            mapping = _WorkflowMapping(issue_type_name, workflow_name, type_id, status_by_id, status_by_name)
            for transition in workflow_transitions.get(workflow_name, []):
                for key, row in mapping.rows(transition, skipped):
                    dedup_transitions.setdefault(key, row)

        mapped = {
            "transitions": list(dedup_transitions.values()),
            "role_ids": role_ids,
            "skipped": skipped,
        }

        return ComponentResult(
            success=True,
            data=mapped,
            total_count=len(dedup_transitions),
            details={
                "transitions_planned": len(dedup_transitions),
                "skipped": len(skipped),
            },
        )

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        """Create workflow entries in OpenProject."""
        if not mapped.success or not isinstance(mapped.data, dict):
            return ComponentResult(
                success=False,
                message="Workflow mapping failed",
                error=mapped.message or "map phase returned no data",
            )

        transitions: list[dict[str, Any]] = mapped.data.get("transitions", [])
        role_ids: list[int] = mapped.data.get("role_ids", [])

        if not transitions:
            return ComponentResult(
                success=True,
                message="No workflow transitions to synchronise",
                details={"created": 0, "existing": 0},
            )

        summary = self.op_client.sync_workflow_transitions(transitions, role_ids)
        created = int(summary.get("created", 0))
        existing = int(summary.get("existing", 0))
        errors = int(summary.get("errors", 0))

        success = errors == 0
        return ComponentResult(
            success=success,
            message="Workflow transitions synchronised",
            success_count=created,
            failed_count=errors,
            details={
                "created": created,
                "existing": existing,
                "errors": errors,
                "skipped": len(mapped.data.get("skipped", [])),
            },
        )

    def run(self) -> ComponentResult:
        """Execute the workflow migration pipeline (extract → map → load)."""
        self.logger.info("Starting workflow transition migration")

        extracted = self._extract()
        if not extracted.success:
            self.logger.error(
                "Workflow extraction failed: %s",
                extracted.message or extracted.error,
            )
            return extracted

        mapped = self._map(extracted)
        if not mapped.success:
            self.logger.error(
                "Workflow mapping failed: %s",
                mapped.message or mapped.error,
            )
            return mapped

        result = self._load(mapped)
        if result.success:
            self.logger.info(
                "Workflow migration completed (created=%s, existing=%s)",
                result.details.get("created", 0),
                result.details.get("existing", 0),
            )
        else:
            self.logger.error(
                "Workflow migration failed: created=%s existing=%s errors=%s",
                result.details.get("created", 0),
                result.details.get("existing", 0),
                result.details.get("errors", 0),
            )
        return result

    @staticmethod
    def _workflows_by_type(issue_types: list, schemes: list) -> tuple[dict[str, str], set[str]]:
        """Resolve scheme mappings and the default workflow for known issue types."""
        issue_type_by_id = {
            str(item.get("id")): item.get("name") for item in issue_types if item.get("id") and item.get("name")
        }

        issue_type_to_workflow: dict[str, str] = {}
        workflow_names: set[str] = set()
        for scheme in schemes:
            for name, workflow in WorkflowMigration._explicit_workflows(scheme, issue_type_by_id):
                issue_type_to_workflow[name] = workflow
                workflow_names.add(workflow)
            default = scheme.get("defaultWorkflow")
            if isinstance(default, str) and default and not issue_type_to_workflow:
                issue_type_to_workflow.update(dict.fromkeys(issue_type_by_id.values(), default))
                if issue_type_to_workflow:
                    workflow_names.add(default)
        return issue_type_to_workflow, workflow_names

    def _workflow_definitions(self, workflow_names: set[str]) -> tuple[dict, dict]:
        """Read transitions and statuses independently for each source workflow."""
        workflow_transitions: dict[str, list[dict[str, Any]]] = {}
        workflow_statuses: dict[str, list[dict[str, Any]]] = {}
        for workflow_name in workflow_names:
            try:
                transitions = self.jira_client.get_workflow_transitions(workflow_name)
            except Exception:
                transitions = []
            workflow_transitions[workflow_name] = transitions

            try:
                statuses = self.jira_client.get_workflow_statuses(workflow_name)
            except Exception:
                statuses = []
            workflow_statuses[workflow_name] = statuses if isinstance(statuses, list) else []
        return workflow_transitions, workflow_statuses

    @staticmethod
    def _workflow_roles(roles: list[dict]) -> list[int]:
        """Select configured target roles, with the existing all-role fallback."""
        desired_role_names = config.migration_config.get(
            "workflow_roles",
            ["Project admin", "Project member"],
        )
        role_ids = [
            int(role["id"]) for role in roles if int(role.get("id", 0)) > 0 and role.get("name") in desired_role_names
        ]
        if not role_ids:
            role_ids = [int(role["id"]) for role in roles if int(role.get("id", 0)) > 0]
        return role_ids

    @staticmethod
    def _explicit_workflows(scheme: dict, issue_type_by_id: dict) -> Iterator[tuple[str, str]]:
        mappings = scheme.get("issueTypeMappings") or scheme.get("mappings") or {}
        if isinstance(mappings, dict):
            for type_id, workflow in mappings.items():
                name = issue_type_by_id.get(str(type_id))
                if name and isinstance(workflow, str) and workflow:
                    yield name, workflow
