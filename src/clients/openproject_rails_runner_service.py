"""Rails console runner — parsing, connectivity, and the small/medium ``execute*`` family.

Phases 2e + 2f of ADR-002 collected the Rails-console helpers off
``OpenProjectClient`` into this focused service. After Phase 2f the
service owns:

* **Connectivity** — ``is_connected()`` round-trips a unique echo against
  the persistent tmux Rails console.
* **Console-output validation** — ``check_console_output_for_errors``,
  ``assert_expected_console_notice`` flag Ruby errors and missing notices.
* **Output parsing** — ``parse_rails_output`` handles JSON, ``=> <value>``
  Rails console responses, scalar values, and TMUX line-wrap artefacts.
* **Small ``execute*`` family** — ``execute`` (raw script with JSON-or-dict
  return), ``execute_query`` (low-level tmux send + capture),
  ``execute_query_to_json_file`` (file-based JSON path),
  ``execute_json_query`` (auto-as_json wrapper), ``execute_transaction``
  (multi-command transaction wrapper).

The remaining heavyweights (``execute_script_with_data`` ~325 LOC,
``execute_large_query_to_json_file`` ~240 LOC, ``_execute_batched_query``
~115 LOC, ``count_records`` ~80 LOC) stay on ``OpenProjectClient`` for
Phase 2g — they have heavier internal coupling (tmux subprocess, file
transfers, retry logic) and want their own focused PR.

``OpenProjectClient`` exposes the service via ``self.rails_runner`` and
keeps thin delegators for the same method names so existing call sites
work unchanged.
"""

from __future__ import annotations

import json
import os
import secrets
from typing import TYPE_CHECKING, Any

from src.clients.exceptions import JsonParseError, QueryExecutionError
from src.clients.rails_console_client import RubyError

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

    def execute(self, script_content: str) -> dict[str, Any]:
        """Execute a Ruby script directly.

        Returns the parsed JSON if the result is valid JSON, otherwise wraps
        the raw text in a ``{"result": ...}`` dict.
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
