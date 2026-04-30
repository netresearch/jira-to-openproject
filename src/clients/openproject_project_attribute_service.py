"""Project-attribute write helpers for the OpenProject Rails console.

Phase 2t of ADR-002 continues the openproject_client.py god-class
decomposition by collecting Project custom-field upsert / rename /
read helpers onto a single focused service. All five methods deal
with ``ProjectCustomField`` (a ``CustomField`` STI subclass) plus its
``ProjectCustomFieldProjectMapping`` enablement row and the
``CustomValue`` row that stores the actual value for a given
project. They share enough Ruby plumbing — section bootstrap, mapping
enablement, ``CustomValue.find_or_initialize_by`` upsert — that
keeping them together as a cohesive subsystem matches the existing
"per-domain service" pattern from earlier phases.

The service owns:

* ``upsert_project_origin_attributes`` — embed the J2O origin block
  (``<!-- J2O_ORIGIN_START -->`` ... ``<!-- J2O_ORIGIN_END -->``)
  into the project description, replacing it deterministically on
  re-runs.
* ``upsert_project_attribute`` — single Project CF upsert: ensure
  the section + ``ProjectCustomField`` exist, enable the mapping for
  the project, then upsert the ``CustomValue`` row.
* ``bulk_upsert_project_attributes`` — same upsert flow as above but
  driven by a JSON heredoc so a single Rails round-trip handles many
  projects.
* ``rename_project_attribute`` — rename a ``ProjectCustomField`` by
  name; idempotent (returns true when already at the new name).
* ``get_project_wp_cf_snapshot`` — read a project's WP custom-field
  state ("J2O Origin Key" + "J2O Last Update Date") plus
  ``updated_at`` for incremental-migration deltas.

``OpenProjectClient`` exposes the service via ``self.project_attributes``
and keeps thin delegators for the same method names so existing call
sites work unchanged.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from src.clients.exceptions import QueryExecutionError

if TYPE_CHECKING:
    from src.clients.openproject_client import OpenProjectClient


class OpenProjectProjectAttributeService:
    """Project CF upsert / rename / read helpers for ``OpenProjectClient``."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    def upsert_project_origin_attributes(
        self,
        project_id: int,
        *,
        origin_system: str,
        project_key: str,
        external_id: str | None = None,
        external_url: str | None = None,
    ) -> bool:
        """Persist origin metadata into Project attributes (description) idempotently.

        We embed a small, machine-readable block between HTML comment markers so we can
        replace it deterministically on subsequent runs without duplicating data.

        Args:
            project_id: OpenProject project ID
            origin_system: e.g. "jira"
            project_key: upstream project key (e.g. "SRVEP")
            external_id: upstream immutable project id (stringified)
            external_url: upstream canonical URL

        Returns:
            True on success, False otherwise.

        """
        # Lazy import to avoid the service ↔ client cycle at module load time.
        from src.clients.openproject_client import escape_ruby_single_quoted

        # The whole body, including the ``int(project_id)`` runtime
        # coercion and the f-string interpolation that escapes user
        # strings, lives inside the existing try/except so a malformed
        # ``project_id`` (``str``-that-isn't-numeric, ``None``, etc.)
        # falls through to the documented ``return False`` contract
        # rather than raising ``ValueError`` / ``TypeError`` past the
        # caller.
        try:
            pid = int(project_id)

            # Escape braces in f-string; Ruby string content uses literal markers.
            marker_start = "<!-- J2O_ORIGIN_START -->"
            marker_end = "<!-- J2O_ORIGIN_END -->"
            payload = (
                f"system={escape_ruby_single_quoted(origin_system)};"
                f"key={escape_ruby_single_quoted(project_key)};"
                f"id={escape_ruby_single_quoted(external_id or '')};"
                f"url={escape_ruby_single_quoted(external_url or '')}"
            )
            # Ruby script to insert/replace the origin block in description.
            # Drop ``.to_json`` from the final expression — the
            # ``execute_query_to_json_file`` Python wrapper already
            # serialises the final value via ``as_json``, so an
            # explicit ``.to_json`` here would double-encode and the
            # Python side would receive a string instead of a parsed
            # dict.
            script = (
                "project = Project.find(%d)\n" % pid
                + f"marker_start = '{marker_start}'\n"
                + f"marker_end = '{marker_end}'\n"
                + f"payload = '{payload}'.dup\n"
                + "desc = project.description.to_s\n"
                + "block = ['\\n', marker_start, '\\n', payload, '\\n', marker_end, '\\n'].join\n"
                + "start_idx = desc.index(marker_start)\n"
                + "end_idx = desc.index(marker_end)\n"
                + "if start_idx && end_idx && end_idx > start_idx\n"
                + "  pre = desc[0...start_idx]\n"
                + "  post = desc[(end_idx + marker_end.length)..-1] || ''\n"
                + "  desc = pre + block + post\n"
                + "else\n"
                + "  desc = desc + block\n"
                + "end\n"
                + "project.update_columns(description: desc)\n"
                + "{ success: true }\n"
            )
            result = self._client.execute_query_to_json_file(script)
            return bool(isinstance(result, dict) and result.get("success"))
        except Exception as e:
            self._logger.warning("Failed to upsert project origin attributes for %s: %s", project_id, e)
            return False

    def upsert_project_attribute(
        self,
        project_id: int,
        *,
        name: str,
        value: str,
        field_format: str = "string",
    ) -> bool:
        """Create/enable a Project attribute (ProjectCustomField) and set its value for a project.

        This uses ProjectCustomField (STI on custom_fields) and ProjectCustomFieldProjectMapping,
        storing the actual value in CustomValue for customized_type='Project'.
        """
        # Lazy import to avoid the service ↔ client cycle at module load time.
        from src.clients.openproject_client import escape_ruby_single_quoted

        # Move ``int(project_id)`` and the f-string interpolation
        # inside the existing try/except so a malformed
        # ``project_id`` (non-numeric / None / etc.) returns
        # ``False`` per the documented contract instead of raising
        # ``ValueError`` / ``TypeError`` past the caller.
        try:
            pid = int(project_id)

            # Drop ``.to_json`` from the final expression — see
            # ``upsert_project_origin_attributes`` for the rationale.
            ruby = f"""
          pid = {pid}
          name = '{escape_ruby_single_quoted(name)}'.dup
          fmt  = '{escape_ruby_single_quoted(field_format)}'.dup
          val  = '{escape_ruby_single_quoted(value)}'.dup

          # Ensure attribute definition
          # Section is required for project attributes
          begin
            section = CustomFieldSection.find_or_create_by!(type: 'ProjectCustomFieldSection', name: 'J2O Origin')
          rescue => e
            section = nil
          end

          cf = ProjectCustomField.find_by(name: name)
          if !cf
            cf = ProjectCustomField.new(
              name: name,
              field_format: fmt,
              is_required: false,
              is_filter: false,
              searchable: true,
              editable: true,
              admin_only: false
            )
            begin
              cf.custom_field_section_id = section.id if section && cf.respond_to?(:custom_field_section_id=)
            rescue
            end
            begin
              cf.is_for_all = false if cf.respond_to?(:is_for_all=)
            rescue
            end
            cf.save!
          end

          # If cf existed without section, attach it
          if (!cf.custom_field_section_id || cf.custom_field_section_id.nil?) && section
            begin
              cf.update!(custom_field_section_id: section.id)
            rescue
            end
          end

          # Ensure mapping enabled for this project
          ProjectCustomFieldProjectMapping.find_or_create_by!(project_id: pid, custom_field_id: cf.id)

          # Upsert value
          cv = CustomValue.find_or_initialize_by(customized_type: 'Project', customized_id: pid, custom_field_id: cf.id)
          cv.value = val
          cv.save!

          {{ success: true, custom_field_id: cf.id, value: cv.value }}
        """
            result = self._client.execute_query_to_json_file(ruby)
            return bool(isinstance(result, dict) and result.get("success"))
        except Exception as e:
            self._logger.warning("Failed to upsert project attribute %s for %s: %s", name, project_id, e)
            return False

    def bulk_upsert_project_attributes(
        self,
        attributes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Bulk upsert project attributes in a single Rails call.

        Args:
            attributes: List of dicts with keys:
                - project_id: int
                - name: str
                - value: str
                - field_format: str (default 'string')

        Returns:
            Dict with 'success': bool, 'processed': int, 'failed': int

        """
        if not attributes:
            return {"success": True, "processed": 0, "failed": 0}

        # Build JSON data for Ruby. Validate each row defensively —
        # malformed rows (missing ``project_id`` / ``name``,
        # non-numeric ``project_id``) are recorded against ``failed``
        # and skipped, so the method always returns its documented
        # ``{success, processed, failed}`` envelope rather than
        # raising ``KeyError`` / ``ValueError`` past the caller.
        data: list[dict[str, Any]] = []
        skipped_failures = 0
        for attr in attributes:
            try:
                row = {
                    "pid": int(attr["project_id"]),
                    "name": str(attr["name"]),
                    "value": str(attr.get("value", "")),
                    "fmt": str(attr.get("field_format", "string")),
                }
            except (KeyError, TypeError, ValueError) as e:
                self._logger.warning(
                    "Skipping malformed attribute row in bulk_upsert_project_attributes: %s (error: %s)",
                    attr,
                    e,
                )
                skipped_failures += 1
                continue
            data.append(row)

        if not data:
            return {"success": False, "processed": 0, "failed": skipped_failures}

        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        # that Ruby misinterprets as invalid Unicode escape sequences.
        # The single-quoted heredoc tag (<<-'J2O_DATA') prevents Ruby
        # interpolation, so emitting the JSON payload directly here is
        # safe — JSON.parse on the Ruby side treats it as data, not code.
        data_json = json.dumps(data, ensure_ascii=False)
        # Drop ``.to_json`` from the final expression — see
        # ``upsert_project_origin_attributes`` for the rationale.
        ruby = f"""
          require 'json'
          data = JSON.parse(<<-'J2O_DATA'
{data_json}
J2O_DATA
)

          # Ensure section exists once
          section = nil
          begin
            section = CustomFieldSection.find_or_create_by!(type: 'ProjectCustomFieldSection', name: 'J2O Origin')
          rescue => e
          end

          # Cache custom fields by name
          cf_cache = {{}}

          results = {{ processed: 0, failed: 0, errors: [] }}

          data.each do |item|
            begin
              pid = item['pid']
              name = item['name']
              fmt = item['fmt']
              val = item['value']

              # Get or create custom field
              cf = cf_cache[name]
              if !cf
                cf = ProjectCustomField.find_by(name: name)
                if !cf
                  cf = ProjectCustomField.new(
                    name: name,
                    field_format: fmt,
                    is_required: false,
                    is_filter: false,
                    searchable: true,
                    editable: true,
                    admin_only: false
                  )
                  cf.custom_field_section_id = section.id if section && cf.respond_to?(:custom_field_section_id=) rescue nil
                  cf.is_for_all = false if cf.respond_to?(:is_for_all=) rescue nil
                  cf.save!
                end
                # Attach section if needed
                if section && (!cf.custom_field_section_id || cf.custom_field_section_id.nil?)
                  cf.update!(custom_field_section_id: section.id) rescue nil
                end
                cf_cache[name] = cf
              end

              # Ensure mapping for project
              ProjectCustomFieldProjectMapping.find_or_create_by!(project_id: pid, custom_field_id: cf.id)

              # Upsert value
              cv = CustomValue.find_or_initialize_by(customized_type: 'Project', customized_id: pid, custom_field_id: cf.id)
              cv.value = val
              cv.save!

              results[:processed] += 1
            rescue => e
              results[:failed] += 1
              results[:errors] << {{ pid: item['pid'], name: item['name'], error: e.message }}
            end
          end

          results[:success] = (results[:failed] == 0)
          results
        """
        try:
            result = self._client.execute_query_to_json_file(ruby)
            if isinstance(result, dict):
                # Roll the Python-side malformed-row failures into the
                # Ruby-side result envelope so callers see a single
                # ``failed`` count covering both validation and
                # Rails-side errors.
                if skipped_failures:
                    result["failed"] = int(result.get("failed", 0)) + skipped_failures
                    if result.get("success") and skipped_failures:
                        result["success"] = False
                return result
            return {
                "success": False,
                "processed": 0,
                "failed": len(attributes),
                "error": str(result),
            }
        except Exception as e:
            self._logger.warning("Bulk upsert project attributes failed: %s", e)
            return {
                "success": False,
                "processed": 0,
                "failed": len(attributes),
                "error": str(e),
            }

    def rename_project_attribute(self, *, old_name: str, new_name: str) -> bool:
        """Rename a Project attribute (ProjectCustomField) if it exists.

        Returns True if renamed or already at new_name; False if missing or failed.
        """
        # Lazy import to avoid the service ↔ client cycle at module load time.
        from src.clients.openproject_client import escape_ruby_single_quoted

        # Drop ``.to_json`` from both branches — see
        # ``upsert_project_origin_attributes`` for the rationale.
        ruby = f"""
          old_name = '{escape_ruby_single_quoted(old_name)}'.dup
          new_name = '{escape_ruby_single_quoted(new_name)}'.dup
          cf = ProjectCustomField.find_by(name: old_name)
          if cf
            cf.update!(name: new_name)
            {{ success: true, id: cf.id }}
          else
            cf2 = ProjectCustomField.find_by(name: new_name)
            {{ success: !!cf2, id: (cf2 ? cf2.id : nil) }}
          end
        """
        try:
            result = self._client.execute_query_to_json_file(ruby)
            return bool(isinstance(result, dict) and result.get("success"))
        except Exception as e:
            self._logger.warning("Failed to rename project attribute %s -> %s: %s", old_name, new_name, e)
            return False

    def get_project_wp_cf_snapshot(self, project_id: int) -> list[dict[str, Any]]:
        """Return snapshot of WorkPackages in a project with Jira CFs and updated_at.

        Each item: { id, updated_at, jira_issue_key, jira_migration_date }

        Raises:
            QueryExecutionError: If the snapshot query fails or
                ``project_id`` cannot be coerced to ``int``. Wrapping
                the coercion error here keeps the method's
                documented exception type uniform — callers only have
                to catch one thing.

        """
        try:
            pid = int(project_id)
        except (TypeError, ValueError) as e:
            msg = f"Invalid project_id for snapshot: {project_id!r}"
            raise QueryExecutionError(msg) from e

        ruby = f"""
          cf_key = CustomField.find_by(type: 'WorkPackageCustomField', name: 'J2O Origin Key')
          cf_mig = CustomField.find_by(type: 'WorkPackageCustomField', name: 'J2O Last Update Date')

          # Pre-load custom values for all WPs in this project for efficiency
          wp_ids = WorkPackage.where(project_id: {pid}).pluck(:id)

          key_values = {{}}
          mig_values = {{}}

          if cf_key
            CustomValue.where(custom_field_id: cf_key.id, customized_type: 'WorkPackage', customized_id: wp_ids)
              .each {{ |cv| key_values[cv.customized_id] = cv.value }}
          end

          if cf_mig
            CustomValue.where(custom_field_id: cf_mig.id, customized_type: 'WorkPackage', customized_id: wp_ids)
              .each {{ |cv| mig_values[cv.customized_id] = cv.value }}
          end

          WorkPackage.where(project_id: {pid}).select(:id, :updated_at).map do |wp|
            {{ id: wp.id, updated_at: (wp.updated_at&.utc&.iso8601), jira_issue_key: key_values[wp.id], jira_migration_date: mig_values[wp.id] }}
          end
        """
        data = self._client.execute_large_query_to_json_file(ruby, timeout=120)
        if not isinstance(data, list):
            msg = "Invalid snapshot from OpenProject"
            raise QueryExecutionError(msg)
        return data
