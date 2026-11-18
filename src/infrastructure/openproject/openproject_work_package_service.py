# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Work-package CRUD helpers for the OpenProject Rails console.

Phase 2n of ADR-002 continues the openproject_client.py god-class
decomposition by collecting the simpler work-package CRUD operations
onto a focused service. The service owns:

* **Single-record writes** — ``create_work_package`` (accepts both
  API-style ``_links`` payload and direct attribute payload),
  ``update_work_package``.
* **Batch writes** — ``batch_update_work_packages`` (per-row
  ``find / send / save!`` loop with structured success/failure
  result).
* **Reads** — ``stream_work_packages_for_project`` yields the first
  ``batch_size`` work packages of a project (the historical
  ``stream_*`` name doesn't paginate; behaviour preserved as-is —
  see method docstring).
* **Cleanup** — ``delete_all_work_packages`` mass-deletes via
  ``WorkPackage.delete_all``. Authorisation is the caller's
  responsibility; the Rails console path runs whatever the
  console session is allowed to run, so wrap it in your own
  guard if you need to restrict it.

What stays on the client (deferred to follow-up phases)
-------------------------------------------------------
- ``bulk_create_records`` and ``_create_work_packages_batch`` — the
  ~750 LOC bulk-creation pipeline. Heavily coupled to the migration
  runner and the work-package CF subsystem; earns its own focused
  diff. ``create_work_package`` reaches the private helper through
  ``self._client._create_work_packages_batch``.
- ``bulk_set_wp_custom_field_values`` — CF-write subsystem.
- ``upsert_work_package_description_section`` /
  ``bulk_upsert_wp_description_sections`` — description-block
  subsystem with its own structured payload contract.
- ``create_work_package_activity`` /
  ``bulk_create_work_package_activities`` — activity/journal writes.
- ``set_wp_last_update_date_by_keys`` — date-back-fill helper used by
  the change-detection pipeline.
- ``stream_work_packages_for_project`` only fetches the FIRST page
  of size ``batch_size``. The "stream" name is aspirational — the
  pre-extraction implementation never paginated. Behaviour is
  preserved here; an honest paginating rewrite earns its own PR.

``OpenProjectClient`` exposes the service via ``self.work_packages``
and keeps thin delegators for the same method names so existing call
sites work unchanged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from src.infrastructure.exceptions import QueryExecutionError
from src.infrastructure.openproject.openproject_bulk_create_service import reject_legacy_journal_operations

if TYPE_CHECKING:
    from src.infrastructure.openproject.openproject_client import OpenProjectClient


class OpenProjectWorkPackageService:
    """Work-package CRUD helpers for ``OpenProjectClient``."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    # ── reads ────────────────────────────────────────────────────────────

    def stream_work_packages_for_project(
        self,
        project_id: int,
        batch_size: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield work packages for a project, capped at ``batch_size``.

        Despite the historical ``stream_*`` name, this method does NOT
        paginate — it runs one ``limit({batch_size})`` query against
        ``project.work_packages`` and yields the resulting list. The
        name and behaviour are preserved as-is from the pre-extraction
        client to avoid changing migration semantics. An honest
        paginating rewrite earns its own PR.
        """
        client = self._client
        # Use ``is not None`` so a caller-supplied ``batch_size=0`` is
        # respected literally rather than swapped for the configured
        # default — consistent with the same fix applied in the
        # records / projects / users batch services.
        effective_batch_size = batch_size if batch_size is not None else client.batch_size

        script = f"""
        project = Project.find({project_id})
        work_packages = project.work_packages.limit({effective_batch_size})

        work_packages.map do |wp|
          {{
            id: wp.id,
            subject: wp.subject,
            description: wp.description,
            status: wp.status.name,
            priority: wp.priority.name,
            type: wp.type.name,
            author: wp.author.name,
            assignee: wp.assigned_to&.name,
            created_at: wp.created_at,
            updated_at: wp.updated_at,
            # Back-compat keys (map *_on to *_at)
            created_on: wp.created_at,
            updated_on: wp.updated_at
          }}
        end
        """

        try:
            results = client.execute_json_query(script)
            if isinstance(results, list):
                yield from results
        except Exception:
            self._logger.exception("Failed to stream work packages for project %s", project_id)

    # ── writes ───────────────────────────────────────────────────────────

    def batch_update_work_packages(
        self,
        updates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Update multiple work packages in batches."""
        reject_legacy_journal_operations(updates)
        if not updates:
            return {"updated": 0, "failed": 0, "results": []}

        updates_json = json.dumps(updates, ensure_ascii=False)
        script = f"""
        updates = JSON.parse(<<-'J2O_UPDATES'
{updates_json}
J2O_UPDATES
)
        updated_count = 0
        failed_count = 0
        results = []

        updates.each do |update|
          begin
            wp = WorkPackage.find(update.fetch('id'))
            wp.with_lock(requires_new: true) do
              attributes = update.except('id', '_rails_operations')
              fields = attributes.extract!(*attributes.keys.grep(/\\AcustomField[0-9]+\\z/))
              target_ids = attributes.delete('target_version_ids')
              if target_ids
                raise "Missing or foreign project version" unless wp.project.versions.where(id: target_ids).count == target_ids.size
                if wp.respond_to?(:target_version_ids_replacements=)
                  raise "Multiple target versions are disabled" if target_ids.many? && !Setting.work_package_multiple_versions?
                  wp.target_version_ids_replacements = target_ids unless wp.target_version_ids.sort == target_ids.sort
                else
                  raise "Target accepts only one version" if target_ids.many?
                  wp.version_id = target_ids.first
                end
              end
              wp.assign_attributes(attributes)
              expected = {{}}
              values = fields.to_h do |key, raw|
                cf = wp.available_custom_fields.find {{ |f| f.id == key.delete_prefix('customField').to_i }}
                raise "Field #{{key}} unavailable for work package" unless cf
                items = raw.is_a?(Array) ? raw : [raw]
                raise "Multiple values for single field #{{key}}" if !cf.multi_value? && items.size > 1
                normalized = items.map do |item|
                  if cf.field_format == 'list' && !item.nil?
                    raise "Cascading selection needs a mapping for #{{key}}" if item.is_a?(Hash) && item['child']
                    label = item.is_a?(Hash) ? (item['value'] || item['name']) : item
                    ids = cf.custom_options.where(value: label.to_s).limit(2).pluck(:id)
                    raise "Missing or ambiguous option for #{{key}}" unless ids.one?
                    item = ids.first
                  elsif cf.field_format == 'user'
                    raise "Unmapped user for #{{key}}" unless item.is_a?(Integer) && User.exists?(item)
                  elsif item.is_a?(Hash) || item.is_a?(Array) || cf.field_format == 'version'
                    raise "Source value needs an explicit mapping for #{{key}}"
                  end
                  CustomValue.new(custom_field: cf, value: item).value
                end
                expected[cf.id] = normalized.compact.reject(&:empty?).sort
                [cf.id, cf.multi_value? ? normalized : normalized.first]
              end
              wp.custom_field_values = values unless values.empty?
              wp.save!
              if target_ids
                stored_ids = wp.reload.respond_to?(:target_version_ids) ? wp.target_version_ids : [wp.version_id].compact
                raise "Target versions were not persisted" unless stored_ids.sort == target_ids.sort
              end
              unless expected.empty?
                stored = CustomValue.where(customized: wp, custom_field_id: expected.keys)
                                    .pluck(:custom_field_id, :value).group_by(&:first)
                expected.each do |id, want|
                  actual = Array(stored[id]).map(&:last).compact.reject(&:empty?).sort
                  raise "Custom field #{{id}} was not persisted" unless actual == want
                end
              end
            end
            updated_count += 1
            results << {{ id: wp.id, status: 'updated' }}
          rescue => e
            failed_count += 1
            results << {{ id: update['id'], status: 'failed', error: e.message }}
          end
        end

        {{
          updated: updated_count,
          failed: failed_count,
          results: results
        }}
        """

        try:
            return self._client.execute_json_query(script)
        except Exception as e:
            msg = f"Failed to batch update work packages: {e}"
            raise QueryExecutionError(msg) from e

    def create_work_package(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Create a single work package.

        Args:
            payload: Work package data. Can be in API format (with _links)
                     or direct format (with project_id, type_id, etc.)

        Returns:
            Created work package data or None on failure

        """
        client = self._client
        reject_legacy_journal_operations([payload])
        wp_data = self._creation_attributes(payload)
        try:
            result = client._create_work_packages_batch([wp_data])
        except Exception as e:
            self._logger.error("Failed to create work package: %s", e)
            return None
        results = result.get("results") if isinstance(result, dict) else None
        if not results:
            return None
        first = results[0]
        return first if isinstance(first, dict) else {"id": first}

    @staticmethod
    def _creation_attributes(payload: dict[str, Any]) -> dict[str, Any]:
        data = {field: payload[field] for field in ("subject", "description") if field in payload}
        if "_links" not in payload:
            ids = ("project_id", "type_id", "status_id", "priority_id", "author_id", "assigned_to_id")
            data.update({key: payload[key] for key in ids if key in payload})
            return data
        for name, resource in (("project", "projects"), ("type", "types"), ("status", "statuses")):
            href = payload["_links"].get(name, {}).get("href")
            if href and (match := re.search(rf"/{resource}/(\d+)", href)):
                data[f"{name}_id"] = int(match.group(1))
        return data

    def update_work_package(
        self,
        wp_id: int,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Update a single work package.

        Args:
            wp_id: Work package ID
            updates: Fields to update

        Returns:
            Updated work package data or None on failure

        """
        update_data = {"id": wp_id, **updates}
        result = self.batch_update_work_packages([update_data])
        if result and result.get("results"):
            return result["results"][0]
        return None

    # ── cleanup ──────────────────────────────────────────────────────────

    def delete_all_work_packages(self) -> int:
        """Delete all work packages in bulk.

        Returns:
            Number of deleted work packages

        Raises:
            QueryExecutionError: If bulk deletion fails

        """
        try:
            # ``execute_query`` returns the raw console output as a string
            # (e.g. ``"10"``), so the previous ``isinstance(count, int)``
            # guard always saw ``False`` and the method silently reported
            # 0 deletions even when ``WorkPackage.delete_all`` succeeded.
            # ``execute_json_query`` parses the response into a real int.
            count = self._client.execute_json_query("WorkPackage.delete_all")
            return count if isinstance(count, int) else 0
        except Exception as e:
            msg = "Failed to delete all work packages."
            raise QueryExecutionError(msg) from e
