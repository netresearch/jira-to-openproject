"""Rails console runner — parsing, connectivity, and the ``execute*`` family.

Phases 2e + 2f + 2g of ADR-002 collected the Rails-console helpers off
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

The two giant heavyweights stay on ``OpenProjectClient`` for follow-up
PRs (Phase 2h / 2i) — they have heavier internal coupling
(per-script subprocess + file transfer + JSON marker extraction +
Rails-runner fallback) and earn their own focused extraction:

* ``execute_script_with_data`` (~325 LOC)
* ``execute_large_query_to_json_file`` (~240 LOC)

``OpenProjectClient`` exposes the service via ``self.rails_runner`` and
keeps thin delegators for the same method names so existing call sites
work unchanged.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src import config
from src.clients.exceptions import JsonParseError, QueryExecutionError
from src.clients.rails_console_client import (
    CommandExecutionError,
    ConsoleNotReadyError,
    RubyError,
)

# Tunables for batched/paged Rails queries. Co-located with the service that
# uses them so the batched-query implementation has no back-reference to
# ``openproject_client``.
BATCH_SIZE_DEFAULT: int = 50
SAFE_OFFSET_LIMIT: int = 5000

if TYPE_CHECKING:
    from src.clients.openproject_client import OpenProjectClient


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
        # Lazy imports avoid the openproject_client ↔ this-module cycle
        # at module load time.
        from src.clients.openproject_client import _RE_ANSI_ESCAPE, _RE_CTRL_CHARS

        if not result_output or result_output.strip() == "":
            self._logger.debug("Empty or None result output")
            return None

        try:
            self._logger.debug("Raw result_output: %s", repr(result_output[:500]))
            text = result_output.strip()

            # Sanitize terminal artifacts to protect JSON parsing
            try:
                text = _RE_ANSI_ESCAPE.sub("", text)
                text = _RE_CTRL_CHARS.sub("", text)
            except Exception:
                self._logger.debug(
                    "ANSI/control char sanitization failed; continuing with raw text",
                )

            # If it's plain JSON, parse immediately
            if text.startswith(("[", "{")):
                try:
                    return json.loads(text)
                except json.JSONDecodeError as e:
                    raise JsonParseError(str(e)) from e

            # TMUX_CMD_* markers removed; rely on EXEC_* markers and direct JSON

            # Drop Rails prompt lines, but preserve following JSON
            lines_in = text.split("\n")
            lines: list[str] = []
            for ln in lines_in:
                if ln.strip().startswith("open-project("):
                    continue
                if ln.strip():
                    lines.append(ln)
            text = "\n".join(lines)

            # Handle Rails prefixed outputs like "=> <value>"
            for ln in (seg.strip() for seg in text.split("\n")):
                if ln.startswith("=> "):
                    val = ln[3:].strip()
                    # If '=> nil' but JSON is present elsewhere, prefer the JSON
                    if val == "nil" and ("[" in text or "{" in text):
                        continue
                    if val.startswith(("[", "{")):
                        try:
                            return json.loads(val)
                        except json.JSONDecodeError as e:
                            raise JsonParseError(str(e)) from e
                    if val == "nil":
                        return None
                    if val == "true":
                        return True
                    if val == "false":
                        return False
                    if val.isdigit():
                        return int(val)
                    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                        return val[1:-1]
                    return val

            # Try bracket-slice JSON extraction (prefer arrays before scalars)
            lb = text.find("[")
            rb = text.rfind("]")
            if lb != -1 and rb != -1 and rb > lb:
                try:
                    return json.loads(text[lb : rb + 1])
                except json.JSONDecodeError as e:
                    cleaned = _RE_CTRL_CHARS.sub("", text[lb : rb + 1])
                    try:
                        return json.loads(cleaned)
                    except Exception:
                        raise JsonParseError(str(e)) from e
            lb = text.find("{")
            rb = text.rfind("}")
            if lb != -1 and rb != -1 and rb > lb:
                try:
                    return json.loads(text[lb : rb + 1])
                except json.JSONDecodeError as e:
                    cleaned = _RE_CTRL_CHARS.sub("", text[lb : rb + 1])
                    try:
                        return json.loads(cleaned)
                    except Exception:
                        raise JsonParseError(str(e)) from e

            # Special case: prompt + JSON + => nil (common Rails console pattern)
            if "=> nil" in text:
                lines2 = text.split("\n")
                for i, ln in enumerate(lines2):
                    if ln.strip().startswith("=> nil") and i > 0:
                        prev = lines2[i - 1].strip()
                        if prev.startswith(("[", "{")):
                            try:
                                return json.loads(prev)
                            except json.JSONDecodeError as e:
                                raise JsonParseError(str(e)) from e
                lb = text.find("[")
                rb = text.rfind("]")
                if lb != -1 and rb != -1 and rb > lb:
                    try:
                        return json.loads(text[lb : rb + 1])
                    except json.JSONDecodeError as e:
                        raise JsonParseError(str(e)) from e
                lb = text.find("{")
                rb = text.rfind("}")
                if lb != -1 and rb != -1 and rb > lb:
                    try:
                        return json.loads(text[lb : rb + 1])
                    except json.JSONDecodeError as e:
                        raise JsonParseError(str(e)) from e

            # Scalars
            t = text.strip()
            if t.isdigit():
                return int(t)
            if t in ("true", "false"):
                return t == "true"
            if t == "nil":
                return None
            if (t.startswith('"') and t.endswith('"')) or (t.startswith("'") and t.endswith("'")):
                return t[1:-1]

            _msg = "Unable to parse Rails console output"
            raise JsonParseError(_msg)

        except JsonParseError:
            raise
        except json.JSONDecodeError as e:
            # Normalize any JSON decoding errors to JsonParseError as tests expect
            raise JsonParseError(str(e)) from e
        except Exception as e:
            self._logger.exception("Failed to process query result: %s", repr(e))
            self._logger.exception("Raw output: %s", result_output[:200])
            msg = f"Failed to parse Rails console output: {e}"
            raise QueryExecutionError(msg) from e

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
            container_file = f"/tmp/j2o_query_{_ts}_{os.getpid()}.json"
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
            simple_query = f"{model_name}.limit({BATCH_SIZE_DEFAULT}).to_json"
            result_output = client.execute_query(simple_query, timeout=timeout)

            try:
                simple_data = client._parse_rails_output(result_output)

                if isinstance(simple_data, list) and len(simple_data) < BATCH_SIZE_DEFAULT:
                    self._logger.debug(
                        "Retrieved %d total records using simple query",
                        len(simple_data),
                    )
                    return simple_data
                if isinstance(simple_data, list) and len(simple_data) == BATCH_SIZE_DEFAULT:
                    self._logger.debug(
                        "Simple query returned %d items, using batched approach for complete data",
                        BATCH_SIZE_DEFAULT,
                    )
                elif isinstance(simple_data, dict):
                    self._logger.debug("Retrieved 1 record using simple query")
                    return [simple_data]
                elif simple_data is not None:
                    self._logger.debug("Retrieved non-list data using simple query")
                    self._logger.warning(
                        "Unexpected data type from simple query: %s",
                        type(simple_data),
                    )
                    return []
                else:
                    return []

            except Exception:
                self._logger.debug(
                    "Simple query failed, falling back to batched approach",
                )

            all_results: list[dict[str, Any]] = []
            batch_size = BATCH_SIZE_DEFAULT
            offset = 0

            while True:
                client.rate_limiter.wait_if_needed(f"batched_query_{model_name}")
                query = f"{model_name}.unscoped.order(:id).offset({offset}).limit({batch_size}).to_json"
                operation_start = time.time()
                result_output = client.execute_query(query, timeout=timeout)
                operation_time = time.time() - operation_start

                try:
                    batch_data = client._parse_rails_output(result_output)
                    client.rate_limiter.record_response(operation_time, 200)

                    if not batch_data or (isinstance(batch_data, list) and len(batch_data) == 0):
                        break

                    if isinstance(batch_data, dict):
                        batch_data = [batch_data]

                    if isinstance(batch_data, list):
                        all_results.extend(batch_data)
                        if len(batch_data) < batch_size:
                            break
                    else:
                        self._logger.warning(
                            "Unexpected data type from batch query: %s",
                            type(batch_data),
                        )
                        break

                    offset += batch_size

                    if offset > SAFE_OFFSET_LIMIT:
                        self._logger.warning(
                            "Reached safety limit of %d records, stopping",
                            SAFE_OFFSET_LIMIT,
                        )
                        break

                except Exception:
                    self._logger.exception("Failed to parse batch at offset %d", offset)
                    client.rate_limiter.record_response(operation_time, 500)
                    break

            self._logger.debug(
                "Retrieved %d total records using batched approach",
                len(all_results),
            )
            return all_results

        except Exception:
            self._logger.exception("Batched query failed")
            return []

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

        pattern = rf"^{re.escape(start_marker)}$\n(\d+)\n^{re.escape(end_marker)}$"
        match = re.search(pattern, result, re.MULTILINE)
        if match:
            return int(match.group(1))

        lines = result.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == start_marker and not line.lstrip().startswith(">>"):
                if i + 2 < len(lines):
                    count_line = lines[i + 1].strip()
                    end_line = lines[i + 2].strip()
                    if count_line.isdigit() and end_line == end_marker:
                        return int(count_line)

        msg = f"Unable to parse count result for {model}: end_marker={end_marker in result}"
        raise QueryExecutionError(msg)

    # ── script + structured-data execution ────────────────────────────────

    def execute_script_with_data(
        self,
        script_content: str,
        data: Any,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Execute a Ruby script in the Rails console with structured input data.

        The data is serialized to JSON, transferred to the container under
        ``/tmp``, and made available to the Ruby script as the variable
        ``input_data``. The script is also written to a temp file and run via
        ``load "/tmp/<script>.rb"``.

        The Ruby script should print a JSON payload between the markers
        ``JSON_OUTPUT_START_<exec_id>`` and ``JSON_OUTPUT_END_<exec_id>`` (the
        exec_id is unique per call to distinguish from earlier-run output
        still in the tmux buffer); the JSON is parsed and returned in the
        ``data`` field.

        Returns:
            Dict with keys: status ("success"|"error"), message, data (parsed JSON), output (raw snippet).

        """
        # Lazy import: the ANSI/control-char regexes live on openproject_client
        # module scope (used by execute_script_with_data here AND by
        # execute_large_query_to_json_file which is still on the client),
        # so importing them lazily here avoids the import cycle at module
        # load time.
        from src.clients.openproject_client import (
            _RE_ANSI_ESCAPE,
            _RE_CTRL_CHARS,
            _RE_OSC_ESCAPE,
            _RE_OTHER_ESCAPE,
        )

        client = self._client
        # Prepare local temp paths
        temp_dir = Path(client.file_manager.data_dir) / "temp_scripts"
        temp_dir.mkdir(parents=True, exist_ok=True)

        local_data_path = temp_dir / f"openproject_input_{os.urandom(4).hex()}.json"
        try:
            with local_data_path.open("w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as e:
            err_msg = f"Failed to serialize input data: {e}"
            raise QueryExecutionError(err_msg) from e

        # Generate per-call unique markers BEFORE composing the script so the
        # Ruby header can define ``$j2o_start_marker``/``$j2o_end_marker``
        # itself. This keeps the tmux and rails-runner fallback paths
        # symmetric: in tmux the globals are also set via ``send-keys`` for
        # backward compatibility, but the runner subprocess (which never sees
        # those tmux ENV writes) gets the same markers from the script body.
        exec_id = os.urandom(8).hex()
        unique_start_marker = f"JSON_OUTPUT_START_{exec_id}"
        unique_end_marker = f"JSON_OUTPUT_END_{exec_id}"

        # Compose Ruby script with a header that defines the markers and
        # loads the input JSON into ``input_data``.
        container_data_path = Path("/tmp") / local_data_path.name
        header = (
            f"$j2o_start_marker = '{unique_start_marker}'\n"
            f"$j2o_end_marker = '{unique_end_marker}'\n"
            f"require 'json'\n"
            f"input_data = JSON.parse(File.read('{container_data_path.as_posix()}'))\n"
        )
        full_script = header + script_content

        local_script_path: Path | None = None
        container_script_path: Path | None = None
        operation_succeeded = False  # Track success for debug file preservation
        try:
            # Create local script file and transfer both script and data
            local_script_path = client._create_script_file(full_script)
            container_script_path = client._transfer_rails_script(local_script_path)

            # Transfer the input JSON to container
            client.transfer_file_to_container(local_data_path, container_data_path)

            load_cmd = f'load "{container_script_path.as_posix()}"'
            try:
                target = client.rails_client._get_target()
                tmux = shutil.which("tmux") or "tmux"

                # Also set the markers in the tmux Rails console session so
                # any caller-script that references the globals before
                # ``load`` runs sees the correct values. Redundant with the
                # script header but kept for backward compatibility.
                marker_setup = f"$j2o_start_marker = '{unique_start_marker}'; $j2o_end_marker = '{unique_end_marker}'"
                subprocess.run(
                    [tmux, "send-keys", "-t", target, marker_setup, "Enter"],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                time.sleep(0.1)

                escaped_cmd = client.rails_client._escape_command(load_cmd)
                subprocess.run(
                    [tmux, "send-keys", "-t", target, escaped_cmd, "Enter"],
                    capture_output=True,
                    text=True,
                    check=True,
                )

                # Poll for our unique JSON_OUTPUT_END marker with timeout
                effective_timeout = timeout or client.rails_client.command_timeout
                start_time = time.time()
                output = ""
                found_markers = False

                while time.time() - start_time < effective_timeout:
                    cap = subprocess.run(
                        [tmux, "capture-pane", "-p", "-S", "-2000", "-t", target],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    output = cap.stdout

                    # Normalize output by removing newlines to handle markers
                    # split by terminal width wrapping in tmux pane capture
                    normalized = output.replace("\n", "").replace("\r", "")

                    if unique_start_marker in normalized and unique_end_marker in normalized:
                        # Use rfind for the LAST occurrence (actual JSON output,
                        # not the command echo which contains markers in quotes).
                        start_pos = normalized.rfind(unique_start_marker)
                        if start_pos != -1:
                            next_char_pos = start_pos + len(unique_start_marker)
                            if next_char_pos < len(normalized):
                                next_char = normalized[next_char_pos]
                                if next_char in "[{":  # Actual JSON content
                                    end_pos = normalized.find(unique_end_marker, next_char_pos)
                                    if end_pos != -1 and end_pos > start_pos:
                                        found_markers = True
                                        output = normalized
                                        break

                    time.sleep(0.2)  # Poll every 200ms

                if not found_markers:
                    self._logger.warning(
                        "JSON_OUTPUT_END_%s marker not found within %d seconds",
                        exec_id,
                        effective_timeout,
                    )
            except Exception as e:
                # Fallback: if Rails console crashed or is unstable (e.g. Reline/IRB
                # errors), execute via non-interactive runner to avoid TTY/Reline issues.
                if isinstance(e, (ConsoleNotReadyError, CommandExecutionError, RubyError)):
                    if not config.migration_config.get("enable_runner_fallback", False):
                        raise
                    self._logger.warning(
                        "Rails console execution failed (%s). Falling back to rails runner.",
                        type(e).__name__,
                    )
                    runner_cmd = (
                        f"(cd /app || cd /opt/openproject) && "
                        f"bundle exec rails runner {container_script_path.as_posix()}"
                    )
                    stdout, stderr, rc = client.docker_client.execute_command(
                        runner_cmd,
                        timeout=timeout or 120,
                    )
                    if rc != 0:
                        q_msg = f"rails runner failed (rc={rc}): {stderr[:500]}"
                        raise QueryExecutionError(q_msg) from e
                    output = stdout
                else:
                    raise

            # Extract JSON payload between unique markers
            normalized_output = output.replace("\n", "").replace("\r", "")
            start_idx = normalized_output.rfind(unique_start_marker)
            if start_idx != -1:
                end_idx = normalized_output.find(unique_end_marker, start_idx + len(unique_start_marker))
            else:
                end_idx = -1

            output = normalized_output

            self._logger.debug(
                "Marker extraction: start_idx=%s, end_idx=%s, marker=%s, output_len=%d",
                start_idx,
                end_idx,
                exec_id,
                len(output),
            )
            if start_idx != -1 and end_idx != -1:
                json_preview = output[start_idx : end_idx + len(unique_end_marker)][:200]
                self._logger.debug("JSON region preview: %r", json_preview)

            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                json_str = output[start_idx + len(unique_start_marker) : end_idx].strip()

                # Sanitisation guards against stray ANSI / control chars from
                # IRB / tmux that would otherwise blow up json.loads.
                def _try_parse(s: str) -> Any:
                    return json.loads(s)

                def _sanitize_control_chars(s: str) -> str:
                    s = _RE_ANSI_ESCAPE.sub("", s)
                    s = _RE_OSC_ESCAPE.sub("", s)
                    s = _RE_OTHER_ESCAPE.sub("", s)
                    return _RE_CTRL_CHARS.sub("", s)

                def _extract_first_json_block(s: str) -> str | None:
                    # Isolate the first balanced JSON object/array
                    for i, ch in enumerate(s):
                        if ch in "{[":
                            opening = ch
                            closing = "}" if ch == "{" else "]"
                            depth = 0
                            in_str = False
                            esc = False
                            for j in range(i, len(s)):
                                c = s[j]
                                if in_str:
                                    if esc:
                                        esc = False
                                    elif c == "\\":
                                        esc = True
                                    elif c == '"':
                                        in_str = False
                                elif c == '"':
                                    in_str = True
                                elif c == opening:
                                    depth += 1
                                elif c == closing:
                                    depth -= 1
                                    if depth == 0:
                                        return s[i : j + 1]
                            break
                    return None

                json_str = _sanitize_control_chars(json_str)
                try:
                    parsed = _try_parse(json_str)
                except Exception:
                    candidate = _extract_first_json_block(json_str) or json_str
                    try:
                        parsed = _try_parse(candidate)
                    except json.JSONDecodeError as e:
                        pos = e.pos if hasattr(e, "pos") else 0
                        start_ctx = max(0, pos - 20)
                        end_ctx = min(len(candidate), pos + 20)
                        ctx = candidate[start_ctx:end_ctx]
                        char_at_pos = repr(candidate[pos : pos + 5]) if pos < len(candidate) else "EOF"
                        self._logger.warning(
                            "JSON parse error at pos %d: char=%s, context=%r",
                            pos,
                            char_at_pos,
                            ctx,
                        )
                        q_msg = f"Failed to parse JSON output: {e}"
                        raise QueryExecutionError(q_msg) from e
                    except Exception as e:
                        q_msg = f"Failed to parse JSON output: {e}"
                        raise QueryExecutionError(q_msg) from e

                operation_succeeded = True
                return {
                    "status": "success",
                    "message": "Script executed successfully",
                    "data": parsed,
                    "output": output[:2000],
                }

            # No JSON markers found — return an error envelope (still mark
            # 'succeeded' since execution completed; just no JSON found).
            operation_succeeded = True
            return {
                "status": "error",
                "message": "JSON markers not found in Rails output",
                "output": output[:2000],
            }

        finally:
            # Cleanup logic: preserve debug files on errors if configured
            preserve_on_error = config.migration_config.get("preserve_debug_files_on_error", True)
            should_cleanup = operation_succeeded or not preserve_on_error

            if not should_cleanup:
                self._logger.warning(
                    "Preserving debug files due to error (set preserve_debug_files_on_error=false to auto-cleanup):\n"
                    "  Local script: %s\n"
                    "  Container script: %s\n"
                    "  Local data: %s\n"
                    "  Container data: %s",
                    local_script_path,
                    container_script_path,
                    local_data_path,
                    container_data_path,
                )
            else:
                try:
                    # ``container_script_path`` may be None if the local file
                    # was created but the container transfer failed; the
                    # underlying cleanup helper accepts ``None`` for the
                    # remote path and will only delete the local file in
                    # that case, which prevents a temp-file leak.
                    if local_script_path is not None:
                        client._cleanup_script_files(local_script_path, container_script_path)
                except Exception as cleanup_err:
                    self._logger.warning(
                        "Failed to cleanup script files (local=%s, container=%s): %s",
                        local_script_path,
                        container_script_path,
                        cleanup_err,
                    )
                try:
                    client._cleanup_script_files(local_data_path, container_data_path)
                except Exception as cleanup_err:
                    self._logger.warning(
                        "Failed to cleanup data files (local=%s, container=%s): %s",
                        local_data_path,
                        container_data_path,
                        cleanup_err,
                    )
