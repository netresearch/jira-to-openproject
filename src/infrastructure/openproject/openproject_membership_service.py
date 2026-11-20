# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any

from src.infrastructure.exceptions import QueryExecutionError
from src.infrastructure.openproject.openproject_client import OpenProjectClient, escape_ruby_single_quoted


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
        except QueryExecutionError:
            # Let the more-specific error pass through unchanged so the
            # downstream message keeps its query/marker context instead
            # of being flattened into a generic "Failed to fetch" wrapper.
            raise
        except Exception as e:
            msg = f"Failed to fetch OpenProject roles: {e}"
            raise QueryExecutionError(msg) from e
        if isinstance(result, list):
            return result
        msg = "Unexpected OpenProject role payload"
        raise QueryExecutionError(msg)

    def get_groups(self) -> list[dict[str, Any]]:
        """Return existing OpenProject groups with member IDs."""
        ruby = (
            "Group.includes(:users).order(:name).map do |g| "
            "  { id: g.id, name: g.name, user_ids: g.users.pluck(:id) }"
            "end"
        )
        try:
            result = self._client.execute_json_query(ruby)
        except QueryExecutionError:
            # Same rationale as ``get_roles`` — preserve the
            # specific Rails-side error rather than re-wrapping.
            raise
        except Exception as e:
            msg = f"Failed to fetch OpenProject groups: {e}"
            raise QueryExecutionError(msg) from e
        if isinstance(result, list):
            return result
        msg = "Unexpected OpenProject group payload"
        raise QueryExecutionError(msg)

    # ── group membership ────────────────────────────────────────────────

    @contextmanager
    def _assignment_files(self, assignments: list[dict[str, Any]]) -> Iterator[tuple[Path, Path, Path]]:
        """Stage membership data in private per-call directories on both systems."""
        client = self._client
        root = Path(client.file_manager.data_dir)
        root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=root) as local:
            source = Path(local) / "assignments.json"
            source.write_text(json.dumps(assignments), encoding="utf-8")
            created = client.execute_json_query("(require 'tmpdir'; Dir.mktmpdir('j2o-members-')).as_json")
            if (
                not isinstance(created, str)
                or not PurePosixPath(created).is_absolute()
                or not PurePosixPath(created).name.startswith("j2o-members-")
            ):
                msg = "OpenProject could not allocate a private membership directory"
                raise QueryExecutionError(msg)
            remote = Path(created)
            try:
                target = remote / "assignments.json"
                client.transfer_file_to_container(source, target)
                yield target, remote / "result.json", Path(local) / "result.json"
            finally:
                path = escape_ruby_single_quoted(created)
                client.execute_query(f"require 'fileutils'; FileUtils.remove_entry_secure('{path}')")

    def sync_group_memberships(self, assignments: list[dict[str, Any]]) -> dict[str, int]:
        """Ensure each group has the provided membership list."""
        if not assignments:
            return {"updated": 0, "errors": 0}

        client = self._client
        with self._assignment_files(assignments) as (container_input, container_output, result_path):
            ruby = (
                "require 'json'\n"
                f"input_path = '{escape_ruby_single_quoted(container_input.as_posix())}'\n"
                f"output_path = '{escape_ruby_single_quoted(container_output.as_posix())}'\n"
                "rows = JSON.parse(File.read(input_path))\n"
                "updated = 0\n"
                "errors = []\n"
                "rows.each do |row|\n"
                "  name = row['name']\n"
                "  next unless name && !name.strip.empty?\n"
                "  begin\n"
                "    ids = Array(row['user_ids']).compact.map(&:to_i).select(&:positive?).uniq.sort\n"
                "    groups = Group.where(name: name)\n"
                "    group = groups.exists? ? groups.sole : nil\n"
                "    next if group && group.user_ids.sort == ids\n"
                "    result = if group\n"
                "      Groups::UpdateService.new(user: User.system, model: group).call(replace_user_ids: ids)\n"
                "    else\n"
                "      Groups::CreateService.new(user: User.system).call(name: name, add_user_ids: ids)\n"
                "    end\n"
                "    raise result.errors.full_messages.join(', ') unless result.success?\n"
                "    updated += 1\n"
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

    # ── project memberships / role assignment ───────────────────────────

    def assign_group_roles(
        self,
        assignments: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Assign OpenProject groups to projects with given role IDs.

        Note on semantics: this is an *additive* operation, not a sync.
        Existing role ids on the project membership are preserved and the
        supplied ``role_ids`` are merged in. This differs from
        ``assign_user_roles`` (which replaces) and
        ``sync_group_memberships`` (which replaces user_ids). The
        difference is intentional: migrations call
        ``assign_group_roles`` to *grant* roles imported from Jira
        without nuking project-side roles that admins may have added by
        hand. Use a separate "sync" entry point if true replacement is
        ever needed.
        """
        if not assignments:
            return {"updated": 0, "errors": 0}

        client = self._client
        with self._assignment_files(assignments) as (container_input, container_output, result_path):
            ruby = (
                "require 'json'\n"
                f"input_path = '{escape_ruby_single_quoted(container_input.as_posix())}'\n"
                f"output_path = '{escape_ruby_single_quoted(container_output.as_posix())}'\n"
                "updated = 0\n"
                "errors = []\n"
                "begin\n"
                "  File.write(output_path, { updated: 0, errors: 0, status: 'initialised' }.to_json)\n"
                "  rows = JSON.parse(File.read(input_path))\n"
                "  Array(rows).each do |row|\n"
                "    begin\n"
                "      name = row['group_name']\n"
                "      project_id = row['project_id'].to_i\n"
                # ``compact`` drops nils, ``select { |id| id.positive? }``
                # drops 0s that ``to_i`` would emit for non-numeric
                # inputs — the prior ``reject(&:nil?)`` after ``to_i``
                # was a no-op (``to_i`` never produces nil).
                "      role_ids = Array(row['role_ids']).compact.map(&:to_i).select { |id| id.positive? }.uniq\n"
                "      next if name.nil? || name.empty? || project_id <= 0 || role_ids.empty?\n"
                "      group = Group.find_by(name: name)\n"
                "      project = Project.find_by(id: project_id)\n"
                "      next unless group && project\n"
                "      member = Member.find_by(project: project, principal: group, entity: nil)\n"
                # OpenProject's member services copy a group's roles to its users
                # (inherited member roles); a bare Member#save grants only the group.
                "      call = if member.nil?\n"
                "        Members::CreateService.new(user: User.system)\n"
                "          .call(principal: group, project: project, role_ids: role_ids, send_notifications: false)\n"
                "      else\n"
                "        existing_ids = member.member_roles.reject(&:inherited_from).map(&:role_id)\n"
                # Additive merge by design — see Python docstring above.
                "        new_ids = (existing_ids + role_ids).uniq\n"
                "        next_ids = new_ids.sort == existing_ids.sort ? nil : new_ids\n"
                "        next_ids && Members::UpdateService.new(user: User.system, model: member)\n"
                "          .call(role_ids: next_ids, send_notifications: false)\n"
                "      end\n"
                "      if call\n"
                "        raise call.errors.full_messages.join(', ') unless call.success?\n"
                "        updated += 1\n"
                "      end\n"
                # Also gives group members access where an earlier run saved the
                # group membership directly. Inserts only missing rows.
                "      inherited = Groups::CreateInheritedRolesService\n"
                "        .new(group, current_user: User.system, contract_class: EmptyContract)\n"
                "        .call(user_ids: group.user_ids, send_notifications: false, project_ids: [project.id])\n"
                "      raise inherited.errors.full_messages.join(', ') unless inherited.success?\n"
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

    def assign_user_roles(
        self,
        *,
        project_id: int,
        user_id: int,
        role_ids: list[int],
    ) -> dict[str, Any]:
        """Ensure a user has the given roles on a project."""
        client = self._client
        # Parse defensively: ``role_ids`` may arrive as a mixed list of
        # ``int``s and string-encoded ints from Ruby/JSON round-trips.
        # The previous comprehension called ``int(r) > 0`` inside the
        # filter, which raises ``ValueError`` on non-numeric strings
        # (e.g. ``"abc"``) and crashes the migration. Use try/except
        # so unparseable entries are skipped silently rather than
        # aborting the whole call.
        valid_role_ids: list[int] = []
        for r in role_ids:
            if not isinstance(r, (int, str)):
                continue
            try:
                parsed = int(r)
            except TypeError, ValueError:
                continue
            if parsed > 0:
                valid_role_ids.append(parsed)
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

member = Member.find_by(project: project, principal: user, entity: nil)
# Compare direct roles only; roles inherited from groups stay untouched.
existing = member ? member.member_roles.reject(&:inherited_from).map(&:role_id) : []
call = if member.nil?
  Members::CreateService.new(user: User.system)
    .call(principal: user, project: project, role_ids: desired, send_notifications: false)
elsif existing.sort != desired.sort
  Members::UpdateService.new(user: User.system, model: member)
    .call(role_ids: desired, send_notifications: false)
end

if call.nil?
  { success: true, changed: false, role_ids: member.role_ids }
elsif call.success?
  { success: true, changed: true, role_ids: call.result.role_ids }
else
  { success: false, error: call.errors.full_messages.join(', ') }
end
"""
        script = head + body
        result = client.execute_query_to_json_file(script, timeout=90)
        if isinstance(result, dict):
            return result
        return {"success": False, "error": "unexpected response"}
