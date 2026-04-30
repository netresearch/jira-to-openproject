"""Roles, groups, and project-membership helpers for the OpenProject Rails console.

Phase 2l of ADR-002 continues the openproject_client.py god-class
decomposition by collecting the role/group/membership read+sync
operations onto a focused service. The service owns:

* **Reads**: ``get_roles`` (all roles with builtin flag) and
  ``get_groups`` (groups with member ids).
* **Group membership sync**: ``sync_group_memberships`` writes a
  payload + result-pair through the container, runs an
  idempotent Ruby loop that creates/updates groups to match the
  desired user-id sets, and reads the JSON summary back.
* **Project membership / role assignment**: ``assign_group_roles``
  (groups → projects with role ids) and ``assign_user_roles`` (single
  user → single project with role ids).

The shared ``_read_result_file`` helper stays on ``OpenProjectClient``
because ``sync_workflow_transitions`` (still on the client) also uses
it. The service reaches through ``self._client._read_result_file``.

``OpenProjectClient`` exposes the service via ``self.memberships`` and
keeps thin delegators for the same method names so existing call sites
work unchanged.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.clients.exceptions import QueryExecutionError

if TYPE_CHECKING:
    from src.clients.openproject_client import OpenProjectClient


class OpenProjectMembershipService:
    """Roles + groups + project-membership helpers for ``OpenProjectClient``."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    # ── reads ────────────────────────────────────────────────────────────

    def get_roles(self) -> list[dict[str, Any]]:
        """Return OpenProject roles (id, name, builtin flag)."""
        ruby = "Role.all.map { |r| r.as_json(only: [:id, :name, :builtin]) }"
        try:
            result = self._client.execute_json_query(ruby)
            if isinstance(result, list):
                return result
            msg = "Unexpected OpenProject role payload"
            raise QueryExecutionError(msg)
        except Exception as e:
            msg = f"Failed to fetch OpenProject roles: {e}"
            raise QueryExecutionError(msg) from e

    def get_groups(self) -> list[dict[str, Any]]:
        """Return existing OpenProject groups with member IDs."""
        ruby = (
            "Group.includes(:users).order(:name).map do |g| "
            "  { id: g.id, name: g.name, user_ids: g.users.pluck(:id) }"
            "end"
        )
        try:
            result = self._client.execute_json_query(ruby)
            if isinstance(result, list):
                return result
            msg = "Unexpected OpenProject group payload"
            raise QueryExecutionError(msg)
        except Exception as e:
            msg = f"Failed to fetch OpenProject groups: {e}"
            raise QueryExecutionError(msg) from e

    # ── group membership ────────────────────────────────────────────────

    def sync_group_memberships(self, assignments: list[dict[str, Any]]) -> dict[str, int]:
        """Ensure each group has the provided membership list."""
        if not assignments:
            return {"updated": 0, "errors": 0}

        client = self._client
        temp_dir = Path(client.file_manager.data_dir) / "group_sync"
        temp_dir.mkdir(parents=True, exist_ok=True)
        payload_path = temp_dir / f"group_memberships_{os.getpid()}_{int(time.time())}.json"
        result_path = temp_dir / (payload_path.name + ".result")

        try:
            with payload_path.open("w", encoding="utf-8") as handle:
                json.dump(assignments, handle)

            container_input = Path("/tmp") / payload_path.name
            container_output = Path("/tmp") / (payload_path.name + ".result")
            client.transfer_file_to_container(payload_path, container_input)

            ruby = (
                "require 'json'\n"
                f"input_path = '{container_input.as_posix()}'\n"
                f"output_path = '{container_output.as_posix()}'\n"
                "rows = JSON.parse(File.read(input_path))\n"
                "updated = 0\n"
                "errors = []\n"
                "rows.each do |row|\n"
                "  name = row['name']\n"
                "  next unless name && !name.strip.empty?\n"
                "  begin\n"
                "    group = Group.find_or_create_by(name: name)\n"
                "    desired_ids = Array(row['user_ids']).map(&:to_i).reject(&:nil?).uniq.sort\n"
                "    current_ids = group.user_ids.sort\n"
                "    if desired_ids != current_ids\n"
                "      group.user_ids = desired_ids\n"
                "      group.save\n"
                "      updated += 1\n"
                "    end\n"
                "  rescue => e\n"
                "    errors << { name: name, error: e.message }\n"
                "  end\n"
                "end\n"
                "File.write(output_path, { updated: updated, errors: errors.length }.to_json)\n"
                "nil\n"
            )

            client.execute_query(ruby, timeout=90)

            summary = client._read_result_file(container_output, result_path)
            return {
                "updated": int(summary.get("updated", 0)),
                "errors": int(summary.get("errors", 0)),
            }
        finally:
            with suppress(OSError):
                payload_path.unlink()
            with suppress(OSError):
                result_path.unlink()

    # ── project memberships / role assignment ───────────────────────────

    def assign_group_roles(
        self,
        assignments: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Assign OpenProject groups to projects with given role IDs."""
        if not assignments:
            return {"updated": 0, "errors": 0}

        client = self._client
        temp_dir = Path(client.file_manager.data_dir) / "group_roles"
        temp_dir.mkdir(parents=True, exist_ok=True)
        payload_path = temp_dir / f"group_roles_{os.getpid()}_{int(time.time())}.json"
        result_path = temp_dir / (payload_path.name + ".result")

        try:
            with payload_path.open("w", encoding="utf-8") as handle:
                json.dump(assignments, handle)

            container_input = Path("/tmp") / payload_path.name
            container_output = Path("/tmp") / (payload_path.name + ".result")
            client.transfer_file_to_container(payload_path, container_input)

            ruby = (
                "require 'json'\n"
                f"input_path = '{container_input.as_posix()}'\n"
                f"output_path = '{container_output.as_posix()}'\n"
                "updated = 0\n"
                "errors = []\n"
                "begin\n"
                "  File.write(output_path, { updated: 0, errors: 0, status: 'initialised' }.to_json)\n"
                "  rows = JSON.parse(File.read(input_path))\n"
                "  Array(rows).each do |row|\n"
                "    begin\n"
                "      name = row['group_name']\n"
                "      project_id = row['project_id'].to_i\n"
                "      role_ids = Array(row['role_ids']).map(&:to_i).reject(&:nil?).uniq\n"
                "      next if name.nil? || name.empty? || project_id <= 0 || role_ids.empty?\n"
                "      group = Group.find_by(name: name)\n"
                "      project = Project.find_by(id: project_id)\n"
                "      next unless group && project\n"
                "      member = Member.find_or_initialize_by(project: project, principal: group)\n"
                "      existing_ids = Array(member.role_ids).map(&:to_i)\n"
                "      new_ids = (existing_ids + role_ids).uniq\n"
                "      if member.new_record? || new_ids.sort != existing_ids.sort\n"
                "        member.role_ids = new_ids\n"
                "        member.save\n"
                "        updated += 1\n"
                "      end\n"
                "    rescue => e\n"
                "      errors << { group: row['group_name'], project: row['project_id'], error: e.message }\n"
                "    end\n"
                "  end\n"
                "rescue => e\n"
                "  errors << { error: e.message }\n"
                "ensure\n"
                "  summary = { updated: updated, errors: errors.length }\n"
                "  summary[:error_details] = errors if errors.any?\n"
                "  File.write(output_path, summary.to_json)\n"
                "end\n"
                "nil\n"
            )

            client.execute_query(ruby, timeout=90)

            summary = client._read_result_file(container_output, result_path)
            return {
                "updated": int(summary.get("updated", 0)),
                "errors": int(summary.get("errors", 0)),
            }
        finally:
            with suppress(OSError):
                payload_path.unlink()
            with suppress(OSError):
                result_path.unlink()

    def assign_user_roles(
        self,
        *,
        project_id: int,
        user_id: int,
        role_ids: list[int],
    ) -> dict[str, Any]:
        """Ensure a user has the given roles on a project."""
        client = self._client
        valid_role_ids = [int(r) for r in role_ids if isinstance(r, (int, str)) and int(r) > 0]
        if not valid_role_ids:
            return {"success": False, "error": "role_ids empty"}

        head = f"project_id = {int(project_id)}\nuser_id = {int(user_id)}\nrole_ids = {json.dumps(valid_role_ids)}\n"
        body = """
project = Project.find_by(id: project_id)
user = User.find_by(id: user_id)

unless project && user
  return { success: false, error: 'project or user not found' }
end

desired = Array(role_ids).map(&:to_i).reject { |rid| rid <= 0 }
if desired.empty?
  return { success: false, error: 'no roles specified' }
end

member = Member.find_or_initialize_by(project: project, principal: user)
existing = Array(member.role_ids).map(&:to_i)

if member.new_record? || (existing.sort != desired.sort)
  member.role_ids = desired
  changed = true
else
  changed = false
end

if member.save
  { success: true, changed: changed, role_ids: member.role_ids }
else
  { success: false, error: member.errors.full_messages.join(', ') }
end
"""
        script = head + body
        result = client.execute_query_to_json_file(script, timeout=90)
        if isinstance(result, dict):
            return result
        return {"success": False, "error": "unexpected response"}
