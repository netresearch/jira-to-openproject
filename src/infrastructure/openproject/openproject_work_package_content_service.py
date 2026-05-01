"""Work-package content helpers for the OpenProject Rails console.

Phase 2s of ADR-002 continues the openproject_client.py god-class
decomposition by collecting work-package *content* helpers — i.e.
data attached to a work package after it exists, as opposed to the
work-package CRUD itself — onto a single focused service.

The service owns:

* **Description sections** — ``upsert_work_package_description_section``
  (single upsert of a marker-delimited section in
  ``WorkPackage#description``) and ``bulk_upsert_wp_description_sections``
  (batched variant driven by a JSON heredoc).
* **Activity journals** — ``create_work_package_activity`` (single
  journal note via ``journal_notes/journal_user`` + ``save!``) and
  ``bulk_create_work_package_activities`` (batched variant driven by
  a JSON heredoc with pre-fetched WP/User maps).

``OpenProjectClient`` exposes the service via ``self.wp_content`` and
keeps thin delegators for the same method names so existing call sites
work unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from src.infrastructure.openproject.openproject_client import OpenProjectClient


class OpenProjectWorkPackageContentService:
    """Description sections + activity journals for ``OpenProjectClient``."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    # ── description sections ─────────────────────────────────────────────

    def upsert_work_package_description_section(
        self,
        work_package_id: int,
        section_marker: str,
        content: str,
    ) -> bool:
        """Upsert a section in a work package's description.

        Args:
            work_package_id: The work package ID
            section_marker: The section title/marker (e.g., "Remote Links")
            content: The markdown content for the section

        Returns:
            True if successful, False otherwise

        """
        # Lazy import: ``escape_ruby_single_quoted`` lives on
        # ``openproject_client`` and importing it eagerly would create a
        # service ↔ client cycle at module load time.
        from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

        # Coerce to int at runtime — the type hint alone doesn't enforce
        # that callers actually pass an int, and a non-int value
        # (especially from untrusted upstream data) would otherwise
        # interpolate raw into the Ruby script.
        wp_id = int(work_package_id)
        # Escape content for Ruby single-quoted string
        safe_content = escape_ruby_single_quoted(content).replace("\n", "\\n")
        safe_marker = escape_ruby_single_quoted(section_marker)

        # Drop ``.to_json`` from the Ruby payload — the
        # ``execute_query_to_json_file`` Python wrapper already
        # serialises the final value via ``as_json``, so an explicit
        # ``.to_json`` here would double-encode and the Python side
        # would receive a string instead of a parsed dict.
        script = f"""
          wp = WorkPackage.find_by(id: {wp_id})
          if !wp
            {{ success: false, error: 'WorkPackage not found' }}
          else
            desc = wp.description || ''
            marker = '## {safe_marker}'
            content = '{safe_content}'

            # Find existing section
            escaped_marker = Regexp.escape('## {safe_marker}')
            section_regex = /\\n?#{{escaped_marker}}\\n[\\s\\S]*?(?=\\n## |\\z)/
            if desc.match?(section_regex)
              # Replace existing section
              new_section = "\\n" + marker + "\\n" + content
              desc = desc.gsub(section_regex, new_section)
            else
              # Append new section
              desc = desc.strip + "\\n\\n" + marker + "\\n" + content
            end

            wp.description = desc.strip
            if wp.save
              {{ success: true }}
            else
              {{ success: false, error: wp.errors.full_messages.join(', ') }}
            end
          end
        """
        try:
            result = self._client.execute_query_to_json_file(script)
            if isinstance(result, dict):
                return result.get("success", False)
            return False
        except Exception as e:
            self._logger.warning("Failed to upsert WP description section: %s", e)
            return False

    def bulk_upsert_wp_description_sections(
        self,
        sections: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Upsert description sections for multiple work packages in a single Rails call.

        Args:
            sections: List of dicts with keys:
                - work_package_id: int
                - section_marker: str
                - content: str

        Returns:
            Dict with 'success': bool, 'updated': int, 'failed': int

        """
        if not sections:
            return {"success": True, "updated": 0, "failed": 0}

        # Build JSON data for Ruby
        data = []
        for s in sections:
            data.append(
                {
                    "wp_id": int(s["work_package_id"]),
                    "marker": str(s["section_marker"]),
                    "content": str(s["content"]),
                },
            )

        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        data_json = json.dumps(data, ensure_ascii=False)
        # Use Ruby heredoc with literal syntax (<<-'X') to prevent \u escape interpretation
        script = f"""
          require 'json'
          data = JSON.parse(<<-'J2O_DATA'
{data_json}
J2O_DATA
)

          results = {{ updated: 0, failed: 0, errors: [] }}

          data.each do |item|
            begin
              wp_id = item['wp_id']
              marker_text = item['marker']
              content = item['content']

              wp = WorkPackage.find_by(id: wp_id)
              if !wp
                results[:failed] += 1
                results[:errors] << {{ wp_id: wp_id, error: 'WorkPackage not found' }}
                next
              end

              desc = wp.description || ''
              marker = '## ' + marker_text

              # Find existing section using regex
              section_regex = Regexp.new("\\n?" + Regexp.escape(marker) + "\\n[\\s\\S]*?(?=\\n## |\\z)")
              if desc.match?(section_regex)
                new_section = "\\n" + marker + "\\n" + content
                desc = desc.gsub(section_regex, new_section)
              else
                desc = desc.strip + "\\n\\n" + marker + "\\n" + content
              end

              wp.description = desc.strip
              if wp.save
                results[:updated] += 1
              else
                results[:failed] += 1
                results[:errors] << {{ wp_id: wp_id, error: wp.errors.full_messages.join(', ') }}
              end
            rescue => e
              results[:failed] += 1
              results[:errors] << {{ wp_id: item['wp_id'], error: e.message }}
            end
          end

          results[:success] = (results[:failed] == 0)
          results
        """
        # See ``upsert_work_package_description_section`` — drop the
        # ``.to_json`` to avoid double-encoding through
        # ``execute_query_to_json_file``.
        try:
            result = self._client.execute_query_to_json_file(script)
            if isinstance(result, dict):
                return result
            return {"success": False, "updated": 0, "failed": len(sections), "error": str(result)}
        except Exception as e:
            self._logger.warning("Bulk upsert WP description sections failed: %s", e)
            return {"success": False, "updated": 0, "failed": len(sections), "error": str(e)}

    # ── activity journals ────────────────────────────────────────────────

    def create_work_package_activity(
        self,
        work_package_id: int,
        activity_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Create a journal/activity (comment) on a work package.

        Args:
            work_package_id: The work package ID
            activity_data: Dict with 'comment' key containing {'raw': 'comment text'}

        Returns:
            Created journal data or None on failure

        """
        # Lazy import: ``escape_ruby_single_quoted`` lives on
        # ``openproject_client`` and importing it eagerly would create a
        # service ↔ client cycle at module load time.
        from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

        # Coerce to int at runtime — see ``upsert_work_package_description_section``.
        wp_id = int(work_package_id)

        comment = activity_data.get("comment", {})
        if isinstance(comment, dict):
            comment_text = comment.get("raw", "")
        else:
            comment_text = str(comment)

        if not comment_text:
            return None

        # Escape single quotes for Ruby
        escaped_comment = escape_ruby_single_quoted(comment_text)

        # OpenProject 15+ requires using journal_notes/journal_user + save!
        script = f"""
        begin
          wp = WorkPackage.find({wp_id})
          user = User.current || User.find_by(admin: true)
          wp.journal_notes = '{escaped_comment}'
          wp.journal_user = user
          wp.save!
          {{ id: wp.journals.last.id, status: 'created' }}
        rescue => e
          {{ error: e.message }}
        end
        """
        try:
            result = self._client.execute_query_to_json_file(script)
            if isinstance(result, dict) and not result.get("error"):
                return result
            self._logger.debug("Failed to create activity: %s", result)
            return None
        except Exception as e:
            self._logger.debug("Failed to create activity for WP#%d: %s", work_package_id, e)
            return None

    def bulk_create_work_package_activities(
        self,
        activities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create multiple journal/activity entries (comments) in a single Rails call.

        Args:
            activities: List of dicts with keys:
                - work_package_id: int
                - comment: str (the comment text)
                - user_id: int (optional, defaults to admin user)

        Returns:
            Dict with 'success': bool, 'created': int, 'failed': int

        """
        if not activities:
            return {"success": True, "created": 0, "failed": 0}

        # Build JSON data for Ruby - escape properly
        data = []
        for act in activities:
            comment = act.get("comment", "")
            if isinstance(comment, dict):
                comment = comment.get("raw", "")
            data.append(
                {
                    "work_package_id": int(act["work_package_id"]),
                    "comment": str(comment),
                    "user_id": act.get("user_id"),
                },
            )

        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        data_json = json.dumps(data, ensure_ascii=False)
        # Use Ruby heredoc with literal syntax (<<-'X') to prevent \u escape interpretation
        # NOTE: OpenProject 15+ requires using journal_notes/journal_user + save!
        # instead of direct journals.create! to properly set validity_period and data_type
        script = f"""
          require 'json'
          data = JSON.parse(<<-'J2O_DATA'
{data_json}
J2O_DATA
)

          results = {{ created: 0, failed: 0, errors: [] }}
          default_user = User.current || User.find_by(admin: true)

          # Pre-fetch all referenced WPs and Users to avoid N+1 queries
          wp_ids = data.map {{ |d| d['work_package_id'] }}.compact.uniq
          user_ids = data.map {{ |d| d['user_id'] }}.compact.uniq
          wps = WorkPackage.where(id: wp_ids).index_by(&:id)
          users = User.where(id: user_ids).index_by(&:id)

          data.each do |item|
            begin
              wp = wps[item['work_package_id']]
              unless wp
                results[:failed] += 1
                results[:errors] << {{ wp_id: item['work_package_id'], error: 'WorkPackage not found' }}
                next
              end

              user = item['user_id'] ? (users[item['user_id']] || default_user) : default_user
              user ||= default_user

              comment_text = item['comment'].to_s
              next if comment_text.empty?

              # OpenProject 15+ journal creation - use journal_notes/journal_user
              wp.journal_notes = comment_text
              wp.journal_user = user
              wp.save!
              results[:created] += 1
            rescue => e
              results[:failed] += 1
              results[:errors] << {{ wp_id: item['work_package_id'], error: e.message }}
            end
          end

          results[:success] = (results[:failed] == 0)
          results
        """
        # Drop ``.to_json`` (see other methods in this file) so
        # ``execute_query_to_json_file`` can serialise via ``as_json``
        # without double-encoding.
        try:
            result = self._client.execute_query_to_json_file(script)
            if isinstance(result, dict):
                return result
            return {"success": False, "created": 0, "failed": len(activities), "error": str(result)}
        except Exception as e:
            self._logger.warning("Bulk create WP activities failed: %s", e)
            return {"success": False, "created": 0, "failed": len(activities), "error": str(e)}
