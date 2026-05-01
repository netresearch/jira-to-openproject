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
from typing import Any

from src.infrastructure.exceptions import QueryExecutionError
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
        if not updates:
            return {"updated": 0, "failed": 0, "results": []}

        # Build batch update script
        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        # that Ruby misinterprets as invalid Unicode escape sequences
        # NOTE: Ruby parses {"key": value} as symbol keys (:key), so we use :id etc.
        updates_json = json.dumps(updates, ensure_ascii=False)
        script = f"""
        updates = {updates_json}
        updated_count = 0
        failed_count = 0
        results = []

        updates.each do |update|
          begin
            wp = WorkPackage.find(update[:id])
            update.each do |key, value|
              next if key == :id
              wp.send("#{{key}}=", value) if wp.respond_to?("#{{key}}=")
            end
            wp.save!
            updated_count += 1
            results << {{ id: wp.id, status: 'updated' }}
          rescue => e
            failed_count += 1
            results << {{ id: update[:id], status: 'failed', error: e.message }}
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
        # Convert API-style payload to batch format if needed
        wp_data: dict[str, Any] = {}

        # Handle API-style _links format
        if "_links" in payload:
            links = payload["_links"]

            # Extract project ID from href
            if "project" in links and "href" in links["project"]:
                href = links["project"]["href"]
                if match := re.search(r"/projects/(\d+)", href):
                    wp_data["project_id"] = int(match.group(1))

            # Extract type ID from href
            if "type" in links and "href" in links["type"]:
                href = links["type"]["href"]
                if match := re.search(r"/types/(\d+)", href):
                    wp_data["type_id"] = int(match.group(1))

            # Extract status ID from href
            if "status" in links and "href" in links["status"]:
                href = links["status"]["href"]
                if match := re.search(r"/statuses/(\d+)", href):
                    wp_data["status_id"] = int(match.group(1))
        else:
            # Direct format - copy relevant fields
            for key in ["project_id", "type_id", "status_id", "priority_id", "author_id", "assigned_to_id"]:
                if key in payload:
                    wp_data[key] = payload[key]

        # Copy subject and description
        if "subject" in payload:
            wp_data["subject"] = payload["subject"]
        if "description" in payload:
            wp_data["description"] = payload["description"]

        # Call the internal batch method (still on the client) directly
        # for a single item, avoiding the ``process_batches`` wrapper
        # which may alter the return format. The bulk-creation
        # pipeline is large enough to deserve its own focused
        # extraction in a follow-up phase — until then the service
        # reaches it through ``self._client._create_work_packages_batch``.
        try:
            result = client._create_work_packages_batch([wp_data])
            if isinstance(result, dict) and result.get("results"):
                results = result["results"]
                if results and len(results) > 0:
                    return results[0] if isinstance(results[0], dict) else {"id": results[0]}
        except Exception as e:
            self._logger.error("Failed to create work package: %s", e)
        return None

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
