"""Work-package custom-field write helpers for the OpenProject Rails console.

Phase 2w of ADR-002 continues the openproject_client.py god-class
decomposition by extracting the two work-package CF write helpers
that don't fit the generic ``OpenProjectCustomFieldService``
(which owns CF schema operations) onto a focused service:

* ``bulk_set_wp_custom_field_values`` — set CF values on many work
  packages in one Rails round-trip.
* ``set_wp_last_update_date_by_keys`` — back-fill the ``J2O Last
  Update Date`` CF on work packages identified by Jira issue key,
  used by the change-detection pipeline.

The previous home for ``bulk_set_wp_custom_field_values`` was
``OpenProjectCustomFieldService`` but that service is about the CF
schema (creation, enablement, lookup, deletion). Per-work-package
value writes belong with the work-package writes.
``set_wp_last_update_date_by_keys`` previously sat directly on the
client.

``OpenProjectClient`` exposes the service via ``self.wp_cf`` and
keeps thin delegators for the same method names so existing call
sites (migrations, tests) work unchanged.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.clients.openproject_client import OpenProjectClient


class OpenProjectWorkPackageCustomFieldService:
    """Bulk CF writes + change-detection date back-fill on work packages."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    # ── bulk CF value write ───────────────────────────────────────────────

    def bulk_set_wp_custom_field_values(
        self,
        cf_values: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Set custom-field values for multiple work packages in one Rails call.

        Args:
            cf_values: List of dicts with keys ``work_package_id`` (int),
                ``custom_field_id`` (int), ``value`` (str).

        Returns:
            Dict with ``success`` (bool), ``updated`` (int), ``failed`` (int).
            Failure responses may also include ``error`` (str) on the
            Python validation/coercion path, or ``errors`` (list|dict)
            on the Rails-side result payload — callers should treat
            both keys as optional metadata rather than load-bearing
            structure.

        """
        if not cf_values:
            return {"success": True, "updated": 0, "failed": 0}

        # ``int()``/``str()`` coercion happens inside the try/except so a
        # malformed row (missing key, non-numeric id) returns the
        # documented ``{success: False, ...}`` envelope rather than
        # raising ``KeyError`` / ``ValueError`` past the caller.
        try:
            data = [
                {
                    "wp_id": int(cv["work_package_id"]),
                    "cf_id": int(cv["custom_field_id"]),
                    "value": str(cv["value"]),
                }
                for cv in cf_values
            ]
        except (KeyError, TypeError, ValueError) as e:
            self._logger.warning("Malformed cf_values row in bulk_set_wp_custom_field_values: %s", e)
            return {
                "success": False,
                "updated": 0,
                "failed": len(cf_values),
                "error": str(e),
            }

        # Use ensure_ascii=False to keep UTF-8 literal; <<-'J2O_DATA'
        # heredoc prevents Ruby interpolation and \u escape
        # interpretation — the JSON contents are data, not code.
        data_json = json.dumps(data, ensure_ascii=False)
        # Drop trailing ``.to_json`` — ``execute_query_to_json_file``
        # already wraps the script tail in ``.as_json`` on the Ruby
        # side, so an explicit ``.to_json`` would double-encode and
        # Python would receive a JSON string instead of a parsed dict.
        script = f"""
          require 'json'
          data = JSON.parse(<<-'J2O_DATA'
{data_json}
J2O_DATA
)

          results = {{ updated: 0, failed: 0, errors: [] }}

          # Pre-fetch all referenced WPs and CFs to avoid N+1 queries
          wp_ids = data.map {{ |d| d['wp_id'] }}.compact.uniq
          cf_ids = data.map {{ |d| d['cf_id'] }}.compact.uniq
          wps = WorkPackage.where(id: wp_ids).index_by(&:id)
          cfs = CustomField.where(id: cf_ids).index_by(&:id)

          data.each do |item|
            begin
              wp_id = item['wp_id']
              cf_id = item['cf_id']
              val = item['value']

              wp = wps[wp_id]
              cf = cfs[cf_id]
              if wp && cf
                cv = wp.custom_value_for(cf)
                if cv
                  cv.value = val
                  # ``save!`` raises into the outer rescue on
                  # validation failure so the row is counted under
                  # ``failed`` instead of silently flagged as
                  # ``updated`` despite the persist not happening.
                  cv.save!
                else
                  wp.custom_field_values = {{ cf.id => val }}
                end
                wp.save!
                results[:updated] += 1
              else
                results[:failed] += 1
                results[:errors] << {{ wp_id: wp_id, cf_id: cf_id, error: 'WorkPackage or CustomField not found' }}
              end
            rescue => e
              results[:failed] += 1
              results[:errors] << {{ wp_id: item['wp_id'], cf_id: item['cf_id'], error: e.message }}
            end
          end

          results[:success] = (results[:failed] == 0)
          results
        """
        try:
            result = self._client.execute_query_to_json_file(script)
            if isinstance(result, dict):
                return result
            return {"success": False, "updated": 0, "failed": len(cf_values), "error": str(result)}
        except Exception as e:
            self._logger.warning("Bulk set WP CF values failed: %s", e)
            return {"success": False, "updated": 0, "failed": len(cf_values), "error": str(e)}

    # ── change-detection back-fill ────────────────────────────────────────

    def set_wp_last_update_date_by_keys(
        self,
        project_id: int,
        jira_keys: list[str],
        date_str: str,
    ) -> dict[str, Any]:
        """Set ``J2O Last Update Date`` CF for work packages by Jira Issue Key.

        Args:
            project_id: OpenProject project ID to scope updates
            jira_keys: List of Jira issue keys to update
            date_str: Date string (YYYY-MM-DD)

        Returns:
            Result dict with counts.

        """
        if not jira_keys:
            return {"updated": 0, "examined": 0}

        # Lazy import to avoid the service ↔ client cycle at module
        # load time.
        from src.clients.openproject_client import escape_ruby_single_quoted

        try:
            # ``int(project_id)`` is moved inside the existing try/except
            # so a malformed ``project_id`` (non-numeric / None / etc.)
            # falls through to the documented error envelope rather
            # than raising ``ValueError`` / ``TypeError`` past the
            # caller.
            proj_id = int(project_id)
            # ``escape_ruby_single_quoted`` neutralises any quotes /
            # backslashes in ``date_str`` before embedding via single
            # quotes. The previous version interpolated ``date_str``
            # raw, which would have allowed Ruby string-literal
            # break-out for an attacker who controlled the date
            # string.
            escaped_date = escape_ruby_single_quoted(str(date_str))
            # Embed the issue-key list as a JSON literal inside a
            # single-quoted ``<<-'J2O_DATA'`` heredoc so Ruby parses
            # it as data via ``JSON.parse``. The previous version
            # round-tripped ``json.dumps(json.dumps(list(...)))`` to
            # build a JSON-string-of-a-JSON-array, then relied on
            # Ruby parsing the outer string literal back to the inner
            # JSON; that worked but was fragile and confusing. The
            # heredoc form is the same pattern used by the rest of
            # the bulk-write services.
            keys_payload = json.dumps([str(k) for k in jira_keys], ensure_ascii=False)
            ruby = f"""
              require 'json'
              proj_id = {proj_id}
              target_date = '{escaped_date}'
              keys = JSON.parse(<<-'J2O_KEYS'
{keys_payload}
J2O_KEYS
)
              updated = 0
              examined = 0
              key_cf = CustomField.find_by(type: 'WorkPackageCustomField', name: 'Jira Issue Key')
              last_cf = CustomField.find_by(type: 'WorkPackageCustomField', name: 'J2O Last Update Date')
              if key_cf && last_cf
                keys.each do |k|
                  examined += 1
                  begin
                    # Find WP id by custom value match in project
                    cv = CustomValue.where(customized_type: 'WorkPackage', custom_field_id: key_cf.id, value: k).first
                    if cv
                      # Ensure WP belongs to project
                      wp = WorkPackage.find_by(id: cv.customized_id, project_id: proj_id)
                      if wp
                        last_cv = CustomValue.find_or_initialize_by(customized_type: 'WorkPackage', customized_id: wp.id, custom_field_id: last_cf.id)
                        if last_cv.new_record? || last_cv.value.to_s.strip != target_date
                          last_cv.value = target_date
                          begin; last_cv.save!; updated += 1; rescue; end
                        end
                      end
                    end
                  rescue
                  end
                end
              end
              {{ updated: updated, examined: examined }}
            """
            # Drop trailing ``.to_json`` — ``execute_query_to_json_file``
            # already wraps the script tail in ``.as_json``; the
            # original method also relied on this contract.
            result = self._client.execute_query_to_json_file(ruby)
            return result if isinstance(result, dict) else {"updated": 0, "examined": 0}
        except Exception as e:
            self._logger.warning(
                "Failed to set J2O Last Update Date for project %s: %s",
                project_id,
                e,
            )
            return {"updated": 0, "examined": 0, "error": str(e)}
