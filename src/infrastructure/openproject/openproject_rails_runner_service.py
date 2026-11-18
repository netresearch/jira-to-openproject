# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Rails console runner — parsing, connectivity, and the ``execute*`` family.

Phases 2e-2i of ADR-002 collected the Rails-console helpers off
``OpenProjectClient`` into this focused service. The service now owns:

* **Connectivity** — ``is_connected()`` round-trips a unique echo against
  the persistent tmux Rails console.
* **Console-output validation** — ``check_console_output_for_errors``,
  ``assert_expected_console_notice`` flag Ruby errors and missing notices.
* **Output parsing** — ``parse_rails_output`` handles JSON, ``=> <value>``
  Rails console responses, scalar values, and TMUX line-wrap artefacts.
* **Small/medium ``execute*`` family** — ``execute`` (raw script with
  JSON-or-dict return), ``execute_query`` (low-level tmux send + capture),
  ``execute_query_to_json_file`` (file-based JSON path),
  ``execute_json_query`` (auto-as_json wrapper), ``execute_transaction``
  (multi-command transaction wrapper).
* **Batched / counted reads** — ``execute_batched_query`` (paged
  ``offset+limit`` reads with adaptive rate limiting) and
  ``count_records`` (marker-bracketed model count via direct tmux).
* **Heavyweight ``execute*`` family** — ``execute_script_with_data``
  (per-call structured-input pipeline with tmux markers and rails-runner
  fallback) and ``execute_large_query_to_json_file`` (large result sets
  written to a container file then ``cat``-piped via SSH to bypass
  tmux/console truncation).

``OpenProjectClient`` exposes the service via ``self.rails_runner`` and
keeps thin delegators for the same method names so existing call sites
work unchanged.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import time
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any

from src import config
from src.infrastructure.exceptions import JsonParseError, QueryExecutionError
from src.infrastructure.openproject.container_staging import private_container_directory
from src.infrastructure.openproject.rails_console_client import (
    CommandExecutionError,
    ConsoleNotReadyError,
    RubyError,
)

# Tunables for batched/paged Rails queries. Co-located with the service that
# uses them so the batched-query implementation has no back-reference to
# ``openproject_client``.
BATCH_SIZE_DEFAULT: int = 50
SAFE_OFFSET_LIMIT: int = 5000
from src.infrastructure.openproject.openproject_client import OpenProjectClient

_SKIP_CLIENT_FILES = (
    "/src/infrastructure/openproject/openproject_client.py",
    "/src/infrastructure/openproject/openproject_rails_runner_service.py",
    "/src/infrastructure/openproject/rails_console_client.py",
    "/src/infrastructure/openproject/docker_client.py",
    "/src/infrastructure/openproject/ssh_client.py",
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _caller_hint(default_component: str) -> str:
    try:
        import sys

        # Use sys._getframe instead of inspect.stack (O(1) per frame, no source loading)
        path: str | None = None
        func: str | None = None
        frame = sys._getframe(1)
        for _ in range(49):
            if frame is None:
                break
            filename = frame.f_code.co_filename
            if "/src/" in filename and not any(skip in filename for skip in _SKIP_CLIENT_FILES):
                path = filename.split("/src/")[-1]
                func = frame.f_code.co_name
                break
            frame = frame.f_back

        # Derive a concise component label from path
        component = default_component
        if path:
            component = (
                path.replace("migrations/", "migration/")
                .replace("clients/", "client/")
                .replace("_migration.py", "")
                .replace(".py", "")
            )

        parts: list[str] = ["j2o:", component]
        if func:
            parts.append(f"func={func}")
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        parts.append(f"ts={ts}")
        # include project filter if configured
        proj = (config.jira_config or {}).get("project_filter")
        if proj:
            parts.append(f"project={proj}")
        return " ".join(parts)
    except Exception:
        return f"j2o: {default_component}"


class OpenProjectRailsRunnerService:
    """Parsing + connectivity helpers for the OpenProject Rails console."""

    def __init__(self, client: OpenProjectClient) -> None:
        self._client = client
        self._logger = client.logger

    # ── connectivity ──────────────────────────────────────────────────────

    def is_connected(self) -> bool:
        """Test if connected to the OpenProject Rails console.

        Sends a unique echo command and confirms the response contains it.
        """
        try:
            unique_id = secrets.token_hex(3)
            command = f'puts "OPENPROJECT_CONNECTION_TEST_{unique_id}"'
            result = self._client.rails_client.execute(command)
            return f"OPENPROJECT_CONNECTION_TEST_{unique_id}" in result
        except Exception:
            self._logger.exception("Connection test failed.")
            return False

    # ── console-output validation ─────────────────────────────────────────

    def check_console_output_for_errors(self, output: str, context: str) -> None:
        """Raise a QueryExecutionError if console output indicates a Ruby error.

        Catches cases where start/end markers were missing and the console
        client returned raw lines, including SystemStackError or other Ruby
        exceptions.
        """
        if not output:
            return
        lines = [ln.strip() for ln in output.strip().splitlines()]
        has_error_marker = any(ln == "--EXEC_ERROR--" or ln.startswith("--EXEC_ERROR--") for ln in lines)
        severe_pattern = (
            ("SystemStackError" in output) or ("full_message':" in output) or ("stack level too deep" in output)
        )
        if has_error_marker or severe_pattern:
            informative = [
                ln
                for ln in lines
                if ("SystemStackError" in ln)
                or ("stack level too deep" in ln)
                or ("full_message':" in ln)
                or ln.startswith("--EXEC_ERROR--")
            ]
            snippet = informative or lines[:6]
            q_msg = f"Console error during {context}: {' | '.join(snippet[:8])}"
            raise QueryExecutionError(q_msg)

    def assert_expected_console_notice(
        self,
        output: str,
        expected_prefix: str,
        context: str,
    ) -> None:
        """Treat any unexpected console response as error in strict file-write flows.

        For file-based JSON writes we expect a specific notice line like
        "Statuses data written to /tmp/...". If it's missing or the output
        contains unrelated content, flag as error to avoid silently accepting
        partial/garbled output.
        """
        if not output:
            q_msg = f"No console output during {context}; expected '{expected_prefix}...'"
            raise QueryExecutionError(q_msg)
        lines = [ln.strip() for ln in output.strip().splitlines() if ln.strip()]
        if not any(expected_prefix in ln for ln in lines):
            sample = " | ".join(lines[:5])
            q_msg = f"Unexpected console output during {context}; expected '{expected_prefix}...'. Got: {sample[:300]}"
            raise QueryExecutionError(q_msg)

    # ── output parsing ────────────────────────────────────────────────────

    def parse_rails_output(self, result_output: str) -> object:
        """Parse Rails console output to extract JSON or scalar values.

        Handles various Rails console output formats including:

        * JSON arrays and objects
        * Scalar values (numbers, booleans, strings)
        * Rails console responses with ``=> `` prefix
        * Empty / nil responses
        * TMUX marker-based output extraction

        Returns the parsed data (dict, list, scalar value, or None).
        """
        from src.infrastructure.openproject.openproject_client import _RE_ANSI_ESCAPE, _RE_CTRL_CHARS

        if not result_output or not result_output.strip():
            return None
        try:
            text = _RE_CTRL_CHARS.sub("", _RE_ANSI_ESCAPE.sub("", result_output.strip()))
            if text.startswith(("[", "{")):
                return json.loads(text)
            lines = [
                line.strip()
                for line in text.splitlines()
                if line.strip() and not line.strip().startswith("open-project(")
            ]
            text = "\n".join(lines)
            for line in lines:
                if line.startswith("=> ") and not (line == "=> nil" and ("[" in text or "{" in text)):
                    return self._console_value(line[3:].strip(), allow_text=True)
            return self._embedded_console_value(lines, text)
        except json.JSONDecodeError as exc:
            raise JsonParseError(str(exc)) from exc

    def _embedded_console_value(self, lines: list[str], text: str) -> Any:
        for left, right in (("[", "]"), ("{", "}")):
            start, end = text.find(left), text.rfind(right)
            if start >= 0 and end > start:
                return json.loads(text[start : end + 1])
        for previous, current in pairwise(lines):
            if current.startswith("=> nil") and previous.startswith(("[", "{")):
                return json.loads(previous)
        return self._console_value(text, allow_text=False)

    @staticmethod
    def _console_value(value: str, *, allow_text: bool) -> Any:
        if value.startswith(("[", "{")):
            return json.loads(value)
        if value == "nil":
            return None
        if value in ("true", "false"):
            return value == "true"
        if value.isdigit():
            return int(value)
        if value and value[0] in {'"', "'"} and value[-1] == value[0]:
            return value[1:-1]
        if allow_text:
            return value
        msg = "Unable to parse Rails console output"
        raise JsonParseError(msg)

    # ── execute family (small/medium) ─────────────────────────────────────

    def execute(self, script_content: str) -> Any:
        """Execute a Ruby script directly.

        Returns the parsed JSON value if the console output is valid JSON
        (which can be a dict, list, or scalar — ``json.loads`` is structure-
        preserving). Otherwise wraps the raw text in a ``{"result": ...}``
        dict.

        The return is therefore deliberately ``Any`` rather than
        ``dict[str, Any]``: callers must inspect the shape if they need to
        distinguish list/scalar/None responses from the dict-wrapped fallback.
        """
        # Route through the client's delegator so monkeypatches on
        # ``op_client.execute_query`` (used in tests) take effect, mirroring
        # the same pattern used in ChangeAwareRunner.
        result = self._client.execute_query(script_content)
        try:
            return json.loads(result)
        except json.JSONDecodeError, TypeError:
            return {"result": result}

    def execute_query(self, query: str, timeout: int | None = None) -> str:
        """Execute a Rails query via the persistent tmux console.

        Args:
            query: Rails query to execute.
            timeout: Timeout in seconds; defaults to 30.

        Returns:
            Raw text output from the Rails console.

        """
        client = self._client
        client._last_query = query
        effective_timeout = timeout if timeout is not None else 30
        return client.rails_client._send_command_to_tmux(
            f"puts ({query})",
            effective_timeout,
        )

    def execute_query_to_json_file(self, query: str, timeout: int | None = None) -> dict[str, Any]:
        """Execute a Rails query and return parsed JSON via the file path.

        File-based execution avoids tmux/console noise; the Ruby payload writes
        to a tempfile inside the container which we read back.

        Routed through the client's ``execute_large_query_to_json_file`` for
        now (that method stays on ``OpenProjectClient`` until Phase 2g).
        """
        try:
            _ts = int(__import__("time").time())
            # Append a per-call random token so two calls within the same
            # wall-clock second in the same process never collide on the
            # tempfile path and read back each other's stale JSON — which
            # surfaced as "Unexpected response when ensuring reporting
            # project" (a stale groups list) during the #260 run.
            container_file = f"j2o_query_{_ts}_{os.getpid()}_{secrets.token_hex(8)}.json"
            return self._client.execute_large_query_to_json_file(
                query,
                container_file=container_file,
                timeout=timeout,
            )
        except RubyError:
            self._logger.exception("Ruby error during execute_query_to_json_file")
            raise
        except Exception as e:
            self._logger.exception("Error in execute_query_to_json_file")
            raise QueryExecutionError(str(e)) from e

    def execute_json_query(self, query: str, timeout: int | None = None) -> object:
        """Execute a Rails query and return parsed JSON.

        Auto-wraps the query with ``.as_json`` if the caller didn't already
        request a JSON conversion. Then routes through
        :py:meth:`execute_query_to_json_file`.
        """
        if not (".to_json" in query or ".as_json" in query):
            json_query = f"{query}.as_json" if query.strip().endswith(")") else f"({query}).as_json"
        else:
            json_query = query

        return self.execute_query_to_json_file(json_query, timeout)

    def execute_transaction(self, commands: list[str]) -> object:
        """Execute multiple Ruby/Rails commands inside a single transaction.

        Wraps the commands in ``ActiveRecord::Base.transaction do ... end``
        and runs the block via :py:meth:`execute_query`.
        """
        transaction_commands = "\n".join(commands)
        transaction_block = f"""
        ActiveRecord::Base.transaction do
          {transaction_commands}
        end
        """

        try:
            # Same monkeypatch-friendly pattern as ``execute`` above —
            # route through the client's delegator.
            return self._client.execute_query(transaction_block)
        except Exception as e:
            msg = "Transaction failed."
            raise QueryExecutionError(msg) from e

    # ── batched / counted queries ─────────────────────────────────────────

    def execute_batched_query(
        self,
        model_name: str,
        timeout: int | None = None,
    ) -> list[dict[str, Any]]:
        """Execute a query in batches to avoid console-buffer truncation.

        Tries a simple non-batched query first for small datasets; falls back
        to ``unscoped.order(:id).offset(N).limit(B)`` paging when more rows
        are available, with adaptive rate-limiting via the client's rate
        limiter.
        """
        client = self._client
        try:
            output = client.execute_query(f"{model_name}.limit({BATCH_SIZE_DEFAULT}).to_json", timeout=timeout)
            try:
                data = client._parse_rails_output(output)
            except Exception:
                self._logger.debug("Simple query failed, falling back to batched approach")
            else:
                if isinstance(data, dict):
                    return [data]
                if not isinstance(data, list):
                    return []
                if len(data) < BATCH_SIZE_DEFAULT:
                    return data
            return self._read_record_pages(model_name, timeout)
        except Exception:
            self._logger.exception("Batched query failed")
            return []

    def _read_record_pages(self, model: str, timeout: int | None) -> list[dict[str, Any]]:
        client = self._client
        results: list[dict[str, Any]] = []
        for offset in range(0, SAFE_OFFSET_LIMIT + 1, BATCH_SIZE_DEFAULT):
            client.rate_limiter.wait_if_needed(f"batched_query_{model}")
            query = f"{model}.unscoped.order(:id).offset({offset}).limit({BATCH_SIZE_DEFAULT}).to_json"
            start = time.monotonic()
            output = client.execute_query(query, timeout=timeout)
            elapsed = time.monotonic() - start
            try:
                page = client._parse_rails_output(output)
            except Exception:
                self._logger.exception("Failed to parse batch at offset %d", offset)
                client.rate_limiter.record_response(elapsed, 500)
                break
            client.rate_limiter.record_response(elapsed, 200)
            if isinstance(page, dict):
                page = [page] if page else []
            if not isinstance(page, list):
                self._logger.warning("Unexpected batch type %s at offset %d", type(page), offset)
                break
            results.extend(page)
            if len(page) < BATCH_SIZE_DEFAULT:
                break
        else:
            self._logger.warning("Reached safety limit of %d records", SAFE_OFFSET_LIMIT)
        return results

    def count_records(self, model: str) -> int:
        """Count records for a given Rails model.

        Sends ``puts {model}.count`` wrapped in unique start/end markers and
        parses the count out of the tmux pane capture. Doesn't go through
        ``execute_query`` because we need exact stdout positioning to extract
        the integer reliably across timing variations.

        Raises:
            QueryExecutionError: If the count cannot be parsed.

        """
        client = self._client
        client._validate_model_name(model)

        marker_id = secrets.token_hex(8)
        start_marker = f"J2O_COUNT_START_{marker_id}"
        end_marker = f"J2O_COUNT_END_{marker_id}"

        query = f'puts "{start_marker}"; puts {model}.count; puts "{end_marker}"'

        target = client.rails_client._get_target()
        tmux = shutil.which("tmux") or "tmux"

        escaped_command = client.rails_client._escape_command(query)
        subprocess.run(
            [tmux, "send-keys", "-t", target, escaped_command, "Enter"],
            capture_output=True,
            text=True,
            check=True,
        )

        max_wait = 30
        start_time = time.time()
        result = ""

        while time.time() - start_time < max_wait:
            time.sleep(0.3)
            cap = subprocess.run(
                [tmux, "capture-pane", "-p", "-S", "-100", "-t", target],
                capture_output=True,
                text=True,
                check=True,
            )
            result = cap.stdout
            if end_marker in result:
                break

        lines = [line.strip() for line in result.splitlines()]
        for start, count, end in zip(lines, lines[1:], lines[2:], strict=False):
            if start == start_marker and end == end_marker and count.isdigit():
                return int(count)

        msg = f"Unable to parse count result for {model}: end_marker={end_marker in result}"
        raise QueryExecutionError(msg)

    # ── script + structured-data execution ────────────────────────────────

    def execute_script_with_data(
        self,
        script_content: str,
        data: Any,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Run a structured-data script in private staging and read its marked JSON output."""
        base = Path(self._client.file_manager.data_dir) / "temp_scripts"
        base.mkdir(parents=True, exist_ok=True)
        with (
            private_container_directory(self._client.docker_client) as remote,
            TemporaryDirectory(dir=base, prefix="structured-", delete=False) as local_dir,
        ):
            local = Path(local_dir)
            try:
                result = self._run_structured_script(script_content, data, timeout, local, remote)
            except BaseException:
                if config.migration_config.get("preserve_debug_files_on_error", True):
                    self._logger.warning("Preserved failed Rails script inputs in %s", local)
                else:
                    shutil.rmtree(local)
                raise
            shutil.rmtree(local)
            return result

    def _run_structured_script(
        self, script_content: str, data: Any, timeout: int | None, local: Path, remote: Path
    ) -> dict[str, Any]:
        from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

        input_path = local / "input.json"
        try:
            with input_path.open("w", encoding="utf-8") as stream:
                json.dump(data, stream)
        except (OSError, ValueError, TypeError) as exc:
            msg = f"Failed to serialize input data: {exc}"
            raise QueryExecutionError(msg) from exc
        marker = secrets.token_hex(8)
        start, end = f"JSON_OUTPUT_START_{marker}", f"JSON_OUTPUT_END_{marker}"
        source = escape_ruby_single_quoted((remote / input_path.name).as_posix())
        output_path = remote / "output.txt"
        output_literal = escape_ruby_single_quoted(output_path.as_posix())
        script = (
            f"require 'json'\ninput_data = JSON.parse(File.read('{source}'))\n"
            "previous_stdout, previous_start, previous_end = $stdout, $j2o_start_marker, $j2o_end_marker\n"
            f"$j2o_start_marker, $j2o_end_marker = '{start}', '{end}'\n"
            f"File.open('{output_literal}', 'w') do |j2o_output|\n"
            "  begin\n    $stdout = j2o_output\n"
            f"{script_content}\n"
            "  ensure\n    $stdout, $j2o_start_marker, $j2o_end_marker = previous_stdout, previous_start, previous_end\n"
            "  end\nend\n"
        )
        script_path = local / "structured.rb"
        script_path.write_text(script, encoding="utf-8")
        self._client.transfer_file_to_container(input_path, remote / input_path.name)
        remote_script = (remote / script_path.name).as_posix()
        self._client.transfer_file_to_container(script_path, Path(remote_script))
        self._execute_structured_file(remote_script, timeout)
        output, stderr, status = self._client.docker_client.execute_command(f"cat -- {shlex.quote(str(output_path))}")
        if status:
            msg = f"Unable to read Rails script output (rc={status}): {stderr[:500]}"
            raise QueryExecutionError(msg)
        return self._structured_result(output, start, end)

    def _execute_structured_file(self, script: str, timeout: int | None) -> None:
        from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

        if os.getenv("J2O_FORCE_RAILS_RUNNER"):
            self._run_staged_query(script, timeout or 120)
            return
        try:
            output = self._client.rails_client.execute(
                f"load '{escape_ruby_single_quoted(script)}'", timeout=timeout
            )
            self.check_console_output_for_errors(output or "", context="structured Rails script")
        except ConsoleNotReadyError:
            if not config.migration_config.get("enable_runner_fallback", False):
                raise
            self._run_staged_query(script, timeout or 120)

    @staticmethod
    def _structured_result(output: str, start: str, end: str) -> dict[str, Any]:
        from src.infrastructure.openproject.openproject_client import (
            _RE_ANSI_ESCAPE,
            _RE_CTRL_CHARS,
            _RE_OSC_ESCAPE,
            _RE_OTHER_ESCAPE,
        )

        text = output.replace("\n", "").replace("\r", "")
        before_end, found_end, _tail = text.rpartition(end)
        _head, found_start, payload = before_end.rpartition(start)
        if not (found_start and found_end):
            return {"status": "error", "message": "JSON markers not found in Rails output", "output": text[:2000]}
        for pattern in (_RE_ANSI_ESCAPE, _RE_OSC_ESCAPE, _RE_OTHER_ESCAPE, _RE_CTRL_CHARS):
            payload = pattern.sub("", payload)
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            candidate = re.search(r"[\[{]", payload)
            try:
                parsed, _ = json.JSONDecoder().raw_decode(payload[candidate.start() :] if candidate else payload)
            except json.JSONDecodeError as exc:
                msg = f"Failed to parse JSON output: {exc}"
                raise QueryExecutionError(msg) from exc
        return {"status": "success", "message": "Script executed successfully", "data": parsed, "output": text[:2000]}

    # ── large-result-set queries via container file ──────────────────────

    def execute_large_query_to_json_file(
        self,
        query: str,
        container_file: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Execute a Rails query by writing JSON to a container file, then read it back.

        Use this for large result sets to avoid tmux/console truncation and parsing fragility.
        This method suppresses console output and relies on Docker+SSH to retrieve data.

        Args:
            query: Rails query to execute; will be coerced to JSON in Ruby
            container_file: Optional output filename. An explicit absolute path selects a caller-owned destination
            timeout: Optional Ruby execution timeout. When ``None`` the per-path
                defaults are used: 90s for the persistent tmux console
                ``execute()`` call and 300s for ``bundle exec rails runner``
                subprocesses (the runner has higher cold-start cost on large
                projects). These values are deliberately tighter than the
                ``RailsConsoleClient.command_timeout`` default (180s) — large
                JSON queries should fail fast when the console is unstable so
                the rails-runner fallback can take over.

        Returns:
            Parsed JSON data

        """
        with private_container_directory(self._client.docker_client) as directory:
            destination = Path(container_file or "result.json")
            if not destination.is_absolute():
                destination = directory / destination.name
            script = self._json_export_script(query, destination)
            self._run_json_script(script, directory, timeout)
            return self._read_query_result(str(destination))

    def read_catalog(self, query: str, catalog: str) -> Any:
        """Read a complete catalog using the persistent console and configured fallback policy."""
        with private_container_directory(self._client.docker_client) as directory:
            destination = directory / "result.json"
            script = self._json_export_script(query, destination)
            if os.getenv("J2O_FORCE_RAILS_RUNNER"):
                self._run_query_file(script, directory, None)
            else:
                try:
                    output = self._client.rails_client.execute(script, suppress_output=True)
                    self.check_console_output_for_errors(output or "", context=catalog)
                except ConsoleNotReadyError, CommandExecutionError, QueryExecutionError:
                    if not config.migration_config.get("enable_runner_fallback", False):
                        raise
                    self._run_query_file(script, directory, None)
            return self._read_query_result(str(destination))

    @staticmethod
    def _json_export_script(query: str, output: Path) -> str:
        from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

        ruby_json_expr = f"({query}).as_json"
        ruby_path_literal = escape_ruby_single_quoted(output.as_posix())
        provenance = _caller_hint("query/json")

        return (
            f"# {provenance}\n"
            "require 'json'\n"
            "begin; require 'fileutils'; rescue; end\n"
            f"data = {ruby_json_expr}\n"
            f"File.open('{ruby_path_literal}', 'w') do |f|\n"
            "  f.write(JSON.generate(data))\n"
            "  begin; f.flush; f.fsync; rescue; end\n"
            "end\n"
            f"begin; FileUtils.chmod(0644, '{ruby_path_literal}'); rescue; end\n"
        )

    def _stage_query_script(self, script: str, directory: Path) -> str:
        destination = directory / "query.rb"
        scripts = Path(self._client.file_manager.data_dir) / "temp_scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=scripts, suffix=".rb") as source:
            source.write(script)
            source.flush()
            self._client.docker_client.transfer_file_to_container(Path(source.name), destination)
        return destination.as_posix()

    def _run_staged_query(self, path: str, timeout: int | None) -> None:
        command = f"(cd /app || cd /opt/openproject) && bundle exec rails runner {shlex.quote(path)}"
        _, stderr, status = self._client.docker_client.execute_command(
            command, timeout=300 if timeout is None else timeout
        )
        if status:
            msg = f"rails runner failed (rc={status}): {stderr[:500]}"
            raise QueryExecutionError(msg)

    def _run_query_file(self, script: str, directory: Path, timeout: int | None) -> None:
        self._run_staged_query(self._stage_query_script(script, directory), timeout)

    def _run_json_script(self, script: str, directory: Path, timeout: int | None) -> None:
        from src.infrastructure.openproject.openproject_client import escape_ruby_single_quoted

        max_lines = _env_int("J2O_SCRIPT_RUNNER_MAX_LINES", 10)
        max_chars = _env_int("J2O_SCRIPT_RUNNER_THRESHOLD", 200)
        if script.count("\n") + 1 >= max_lines or len(script) >= max_chars:
            path = self._stage_query_script(script, directory)
            if (
                os.getenv("J2O_FORCE_RAILS_RUNNER")
                or (os.getenv("J2O_SCRIPT_LOAD_MODE") or "console").lower() != "console"
            ):
                self._run_staged_query(path, timeout)
                return
            try:
                output = self._client.rails_client.execute(
                    f"load '{escape_ruby_single_quoted(path)}'", timeout=timeout or 90, suppress_output=True
                )
                self.check_console_output_for_errors(output or "", context="execute_large_query_to_json_file(load)")
            except Exception:
                self._run_staged_query(path, timeout)
            return

        try:
            output = self._client.rails_client.execute(script, timeout=timeout or 90, suppress_output=True)
            self.check_console_output_for_errors(output or "", context="execute_large_query_to_json_file")
        except ConsoleNotReadyError, CommandExecutionError:
            if not config.migration_config.get("enable_runner_fallback", False):
                raise
            self._run_query_file(script, directory, timeout)

    def _read_query_result(self, container_file: str) -> Any:
        client = self._client
        # Read file back from container via SSH (avoids tmux buffer limits)
        ssh_command = f"docker exec {shlex.quote(client.container_name)} cat {shlex.quote(container_file)}"

        attempts = max(1, _env_int("J2O_QUERY_RESULT_WAIT_SECONDS", 600) * 2)
        for attempt in range(attempts):
            try:
                stdout, stderr, returncode = client.ssh_client.execute_command(ssh_command, check=False)
            except Exception as exc:
                raise QueryExecutionError(str(exc)) from exc
            if returncode == 0 and stdout:
                break
            if returncode and "No such file or directory" not in (stderr or ""):
                break
            if attempt and attempt % 10 == 0:
                self._logger.info("Waiting for query result file %s (waited %.1fs)", container_file, attempt / 2)
            time.sleep(0.5)
        if returncode:
            msg = f"SSH command failed with code {returncode}: {stderr}"
            raise QueryExecutionError(msg)
        try:
            return json.loads(stdout.strip())
        except ValueError as exc:
            raise JsonParseError(str(exc)) from exc
