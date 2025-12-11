from __future__ import annotations

from collections import defaultdict
from typing import Any

from src import config
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult, MigrationError


@register_entity_types("groups")
class GroupMigration(BaseMigration):
    """Synchronize Jira groups and project role memberships into OpenProject."""

    def __init__(
        self,
        jira_client: Any | None = None,
        op_client: Any | None = None,
    ) -> None:
        super().__init__(jira_client=jira_client, op_client=op_client)
        self.jira_groups: list[dict[str, Any]] = []
        self.op_groups: list[dict[str, Any]] = []
        self.jira_groups_file = self.data_dir / "jira_groups.json"
        self.op_groups_file = self.data_dir / "op_groups.json"
        self.group_mapping_file = self.data_dir / "group_mapping.json"
        stored_mapping = self._load_from_json(self.group_mapping_file, default=None)
        self.group_mapping: dict[str, Any] = (
            stored_mapping
            if isinstance(stored_mapping, dict) and stored_mapping
            else config.mappings.get_mapping("group") or {}
        )

    # ------------------------------------------------------------------
    # BaseMigration overrides
    # ------------------------------------------------------------------
    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities from Jira for a specific type.

        This method enables idempotent workflow caching by providing a standard
        interface for entity retrieval. Called by run_with_change_detection() to fetch data
        with automatic thread-safe caching.

        Args:
            entity_type: The type of entities to retrieve (e.g., "groups")

        Returns:
            List of group entities with membership data

        Raises:
            ValueError: If entity_type is not supported by this migration

        """
        # Check if this is the entity type we handle
        if entity_type != "groups":
            msg = f"GroupMigration does not support entity type: {entity_type}. Supported types: ['groups']"
            raise ValueError(msg)

        # Fetch Jira groups (API call 1)
        self.logger.info("Fetching Jira groups and memberships")
        groups = self.jira_client.get_groups()

        # Fetch members for each group (API call 2 per group)
        group_members: list[dict[str, Any]] = []
        for group in groups:
            name = group.get("name")
            if not name:
                continue
            members = self.jira_client.get_group_members(name)
            group_payload = {
                "name": name,
                "groupId": group.get("groupId"),
                "members": members,
            }
            group_members.append(group_payload)

        self.logger.info("Discovered %s Jira groups", len(group_members))
        return group_members

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------
    def extract_jira_groups(self) -> list[dict[str, Any]]:
        if self.jira_groups:
            return self.jira_groups

        self.logger.info("Fetching Jira groups and memberships")
        groups = self.jira_client.get_groups()
        group_members: list[dict[str, Any]] = []
        for group in groups:
            name = group.get("name")
            if not name:
                continue
            members = self.jira_client.get_group_members(name)
            group_payload = {
                "name": name,
                "groupId": group.get("groupId"),
                "members": members,
            }
            group_members.append(group_payload)

        self.jira_groups = group_members
        self._save_to_json(group_members, self.jira_groups_file)
        self.logger.info("Discovered %s Jira groups", len(group_members))
        return self.jira_groups

    def extract_openproject_groups(self) -> list[dict[str, Any]]:
        self.logger.debug("Fetching OpenProject groups")
        groups = self.op_client.get_groups()
        self.op_groups = groups
        self._save_to_json(groups, self.op_groups_file)
        return groups

    # ------------------------------------------------------------------
    # Core execution
    # ------------------------------------------------------------------
    def run(self, batch_size: int | None = None) -> ComponentResult:  # noqa: ARG002
        try:
            jira_groups = self.extract_jira_groups()
            op_groups = self.extract_openproject_groups()
            summary = self._synchronize_groups(jira_groups, op_groups)
            return ComponentResult(
                success=True,
                message="Group migration completed",
                data=summary,
                total_count=summary.get("total_groups", 0),
                success_count=summary.get("groups_synced", 0),
            )
        except Exception as exc:
            self.logger.exception("Group migration failed")
            return ComponentResult(
                component="groups",
                status="failed",
                success=False,
                message=f"Group migration failed: {exc}",
                data={"error": str(exc)},
            )

    # ------------------------------------------------------------------
    # Synchronization logic
    # ------------------------------------------------------------------
    def _synchronize_groups(
        self,
        jira_groups: list[dict[str, Any]],
        op_groups: list[dict[str, Any]],
    ) -> dict[str, Any]:
        existing_groups = {str(g.get("name", "")).lower(): g for g in op_groups}
        groups_to_create: list[dict[str, Any]] = []

        # Track Jira group memberships by name for later lookups
        jira_members_by_group: dict[str, set[str]] = {}
        for group in jira_groups:
            name = group.get("name")
            if not name:
                continue
            members = group.get("members", []) or []
            member_keys = {
                str(m.get("key") or m.get("name") or m.get("accountId"))
                for m in members
                if m.get("key") or m.get("name") or m.get("accountId")
            }
            jira_members_by_group[name.lower()] = member_keys
            if name.lower() not in existing_groups:
                groups_to_create.append({"name": name})

        role_groups, project_role_assignments = self._collect_project_role_groups(jira_members_by_group)
        for role_group_name in role_groups.keys():
            if role_group_name.lower() not in existing_groups:
                groups_to_create.append({"name": role_group_name})

        created_count = 0
        if groups_to_create:
            self.logger.info("Creating %s OpenProject groups", len(groups_to_create))
            result = self.op_client.bulk_create_records(
                model="Group",
                records=groups_to_create,
                timeout=120,
                result_basename="j2o_group_bulk.json",
            )
            if result.get("status") != "success":
                msg = result.get("message", "Failed to create OpenProject groups")
                raise MigrationError(msg)
            created_count = len(result.get("created", []) or [])
            op_groups = self.extract_openproject_groups()
            existing_groups = {str(g.get("name", "")).lower(): g for g in op_groups}

        mapping: dict[str, Any] = {}
        for group in jira_groups:
            name = group.get("name")
            if not name:
                continue
            op_group = existing_groups.get(name.lower())
            if not op_group:
                continue
            mapping[name] = {
                "jira_name": name,
                "jira_group_id": group.get("groupId"),
                "openproject_id": op_group.get("id"),
                "created_new": name not in self.group_mapping,
            }

        for role_group_name in role_groups.keys():
            op_group = existing_groups.get(role_group_name.lower())
            if not op_group:
                continue
            mapping[role_group_name] = {
                "jira_name": role_group_name,
                "jira_group_id": None,
                "openproject_id": op_group.get("id"),
                "created_new": role_group_name not in self.group_mapping,
                "role_backed": True,
            }

        if mapping:
            config.mappings.set_mapping("group", mapping)
            self.group_mapping = mapping
            try:
                self._save_to_json(mapping, self.group_mapping_file)
            except Exception:  # noqa: BLE001
                self.logger.debug("Failed to persist group mapping to %s", self.group_mapping_file)

        membership_updates = self._synchronize_memberships(jira_members_by_group, role_groups, mapping)
        role_updates = self._assign_project_roles(project_role_assignments, mapping)

        summary = {
            "total_groups": len(mapping),
            "groups_synced": len(mapping),
            "groups_created": created_count,
            "membership_updates": membership_updates,
            "role_assignments": role_updates,
        }
        self.logger.info(
            "Group migration summary: %s created, %s synced",
            created_count,
            len(mapping),
        )
        return summary

    def _synchronize_memberships(
        self,
        jira_members_by_group: dict[str, set[str]],
        role_groups: dict[str, set[str]],
        mapping: dict[str, Any],
    ) -> dict[str, int]:
        user_mapping = config.mappings.get_mapping("user") or {}
        user_lookup: dict[str, Any] = {}
        for key, entry in user_mapping.items():
            if not isinstance(entry, dict):
                continue
            lower_key = str(key).lower()
            user_lookup[lower_key] = entry
            if entry.get("jira_key"):
                user_lookup[str(entry["jira_key"]).lower()] = entry
            if entry.get("jira_name"):
                user_lookup[str(entry["jira_name"]).lower()] = entry

        assignments: list[dict[str, Any]] = []
        role_groups_lower = {name.lower(): members for name, members in (role_groups or {}).items()}

        for jira_name, entry in mapping.items():
            group_name = jira_name
            member_keys = set()
            lookup_key = jira_name.lower()
            if lookup_key in jira_members_by_group:
                member_keys.update(jira_members_by_group[lookup_key])
            if lookup_key in role_groups_lower:
                member_keys.update(role_groups_lower[lookup_key])
            openproject_ids: set[int] = set()
            for key in member_keys:
                if not key:
                    continue
                entry_map = user_lookup.get(str(key).lower())
                if not entry_map:
                    continue
                op_id = entry_map.get("openproject_id") or entry_map.get("openproject_user_id")
                try:
                    if op_id:
                        openproject_ids.add(int(op_id))
                except Exception:  # noqa: BLE001
                    continue
            assignments.append({"name": group_name, "user_ids": sorted(openproject_ids)})

        result = self.op_client.sync_group_memberships(assignments)
        return {
            "updated": result.get("updated", 0),
            "errors": result.get("errors", 0),
        }

    def _collect_project_role_groups(
        self,
        jira_members_by_group: dict[str, set[str]],
    ) -> tuple[dict[str, set[str]], list[dict[str, Any]]]:
        project_mapping = config.mappings.get_mapping("project") or {}
        role_groups: dict[str, set[str]] = {}
        assignments: list[dict[str, Any]] = []

        for project_key, entry in project_mapping.items():
            op_project_id = entry.get("openproject_id")
            if not op_project_id:
                continue
            try:
                op_project_id_int = int(op_project_id)
            except Exception:  # noqa: BLE001
                continue

            try:
                roles = self.jira_client.get_project_roles(project_key)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Failed to fetch roles for project %s: %s", project_key, exc)
                continue

            for role in roles:
                role_name = role.get("name") or ""
                if not role_name:
                    continue
                group_name = f"J2O Role {project_key}::{role_name}".strip()
                members: set[str] = role_groups.setdefault(group_name, set())
                for actor in role.get("actors", []):
                    actor_type = str(actor.get("type", "")).lower()
                    if "group" in actor_type:
                        actor_group = actor.get("name") or actor.get("groupName")
                        if actor_group and actor_group.lower() in jira_members_by_group:
                            members.update(jira_members_by_group[actor_group.lower()])
                    elif "user" in actor_type:
                        key = actor.get("userKey") or actor.get("name") or actor.get("accountId")
                        if key:
                            members.add(str(key))
                if members:
                    assignments.append(
                        {
                            "group_name": group_name,
                            "project_key": project_key,
                            "openproject_project_id": op_project_id_int,
                            "role_name": role_name,
                        },
                    )

        role_groups = {name: members for name, members in role_groups.items() if members}
        return role_groups, assignments

    def _assign_project_roles(
        self,
        assignments: list[dict[str, Any]],
        mapping: dict[str, Any],
    ) -> dict[str, int]:
        if not assignments:
            return {"updated": 0, "errors": 0}

        roles = self.op_client.get_roles()
        role_lookup = {str(r.get("name", "")).lower(): int(r.get("id")) for r in roles if r.get("id")}

        payload_by_pair: dict[tuple[str, int], set[int]] = defaultdict(set)
        for assignment in assignments:
            group_name = assignment["group_name"]
            op_group_entry = mapping.get(group_name) or mapping.get(group_name.lower())
            if not op_group_entry:
                continue
            role_ids = self._resolve_role_ids(assignment["role_name"], role_lookup)
            if not role_ids:
                continue
            payload_by_pair[(group_name, assignment["openproject_project_id"])].update(role_ids)

        payload: list[dict[str, Any]] = []
        for (group_name, project_id), role_ids in payload_by_pair.items():
            payload.append(
                {
                    "group_name": group_name,
                    "project_id": project_id,
                    "role_ids": sorted(role_ids),
                },
            )

        result = self.op_client.assign_group_roles(payload)
        return {
            "updated": result.get("updated", 0),
            "errors": result.get("errors", 0),
        }

    def _resolve_role_ids(self, role_name: str, role_lookup: dict[str, int]) -> list[int]:
        if not role_lookup:
            return []

        normalized = str(role_name).strip().lower()
        if normalized in role_lookup:
            return [role_lookup[normalized]]

        heuristics = [
            ("admin" in normalized or "lead" in normalized, ["project admin", "manager", "administrator"]),
            ("manage" in normalized, ["project admin", "manager"]),
            ("develop" in normalized or "engineer" in normalized, ["developer", "member"]),
            ("test" in normalized or "qa" in normalized, ["developer", "member"]),
            ("read" in normalized or "view" in normalized or "observe" in normalized, ["reader", "viewer"]),
            (True, ["member"]),
        ]

        for condition, candidates in heuristics:
            if not condition:
                continue
            for candidate in candidates:
                candidate_key = candidate.lower()
                if candidate_key in role_lookup:
                    return [role_lookup[candidate_key]]
        return []
