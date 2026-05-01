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

import json
import os
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src import config
from src.infrastructure.exceptions import (
    QueryExecutionError,
    RecordNotFoundError,
)
from src.utils.idempotency_decorators import batch_idempotent

if TYPE_CHECKING:
    from src.infrastructure.openproject.openproject_client import OpenProjectClient


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
        client = self._client
        # Track temp paths so the ``finally`` block can clean them up even
        # when an early exception bypasses normal flow. Container paths
        # land in ``/tmp`` inside the OpenProject container; the local
        # ``runner_script_local_tmp`` is written under the data dir.
        runner_script_container: str | None = None
        runner_script_local_tmp: Path | None = None
        file_path: str | None = None
        try:
            # Use pure file-based approach - write to file and read directly from filesystem
            file_path = client._generate_unique_temp_filename("projects")

            # Execute command to write JSON to file - use a simple command that returns minimal output
            # Split into Python variable interpolation (f-string) and Ruby script (raw string)
            file_path_interpolated = f"'{file_path}'"
            scope = "Project.where(parent_id: nil)" if top_level_only else "Project.all"
            write_query = (
                "require 'json'\n"
                "projects = " + scope + ".includes(:enabled_modules).map do |p|\n"
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
                "end\n" + f"File.write({file_path_interpolated}, JSON.pretty_generate(projects))\n" + "nil"
            )

            # Execute the write command - verify console output and fallback if needed
            try:
                out = client.rails_client.execute(write_query, suppress_output=True)
                client._check_console_output_for_errors(out or "", context="get_projects")
                self._logger.debug("Successfully executed projects write command")
            except Exception as e:
                from src.infrastructure.openproject.rails_console_client import (
                    CommandExecutionError,
                    ConsoleNotReadyError,
                    RubyError,
                )

                if isinstance(e, (ConsoleNotReadyError, CommandExecutionError, RubyError, QueryExecutionError)):
                    if not config.migration_config.get("enable_runner_fallback", False):
                        raise
                    self._logger.warning(
                        "Rails console failed for projects (%s); falling back to rails runner",
                        type(e).__name__,
                    )
                    runner_script_path = f"/tmp/j2o_runner_{os.urandom(4).hex()}.rb"
                    runner_script_container = runner_script_path
                    local_tmp = Path(client.file_manager.data_dir) / "temp_scripts" / Path(runner_script_path).name
                    runner_script_local_tmp = local_tmp
                    local_tmp.parent.mkdir(parents=True, exist_ok=True)
                    top_selector_line = (
                        "projects = Project.where(parent_id: nil).select("
                        ":id, :name, :identifier, :description, :status_code"
                        ").as_json\n"
                    )
                    all_selector_line = (
                        "projects = Project.all.select(:id, :name, :identifier, :description, :status_code).as_json\n"
                    )
                    ruby_runner = (
                        "require 'json'\n"
                        + (top_selector_line if top_level_only else all_selector_line)
                        + f"File.write('{file_path}', JSON.pretty_generate(projects))\n"
                    )
                    with local_tmp.open("w", encoding="utf-8") as f:
                        f.write(ruby_runner)
                    client.docker_client.transfer_file_to_container(local_tmp, Path(runner_script_path))
                    runner_cmd = f"(cd /app || cd /opt/openproject) && bundle exec rails runner {runner_script_path}"
                    stdout, stderr, rc = client.docker_client.execute_command(runner_cmd)
                    if rc != 0:
                        _emsg = f"rails runner failed (rc={rc}): {stderr[:500]}"
                        raise QueryExecutionError(_emsg) from e
                else:
                    raise

            # Read the JSON directly from the Docker container file system via SSH
            # Use SSH to read the file from the Docker container
            ssh_command = f"docker exec {shlex.quote(client.container_name)} cat {shlex.quote(file_path)}"
            try:
                stdout, stderr, returncode = client.ssh_client.execute_command(ssh_command)
            except Exception as e:
                msg = f"SSH command failed: {e}"
                raise QueryExecutionError(msg) from e
            if returncode != 0:
                self._logger.error(
                    "Failed to read file from container, stderr: %s",
                    stderr,
                )
                msg = f"SSH command failed with code {returncode}: {stderr}"
                raise QueryExecutionError(msg)

            file_content = stdout.strip()
            self._logger.debug(
                "Successfully read projects file from container, content length: %d",
                len(file_content),
            )

            # Parse the JSON content
            try:
                result = json.loads(file_content)
            except json.JSONDecodeError as e:
                self._logger.exception("Failed to read projects from container file %s", file_path)
                msg = f"Failed to read projects from container file: {e}"
                raise QueryExecutionError(msg) from e
            else:
                self._logger.info(
                    "Successfully loaded %d projects from container file",
                    len(result) if isinstance(result, list) else 0,
                )

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
        finally:
            # Best-effort cleanup of temp files left behind in the
            # container (and on the host for the runner-fallback script).
            # Failures here are logged and swallowed because cleanup is
            # advisory — the real work has already succeeded or raised
            # by this point.
            if file_path:
                try:
                    rm_cmd = f"docker exec {shlex.quote(client.container_name)} rm -f {shlex.quote(file_path)}"
                    client.ssh_client.execute_command(rm_cmd, check=False)
                except Exception as cleanup_err:
                    self._logger.debug(
                        "Non-critical: failed to remove container projects file %s: %s",
                        file_path,
                        cleanup_err,
                    )
            if runner_script_container:
                try:
                    rm_cmd = (
                        f"docker exec {shlex.quote(client.container_name)} rm -f {shlex.quote(runner_script_container)}"
                    )
                    client.ssh_client.execute_command(rm_cmd, check=False)
                except Exception as cleanup_err:
                    self._logger.debug(
                        "Non-critical: failed to remove container runner script %s: %s",
                        runner_script_container,
                        cleanup_err,
                    )
            if runner_script_local_tmp is not None:
                try:
                    if runner_script_local_tmp.exists():
                        runner_script_local_tmp.unlink()
                except Exception as cleanup_err:
                    self._logger.debug(
                        "Non-critical: failed to remove local runner script %s: %s",
                        runner_script_local_tmp,
                        cleanup_err,
                    )

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

                if batch_results:
                    # Ensure we have a list
                    if isinstance(batch_results, dict):
                        batch_results = [batch_results]

                    # Map results by identifier
                    for record in batch_results:
                        if isinstance(record, dict) and "identifier" in record:
                            identifier = record["identifier"]
                            if identifier in batch_identifiers:
                                results[identifier] = record

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
