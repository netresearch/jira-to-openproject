"""Custom-field operations against an OpenProject instance.

Phase 2 split of ``OpenProjectClient`` (ADR-002 phase 2): all the
custom-field methods live here, organised by purpose:

* **Creation / enablement** — ``ensure_wp_custom_field_id``,
  ``enable_custom_field_for_projects``, ``ensure_work_package_custom_field``
  (is_for_all=true variant), ``ensure_custom_field`` (generic per-type),
  ``ensure_origin_custom_fields``.
* **Lookup** — ``get_by_name``, ``get_id_by_name``, ``get_all``.
* **Deletion** — ``remove_custom_field``, ``delete_all_custom_fields``.
* **Bulk read** — ``batch_get_custom_fields_by_names``.

The per-work-package CF *value* writes
(``bulk_set_wp_custom_field_values``) live on
``OpenProjectWorkPackageCustomFieldService`` (Phase 2w), exposed on
the client as ``self.wp_cf``.

The provenance custom-field helpers (``ensure_j2o_provenance_custom_fields``
plus the rest of the J2O Migration Provenance machinery) stay on
``OpenProjectClient`` for now — they will move into a separate
``OpenProjectProvenanceService`` in Phase 2c since they're a coherent,
domain-specific subsystem of their own.

Design note
-----------

The service holds a back-reference to its parent ``OpenProjectClient`` so it
can reuse the script-execution machinery (``execute_query``, ``find_record``,
``execute_large_query_to_json_file``, ``_generate_unique_temp_filename``,
``execute_json_query``, ``execute_query_to_json_file``,
``_validate_batch_size``, ``_build_safe_batch_query``,
``_retry_with_exponential_backoff``) rather than duplicating it.
``OpenProjectClient`` exposes the service via ``self.custom_fields`` and
keeps thin delegators for the same method names so existing call sites
(migrations, tests) work unchanged.
"""

from __future__ import annotations

import json
import time
from typing import Any

from src.infrastructure.exceptions import QueryExecutionError, RecordNotFoundError
from src.infrastructure.openproject.openproject_client import OpenProjectClient


class OpenProjectCustomFieldService:
    """Read/write helpers for OpenProject CustomField records."""

    # Cache duration for ``get_all`` (seconds).
    CACHE_TTL_SECONDS: int = 300

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger
        self._cache: list[dict[str, Any]] | None = None
        self._cache_time: float = 0.0

    # ── creation / enablement ─────────────────────────────────────────────

    def ensure_wp_custom_field_id(self, name: str, field_format: str = "text") -> int:
        """Ensure a WorkPackageCustomField exists, returning its ID.

        Unlike :py:meth:`OpenProjectClient.ensure_work_package_custom_field`
        (which uses ``is_for_all: true`` and returns the full CF dict), this
        variant creates the CF with ``is_for_all: false`` so the caller can
        then selectively enable it on specific projects via
        :py:meth:`enable_custom_field_for_projects`. Returns just the ID.

        Migration components that need per-project CF enablement (Labels,
        AffectsVersions, StoryPoints, Sprint/Epic, etc.) call this.

        Args:
            name: Custom field display name
            field_format: Rails field format (string, text, int, float, etc.)

        Returns:
            Custom field ID, or 0 if creation failed.

        """
        # Lazy import avoids the openproject_client ↔ this-module import cycle
        # at module load time.
        from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

        try:
            cf = self.get_by_name(name)
            cf_id = int(cf.get("id", 0) or 0) if isinstance(cf, dict) else None
            if cf_id:
                return cf_id
        except RecordNotFoundError:
            self._logger.info("CF '%s' not found; will create (format=%s)", name, field_format)

        escaped = escape_ruby_single_quoted(name)
        escaped_format = escape_ruby_single_quoted(field_format)
        script = (
            f"cf = CustomField.find_by(type: 'WorkPackageCustomField', name: '{escaped}'); "
            f"if !cf; cf = CustomField.new(name: '{escaped}', field_format: '{escaped_format}', "
            f"is_required: false, is_for_all: false, type: 'WorkPackageCustomField'); cf.save; end; cf.id"
        )
        result = self._client.execute_query(script)
        return int(result) if result else 0

    def enable_custom_field_for_projects(
        self,
        cf_id: int,
        project_ids: set[int],
        cf_name: str | None = None,
    ) -> None:
        """Enable a custom field for specific projects only.

        Pairs with :py:meth:`ensure_wp_custom_field_id` (which creates the CF
        with ``is_for_all: false``). Idempotent: uses ``find_or_create_by!``
        on the join table.

        Args:
            cf_id: Custom field ID
            project_ids: Set of project IDs to enable the field for
            cf_name: Optional display name for logging

        """
        if not project_ids:
            return
        project_ids_str = ", ".join(str(pid) for pid in sorted(project_ids))
        # ``find_by(id:)`` returns nil instead of raising when the CF was
        # already removed, so the rest of the script can no-op gracefully
        # rather than failing the whole call.
        script = (
            f"cf = CustomField.find_by(id: {cf_id})\n"
            f"if cf\n"
            f"  [{project_ids_str}].each do |pid|\n"
            f"    begin\n"
            f"      project = Project.find(pid)\n"
            f"      CustomFieldsProject.find_or_create_by!(custom_field: cf, project: project)\n"
            f"    rescue ActiveRecord::RecordNotFound\n"
            f"    end\n"
            f"  end\n"
            f"end\n"
            f"true"
        )
        try:
            self._client.execute_query(script)
            display = cf_name or str(cf_id)
            self._logger.info("Enabled %s CF for %d projects", display, len(project_ids))
        except Exception as e:
            self._logger.warning("Failed to enable CF for some projects: %s", e)

    # ── lookup ────────────────────────────────────────────────────────────

    def get_by_name(self, name: str) -> dict[str, Any]:
        """Find a custom field by name.

        Raises:
            RecordNotFoundError: If custom field with given name is not found

        """
        return self._client.find_record("CustomField", {"name": name})

    def get_id_by_name(self, name: str) -> int:
        """Find a custom field ID by name.

        Raises:
            RecordNotFoundError: If custom field with given name is not found
            QueryExecutionError: If query fails

        """
        # Lazy import avoids the import cycle at module load time.
        from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

        try:
            result = self._client.execute_query(
                f"CustomField.where(name: '{escape_ruby_single_quoted(name)}').first&.id",
            )

            if result is None:
                msg = f"Custom field '{name}' not found"
                raise RecordNotFoundError(msg)

            if isinstance(result, int):
                return result

            if isinstance(result, str):
                try:
                    return int(result)
                except ValueError:
                    msg = f"Invalid ID format: {result}"
                    raise QueryExecutionError(msg) from None

            msg = f"Unexpected result type: {type(result)}"
            raise QueryExecutionError(msg)

        except RecordNotFoundError:
            raise
        except Exception as e:
            msg = "Error getting custom field ID."
            raise QueryExecutionError(msg) from e

    def get_all(self, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        """Get all custom fields from OpenProject (with a 5-minute cache).

        Args:
            force_refresh: If True, bypass the cache and refresh from the server.

        Raises:
            QueryExecutionError: If query execution fails

        """
        current_time = time.time()

        # ``self._cache is not None`` (rather than ``self._cache``) so that an
        # empty list — i.e. a server with zero custom fields — is still served
        # from cache within the TTL instead of re-running the Rails query.
        if not force_refresh and self._cache is not None and (current_time - self._cache_time) < self.CACHE_TTL_SECONDS:
            self._logger.debug(
                "Using cached custom fields (age: %.1fs)",
                current_time - self._cache_time,
            )
            return self._cache

        try:
            file_path = self._client._generate_unique_temp_filename("custom_fields")
            custom_fields = self._client.execute_large_query_to_json_file(
                "CustomField.all",
                container_file=file_path,
                timeout=90,
            )

            self._cache = custom_fields or []
            self._cache_time = current_time

            return custom_fields if isinstance(custom_fields, list) else []

        except Exception as e:
            msg = "Failed to get custom fields."
            raise QueryExecutionError(msg) from e

    # ── creation (full-spec ensure variants) ──────────────────────────────

    def ensure_work_package_custom_field(self, name: str, field_format: str = "string") -> dict[str, Any]:
        """Ensure a WorkPackage custom field exists with ``is_for_all: true``.

        Distinct from :py:meth:`ensure_wp_custom_field_id`: this returns the
        full record dict and uses the ``is_for_all: true`` semantics (the field
        is enabled globally rather than selectively per project).
        """
        # Lazy import avoids the openproject_client ↔ this-module import cycle.
        from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

        ruby = f"""
          cf = CustomField.find_by(type: 'WorkPackageCustomField', name: '{escape_ruby_single_quoted(name)}')
          if !cf
            cf = CustomField.new(name: '{escape_ruby_single_quoted(name)}', field_format: '{escape_ruby_single_quoted(field_format)}', is_required: false, is_for_all: true, type: 'WorkPackageCustomField')
            cf.save
          end
          cf && cf.as_json(only: [:id, :name, :field_format])
        """
        try:
            result = self._client.execute_json_query(ruby)
            if isinstance(result, dict) and result.get("id"):
                return result
            msg = f"Failed ensuring WorkPackage custom field '{name}' (format={field_format})"
            raise QueryExecutionError(msg)
        except Exception as e:
            msg = f"Failed to ensure custom field '{name}': {e}"
            raise QueryExecutionError(msg) from e

    def ensure_custom_field(
        self,
        name: str,
        *,
        field_format: str = "string",
        cf_type: str = "WorkPackageCustomField",
        searchable: bool = False,
    ) -> dict[str, Any]:
        """Ensure a CustomField of any type exists, create if missing.

        Handles the broadest set of options: per-type creation
        (WorkPackage / Project / User / TimeEntry), searchable flag,
        WorkPackage type-id population, UserCustomField activation. Returns
        the resulting CF record dict.
        """
        from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

        searchable_str = "true" if searchable else "false"

        ruby = f"""
          cf = CustomField.find_by(type: '{escape_ruby_single_quoted(cf_type)}', name: '{escape_ruby_single_quoted(name)}')
          if !cf
            cf = CustomField.new(name: '{escape_ruby_single_quoted(name)}', field_format: '{escape_ruby_single_quoted(field_format)}', is_required: false, type: '{escape_ruby_single_quoted(cf_type)}')
            begin
              cf.is_for_all = true
            rescue
            end
            begin
              cf.searchable = {searchable_str}
            rescue
            end
            cf.save
            # For WorkPackageCustomField, explicitly enable for all types
            if cf.type == 'WorkPackageCustomField' && cf.type_ids.empty?
              begin
                cf.type_ids = Type.all.pluck(:id)
                cf.save
              rescue
              end
            end
          else
            # Update searchable if it doesn't match
            begin
              if cf.respond_to?(:searchable) && cf.searchable != {searchable_str}
                cf.searchable = {searchable_str}
                cf.save
              end
            rescue
            end
            # Ensure WP CFs are enabled for all types
            if cf.type == 'WorkPackageCustomField' && cf.type_ids.empty?
              begin
                cf.type_ids = Type.all.pluck(:id)
                cf.save
              rescue
              end
            end
          end
          if cf && cf.type == 'UserCustomField'
            begin
              cf.activate! if cf.respond_to?(:active?) && !cf.active?
            rescue
            end
          end
          cf && cf.as_json(only: [:id, :name, :field_format, :type])
        """
        try:
            result = self._client.execute_json_query(ruby)
            if isinstance(result, dict) and result.get("id"):
                return result
            msg = f"Failed ensuring {cf_type} '{name}'"
            raise QueryExecutionError(msg)
        except Exception as e:
            msg = f"Failed to ensure custom field '{name}' ({cf_type}): {e}"
            raise QueryExecutionError(msg) from e

    def remove_custom_field(self, name: str, *, cf_type: str | None = None) -> dict[str, int]:
        """Remove CustomField records matching the provided name/type."""
        # Use ensure_ascii=False to output UTF-8 directly, avoiding \uXXXX escapes
        name_literal = json.dumps(name, ensure_ascii=False)
        type_filter = ""
        if cf_type:
            type_literal = json.dumps(cf_type, ensure_ascii=False)
            type_filter = f"scope = scope.where(type: {type_literal})\n"

        ruby = (
            f"scope = CustomField.where(name: {name_literal})\n"
            f"{type_filter}"
            "removed = 0\n"
            "scope.find_each do |cf|\n"
            "  begin\n"
            "    cf.destroy\n"
            "    removed += 1\n"
            "  rescue => e\n"
            '    Rails.logger.warn("Failed to destroy custom field #{cf.id}: #{e.message}")\n'
            "  end\n"
            "end\n"
            "{ removed: removed }.to_json\n"
        )

        try:
            result = self._client.execute_json_query(ruby)
            if isinstance(result, dict):
                return {"removed": int(result.get("removed", 0) or 0)}
            msg = "Unexpected response removing custom field"
            raise QueryExecutionError(msg)
        except Exception as e:
            msg = f"Failed to remove custom field '{name}'"
            raise QueryExecutionError(msg) from e

    def ensure_origin_custom_fields(self) -> dict[str, list[dict[str, Any]]]:
        """Ensure origin mapping CFs exist for WP, User, TimeEntry.

        Project CFs are intentionally skipped (this OpenProject instance
        doesn't expose them via this path; persistence happens via
        ``OpenProjectClient.upsert_project_origin_attributes`` instead).
        """
        ensured: dict[str, list[dict[str, Any]]] = {
            "work_package": [],
            "project": [],
            "user": [],
            "time_entry": [],
        }

        for name, fmt in (
            ("J2O Origin System", "string"),
            ("J2O Origin ID", "string"),
            ("J2O Origin Key", "string"),
            ("J2O Origin URL", "string"),
            ("J2O Project Key", "string"),
            ("J2O Project ID", "string"),
            ("J2O First Migration Date", "date"),
            ("J2O Last Update Date", "date"),
        ):
            try:
                ensured["work_package"].append(
                    self.ensure_custom_field(name, field_format=fmt, cf_type="WorkPackageCustomField"),
                )
            except Exception as e:
                self._logger.warning("Failed ensuring WP CF %s: %s", name, e)

        # NOTE: This OpenProject instance does not support Project custom fields
        # via this path. Persist origin for projects using project attributes.
        ensured["project"] = []

        for name, fmt in (
            ("J2O Origin System", "string"),
            ("J2O User ID", "string"),
            ("J2O User Key", "string"),
            ("J2O External URL", "string"),
        ):
            try:
                ensured["user"].append(self.ensure_custom_field(name, field_format=fmt, cf_type="UserCustomField"))
            except Exception as e:
                self._logger.warning("Failed ensuring User CF %s: %s", name, e)

        for name, fmt in (
            ("J2O Origin Worklog Key", "string"),
            ("J2O Origin Issue ID", "string"),
            ("J2O Origin Issue Key", "string"),
            ("J2O Origin System", "string"),
            ("J2O First Migration Date", "date"),
            ("J2O Last Update Date", "date"),
        ):
            try:
                ensured["time_entry"].append(
                    self.ensure_custom_field(name, field_format=fmt, cf_type="TimeEntryCustomField"),
                )
            except Exception as e:
                self._logger.warning("Failed ensuring TE CF %s: %s", name, e)

        return ensured

    # ── deletion ──────────────────────────────────────────────────────────

    def delete_all_custom_fields(self) -> int:
        """Delete every CustomField record (uses destroy_all for cleanup).

        Returns:
            Number of deleted custom fields.

        Raises:
            QueryExecutionError: If bulk deletion fails.

        """
        try:
            count = self._client.execute_query("CustomField.count")
            self._client.execute_query("CustomField.destroy_all")
            return count if isinstance(count, int) else 0
        except Exception as e:
            msg = "Failed to delete all custom fields."
            raise QueryExecutionError(msg) from e

    # ── bulk read ─────────────────────────────────────────────────────────

    def batch_get_custom_fields_by_names(
        self,
        names: list[str],
        batch_size: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Look up CF records by name in batches with retry support.

        Returns a dict mapping name -> CF record. Names that don't exist on the
        server are simply omitted from the result.
        """
        if not names:
            return {}

        # Reuse the client's batch-size validation, query builder, and retry.
        client = self._client
        effective_batch_size = batch_size or getattr(client, "batch_size", 100)
        effective_batch_size = client._validate_batch_size(effective_batch_size)

        results: dict[str, dict[str, Any]] = {}

        for i in range(0, len(names), effective_batch_size):
            batch_names = names[i : i + effective_batch_size]

            def batch_operation(_batch_names: list[str] = batch_names) -> list[dict[str, Any]]:
                query = client._build_safe_batch_query("CustomField", "name", _batch_names)
                return client.execute_json_query(query)  # type: ignore[return-value]

            try:
                batch_results = client._retry_with_exponential_backoff(
                    batch_operation,
                    f"Batch fetch custom fields by name {batch_names[:2]}{'...' if len(batch_names) > 2 else ''}",
                )
                if batch_results:
                    if isinstance(batch_results, dict):
                        batch_results = [batch_results]
                    for record in batch_results:
                        if isinstance(record, dict) and "name" in record:
                            name = record["name"]
                            if name in batch_names:
                                results[name] = record

            except Exception as e:
                self._logger.warning(
                    "Failed to fetch batch of custom field names %s after retries: %s",
                    batch_names,
                    e,
                )
                for name in batch_names:
                    self._logger.debug(
                        "Failed to fetch custom field by name %s: %s",
                        name,
                        e,
                    )
                continue

        return results
