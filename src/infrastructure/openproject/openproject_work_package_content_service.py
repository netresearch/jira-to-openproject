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

Both activity helpers embed a provenance marker
``<!-- j2o:jira-comment-id:{id} -->`` at the end of each comment body
when ``jira_comment_id`` is supplied.  The marker is a CommonMark HTML
comment, which is stripped from rendered output by every compliant
Markdown renderer (including OpenProject's).  The bulk helper also
pre-fetches already-migrated markers for the target WPs and skips any
activity whose marker is already present, making comment migration
idempotent across re-runs.

``OpenProjectClient`` exposes the service via ``self.wp_content`` and
keeps thin delegators for the same method names so existing call sites
work unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from src.infrastructure.openproject.openproject_client import OpenProjectClient

# Provenance marker template embedded at the end of every migrated comment.
# CommonMark HTML comments (<!-- ... -->) are stripped from rendered output by
# all compliant Markdown renderers including OpenProject's CommonMark renderer.
# The marker is NOT visible to end users but IS present in the raw ``notes``
# column, making it grep-able inside Rails for idempotency checks.
_COMMENT_PROVENANCE_MARKER = "<!-- j2o:jira-comment-id:{jira_comment_id} -->"


def _build_comment_with_marker(comment_text: str, jira_comment_id: str | int | None) -> str:
    """Append the provenance marker to *comment_text* when *jira_comment_id* is set.

    If *jira_comment_id* is ``None`` or empty the comment is returned unchanged
    so callers that don't have a Jira comment id (e.g. legacy paths) still work.
    """
    if not jira_comment_id:
        return comment_text
    marker = _COMMENT_PROVENANCE_MARKER.format(jira_comment_id=jira_comment_id)
    # Append on a new line so it doesn't run into the last word of the comment.
    return f"{comment_text}\n{marker}"


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

        When ``activity_data`` contains a ``jira_comment_id`` key the method
        embeds a provenance marker ``<!-- j2o:jira-comment-id:{id} -->`` at
        the end of the comment body and skips creation if that marker is
        already present in an existing journal for the same WP (idempotency).

        Args:
            work_package_id: The work package ID
            activity_data: Dict with 'comment' key containing {'raw': 'comment text'}
                and optional 'user_id' key (int) to attribute the journal entry
                to a specific OpenProject user.  When ``user_id`` is absent or
                the user cannot be found the Rails default user is used as a
                fallback (mirrors ``bulk_create_work_package_activities``).
                Optional 'jira_comment_id' key (str/int) enables idempotency
                via the provenance marker.

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

        jira_comment_id = activity_data.get("jira_comment_id")
        comment_text = _build_comment_with_marker(comment_text, jira_comment_id)

        # Escape single quotes for Ruby
        escaped_comment = escape_ruby_single_quoted(comment_text)

        # Resolve the author user_id, defaulting to nil so Ruby falls back to
        # default_user (mirrors the bulk helper's ``users[item['user_id']] || default_user``).
        raw_user_id = activity_data.get("user_id")
        ruby_user_id = int(raw_user_id) if raw_user_id is not None else "nil"

        # Build the idempotency check expression for Ruby.
        # When jira_comment_id is present we grep existing journals for the
        # provenance marker; if found we skip (idempotent re-run).
        if jira_comment_id:
            escaped_marker = escape_ruby_single_quoted(
                _COMMENT_PROVENANCE_MARKER.format(jira_comment_id=jira_comment_id)
            )
            ruby_idempotency_check = (
                f"existing_journal = wp.journals.where(\"notes LIKE '%{escaped_marker}%'\").first\n"
                "          return {{ id: existing_journal.id, status: 'skipped' }} if existing_journal"
            )
        else:
            ruby_idempotency_check = ""

        # OpenProject 15+ requires using journal_notes/journal_user + save!
        script = f"""
        begin
          wp = WorkPackage.find({wp_id})
          default_user = User.current || User.find_by(admin: true)
          user_id = {ruby_user_id}
          user = user_id ? (User.find_by(id: user_id) || default_user) : default_user
          {ruby_idempotency_check}
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

    def fetch_migrated_comment_ids(
        self,
        wp_ids: list[int],
    ) -> set[tuple[int, str]]:
        """Return the set of (wp_id, jira_comment_id) pairs already in OP.

        Queries Rails for all journals on the given work packages whose
        ``notes`` column contains the j2o provenance marker pattern.
        Returns a set of ``(openproject_wp_id, jira_comment_id)`` tuples so
        the Python layer can skip already-migrated comments before sending
        the bulk-create payload.

        Args:
            wp_ids: OpenProject work package IDs to query.

        Returns:
            Set of (wp_id, jira_comment_id) tuples already present in OP.

        """
        if not wp_ids:
            return set()

        wp_ids_ruby = json.dumps([int(w) for w in wp_ids])
        # Extract the jira_comment_id from the marker using a Ruby regex.
        # The marker format is: <!-- j2o:jira-comment-id:{id} -->
        script = f"""
          require 'json'
          wp_ids = {wp_ids_ruby}
          marker_re = /<!--\\s*j2o:jira-comment-id:(\\S+?)\\s*-->/
          pairs = Journal
            .where(journable_type: 'WorkPackage', journable_id: wp_ids)
            .where("notes LIKE '%j2o:jira-comment-id:%'")
            .pluck(:journable_id, :notes)
            .filter_map do |wp_id, notes|
              m = marker_re.match(notes.to_s)
              m ? [wp_id, m[1]] : nil
            end
          pairs
        """
        try:
            result = self._client.execute_query_to_json_file(script)
            if isinstance(result, list):
                return {(int(wp_id), str(jira_id)) for wp_id, jira_id in result}
            return set()
        except Exception as e:
            self._logger.warning("Failed to fetch migrated comment IDs: %s", e)
            return set()

    def bulk_create_work_package_activities(
        self,
        activities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create multiple journal/activity entries (comments) in a single Rails call.

        Each entry may optionally carry a ``jira_comment_id`` key.  When
        present the provenance marker ``<!-- j2o:jira-comment-id:{id} -->`` is
        appended to the comment body and the Rails script skips any activity
        whose marker is already found in an existing journal for that WP
        (idempotency across re-runs).

        Args:
            activities: List of dicts with keys:
                - work_package_id: int
                - comment: str (the comment text)
                - user_id: int (optional, defaults to admin user)
                - jira_comment_id: str/int (optional, enables idempotency)

        Returns:
            Dict with 'success': bool, 'created': int, 'skipped': int, 'failed': int

        """
        if not activities:
            return {"success": True, "created": 0, "skipped": 0, "failed": 0}

        # Build JSON data for Ruby - embed provenance marker in comment body
        data = []
        for act in activities:
            comment = act.get("comment", "")
            if isinstance(comment, dict):
                comment = comment.get("raw", "")
            comment_str = str(comment)
            jira_comment_id = act.get("jira_comment_id")
            comment_with_marker = _build_comment_with_marker(comment_str, jira_comment_id)
            data.append(
                {
                    "work_package_id": int(act["work_package_id"]),
                    "comment": comment_with_marker,
                    "user_id": act.get("user_id"),
                    "jira_comment_id": str(jira_comment_id) if jira_comment_id is not None else None,
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

          results = {{ created: 0, skipped: 0, failed: 0, errors: [] }}
          default_user = User.current || User.find_by(admin: true)

          # Pre-fetch all referenced WPs and Users to avoid N+1 queries
          wp_ids = data.map {{ |d| d['work_package_id'] }}.compact.uniq
          user_ids = data.map {{ |d| d['user_id'] }}.compact.uniq
          wps = WorkPackage.where(id: wp_ids).index_by(&:id)
          users = User.where(id: user_ids).index_by(&:id)

          # Pre-fetch already-migrated provenance markers for idempotency.
          # Collect all (wp_id, jira_comment_id) pairs that already exist in
          # Journal#notes so we can skip them without a per-item DB query.
          marker_re = /<!--\\s*j2o:jira-comment-id:(\\S+?)\\s*-->/
          migrated_pairs = Journal
            .where(journable_type: 'WorkPackage', journable_id: wp_ids)
            .where("notes LIKE '%j2o:jira-comment-id:%'")
            .pluck(:journable_id, :notes)
            .each_with_object(Set.new) do |(wp_id, notes), set|
              m = marker_re.match(notes.to_s)
              set.add([wp_id, m[1]]) if m
            end

          data.each do |item|
            begin
              wp = wps[item['work_package_id']]
              unless wp
                results[:failed] += 1
                results[:errors] << {{ wp_id: item['work_package_id'], error: 'WorkPackage not found' }}
                next
              end

              # Idempotency: skip if this jira_comment_id already migrated for this WP
              jira_cid = item['jira_comment_id']
              if jira_cid && migrated_pairs.include?([item['work_package_id'], jira_cid])
                results[:skipped] += 1
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
            return {"success": False, "created": 0, "skipped": 0, "failed": len(activities), "error": str(result)}
        except Exception as e:
            self._logger.warning("Bulk create WP activities failed: %s", e)
            return {"success": False, "created": 0, "skipped": 0, "failed": len(activities), "error": str(e)}
