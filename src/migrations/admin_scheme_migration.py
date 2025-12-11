"""Migrate Jira permission roles into OpenProject project memberships."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.clients.jira_client import JiraClient
    from src.clients.openproject_client import OpenProjectClient

from src import config
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

ROLE_NAME_MAPPING = {
    "administrators": ["Project admin"],
    "project administrators": ["Project admin"],
    "project admins": ["Project admin"],
    "admin": ["Project admin"],
    "admins": ["Project admin"],
    "developers": ["Project member"],
    "users": ["Project member"],
    "members": ["Project member"],
    "team members": ["Project member"],
    "viewers": ["Viewer", "Reader"],
    "observers": ["Viewer", "Reader"],
}


@register_entity_types("admin_schemes")
class AdminSchemeMigration(BaseMigration):
    """Synchronise Jira project role actors to OpenProject roles and memberships."""

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:  # noqa: D107
        super().__init__(jira_client=jira_client, op_client=op_client)
        self.project_mapping = config.mappings.get_mapping("project") or {}
        self.user_mapping = config.mappings.get_mapping("user") or {}
        self.group_mapping = config.mappings.get_mapping("group") or {}

    # ------------------------------------------------------------------ #
    # BaseMigration overrides                                            #
    # ------------------------------------------------------------------ #

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities from Jira for a specific type.

        This method enables idempotent workflow caching by providing a standard
        interface for entity retrieval. Called by run_with_change_detection() to fetch data
        with automatic thread-safe caching.

        Args:
            entity_type: The type of entities to retrieve (e.g., "admin_schemes")

        Returns:
            List of project admin scheme entities (project roles + permission schemes)

        Raises:
            ValueError: If entity_type is not supported by this migration

        """
        # Check if this is the entity type we handle
        if entity_type != "admin_schemes":
            msg = (
                f"AdminSchemeMigration does not support entity type: {entity_type}. Supported types: ['admin_schemes']"
            )
            raise ValueError(msg)

        # Aggregate data from multiple API calls per project
        projects: list[dict[str, Any]] = []

        for project_key, entry in self.project_mapping.items():
            op_project_id = int(entry.get("openproject_id", 0) or 0) if isinstance(entry, dict) else 0
            if op_project_id <= 0:
                continue

            try:
                # Two API calls per project: roles and permission scheme
                roles = self.jira_client.get_project_roles(project_key)
                scheme = self.jira_client.get_project_permission_scheme(project_key)
            except Exception as exc:
                self.logger.exception(
                    "Failed to fetch admin scheme for project %s: %s",
                    project_key,
                    exc,
                )
                continue

            projects.append(
                {
                    "project_key": project_key,
                    "openproject_id": op_project_id,
                    "roles": roles,
                    "permission_scheme": scheme,
                },
            )

        return projects

    def _extract(self) -> ComponentResult:
        """Gather Jira project role assignments for mapped projects."""
        projects = []
        for project_key, entry in self.project_mapping.items():
            op_project_id = int(entry.get("openproject_id", 0) or 0) if isinstance(entry, dict) else 0
            if op_project_id <= 0:
                continue
            try:
                roles = self.jira_client.get_project_roles(project_key)
                scheme = self.jira_client.get_project_permission_scheme(project_key)
            except Exception as exc:
                self.logger.exception(
                    "Failed to extract admin scheme for project %s: %s",
                    project_key,
                    exc,
                )
                continue

            projects.append(
                {
                    "project_key": project_key,
                    "openproject_id": op_project_id,
                    "roles": roles,
                    "permission_scheme": scheme,
                },
            )

        return ComponentResult(
            success=True,
            data={"projects": projects},
            total_count=len(projects),
        )

    def _map(self, extracted: ComponentResult) -> ComponentResult:
        """Convert Jira actors to OpenProject user/group assignments."""
        if not extracted.success or not isinstance(extracted.data, dict):
            return ComponentResult(
                success=False,
                message="Admin scheme extraction failed",
                error=extracted.message or "extract phase returned no data",
            )

        projects: list[dict[str, Any]] = extracted.data.get("projects", [])
        op_roles = self.op_client.get_roles()
        role_name_to_id = {role.get("name"): int(role.get("id", 0) or 0) for role in op_roles if role.get("name")}

        group_assignments: defaultdict[tuple[str, int], set[int]] = defaultdict(set)
        user_assignments: defaultdict[tuple[int, int], set[int]] = defaultdict(set)
        skipped: list[dict[str, Any]] = []

        def resolve_role_ids(jira_role_name: str) -> list[int]:
            desired = ROLE_NAME_MAPPING.get(jira_role_name.lower())
            if not desired:
                return []
            role_ids = [role_name_to_id.get(name) for name in desired if role_name_to_id.get(name)]
            return [rid for rid in role_ids if rid]

        def resolve_user_id(actor: dict[str, Any]) -> int | None:
            candidates = [
                actor.get("accountId"),
                actor.get("userKey"),
                actor.get("name"),
            ]
            for candidate in candidates:
                if candidate and candidate in self.user_mapping:
                    entry = self.user_mapping[candidate]
                    op_id = entry.get("openproject_id") if isinstance(entry, dict) else None
                    if op_id:
                        return int(op_id)
            return None

        def resolve_group_name(actor: dict[str, Any]) -> str | None:
            group_name = actor.get("groupName") or actor.get("name")
            if not group_name:
                return None
            if group_name in self.group_mapping:
                return group_name
            # Some mappings may store case-variant names
            for known in self.group_mapping.keys():
                if known.lower() == group_name.lower():
                    return known
            return None

        for project in projects:
            project_key = project.get("project_key")
            project_id = int(project.get("openproject_id", 0) or 0)
            if project_id <= 0:
                continue

            for role in project.get("roles", []):
                role_name = role.get("name")
                if not isinstance(role_name, str):
                    continue
                role_ids = resolve_role_ids(role_name)
                if not role_ids:
                    skipped.append(
                        {
                            "project_key": project_key,
                            "role": role_name,
                            "reason": "unmapped_role",
                        },
                    )
                    continue

                for actor in role.get("actors", []):
                    actor_type = (actor.get("type") or "").lower()
                    if "user" in actor_type:
                        user_id = resolve_user_id(actor)
                        if user_id:
                            user_assignments[(project_id, user_id)].update(role_ids)
                        else:
                            skipped.append(
                                {
                                    "project_key": project_key,
                                    "role": role_name,
                                    "actor": actor,
                                    "reason": "user_not_mapped",
                                },
                            )
                    elif "group" in actor_type:
                        group_name = resolve_group_name(actor)
                        if group_name:
                            group_assignments[(group_name, project_id)].update(role_ids)
                        else:
                            skipped.append(
                                {
                                    "project_key": project_key,
                                    "role": role_name,
                                    "actor": actor,
                                    "reason": "group_not_mapped",
                                },
                            )

        mapped = {
            "user_assignments": [
                {
                    "project_id": project_id,
                    "user_id": user_id,
                    "role_ids": sorted(role_ids),
                }
                for (project_id, user_id), role_ids in user_assignments.items()
            ],
            "group_assignments": [
                {
                    "group_name": group_name,
                    "project_id": project_id,
                    "role_ids": sorted(role_ids),
                }
                for (group_name, project_id), role_ids in group_assignments.items()
            ],
            "skipped": skipped,
        }

        return ComponentResult(
            success=True,
            data=mapped,
            total_count=len(mapped["user_assignments"]) + len(mapped["group_assignments"]),
            details={
                "user_assignments": len(mapped["user_assignments"]),
                "group_assignments": len(mapped["group_assignments"]),
                "skipped": len(skipped),
            },
        )

    def _load(self, mapped: ComponentResult) -> ComponentResult:
        """Create OpenProject memberships according to mapped roles."""
        if not mapped.success or not isinstance(mapped.data, dict):
            return ComponentResult(
                success=False,
                message="Admin scheme mapping failed",
                error=mapped.message or "map phase returned no data",
            )

        user_assignments: list[dict[str, Any]] = mapped.data.get("user_assignments", [])
        group_assignments: list[dict[str, Any]] = mapped.data.get("group_assignments", [])

        updated = 0
        failed = 0

        if group_assignments:
            try:
                result = self.op_client.assign_group_roles(group_assignments)
                if isinstance(result, dict):
                    updated += int(result.get("updated", 0))
                    group_errors = int(result.get("errors", 0))
                    if group_errors:
                        failed += group_errors
                        details = result.get("error_details") or result.get("errors_detail")
                        if details:
                            self.logger.error("Group role assignment errors: %s", details)
                        else:
                            self.logger.error("Group role assignment reported %s errors without details", group_errors)
            except Exception as exc:
                failed += len(group_assignments)
                self.logger.exception("Failed to assign group roles: %s", exc)

        for assignment in user_assignments:
            try:
                result = self.op_client.assign_user_roles(**assignment)
                if result.get("success"):
                    updated += 1
                else:
                    failed += 1
                    self.logger.error(
                        "User role assignment failed for project %s user %s: %s",
                        assignment.get("project_id"),
                        assignment.get("user_id"),
                        result.get("error") or result,
                    )
            except Exception as exc:
                failed += 1
                self.logger.exception(
                    "Failed to assign user %s to project %s: %s",
                    assignment.get("user_id"),
                    assignment.get("project_id"),
                    exc,
                )

        return ComponentResult(
            success=failed == 0,
            message="Admin scheme memberships synchronised",
            success_count=updated,
            failed_count=failed,
            details={
                "user_assignments": len(user_assignments),
                "group_assignments": len(group_assignments),
                "skipped": len(mapped.data.get("skipped", [])),
            },
        )

    def run(self) -> ComponentResult:
        """Execute the admin scheme migration pipeline."""
        self.logger.info("Starting admin scheme migration")

        if not self.group_mapping:
            self._refresh_group_mapping()

        extracted = self._extract()
        if not extracted.success:
            self.logger.error(
                "Admin scheme extraction failed: %s",
                extracted.message or extracted.error,
            )
            return extracted

        mapped = self._map(extracted)
        if not mapped.success:
            self.logger.error(
                "Admin scheme mapping failed: %s",
                mapped.message or mapped.error,
            )
            return mapped

        result = self._load(mapped)
        if result.success:
            self.logger.info(
                "Admin scheme migration complete (updates=%s)",
                result.success_count,
            )
        else:
            self.logger.error(
                "Admin scheme migration failure (failed=%s)",
                result.failed_count,
            )
        return result

    def _refresh_group_mapping(self) -> None:
        """Populate group mapping directly from OpenProject when absent."""
        try:
            groups = self.op_client.get_groups()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Unable to refresh group mapping: %s", exc)
            return

        mapping: dict[str, Any] = {}
        for group in groups or []:
            name = group.get("name")
            gid = group.get("id")
            if not name or gid is None:
                continue
            mapping[str(name)] = {
                "jira_name": name,
                "openproject_id": gid,
                "created_new": False,
            }

        if mapping:
            self.group_mapping = mapping
            try:
                config.mappings.set_mapping("group", mapping)
            except Exception:  # noqa: BLE001
                self.logger.debug("Persisting refreshed group mapping failed; continuing in-memory only")
