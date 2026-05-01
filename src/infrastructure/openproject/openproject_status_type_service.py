"""Status + work-package-type read helpers for the OpenProject Rails console.

Phase 2r of ADR-002 continues the openproject_client.py god-class
decomposition by collecting two reference-data reads onto a focused
service. The service owns:

* ``get_statuses`` — dump all ``Status`` rows to a container JSON file
  via ``File.write`` and read back via ``docker exec cat``. Falls back
  to a fresh ``bundle exec rails runner`` process when the persistent
  Rails console misbehaves and ``enable_runner_fallback`` is on.
* ``get_work_package_types`` — same shape as ``get_statuses`` but
  pulls only minimal ``id`` / ``name`` columns from ``Type`` (avoids
  recursive ``as_json`` walks that have caused IRB stack overflows).

Both methods follow the same pattern:

1. Generate a unique container temp filename via the client's
   ``_generate_unique_temp_filename``.
2. Build a small Ruby script that ``File.write``s the JSON dump.
3. Run via the persistent Rails console; fall back to a rails-runner
   subprocess when the console is unavailable AND
   ``enable_runner_fallback`` is enabled.
4. SSH into the container with a tight retry loop, ``cat`` the file,
   parse JSON.
5. Best-effort delete the container temp file (preserved on error
   when ``preserve_debug_files_on_error`` is true).

Neither method interpolates user-controlled strings into Ruby — the
only dynamic content is the generated container path, so no
``escape_ruby_single_quoted`` is needed here.

``OpenProjectClient`` exposes the service via ``self.status_types``
and keeps thin delegators for the same method names so existing call
sites work unchanged.
"""

from __future__ import annotations

import json
import os
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src import config
from src.infrastructure.exceptions import QueryExecutionError

if TYPE_CHECKING:
    from src.infrastructure.openproject.openproject_client import OpenProjectClient


class OpenProjectStatusTypeService:
    """Status + work-package-type read helpers for ``OpenProjectClient``."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    def get_statuses(self) -> list[dict[str, Any]]:
        """Get all statuses from OpenProject.

        Returns:
            List of status objects

        Raises:
            QueryExecutionError: If query fails

        """
        client = self._client
        try:
            # Use file-based JSON to avoid tmux/console control characters
            file_path = client._generate_unique_temp_filename("statuses")
            file_path_interpolated = f"'{file_path}'"
            write_query = (
                "require 'json'; "
                f"statuses = Status.all.as_json; File.write({file_path_interpolated}, "
                "JSON.pretty_generate(statuses)); nil"
            )

            try:
                # Skip console attempt entirely if forced runner mode
                if os.environ.get("J2O_FORCE_RAILS_RUNNER"):
                    from src.infrastructure.openproject.rails_console_client import ConsoleNotReadyError

                    msg = "Forced runner mode via J2O_FORCE_RAILS_RUNNER"
                    raise ConsoleNotReadyError(msg)
                output = client.rails_client.execute(write_query, suppress_output=True)
                client._check_console_output_for_errors(output or "", context="get_statuses")
                self._logger.debug("Successfully executed statuses write command")
            except Exception as e:
                from src.infrastructure.openproject.rails_console_client import (
                    CommandExecutionError,
                    ConsoleNotReadyError,
                    RubyError,
                )

                if isinstance(e, (ConsoleNotReadyError, CommandExecutionError, RubyError, QueryExecutionError)):
                    if not config.migration_config.get("enable_runner_fallback", False):
                        # Respect user's preference to avoid per-request rails runner fallback
                        raise
                    self._logger.warning(
                        "Rails console failed for statuses (%s); falling back to rails runner",
                        type(e).__name__,
                    )
                    runner_script_path = f"/tmp/j2o_runner_{os.urandom(4).hex()}.rb"
                    local_tmp = Path(client.file_manager.data_dir) / "temp_scripts" / Path(runner_script_path).name
                    local_tmp.parent.mkdir(parents=True, exist_ok=True)
                    ruby_runner = (
                        "require 'json'\n"
                        "statuses = Status.all.as_json\n"
                        f"File.write('{file_path}', JSON.pretty_generate(statuses))\n"
                    )
                    with local_tmp.open("w", encoding="utf-8") as f:
                        f.write(ruby_runner)
                    client.docker_client.transfer_file_to_container(local_tmp, Path(runner_script_path))
                    runner_cmd = f"(cd /app || cd /opt/openproject) && bundle exec rails runner {runner_script_path}"
                    stdout, stderr, rc = client.docker_client.execute_command(runner_cmd)
                    if rc != 0:
                        msg = f"rails runner failed (rc={rc}): {stderr[:500]}"
                        raise QueryExecutionError(msg) from e
                else:
                    raise

            operation_succeeded = False  # Track success for debug file preservation
            try:
                ssh_command = f"docker exec {shlex.quote(client.container_name)} cat {shlex.quote(file_path)}"
                stdout = ""
                stderr = ""
                returncode = 1
                # Small retry loop to handle race where file may not be written yet
                for attempt in range(8):  # ~2 seconds total
                    try:
                        stdout, stderr, returncode = client.ssh_client.execute_command(ssh_command)
                    except Exception as e:
                        if "No such file or directory" in str(e):
                            time.sleep(0.25)
                            continue
                        raise
                    if returncode == 0:
                        if attempt > 0:
                            self._logger.debug(
                                "Recovered after %d attempts reading container file %s",
                                attempt + 1,
                                file_path,
                            )
                        break
                    if stderr and "No such file or directory" in stderr:
                        time.sleep(0.25)
                        continue
                    _emsg = f"Failed to read statuses file: {stderr or 'unknown error'}"
                    raise QueryExecutionError(_emsg)
                # Final returncode check after the retry loop — if the
                # file never appeared (e.g. persistent "No such file"
                # across all 8 attempts), ``stdout`` is empty and the
                # later ``json.loads`` would raise ``JSONDecodeError``,
                # obscuring the real failure. Match the explicit guard
                # ``get_work_package_types`` already has.
                if returncode != 0:
                    _emsg = f"Failed to read statuses file after retries: {stderr or 'unknown error'}"
                    raise QueryExecutionError(_emsg)
                parsed = json.loads(stdout)
                self._logger.info("Successfully loaded %d statuses from container file", len(parsed))
                operation_succeeded = True
                return parsed if isinstance(parsed, list) else []
            finally:
                preserve_on_error = config.migration_config.get("preserve_debug_files_on_error", True)
                should_cleanup = operation_succeeded or not preserve_on_error
                if not should_cleanup:
                    self._logger.warning(
                        "Preserving debug file due to error: %s (set preserve_debug_files_on_error=false to auto-cleanup)",
                        file_path,
                    )
                else:
                    try:
                        client.ssh_client.execute_command(
                            f"docker exec {shlex.quote(client.container_name)} rm -f {shlex.quote(file_path)}",
                        )
                    except Exception as cleanup_err:
                        self._logger.warning(
                            "Failed to cleanup container temp file %s: %s",
                            file_path,
                            cleanup_err,
                        )
        except Exception as e:
            msg = "Failed to get statuses."
            raise QueryExecutionError(msg) from e

    def get_work_package_types(self) -> list[dict[str, Any]]:
        """Get all work package types from OpenProject.

        Returns:
            List of work package type objects

        Raises:
            QueryExecutionError: If query fails

        """
        client = self._client
        try:
            # Use file-based JSON to avoid tmux/console artifacts and project only minimal fields
            file_path = client._generate_unique_temp_filename("work_package_types")
            file_path_interpolated = f"'{file_path}'"
            # Avoid Type#as_json on full AR models to prevent recursion/stack overflows in IRB
            # Only extract minimal attributes we actually need for mapping
            write_query = (
                "require 'json'; "
                "types = Type.select(:id, :name).map { |t| { id: t.id, name: t.name } }; "
                f"File.write({file_path_interpolated}, JSON.pretty_generate(types)); nil"
            )

            try:
                # Skip console attempt entirely if forced runner mode
                if os.environ.get("J2O_FORCE_RAILS_RUNNER"):
                    from src.infrastructure.openproject.rails_console_client import ConsoleNotReadyError

                    msg = "Forced runner mode via J2O_FORCE_RAILS_RUNNER"
                    raise ConsoleNotReadyError(msg)
                client.rails_client.execute(write_query, suppress_output=True)
                self._logger.debug("Successfully executed work package types write command")
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
                        "Rails console failed for work package types (%s); falling back to rails runner",
                        type(e).__name__,
                    )
                    runner_script_path = f"/tmp/j2o_runner_{os.urandom(4).hex()}.rb"
                    local_tmp = Path(client.file_manager.data_dir) / "temp_scripts" / Path(runner_script_path).name
                    local_tmp.parent.mkdir(parents=True, exist_ok=True)
                    ruby_runner = (
                        "require 'json'\n"
                        "types = Type.select(:id, :name).map { |t| { id: t.id, name: t.name } }\n"
                        f"File.write('{file_path}', JSON.pretty_generate(types))\n"
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

            operation_succeeded = False  # Track success for debug file preservation
            try:
                ssh_command = f"docker exec {shlex.quote(client.container_name)} cat {shlex.quote(file_path)}"
                stdout = ""
                stderr = ""
                returncode = 1
                # Small retry loop to handle race where file may not be written yet
                for _ in range(8):  # ~2 seconds total
                    try:
                        stdout, stderr, returncode = client.ssh_client.execute_command(ssh_command)
                    except Exception as e:
                        if "No such file or directory" in str(e):
                            time.sleep(0.25)
                            continue
                        raise
                    if returncode == 0:
                        break
                    if stderr and "No such file or directory" in stderr:
                        time.sleep(0.25)
                        continue
                    _emsg = f"Failed to read work package types file: {stderr or 'unknown error'}"
                    raise QueryExecutionError(_emsg)
                if returncode != 0:
                    _emsg = f"Failed to read work package types file: {stderr or 'unknown error'}"
                    raise QueryExecutionError(_emsg)
                parsed = json.loads(stdout.strip())
                self._logger.info(
                    "Successfully loaded %d work package types from container file",
                    len(parsed) if isinstance(parsed, list) else 0,
                )
                operation_succeeded = True
                return parsed if isinstance(parsed, list) else []
            finally:
                preserve_on_error = config.migration_config.get("preserve_debug_files_on_error", True)
                should_cleanup = operation_succeeded or not preserve_on_error
                if not should_cleanup:
                    self._logger.warning(
                        "Preserving debug file due to error: %s (set preserve_debug_files_on_error=false to auto-cleanup)",
                        file_path,
                    )
                else:
                    try:
                        client.ssh_client.execute_command(
                            f"docker exec {shlex.quote(client.container_name)} rm -f {shlex.quote(file_path)}",
                        )
                    except Exception as cleanup_err:
                        self._logger.warning(
                            "Failed to cleanup container temp file %s: %s",
                            file_path,
                            cleanup_err,
                        )
        except Exception as e:
            msg = "Failed to get work package types."
            raise QueryExecutionError(msg) from e
