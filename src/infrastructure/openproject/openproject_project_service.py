# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Project-related read operations against the OpenProject Rails console.

Phase 2k of ADR-002 continues the openproject_client.py god-class
decomposition by collecting the project-read helpers onto a focused
service. The service owns:

* ``get_projects`` — full-list read with optional ``top_level_only``
  filter, written via Rails console with a rails-runner fallback for
  console instability.
* ``get_project_by_identifier`` — single-project lookup by slug.
* ``get_project_enhanced`` — comprehensive project info (with
  work-package count) via JSON query.
* ``batch_get_projects_by_identifiers`` — paged ActiveRecord lookup
  with the shared ``@batch_idempotent`` decorator and a ``headers``
  kwarg for callers that want real cache hits.

The project *write* helpers (``upsert_project_attribute``,
``bulk_upsert_project_attributes``, ``ensure_project_version``, the
modules family, etc.) stay on the client for now and earn their own
follow-up extractions (Phase 2k.2 / 2k.3) — they have heavier coupling
to the work-package CF subsystem and the migration runner, and earn
focused diffs.

``OpenProjectClient`` exposes the service via ``self.projects`` and
keeps thin delegators for the same method names so existing call sites
work unchanged.
"""

from __future__ import annotations

from typing import Any

from src.infrastructure.exceptions import (
    QueryExecutionError,
    RecordNotFoundError,
)
from src.infrastructure.openproject.batch_records import index_batch_rows
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.utils.idempotency_decorators import batch_idempotent


class OpenProjectProjectService:
    """Project-related Rails-console read helpers for ``OpenProjectClient``."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    # ── reads ────────────────────────────────────────────────────────────

    def get_projects(self, *, top_level_only: bool = False) -> list[dict[str, Any]]:
        """Get projects from OpenProject using file-based approach.

        Args:
            top_level_only: When True, returns only top-level (company) projects with no parent

        Returns:
            List of OpenProject projects as dictionaries

        Raises:
            QueryExecutionError: If unable to retrieve projects

        """
        try:
            scope = "Project.where(parent_id: nil)" if top_level_only else "Project.all"
            query = (
                f"{scope}.includes(:enabled_modules).map do |p|\n"
                "  {\n"
                "    id: p.id,\n"
                "    name: p.name,\n"
                "    identifier: p.identifier,\n"
                "    description: p.description.to_s,\n"
                "    status: (p.respond_to?(:status) ? p.status&.name : nil),\n"
                "    status_code: p.status_code,\n"
                "    parent_id: p.parent_id,\n"
                "    public: p.public?,\n"
                "    active: p.active?,\n"
                "    enabled_modules: p.enabled_module_names\n"
                "  }\n"
                "end"
            )
            result = self._client.rails_runner.read_catalog(query, "get_projects")
            # The execute_query_to_json_file method should return the parsed JSON
            if not isinstance(result, list):
                self._logger.error(
                    "Expected list of projects, got %s: %s",
                    type(result),
                    str(result)[:200],
                )
                msg = f"Invalid projects data format - expected list, got {type(result)}"
                raise QueryExecutionError(msg)

            # Validate and clean project data
            validated_projects = []
            for project in result:
                if isinstance(project, dict) and project.get("id"):
                    # For OpenProject projects, identifier might be optional or missing
                    # Accept projects with at least an ID and name
                    enabled_modules = project.get("enabled_modules") or []
                    if isinstance(enabled_modules, list):
                        enabled_modules = sorted({str(mod) for mod in enabled_modules if mod})
                    else:
                        enabled_modules = []

                    validated_project = {
                        "id": project.get("id"),
                        "name": project.get("name", ""),
                        "identifier": project.get(
                            "identifier",
                            f"project-{project.get('id')}",
                        ),
                        "description": project.get("description", ""),
                        "public": bool(project.get("public", False)),
                        "status": project.get("status"),
                        "status_code": project.get("status_code"),
                        "parent_id": project.get("parent_id"),
                        "active": project.get("active"),
                        "enabled_modules": enabled_modules,
                    }
                    validated_projects.append(validated_project)
                    self._logger.debug("Validated project: %s", validated_project)
                else:
                    self._logger.debug(
                        "Skipping invalid project data (missing ID): %s",
                        project,
                    )

            self._logger.info(
                "Retrieved %d projects using file-based method",
                len(validated_projects),
            )
            return validated_projects

        except Exception as e:
            self._logger.exception("Failed to get projects using file-based method")
            msg = f"Failed to retrieve projects: {e}"
            raise QueryExecutionError(msg) from e

    def get_project_by_identifier(self, identifier: str) -> dict[str, Any]:
        """Get a project by identifier.

        Args:
            identifier: Project identifier or slug

        Returns:
            Project object

        Raises:
            RecordNotFoundError: If project with given identifier is not found
            QueryExecutionError: If query fails

        """
        # Lazy import: ``escape_ruby_single_quoted`` lives on the client
        # module; lazy keeps the service ↔ client cycle out of module-load
        # time. (See the same pattern in user/CF/runner services.)
        from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

        try:
            project = self._client.execute_json_query(
                f"Project.find_by(identifier: '{escape_ruby_single_quoted(identifier)}')",
            )
        except Exception as e:
            msg = "Failed to get project."
            raise QueryExecutionError(msg) from e
        if project is None:
            msg = f"Project with identifier '{identifier}' not found"
            raise RecordNotFoundError(msg)
        return project

    def get_project_enhanced(self, project_id: int) -> dict[str, Any]:
        """Get comprehensive project information.

        Returns the project record plus a small ``statistics`` block
        (work-package count, active flag).
        """
        script = f"""
        project = Project.find({project_id})
        work_package_count = project.work_packages.count

        {{
          project: {{
            id: project.id,
            name: project.name,
            identifier: project.identifier,
            description: project.description,
            status: project.status,
            created_at: project.created_at,
            updated_at: project.updated_at,
            # Back-compat keys (map *_on to *_at)
            created_on: project.created_at,
            updated_on: project.updated_at
          }},
          statistics: {{
            work_package_count: work_package_count,
            active: project.active?
          }}
        }}
        """

        try:
            return self._client.execute_json_query(script)
        except Exception as e:
            msg = f"Failed to get enhanced project data for ID {project_id}: {e}"
            raise QueryExecutionError(msg) from e

    @batch_idempotent(ttl=3600)  # 1 hour TTL for project identifier lookups
    def batch_get_projects_by_identifiers(
        self,
        identifiers: list[str],
        batch_size: int | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Find multiple projects by identifiers in batches with idempotency support.

        Args:
            identifiers: List of project identifiers to find
            batch_size: Size of each batch (defaults to configured batch_size)
            headers: Optional headers dict; when ``X-Idempotency-Key`` is
                present the ``@batch_idempotent`` decorator caches the
                result under that key for the configured TTL. Without a
                header the decorator's per-call UUID makes the cache a
                no-op (so callers that need real idempotency MUST pass
                a stable key). Keyword-only so it cannot be passed
                positionally — the decorator's ``extract_headers_from_kwargs``
                only sees real kwargs, so positional arguments would
                silently disable caching.

        Returns:
            Dictionary mapping identifier to project data for successfully
            fetched projects. Missing identifiers are omitted. If one or
            more batches fail, those failures are logged and the method
            continues processing remaining batches, so partial results may
            be returned rather than raising.

        Raises:
            QueryExecutionError: If a non-batch error occurs (e.g. the
                ``_validate_batch_size`` call rejects the input).

        """
        # ``headers`` is consumed by the ``@batch_idempotent`` decorator's
        # ``extract_headers_from_kwargs`` helper before the function body
        # runs; we accept-and-discard it here to keep the signature
        # compatible with that contract.
        del headers

        client = self._client
        if not identifiers:
            return {}

        # Validate and clamp batch size to prevent memory exhaustion. Use
        # ``is not None`` so a caller-supplied ``batch_size=0`` is
        # respected literally (rather than silently swapped for the
        # default) — ``_validate_batch_size`` will then reject it
        # explicitly if 0 is invalid.
        effective_batch_size = batch_size if batch_size is not None else getattr(client, "batch_size", 100)
        effective_batch_size = client._validate_batch_size(effective_batch_size)

        results: dict[str, dict[str, Any]] = {}

        # Process identifiers in batches
        for i in range(0, len(identifiers), effective_batch_size):
            batch_identifiers = identifiers[i : i + effective_batch_size]

            def batch_operation(batch_identifiers: list[str] = batch_identifiers) -> list[dict[str, Any]]:
                # Use safe query builder with ActiveRecord parameterization
                query = client._build_safe_batch_query(
                    "Project",
                    "identifier",
                    batch_identifiers,
                )
                return client.execute_json_query(query)  # type: ignore[return-value]

            try:
                # Execute batch operation with retry logic (with idempotency key propagation)
                batch_results = client._retry_with_exponential_backoff(
                    batch_operation,
                    f"Batch fetch projects by identifier "
                    f"{batch_identifiers[:2]}{'...' if len(batch_identifiers) > 2 else ''}",
                )

                results.update(index_batch_rows(batch_results, batch_identifiers, "identifier"))

            except Exception as e:
                self._logger.warning(
                    "Failed to fetch batch of project identifiers %s after retries: %s",
                    batch_identifiers,
                    e,
                )
                # Continue processing other batches rather than failing completely
                # Log individual failures for post-run review
                for identifier in batch_identifiers:
                    self._logger.debug(
                        "Failed to fetch project by identifier %s: %s",
                        identifier,
                        e,
                    )
                continue

        return results
