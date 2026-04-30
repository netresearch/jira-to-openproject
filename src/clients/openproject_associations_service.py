"""Watcher + relation associations for the OpenProject Rails console.

Phase 2o of ADR-002 continues the openproject_client.py god-class
decomposition by collecting two small, cohesive subsystems onto a
single focused service: work-package watchers and work-package
relations. Both deal with associating principals or other work
packages to a work package, both follow the same find / single-add /
bulk-add pattern, and both are small enough that splitting them into
two services would just multiply ceremony without separating
concerns.

The service owns:

* **Watchers** — ``find_watcher`` (existence check),
  ``add_watcher`` (idempotent single add), ``bulk_add_watchers``
  (Rails ``insert_all`` with pre-fetched validity sets and
  conflict-skipping).
* **Relations** — ``find_relation`` (file-based read), ``create_relation``
  (idempotent single create), ``bulk_create_relations`` (per-row
  ``find / save`` loop with structured success/failure result and
  symmetric ``relates`` dedupe).

``OpenProjectClient`` exposes the service via ``self.associations``
and keeps thin delegators for the same method names so existing call
sites work unchanged.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from src.clients.exceptions import QueryExecutionError

if TYPE_CHECKING:
    from src.clients.openproject_client import OpenProjectClient


class OpenProjectAssociationsService:
    """Watchers + relations helpers for ``OpenProjectClient``."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    # ── watchers ─────────────────────────────────────────────────────────

    def find_watcher(self, work_package_id: int, user_id: int) -> dict[str, Any] | None:
        """Find a watcher for a work package and user if it exists."""
        # ``execute_query`` returns the raw console string, so the
        # previous ``isinstance(res, dict)`` guard always saw ``False``
        # and this method always returned ``None``. Switch to
        # ``execute_query_to_json_file`` so the Ruby hash is parsed into
        # a real Python dict.
        query = (
            "Watcher.where(watchable_type: 'WorkPackage', watchable_id: %d, user_id: %d).limit(1).map do |w| "
            "{ id: w.id, user_id: w.user_id, watchable_id: w.watchable_id } end.first"
        ) % (work_package_id, user_id)
        try:
            res = self._client.execute_query_to_json_file(query)
            if isinstance(res, dict) and res:
                return res
            return None
        except Exception as e:
            msg = "Failed to query watcher."
            raise QueryExecutionError(msg) from e

    def add_watcher(self, work_package_id: int, user_id: int) -> bool:
        """Idempotently add a watcher to the work package.

        Returns True if the watcher exists or was created successfully.
        """
        try:
            if self.find_watcher(work_package_id, user_id):
                return True
        except Exception:
            # Proceed to attempt create even if find failed
            pass

        # Drop the ``.to_json`` calls in the Ruby script:
        # ``execute_query_to_json_file`` already serialises the final
        # expression to JSON. Returning a JSON *string* instead would
        # double-encode and the resulting Python value would always be
        # a non-empty string — so the previous ``isinstance(created, dict)``
        # branch was unreachable and the fallback ``bool(created)`` was
        # always ``True`` regardless of whether the watcher actually
        # was created.
        script = (
            "wp = WorkPackage.find(%d); u = User.find(%d); "
            "if !Watcher.exists?(watchable_type: 'WorkPackage', watchable_id: wp.id, user_id: u.id); "
            "w = Watcher.new(user: u, watchable: wp); w.save!; {created: true}; else; {created: false}; end"
        ) % (work_package_id, user_id)
        try:
            created = self._client.execute_query_to_json_file(script)
            # Either branch of the Ruby script returns a dict, and both
            # mean the watcher now exists on the work package — the
            # ``created`` key only distinguishes "newly inserted" from
            # "already existed". This method's contract is "watcher
            # exists OR was created", so both dict shapes are success.
            return isinstance(created, dict)
        except Exception as e:
            msg = "Failed to add watcher."
            raise QueryExecutionError(msg) from e

    def bulk_add_watchers(
        self,
        watchers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Add multiple watchers in a single Rails call.

        Args:
            watchers: List of dicts with keys:
                - work_package_id: int
                - user_id: int

        Returns:
            Dict with 'success': bool, 'created': int, 'skipped': int, 'failed': int

        """
        if not watchers:
            return {"success": True, "created": 0, "skipped": 0, "failed": 0}

        # Build JSON data for Ruby
        data = []
        for w in watchers:
            data.append(
                {
                    "wp_id": int(w["work_package_id"]),
                    "user_id": int(w["user_id"]),
                },
            )

        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        # that Ruby misinterprets as invalid Unicode escape sequences
        data_json = json.dumps(data, ensure_ascii=False)
        # Use Ruby heredoc with literal syntax (<<-'X') to prevent \u escape interpretation
        # Optimized: Use bulk insert with conflict handling for speed
        script = f"""
          require 'json'
          data = JSON.parse(<<-'J2O_DATA'
{data_json}
J2O_DATA
)

          results = {{ created: 0, skipped: 0, failed: 0, errors: [] }}

          # Build bulk insert data
          now = Time.current
          records = []
          wp_ids = data.map {{ |d| d['wp_id'] }}.uniq
          user_ids = data.map {{ |d| d['user_id'] }}.uniq

          # Pre-fetch valid IDs for faster lookup
          valid_wps = WorkPackage.where(id: wp_ids).pluck(:id).to_set
          valid_users = User.where(id: user_ids).pluck(:id).to_set
          existing = Watcher.where(watchable_type: 'WorkPackage', watchable_id: wp_ids, user_id: user_ids)
                           .pluck(:watchable_id, :user_id).to_set

          data.each do |item|
            wp_id = item['wp_id']
            user_id = item['user_id']

            unless valid_wps.include?(wp_id) && valid_users.include?(user_id)
              results[:failed] += 1
              results[:errors] << {{ wp_id: wp_id, user_id: user_id, error: 'WorkPackage or User not found' }}
              next
            end

            if existing.include?([wp_id, user_id])
              results[:skipped] += 1
            else
              records << {{ watchable_type: 'WorkPackage', watchable_id: wp_id, user_id: user_id }}
            end
          end

          # Bulk insert new watchers
          if records.any?
            begin
              Watcher.insert_all(records)
              results[:created] = records.size
            rescue => e
              results[:failed] = records.size
              results[:errors] << {{ error: e.message }}
            end
          end

          results[:success] = (results[:failed] == 0)
          results
        """
        # Drop the ``.to_json`` from the Ruby script:
        # ``execute_query_to_json_file`` already serialises the final
        # expression via ``as_json``, so returning an explicit JSON
        # string here would double-encode and the Python side would
        # see a *string* (the JSON repr of the hash) rather than a
        # parsed dict.
        try:
            result = self._client.execute_query_to_json_file(script)
            if isinstance(result, dict):
                return result
            return {"success": False, "created": 0, "skipped": 0, "failed": len(watchers), "error": str(result)}
        except Exception as e:
            self._logger.warning("Bulk add watchers failed: %s", e)
            return {"success": False, "created": 0, "skipped": 0, "failed": len(watchers), "error": str(e)}

    # ── relations ────────────────────────────────────────────────────────

    def find_relation(
        self,
        from_work_package_id: int,
        to_work_package_id: int,
    ) -> dict[str, Any] | None:
        """Find a relation between two work packages if it exists.

        Returns minimal relation info or None.
        """
        query = (
            "Relation.where(from_id: %d, to_id: %d).limit(1).map do |r| "
            "{ id: r.id, relation_type: r.relation_type, from_id: r.from_id, to_id: r.to_id } end.first"
            % (from_work_package_id, to_work_package_id)
        )
        # Generate a unique container filename so two concurrent
        # ``find_relation`` calls (e.g. across the WP-relation
        # migration's per-row loop) cannot race on the same
        # ``/tmp/j2o_find_relation.json`` path and read each other's
        # half-written results.
        container_file = self._client._generate_unique_temp_filename("find_relation")
        try:
            result = self._client.execute_large_query_to_json_file(
                query,
                container_file=container_file,
                timeout=30,
            )
            return result if isinstance(result, dict) else None
        except Exception as e:
            self._logger.warning("Failed to find relation: %s", e)
            return None

    def create_relation(
        self,
        from_work_package_id: int,
        to_work_package_id: int,
        relation_type: str,
    ) -> dict[str, Any] | None:
        """Create a relation idempotently between two work packages.

        On success returns dict with id and status ('created' or 'exists'); otherwise None.
        """
        # Lazy import: keeps the service ↔ client cycle out of module-load time.
        from src.clients.openproject_client import escape_ruby_single_quoted

        script = f"""
        begin
          from_wp = WorkPackage.find_by(id: {from_work_package_id})
          to_wp = WorkPackage.find_by(id: {to_work_package_id})
          if !from_wp || !to_wp
            {{ error: 'NotFound' }}
          else
            rel = Relation.where(from_id: {from_work_package_id}, to_id: {to_work_package_id}, relation_type: '{escape_ruby_single_quoted(relation_type)}').first
            if rel
              {{ id: rel.id, status: 'exists', relation_type: rel.relation_type, from_id: rel.from_id, to_id: rel.to_id }}
            else
              rel = Relation.create(from: from_wp, to: to_wp, relation_type: '{escape_ruby_single_quoted(relation_type)}')
              if rel.persisted?
                {{ id: rel.id, status: 'created', relation_type: rel.relation_type, from_id: rel.from_id, to_id: rel.to_id }}
              else
                {{ error: 'Validation failed', errors: rel.errors.full_messages }}
              end
            end
          end
        rescue => e
          {{ error: 'Creation failed', message: e.message }}
        end
        """
        try:
            result = self._client.execute_query_to_json_file(script)
            if isinstance(result, dict):
                if result.get("error"):
                    self._logger.warning("Relation creation failed: %s", result)
                    return None
                return result
            self._logger.warning("Unexpected relation creation result: %s", result)
            return None
        except Exception as e:
            msg = f"Failed to create relation: {e}"
            raise QueryExecutionError(msg) from e

    def bulk_create_relations(
        self,
        relations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create multiple relations in a single Rails call.

        Args:
            relations: List of dicts with keys:
                - from_id: int (from work package ID)
                - to_id: int (to work package ID)
                - relation_type: str (relates, duplicates, blocks, precedes, follows)

        Returns:
            Dict with 'success': bool, 'created': int, 'skipped': int, 'failed': int

        """
        if not relations:
            return {"success": True, "created": 0, "skipped": 0, "failed": 0}

        # Build JSON data for Ruby
        data = []
        for rel in relations:
            data.append(
                {
                    "from_id": int(rel["from_id"]),
                    "to_id": int(rel["to_id"]),
                    "type": str(rel["relation_type"]),
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

          results = {{ created: 0, skipped: 0, failed: 0, errors: [] }}

          data.each do |item|
            begin
              from_id = item['from_id']
              to_id = item['to_id']
              rel_type = item['type']

              # Check if relation already exists (either direction for symmetric types).
              # Match on ``relation_type`` too — without that filter a
              # caller asking for a 'blocks' relation would be skipped
              # whenever any other relation (e.g. 'relates') already
              # connects the same pair of work packages.
              existing = Relation.where(from_id: from_id, to_id: to_id, relation_type: rel_type).first
              if existing.nil? && ['relates'].include?(rel_type)
                existing = Relation.where(from_id: to_id, to_id: from_id, relation_type: rel_type).first
              end

              if existing
                results[:skipped] += 1
              else
                from_wp = WorkPackage.find_by(id: from_id)
                to_wp = WorkPackage.find_by(id: to_id)
                if from_wp && to_wp
                  rel = Relation.new(from: from_wp, to: to_wp, relation_type: rel_type)
                  if rel.save
                    results[:created] += 1
                  else
                    results[:failed] += 1
                    results[:errors] << {{ from: from_id, to: to_id, error: rel.errors.full_messages.join(', ') }}
                  end
                else
                  results[:failed] += 1
                  results[:errors] << {{ from: from_id, to: to_id, error: 'WorkPackage not found' }}
                end
              end
            rescue => e
              results[:failed] += 1
              results[:errors] << {{ from: item['from_id'], to: item['to_id'], error: e.message }}
            end
          end

          results[:success] = (results[:failed] == 0)
          results
        """
        try:
            result = self._client.execute_query_to_json_file(script)
            if isinstance(result, dict):
                return result
            return {"success": False, "created": 0, "skipped": 0, "failed": len(relations), "error": str(result)}
        except Exception as e:
            self._logger.warning("Bulk create relations failed: %s", e)
            return {"success": False, "created": 0, "skipped": 0, "failed": len(relations), "error": str(e)}
