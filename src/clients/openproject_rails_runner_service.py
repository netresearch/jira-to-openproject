"""Rails console parsing helpers + connection check for OpenProject.

Phase 2e of ADR-002: continues the Phase 2 god-class split. This first
slice of the Rails-runner extraction moves the small/medium parsing
and connectivity methods into a focused service:

* ``is_connected()`` — round-trip test against the persistent tmux Rails console.
* ``check_console_output_for_errors(...)`` — flag Ruby-error markers and severe
  patterns (SystemStackError, "stack level too deep", etc.).
* ``assert_expected_console_notice(...)`` — strict output checks for file-write
  flows that expect a specific success notice.
* ``parse_rails_output(...)`` — the central output parser. Handles JSON,
  ``=> <value>`` Rails console responses, scalar values, and various
  TMUX/console formatting artefacts.

The bigger ``execute*`` family (``execute``, ``execute_query``,
``execute_query_to_json_file``, ``execute_large_query_to_json_file``,
``execute_script_with_data``, ``_execute_batched_query``,
``execute_json_query``, ``count_records``, ``execute_transaction``) stays
on ``OpenProjectClient`` for now; those will move into this service —
or split into a dedicated runner — in Phase 2f.

``OpenProjectClient`` exposes the service via ``self.rails_runner`` and
keeps thin delegators for the same method names so existing call sites
work unchanged.
"""

from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING

from src.clients.exceptions import JsonParseError, QueryExecutionError

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
