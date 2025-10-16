"""Workflow migration: aligns Jira workflows with OpenProject transitions."""

from __future__ import annotations

from typing import Any

from src import config
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult


@register_entity_types("workflows")
class WorkflowMigration(BaseMigration):
    """Synchronise Jira workflow transitions with OpenProject workflow records."""

    def __init__(self, jira_client, op_client) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)
        self.mappings = config.mappings

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
            msg = (
                f"WorkflowMigration does not support entity type: {entity_type}. "
                f"Supported types: ['workflows']"
            )
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

        issue_type_by_id = {
            str(item.get("id")): item.get("name")
            for item in issue_types
            if item.get("id") and item.get("name")
        }

        issue_type_to_workflow: dict[str, str] = {}
        workflow_names: set[str] = set()
        for scheme in schemes:
            mappings = scheme.get("issueTypeMappings") or scheme.get("mappings") or {}
            if isinstance(mappings, dict):
                for issue_type_id, workflow_name in mappings.items():
                    jira_name = issue_type_by_id.get(str(issue_type_id))
                    if jira_name and isinstance(workflow_name, str) and workflow_name:
                        issue_type_to_workflow[jira_name] = workflow_name
                        workflow_names.add(workflow_name)
            default_workflow = scheme.get("defaultWorkflow")
            if (
                isinstance(default_workflow, str)
                and default_workflow
                and not issue_type_to_workflow
            ):
                # Fallback: apply default workflow to every known issue type if no explicit mappings
                for name in issue_type_by_id.values():
                    if name not in issue_type_to_workflow:
                        issue_type_to_workflow[name] = default_workflow
                        workflow_names.add(default_workflow)

        # Fetch transitions and statuses for each workflow (API calls 4 & 5 per workflow)
        workflow_transitions: dict[str, list[dict[str, Any]]] = {}
        workflow_statuses: dict[str, list[dict[str, Any]]] = {}
        for workflow_name in workflow_names:
            try:
                transitions = self.jira_client.get_workflow_transitions(workflow_name)
            except Exception:  # noqa: BLE001
                transitions = []
            workflow_transitions[workflow_name] = transitions

            try:
                statuses = self.jira_client.get_workflow_statuses(workflow_name)
            except Exception:  # noqa: BLE001
                statuses = []
            workflow_statuses[workflow_name] = statuses if isinstance(statuses, list) else []

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
            issue_types = self.jira_client.get_issue_types()
            schemes = self.jira_client.get_workflow_schemes()
            roles = self.op_client.get_roles()
        except Exception as exc:  # noqa: BLE001
            return ComponentResult(
                success=False,
                message=f"Failed to extract workflow metadata: {exc}",
                error=str(exc),
            )

        issue_type_by_id = {
            str(item.get("id")): item.get("name")
            for item in issue_types
            if item.get("id") and item.get("name")
        }

        issue_type_to_workflow: dict[str, str] = {}
        workflow_names: set[str] = set()
        for scheme in schemes:
            mappings = scheme.get("issueTypeMappings") or scheme.get("mappings") or {}
            if isinstance(mappings, dict):
                for issue_type_id, workflow_name in mappings.items():
                    jira_name = issue_type_by_id.get(str(issue_type_id))
                    if jira_name and isinstance(workflow_name, str) and workflow_name:
                        issue_type_to_workflow[jira_name] = workflow_name
                        workflow_names.add(workflow_name)
            default_workflow = scheme.get("defaultWorkflow")
            if (
                isinstance(default_workflow, str)
                and default_workflow
                and not issue_type_to_workflow
            ):
                # Fallback: apply default workflow to every known issue type if no explicit mappings
                for name in issue_type_by_id.values():
                    if name not in issue_type_to_workflow:
                        issue_type_to_workflow[name] = default_workflow
                        workflow_names.add(default_workflow)

        workflow_transitions: dict[str, list[dict[str, Any]]] = {}
        workflow_statuses: dict[str, list[dict[str, Any]]] = {}
        for workflow_name in workflow_names:
            try:
                transitions = self.jira_client.get_workflow_transitions(workflow_name)
            except Exception:  # noqa: BLE001
                transitions = []
            workflow_transitions[workflow_name] = transitions

            try:
                statuses = self.jira_client.get_workflow_statuses(workflow_name)
            except Exception:  # noqa: BLE001
                statuses = []
            workflow_statuses[workflow_name] = statuses if isinstance(statuses, list) else []

        data = {
            "issue_type_to_workflow": issue_type_to_workflow,
            "workflow_transitions": workflow_transitions,
            "workflow_statuses": workflow_statuses,
            "roles": roles,
        }

        return ComponentResult(
            success=True,
            data=data,
            total_count=len(workflow_transitions),
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

        desired_role_names = config.migration_config.get(
            "workflow_roles",
            ["Project admin", "Project member"],
        )
        role_ids = [
            int(role["id"])
            for role in roles
            if int(role.get("id", 0)) > 0 and role.get("name") in desired_role_names
        ]
        if not role_ids:
            role_ids = [int(role["id"]) for role in roles if int(role.get("id", 0)) > 0]

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

            for transition in workflow_transitions.get(workflow_name, []):
                to_block = transition.get("to") or {}
                to_status_id = str(to_block.get("id") or "").strip()
                to_entry = status_by_id.get(to_status_id) or status_by_name.get(
                    str(to_block.get("name", "")).lower(),
                )
                if not to_entry:
                    skipped.append(
                        {
                            "reason": "missing_status_mapping",
                            "issue_type": issue_type_name,
                            "workflow": workflow_name,
                            "status_id": to_status_id,
                        },
                    )
                    continue

                op_to = int(to_entry.get("openproject_id", 0) or 0)
                if op_to <= 0:
                    continue

                from_status_ids = transition.get("from")
                if isinstance(from_status_ids, str):
                    from_status_list = [from_status_ids]
                elif isinstance(from_status_ids, list):
                    from_status_list = from_status_ids
                else:
                    from_status_list = []

                for from_status_id in from_status_list:
                    from_entry = (
                        status_by_id.get(str(from_status_id))
                        or status_by_name.get(
                            str(transition.get("name", "")).lower(),
                        )
                    )
                    if not from_entry:
                        skipped.append(
                            {
                                "reason": "missing_status_mapping",
                                "issue_type": issue_type_name,
                                "workflow": workflow_name,
                                "status_id": str(from_status_id),
                            },
                        )
                        continue

                    op_from = int(from_entry.get("openproject_id", 0) or 0)
                    if op_from <= 0:
                        continue

                    key = (type_id, op_from, op_to)
                    dedup_transitions.setdefault(
                        key,
                        {
                            "type_id": type_id,
                            "from_status_id": op_from,
                            "to_status_id": op_to,
                            "jira_issue_type": issue_type_name,
                            "jira_workflow": workflow_name,
                        },
                    )

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
