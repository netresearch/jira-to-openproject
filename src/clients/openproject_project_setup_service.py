"""Project-setup helpers for the OpenProject Rails console.

Phase 2u of ADR-002 continues the openproject_client.py god-class
decomposition by collecting one-off project bootstrap operations onto
a single focused service: ensuring the J2O reporting project exists,
seeding workflow transitions for migrated roles, upserting Versions
on a project, and enabling Rails-side modules (singly or in bulk).
These five methods share enough domain (a project as an
OpenProject-side configuration target) that keeping them together
matches the per-domain service pattern from earlier phases.

The service owns:

* ``ensure_reporting_project`` — create-or-find the dedicated J2O
  reporting Project (with the wiki module pre-enabled) and return its
  id.
* ``sync_workflow_transitions`` — bulk-create ``Workflow`` rows for
  every (type, from_status, to_status, role) combination, using a
  temp file payload via ``client._read_result_file``.
* ``ensure_project_version`` — create-or-update a ``Version`` row for
  a project (sprint / release).
* ``enable_project_modules`` — idempotent module-enable for a single
  project.
* ``bulk_enable_project_modules`` — heredoc-driven multi-project
  variant of the above (single Rails round-trip).

``OpenProjectClient`` exposes the service via ``self.project_setup``
and keeps thin delegators for the same method names so existing call
sites work unchanged. The shared ``_read_result_file`` helper stays
on ``OpenProjectClient`` because the membership service also uses it
(same contract, same temp-file polling).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.clients.exceptions import QueryExecutionError

if TYPE_CHECKING:
    from src.clients.openproject_client import OpenProjectClient


class OpenProjectProjectSetupService:
    """Reporting project, workflow transitions, versions, modules."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    # ── reporting project ──────────────────────────────────────────────

    def ensure_reporting_project(self, identifier: str, name: str) -> int:
        """Ensure a dedicated OpenProject project exists for reporting artefacts.

        Creates the project when missing, enables the wiki module, and returns its ID.

        Args:
            identifier: Desired project identifier (lowercase/hyphenated)
            name: Human readable project name

        Returns:
            OpenProject project ID

        Raises:
            QueryExecutionError: when creation fails or no project can be ensured

        """
        # Lazy import to avoid the service ↔ client cycle at module load time.
        from src.clients.openproject_client import escape_ruby_single_quoted

        clean_identifier = re.sub(r"[^a-z0-9-]", "-", identifier.lower()).strip("-")
        clean_identifier = re.sub(r"-+", "-", clean_identifier) or "j2o-reporting"
        clean_name = name.strip() or "Jira Dashboards"

        # ``clean_identifier`` is sanitised by the regex above to only
        # contain ``[a-z0-9-]`` so embedding it via single-quoted Ruby
        # string is safe; ``clean_name`` goes through
        # ``escape_ruby_single_quoted`` because it may contain quotes
        # and other Ruby-meaningful characters.
        script = (
            "begin\n"
            "  user = User.admin.first || User.active.first || User.first\n"
            "  raise 'no admin user available' unless user\n"
            f"  identifier = '{clean_identifier}'\n"
            f"  display_name = '{escape_ruby_single_quoted(clean_name)}'\n"
            "  project = Project.find_by(identifier: identifier)\n"
            "  created = false\n"
            "  unless project\n"
            "    if defined?(::Projects::CreateService)\n"
            "      service = ::Projects::CreateService.new(user: user)\n"
            "      params = { name: display_name, identifier: identifier, public: false, active: false, enabled_module_names: ['wiki'], workspace_type: 'project' }\n"
            "      result = service.call(**params)\n"
            "      unless result.success?\n"
            "        raise result.errors.full_messages.join(', ')\n"
            "      end\n"
            "      project = result.result\n"
            "    else\n"
            "      project = Project.new(name: display_name, identifier: identifier)\n"
            "      project.public = false if project.respond_to?(:public=)\n"
            "      project.active = false if project.respond_to?(:active=)\n"
            "      project.workspace_type = 'project' if project.respond_to?(:workspace_type=)\n"
            "      project.enabled_module_names = ['wiki'] if project.respond_to?(:enabled_module_names=)\n"
            "      project.save!\n"
            "    end\n"
            "    created = true\n"
            "  end\n"
            "  if project.enabled_module_names.exclude?('wiki')\n"
            "    project.enabled_module_names = (project.enabled_module_names + ['wiki']).uniq\n"
            "    project.save!\n"
            "  end\n"
            "  if project.respond_to?(:workspace_type=) && project.workspace_type != 'project'\n"
            "    project.workspace_type = 'project'\n"
            "    project.save!\n"
            "  end\n"
            "  { success: true, id: project.id, created: created, identifier: project.identifier }\n"
            "rescue => e\n"
            "  { success: false, error: e.message }\n"
            "end\n"
        )

        result = self._client.execute_query_to_json_file(script, timeout=180)
        if not isinstance(result, dict):
            msg = f"Unexpected response when ensuring reporting project: {result!r}"
            raise QueryExecutionError(msg)
        if not result.get("success"):
            msg = f"Failed to ensure reporting project '{clean_identifier}': {result.get('error')}"
            raise QueryExecutionError(
                msg,
            )
        project_id = int(result.get("id", 0) or 0)
        if project_id <= 0:
            msg = f"Reporting project '{clean_identifier}' returned invalid id: {project_id}"
            raise QueryExecutionError(
                msg,
            )
        return project_id

    # ── workflow transitions ───────────────────────────────────────────

    def sync_workflow_transitions(
        self,
        transitions: list[dict[str, int]],
        role_ids: list[int],
    ) -> dict[str, int]:
        """Ensure workflow transitions exist for the provided type/status/role combinations."""
        if not transitions or not role_ids:
            return {"created": 0, "existing": 0, "errors": 0}

        client = self._client
        temp_dir = Path(client.file_manager.data_dir) / "workflow_sync"
        temp_dir.mkdir(parents=True, exist_ok=True)
        # ``token_hex(4)`` adds 8 random hex chars so two calls within
        # the same process during the same wall-clock second can't
        # collide on the payload, the ``.result`` file, or the
        # container ``/tmp`` paths derived from the filename. Same
        # pattern the membership service uses for identical reasons.
        unique_suffix = f"{os.getpid()}_{int(time.time())}_{secrets.token_hex(4)}"
        payload_path = temp_dir / f"workflow_transitions_{unique_suffix}.json"
        result_path = temp_dir / (payload_path.name + ".result")

        payload = {
            "transitions": [
                {
                    "type_id": int(row.get("type_id", 0)),
                    "from_status_id": int(row.get("from_status_id", 0)),
                    "to_status_id": int(row.get("to_status_id", 0)),
                }
                for row in transitions
            ],
            "role_ids": [int(r) for r in role_ids if int(r) > 0],
        }

        try:
            with payload_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle)

            container_payload = Path("/tmp") / payload_path.name
            container_output = Path("/tmp") / (payload_path.name + ".result")
            client.transfer_file_to_container(payload_path, container_payload)

            ruby = (
                "require 'json'\n"
                f"payload_path = '{container_payload.as_posix()}'\n"
                f"output_path = '{container_output.as_posix()}'\n"
                "data = JSON.parse(File.read(payload_path))\n"
                "transitions = Array(data['transitions'])\n"
                "role_ids = Array(data['role_ids']).map(&:to_i).reject { |rid| rid <= 0 }.uniq\n"
                "created = 0\n"
                "existing = 0\n"
                "errors = []\n"
                "seen = {}\n"
                "transitions.each do |row|\n"
                "  type_id = row['type_id'].to_i\n"
                "  from_id = row['from_status_id'].to_i\n"
                "  to_id = row['to_status_id'].to_i\n"
                "  next if type_id <= 0 || from_id <= 0 || to_id <= 0\n"
                "  key = [type_id, from_id, to_id]\n"
                "  next if seen[key]\n"
                "  seen[key] = true\n"
                "  role_ids.each do |role_id|\n"
                "    begin\n"
                "      wf = Workflow.find_by(type_id: type_id, role_id: role_id, old_status_id: from_id, new_status_id: to_id)\n"
                "      if wf\n"
                "        existing += 1\n"
                "      else\n"
                "        Workflow.create!(type_id: type_id, role_id: role_id, old_status_id: from_id, new_status_id: to_id)\n"
                "        created += 1\n"
                "      end\n"
                "    rescue => e\n"
                "      errors << { type_id: type_id, role_id: role_id, from: from_id, to: to_id, error: e.message }\n"
                "    end\n"
                "  end\n"
                "end\n"
                "File.write(output_path, { created: created, existing: existing, errors: errors.length }.to_json)\n"
                "nil\n"
            )

            client.execute_query(ruby, timeout=180)
            summary = client._read_result_file(container_output, result_path)
            return {
                "created": int(summary.get("created", 0)),
                "existing": int(summary.get("existing", 0)),
                "errors": int(summary.get("errors", 0)),
            }
        finally:
            with suppress(OSError):
                payload_path.unlink()
            with suppress(OSError):
                result_path.unlink()

    # ── versions ───────────────────────────────────────────────────────

    def ensure_project_version(
        self,
        project_id: int,
        *,
        name: str,
        description: str | None = None,
        start_date: str | None = None,
        due_date: str | None = None,
        status: str | None = None,
        sharing: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a Version (Sprint/Release) for a project.

        Returns the Rails-side result dict on success, or a
        ``{"success": False, "error": ...}`` envelope when the
        ``project_id`` cannot be coerced to an int / the script fails.
        """
        # ``int(project_id)`` is moved inside the existing try/except
        # so a malformed ``project_id`` (non-numeric / None / etc.)
        # falls through to the documented ``{success: False, ...}``
        # error envelope rather than raising ``ValueError`` /
        # ``TypeError`` past the caller.
        try:
            payload = {
                "project_id": int(project_id),
                "name": name,
                "description": description,
                "start_date": start_date,
                "due_date": due_date,
                "status": status,
                "sharing": sharing or "none",
            }

            # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
            # that Ruby misinterprets as invalid Unicode escape sequences. The
            # single-quoted heredoc tag (``<<'JSON_DATA'``) prevents Ruby
            # interpolation, so emitting the JSON payload directly here is safe —
            # ``JSON.parse`` on the Ruby side treats it as data, not code.
            payload_json = json.dumps(payload, ensure_ascii=False)
            # Drop ``.to_json`` from both terminal expressions —
            # ``execute_query_to_json_file`` already wraps the script
            # tail in ``.as_json`` on the Ruby side, so an explicit
            # ``.to_json`` would double-encode and Python would receive
            # a JSON string instead of a parsed dict.
            script = f"""
            require 'json'
            input = JSON.parse(<<'JSON_DATA')
{payload_json}
JSON_DATA

            project = Project.find_by(id: input['project_id'].to_i)
            if project.nil?
              {{ success: false, error: 'project not found' }}
            else
              version = project.versions.where(name: input['name']).first_or_initialize
              was_new = version.new_record?
              attrs = {{ name: input['name'], sharing: input['sharing'] || 'none' }}
              attrs[:description] = input['description'] if input['description']
              attrs[:start_date] = input['start_date'] if input['start_date']
              attrs[:due_date] = input['due_date'] if input['due_date']
              attrs[:status] = input['status'] if input['status']
              version.assign_attributes(attrs)

              changed = version.changed?
              if changed
                version.save!
              else
                version.save! if was_new
              end

              {{
                success: true,
                id: version.id,
                created: was_new,
                updated: changed
              }}
            end
            """

            result = self._client.execute_query_to_json_file(script, timeout=90)
            if isinstance(result, dict):
                return result
            return {"success": False, "error": "unexpected response"}
        except Exception as e:
            self._logger.warning(
                "Failed to ensure project version %s for project %s: %s",
                name,
                project_id,
                e,
            )
            return {"success": False, "error": str(e)}

    # ── modules ────────────────────────────────────────────────────────

    def enable_project_modules(self, project_id: int, modules: list[str]) -> bool:
        """Ensure the given project has the specified modules enabled.

        Idempotent: adds any missing modules to ``enabled_module_names`` and
        saves the project.

        Args:
            project_id: OpenProject project ID
            modules: List of module identifiers (e.g., ['time_tracking'])

        Returns:
            True if the modules are enabled (already or after change), False on error

        """
        if not modules:
            return True
        # Move ``int(project_id)`` and the f-string interpolation inside
        # the existing try/except so a malformed ``project_id``
        # (non-numeric / None / etc.) returns ``False`` per the
        # documented contract instead of raising ``ValueError`` /
        # ``TypeError`` past the caller.
        try:
            pid = int(project_id)
            # Build Ruby script that ensures all modules are present.
            # ``json.dumps`` emits a JSON list literal which Ruby parses
            # as a ``["a", "b"]`` array literal — fine because the
            # contents are coerced to ``str`` first and JSON's escaping
            # rules cover any backslashes / quotes that would otherwise
            # break out of the Ruby array.
            mods_json = json.dumps([str(m) for m in modules])
            script = f"""
            begin
              p = Project.find({pid})
              names = p.enabled_module_names.map(&:to_s)
              desired = {mods_json}
              added = false
              desired.each do |m|
                unless names.include?(m)
                  names << m
                  added = true
                end
              end
              if added
                p.enabled_module_names = names
                p.save!
              end
              {{ changed: added, enabled: names }}
            rescue => e
              {{ error: e.message }}
            end
            """
            result = self._client.execute_json_query(script)
            if isinstance(result, dict) and not result.get("error"):
                return True
            self._logger.warning(
                "Failed to enable modules on project %s: %s",
                project_id,
                result,
            )
            return False
        except Exception as e:
            self._logger.warning(
                "Exception enabling modules on project %s: %s",
                project_id,
                e,
            )
            return False

    def bulk_enable_project_modules(
        self,
        project_modules: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Enable modules for multiple projects in a single Rails call.

        Args:
            project_modules: List of dicts with keys:
                - project_id: int
                - modules: list[str]

        Returns:
            Dict with 'success': bool, 'processed': int, 'failed': int

        """
        if not project_modules:
            return {"success": True, "processed": 0, "failed": 0}

        # Build JSON data for Ruby. Validate each row defensively —
        # malformed rows (missing ``project_id``, non-numeric
        # ``project_id``) are recorded against ``failed`` and skipped,
        # so the method always returns its documented
        # ``{success, processed, failed}`` envelope rather than raising
        # ``KeyError`` / ``ValueError`` past the caller.
        data: list[dict[str, Any]] = []
        skipped_failures = 0
        for pm in project_modules:
            if not pm.get("modules"):
                continue
            try:
                row = {
                    "pid": int(pm["project_id"]),
                    "modules": [str(m) for m in pm["modules"]],
                }
            except (KeyError, TypeError, ValueError) as e:
                self._logger.warning(
                    "Skipping malformed row in bulk_enable_project_modules: %s (error: %s)",
                    pm,
                    e,
                )
                skipped_failures += 1
                continue
            data.append(row)

        if not data:
            if skipped_failures:
                return {
                    "success": False,
                    "processed": 0,
                    "failed": skipped_failures,
                }
            return {"success": True, "processed": 0, "failed": 0}

        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes.
        # Use Ruby heredoc with literal syntax (``<<-'X'``) to prevent both Ruby
        # interpolation and ``\u`` escape interpretation — the JSON contents
        # are data, not code.
        data_json = json.dumps(data, ensure_ascii=False)
        # Drop trailing ``.to_json`` — ``execute_json_query`` skips its
        # auto-wrap when it sees ``.to_json`` in the script and routes
        # to ``execute_query_to_json_file``, which itself wraps the
        # tail in ``.as_json``. With both in place we'd JSON-encode the
        # already-encoded string and Python would receive a string,
        # not a parsed dict.
        script = f"""
          require 'json'
          data = JSON.parse(<<-'J2O_DATA'
{data_json}
J2O_DATA
)

          results = {{ processed: 0, failed: 0, errors: [] }}

          data.each do |item|
            begin
              p = Project.find(item['pid'])
              names = p.enabled_module_names.map(&:to_s)
              desired = item['modules']
              added = false
              desired.each do |m|
                unless names.include?(m)
                  names << m
                  added = true
                end
              end
              if added
                p.enabled_module_names = names
                p.save!
              end
              results[:processed] += 1
            rescue => e
              results[:failed] += 1
              results[:errors] << {{ pid: item['pid'], error: e.message }}
            end
          end

          results[:success] = (results[:failed] == 0)
          results
        """
        try:
            result = self._client.execute_json_query(script)
            if isinstance(result, dict):
                # Roll the Python-side malformed-row failures into the
                # Ruby-side result envelope so callers see a single
                # ``failed`` count covering both validation and
                # Rails-side errors.
                if skipped_failures:
                    result["failed"] = int(result.get("failed", 0)) + skipped_failures
                    if result.get("success"):
                        result["success"] = False
                return result
            return {
                "success": False,
                "processed": 0,
                "failed": len(data) + skipped_failures,
                "error": str(result),
            }
        except Exception as e:
            self._logger.warning("Bulk enable project modules failed: %s", e)
            return {
                "success": False,
                "processed": 0,
                "failed": len(data) + skipped_failures,
                "error": str(e),
            }
