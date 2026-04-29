"""Custom-field operations against an OpenProject instance.

First slice of the Phase 2 split of ``OpenProjectClient`` (ADR-002 phase 2):
the simpler, mostly self-contained custom-field helpers move here. The
heavier methods (``ensure_work_package_custom_field``, ``ensure_custom_field``,
``remove_custom_field``, ``ensure_origin_custom_fields``,
``ensure_j2o_provenance_custom_fields``, ``delete_all_custom_fields``,
``bulk_set_wp_custom_field_values``, ``batch_get_custom_fields_by_names``)
will follow in subsequent PRs.

Design note
-----------

The service holds a back-reference to its parent ``OpenProjectClient`` so it
can reuse the script-execution machinery (``execute_query``, ``find_record``,
``execute_large_query_to_json_file``, ``_generate_unique_temp_filename``)
rather than duplicating it. ``OpenProjectClient`` exposes the service via
``self.custom_fields`` and keeps thin delegators for the same method names
so existing call sites (migrations, tests) work unchanged.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from src.clients.exceptions import QueryExecutionError, RecordNotFoundError

if TYPE_CHECKING:
    from src.clients.openproject_client import OpenProjectClient


class OpenProjectCustomFieldService:
    """Read/write helpers for OpenProject CustomField records."""

    # Cache duration for ``get_custom_fields`` (seconds).
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
        from src.clients.openproject_client import escape_ruby_single_quoted

        try:
            cf = self.get_by_name(name)
            cf_id = int(cf.get("id", 0) or 0) if isinstance(cf, dict) else None
            if cf_id:
                return cf_id
        except Exception:
            self._logger.info("CF '%s' not found; will create (format=%s)", name, field_format)

        escaped = escape_ruby_single_quoted(name)
        script = (
            f"cf = CustomField.find_by(type: 'WorkPackageCustomField', name: '{escaped}'); "
            f"if !cf; cf = CustomField.new(name: '{escaped}', field_format: '{field_format}', "
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
        script = (
            f"cf = CustomField.find({cf_id})\n"
            f"[{project_ids_str}].each do |pid|\n"
            f"  begin\n"
            f"    project = Project.find(pid)\n"
            f"    CustomFieldsProject.find_or_create_by!(custom_field: cf, project: project)\n"
            f"  rescue ActiveRecord::RecordNotFound\n"
            f"  end\n"
            f"end\n"
            f"true"
        )
        try:
            self._client.execute_query(script)
            display = cf_name or str(cf_id)
            self._logger.info("Enabled %s CF for %d projects", display, len(project_ids))
        except Exception:
            self._logger.warning("Failed to enable CF for some projects")

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
        from src.clients.openproject_client import escape_ruby_single_quoted

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

        if not force_refresh and self._cache and (current_time - self._cache_time) < self.CACHE_TTL_SECONDS:
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
