# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Generic ActiveRecord CRUD helpers for the OpenProject Rails console.

Phase 2m of ADR-002 continues the openproject_client.py god-class
decomposition by collecting the generic ActiveRecord CRUD operations
onto a focused service. The service owns:

* **Single-record reads** — ``find_record`` (id or conditions hash).
* **Single-record writes** — ``create_record``, ``update_record``,
  ``delete_record``.
* **Multi-record reads** — ``find_all_records`` (where + includes +
  limit) and ``batch_find_records`` (paged id lookup with the shared
  ``@batch_idempotent`` decorator and a keyword-only ``headers`` kwarg
  for callers that want real cache hits).

Cross-service helpers stay on ``OpenProjectClient`` and are reached
through ``self._client.<helper>``:

* ``_retry_with_exponential_backoff`` — used by sibling services
  (CustomField, User, Project, this one); keeping it on the client
  avoids cross-service references.
* ``_validate_batch_size``, ``_build_safe_batch_query`` — shared with
  the user/project batch lookups.
* ``_validate_model_name`` (both the module-level function and the
  client method) — referenced via lazy imports so the service ↔
  client cycle is kept out of module-load time.
* ``escape_ruby_single_quoted`` — same lazy-import treatment as in
  the user/project services.

``OpenProjectClient`` exposes the service via ``self.records`` and
keeps thin delegators for the same method names so existing call sites
work unchanged.
"""

from __future__ import annotations

import json
from typing import Any

from src.infrastructure.exceptions import (
    JsonParseError,
    QueryExecutionError,
    RecordNotFoundError,
)
from src.infrastructure.openproject.batch_records import index_batch_rows
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.infrastructure.openproject.rails_console_client import RubyError
from src.utils.idempotency_decorators import batch_idempotent

# Sample size used when building log labels for batch operations
# ("Batch fetch User records [1, 2, 3, ...]"); kept here rather than
# referencing the client's ``BATCH_LABEL_SAMPLE`` to avoid a stale
# import surface if the constant is later relocated.
BATCH_LABEL_SAMPLE = 3


def _ruby_hash(attributes: dict[str, Any]) -> str:
    from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

    def literal(value: Any) -> str:
        if isinstance(value, str):
            return f"'{escape_ruby_single_quoted(value)}'"
        if value is None:
            return "nil"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (list, dict)):
            return f"JSON.parse('{escape_ruby_single_quoted(json.dumps(value, allow_nan=False))}')"
        return json.dumps(value, allow_nan=False)

    return ", ".join(f"{literal(str(key))} => {literal(value)}" for key, value in attributes.items())


class OpenProjectRecordsService:
    """Generic ActiveRecord CRUD helpers for ``OpenProjectClient``."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    # ── single-record reads ──────────────────────────────────────────────

    def find_record(
        self,
        model: str,
        id_or_conditions: int | dict[str, Any],
    ) -> dict[str, Any]:
        """Find a record by ID or conditions.

        Args:
            model: Model name (e.g., "User", "Project")
            id_or_conditions: ID or conditions hash

        Returns:
            Record data

        Raises:
            RecordNotFoundError: If no record is found
            QueryExecutionError: If query fails

        """
        # Lazy import: ``_validate_model_name`` is a module-level function
        # on ``openproject_client``; lazy keeps the service ↔ client cycle
        # out of module-load time.
        from src.infrastructure.openproject.openproject_client import (
            _validate_model_name,
        )

        _validate_model_name(model)
        try:
            if isinstance(id_or_conditions, int):
                query = f"{model}.find_by(id: {id_or_conditions})&.as_json"
            else:
                query = f"{model}.find_by({{{_ruby_hash(id_or_conditions)}}})&.as_json"

            result = self._client.execute_json_query(query)
        except (QueryExecutionError, JsonParseError) as e:
            msg = f"Error finding record for {model}."
            raise QueryExecutionError(msg) from e
        if result is None:
            msg = f"No {model} found with {id_or_conditions}"
            raise RecordNotFoundError(msg)
        return result

    # ── multi-record reads ──────────────────────────────────────────────

    @batch_idempotent(ttl=3600)  # 1 hour TTL for batch record lookups
    def batch_find_records(
        self,
        model: str,
        ids: list[int | str],
        batch_size: int | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[int | str, dict[str, Any]]:
        """Find multiple records by IDs in batches with idempotency support.

        Args:
            model: Model name (e.g., "User", "Project")
            ids: List of IDs to find
            batch_size: Size of each batch (defaults to configured batch_size)
            headers: Optional headers dict; when ``X-Idempotency-Key`` is
                present the ``@batch_idempotent`` decorator caches the
                result under that key for the configured TTL. Without a
                header the decorator's per-call UUID makes the cache a
                no-op. Keyword-only so it cannot be passed positionally —
                the decorator's ``extract_headers_from_kwargs`` only sees
                real kwargs, so positional args would silently disable
                caching.

        Returns:
            Dictionary mapping ID to record data for successfully fetched
            records. Missing IDs are omitted. If one or more batches fail,
            those failures are logged and the method continues processing
            remaining batches, so partial results may be returned rather
            than raising.

        Raises:
            ValueError: If ``batch_size`` is not a positive integer and
                ``_validate_batch_size`` rejects the input (this is the
                actual exception type the validator emits, not
                ``QueryExecutionError``).
            QueryExecutionError: If a non-batch query-execution error
                propagates out of an unexpected code path.

        """
        # ``headers`` is consumed by the ``@batch_idempotent`` decorator's
        # ``extract_headers_from_kwargs`` helper before the function body
        # runs; we accept-and-discard it here to keep the signature
        # compatible with that contract.
        del headers

        client = self._client
        if not ids:
            return {}

        # Validate and clamp batch size to prevent memory exhaustion. Use
        # ``is not None`` so a caller-supplied ``batch_size=0`` is
        # respected literally rather than swapped for the default.
        effective_batch_size = batch_size if batch_size is not None else getattr(client, "batch_size", 100)
        effective_batch_size = client._validate_batch_size(effective_batch_size)

        results: dict[int | str, dict[str, Any]] = {}

        # Process IDs in batches
        for i in range(0, len(ids), effective_batch_size):
            batch_ids = ids[i : i + effective_batch_size]

            def batch_operation(batch_ids: list[int | str] = batch_ids) -> object:
                # Use safe query builder with ActiveRecord parameterization
                query = client._build_safe_batch_query(model, "id", batch_ids)
                return client.execute_json_query(query)

            try:
                # Execute batch operation with retry logic (with idempotency key propagation)
                label_prefix = f"Batch fetch {model} records "
                sample_label = f"{batch_ids[:BATCH_LABEL_SAMPLE]}{'...' if len(batch_ids) > BATCH_LABEL_SAMPLE else ''}"
                batch_results = client._retry_with_exponential_backoff(
                    batch_operation,
                    f"{label_prefix}{sample_label}",
                    jitter=True,
                )

                results.update(index_batch_rows(batch_results, batch_ids, "id", stringify=True))

            except Exception as e:
                self._logger.warning(
                    "Failed to fetch batch of %s records (IDs %s) after retries: %s",
                    model,
                    batch_ids,
                    e,
                )
                # Continue processing other batches rather than failing completely
                # Log individual failures for post-run review
                for batch_id in batch_ids:
                    self._logger.debug(
                        "Failed to fetch %s record ID %s: %s",
                        model,
                        batch_id,
                        e,
                    )
                continue

        return results

    def find_all_records(
        self,
        model: str,
        conditions: dict[str, Any] | None = None,
        limit: int | None = None,
        includes: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Find all records matching conditions.

        Args:
            model: Model name (e.g., "User", "Project")
            conditions: Optional conditions hash
            limit: Optional limit on number of records
            includes: Optional list of associations to include

        Returns:
            List of record data

        Raises:
            QueryExecutionError: If query fails

        """
        # Lazy import to avoid the service ↔ client cycle.
        from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

        client = self._client
        # Use the client's instance-method validator (returns the model)
        # rather than the module-level one used by find_record/create — the
        # behaviour is equivalent for the names we care about; this keeps
        # parity with the pre-extraction call site.
        client._validate_model_name(model)
        # Start building the query
        query = f"{model}"

        # Add conditions if provided (with safe escaping)
        if conditions:
            query += f".where({{{_ruby_hash(conditions)}}})"

        # Add includes if provided
        if includes:
            symbols = ", ".join(f"'{escape_ruby_single_quoted(inc)}'" for inc in includes)
            query += f".includes({symbols})"

        # Add limit if provided
        if limit:
            query += f".limit({int(limit)})"

        # Build Ruby expression that returns array/dicts directly
        ruby_expr = f"{query}.as_json"

        try:
            # Prefer file-based for multi-record results to avoid console artifacts
            data = client.execute_large_query_to_json_file(
                ruby_expr,
                container_file=client._generate_unique_temp_filename(f"{model.lower()}_records"),
                timeout=60,
            )
            if data is None:
                return []
            return data if isinstance(data, list) else [data]
        except Exception as e:
            msg = f"Error finding records for {model}."
            raise QueryExecutionError(msg) from e

    # ── single-record writes ────────────────────────────────────────────

    def create_record(self, model: str, attributes: dict[str, Any]) -> dict[str, Any]:
        """Create a new record.

        Args:
            model: Model name (e.g., "User", "Project")
            attributes: Attributes to set on the record

        Returns:
            Created record data

        Raises:
            QueryExecutionError: If creation fails

        """
        # Lazy imports avoid the service ↔ client cycle.
        from src.infrastructure.openproject.openproject_client import (
            _validate_model_name,
        )

        client = self._client
        _validate_model_name(model)

        # Build Rails command for creating a record
        # Use a simple, single-line approach that works well with tmux console
        # Convert Python values to Ruby equivalents. Aligned with
        # ``update_record``'s ``format_value`` so a ``None`` attribute
        # becomes Ruby ``nil`` (creating a record with a nullable
        # column) rather than the bare token ``None``, which the
        # Rails parser would treat as a constant lookup and raise
        # ``NameError``.
        attributes_str = _ruby_hash(attributes)
        command = (
            f"record = {model}.new({{{attributes_str}}}); "
            f"record.save ? record.as_json : {{'error' => record.errors.full_messages}}"
        )

        try:
            # Try execute_query_to_json_file first for better output handling
            result = client.execute_query_to_json_file(command)

            # Check if we got a valid dictionary
            if isinstance(result, dict):
                return result
            msg = f"Cannot confirm creation of {model} from the target response. Verify the target before retrying."
            raise QueryExecutionError(msg)
        except QueryExecutionError:
            raise
        except RubyError as e:
            msg = f"Failed to create {model}."
            raise QueryExecutionError(msg) from e
        except Exception as e:
            msg = f"Error creating {model}."
            raise QueryExecutionError(msg) from e

    def update_record(
        self,
        model: str,
        record_id: int,
        attributes: dict[str, Any],
    ) -> dict[str, Any]:
        """Update a record with given attributes.

        Args:
            model: Model name (e.g., "User", "Project")
            record_id: Record ID
            attributes: Attributes to update

        Returns:
            Updated record data

        Raises:
            RecordNotFoundError: If record doesn't exist
            QueryExecutionError: If update fails

        """
        from src.infrastructure.openproject.openproject_client import (
            _validate_model_name,
        )

        client = self._client
        _validate_model_name(model)

        # Build safely-escaped attributes for Ruby
        attributes_str = _ruby_hash(attributes)
        command = f"""
        record = {model}.find_by(id: {record_id})
        if record.nil?
          raise "Record not found"
        elsif record.update({{{attributes_str}}})
          record.as_json
        else
          raise "Failed to update record: #{{record.errors.full_messages.join(', ')}}"
        end
        """

        try:
            result = client.execute_json_query(command)
        except RubyError as e:
            if "Record not found" in str(e):
                msg = f"{model} with ID {record_id} not found"
                raise RecordNotFoundError(msg) from e
            msg = f"Failed to update {model}."
            raise QueryExecutionError(msg) from e
        except Exception as e:
            # Normalize to the same message tests expect for generic failures
            msg = f"Failed to update {model}."
            raise QueryExecutionError(msg) from e
        else:
            if not isinstance(result, dict):
                msg = (
                    f"Failed to update {model}: Invalid response from OpenProject (type={type(result)}, value={result})"
                )
                raise QueryExecutionError(msg)
            return result

    def delete_record(self, model: str, record_id: int) -> None:
        """Delete a record.

        Args:
            model: Model name (e.g., "User", "Project")
            record_id: Record ID

        Raises:
            RecordNotFoundError: If record doesn't exist
            QueryExecutionError: If deletion fails

        """
        from src.infrastructure.openproject.openproject_client import _validate_model_name

        client = self._client
        _validate_model_name(model)
        command = f"""
        record = {model}.find_by(id: {record_id})
        if record.nil?
          raise "Record not found"
        elsif record.destroy
          true
        else
          raise "Failed to delete record: #{{record.errors.full_messages.join(', ')}}"
        end
        """

        try:
            client.execute_query(command)
        except RubyError as e:
            if "Record not found" in str(e):
                msg = f"{model} with ID {record_id} not found"
                raise RecordNotFoundError(msg) from e
            msg = f"Failed to delete {model}."
            raise QueryExecutionError(msg) from e
        except Exception as e:
            msg = f"Error deleting {model}."
            raise QueryExecutionError(msg) from e
