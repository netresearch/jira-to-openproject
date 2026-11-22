# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""RailsConsoleClient.

Client for executing commands on a Rails console running in a tmux session.
Uses exception-based error handling for all operations.
"""

import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from typing import Any

from src.display import configure_logging

_RUBY_ERROR_PREFIX = "Ruby error:"
_RUBY_FULL_MESSAGE_MARKER = "full_message':"
_IRB_MAIN_PROMPT = "irb(main):"
_RUBY_STACK_OVERFLOW = "stack level too deep"


try:
    from src import config  # type: ignore
except Exception:
    config = None
from src.utils.file_manager import FileManager

logger = configure_logging("INFO", None)


# Prefixes that mark a captured tmux line as console noise (sentinels, irb
# prompts, irb auto-print results) rather than actual script output.
_CONSOLE_NOISE_PREFIXES: tuple[str, ...] = (
    "--EXEC_",
    "TMUX_CMD_",
    "=> ",
    _IRB_MAIN_PROMPT,
    "open-project(",
)


def filter_console_output_lines(lines: list[str]) -> str:
    """Strip console noise from a slice of captured tmux lines.

    Removes blank lines, ``--EXEC_*`` sentinels, irb prompts (including the
    ``open-project(prod):NNNN*`` continuation prompts that irb echoes when a
    multi-line wrapped script is pasted into the console) and irb's ``=>``
    auto-print lines, then joins the survivors with newlines.

    Returning a trimmed string keeps callers' downstream parsing
    (e.g. ``int(...)`` for a custom-field id) immune to console echo noise.
    """
    kept: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith(_CONSOLE_NOISE_PREFIXES):
            continue
        kept.append(stripped)
    return "\n".join(kept).strip()


class RailsConsoleError(Exception):
    """Base exception for all Rails Console errors."""


class TmuxSessionError(RailsConsoleError):
    """Error when interacting with tmux session."""


class ConsoleNotReadyError(RailsConsoleError):
    """Error when Rails console is not in a ready state."""


class CommandExecutionError(RailsConsoleError):
    """Error when executing a Ruby command in the Rails console."""


class RubyError(CommandExecutionError):
    """Error when Ruby reports a specific error."""


class RailsConsoleClient:
    """Client for interacting with Rails console via a local tmux session."""

    def __init__(
        self,
        tmux_session_name: str = "rails_console",
        window: int | None = None,
        pane: int | None = None,
        command_timeout: int = 180,
        inactivity_timeout: int = 30,
    ) -> None:
        """Initialize the Rails console client.

        Args:
            tmux_session_name: tmux session name (default: "rails_console")
            window: tmux window number (default: the session's current window)
            pane: tmux pane number (default: the window's active pane)
            command_timeout: Command timeout in seconds (default: 180)
            inactivity_timeout: Inactivity timeout in seconds (default: 30)

        Raises:
            TmuxSessionError: If tmux session does not exist

        """
        self.tmux_session_name = tmux_session_name
        self.window = window
        self.pane = pane
        self.command_timeout = command_timeout
        self.inactivity_timeout = inactivity_timeout
        self.file_manager = FileManager()
        self._rails_command = "bundle exec rails console"
        self._tmux_path = shutil.which("tmux") or "tmux"

        # Skip tmux session check if forced runner mode (tmux not needed)
        if os.environ.get("J2O_FORCE_RAILS_RUNNER"):
            logger.info("Skipping tmux session check (J2O_FORCE_RAILS_RUNNER=1)")
        elif not self._session_exists():
            msg = f"tmux session '{self.tmux_session_name}' does not exist"
            raise TmuxSessionError(msg)
        else:
            logger.success("Connected to tmux session '%s'", self.tmux_session_name)

        try:
            self._configure_irb_settings()
        except Exception as e:
            logger.warning("Failed to configure IRB settings: %s", e)

    @staticmethod
    def _extract_error_summary(text: str) -> str:
        """Extract a concise, high-signal Ruby error summary from tmux output.

        Prefer specific exception lines like SystemStackError or 'stack level too deep'.
        Falls back to last non-marker lines.
        """
        try:
            lines = [ln.strip() for ln in text.split("\n")]

            # Filter out prompts, markers, and Ruby nil/inspects (same set as
            # ``filter_console_output_lines`` uses for normal output extraction).
            candidates = [ln for ln in lines if ln and not ln.startswith(_CONSOLE_NOISE_PREFIXES)]

            # Targeted patterns
            key_preds = [
                lambda s: "SystemStackError" in s,
                lambda s: _RUBY_STACK_OVERFLOW in s,
                lambda s: _RUBY_FULL_MESSAGE_MARKER in s,
                lambda s: s.startswith(_RUBY_ERROR_PREFIX),
            ]

            matched: list[str] = []
            for pred in key_preds:
                matched.extend([ln for ln in candidates if pred(ln)])
            # Also include preceding file/line if available
            enriched: list[str] = list(matched)
            if not enriched:
                # Fallback to last few non-noise lines
                enriched = list(candidates[-5:])
            # Deduplicate and clip
            seen: set[str] = set()
            unique = []
            for ln in enriched:
                if ln not in seen:
                    seen.add(ln)
                    unique.append(ln)
            summary = " | ".join(unique)[:500]
            return summary or text.strip()[:300]
        except Exception:
            return text.strip()[:300]

    def _session_exists(self) -> bool:
        """Check if the specified tmux session exists locally.

        Returns:
            True if session exists, False otherwise

        """
        try:
            tmux = self._tmux_path
            cmd = [tmux, "has-session", "-t", self.tmux_session_name]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except subprocess.SubprocessError:
            logger.exception("Error checking tmux session")
            return False
        else:
            return result.returncode == 0

    def _get_target(self) -> str:
        """Get the tmux target string for the session, window, and pane.

        Without an explicit window and pane the target is the session itself, which tmux resolves
        to the active pane of its current window: a fixed ``0.0`` does not exist where tmux
        counts windows and panes from 1 (``base-index``, ``pane-base-index``).

        Returns:
            tmux target string

        """
        if self.window is None:
            return self.tmux_session_name if self.pane is None else f"{self.tmux_session_name}:.{self.pane}"
        return f"{self.tmux_session_name}:{self.window}" + ("" if self.pane is None else f".{self.pane}")

    def _configure_irb_settings(self) -> None:
        """Configure IRB settings for better output and interaction.

        Raises:
            TmuxSessionError: If failed to configure IRB settings

        """
        config_cmd = """
        IRB.conf[:USE_COLORIZE] = false
        IRB.conf[:INSPECT_MODE] = :to_s

        # Minimize reliance on cursor position / interactive editing features
        begin
          IRB.conf[:USE_MULTILINE] = false
          IRB.conf[:PROMPT_MODE] = :SIMPLE
          if IRB.conf.key?(:USE_AUTOCOMPLETE)
            IRB.conf[:USE_AUTOCOMPLETE] = false
          end
        rescue => e
          puts "IRB basic config error: #{e.message}"
        end

        # Handle non-interactive terminals better
        begin
          # Environment mitigations for Reline in non-interactive contexts
          begin; ENV['RELINE_OUTPUT_ESCAPES'] = 'false'; rescue; end
          begin; ENV['RELINE_INPUTRC'] = '/dev/null'; rescue; end

          if defined?(Reline)
            Reline.output_modifier_proc = nil
            Reline.completion_proc = nil
            Reline.prompt_proc = nil
          end

          IRB.conf[:SAVE_HISTORY] = nil
          IRB.conf[:HISTORY_FILE] = nil
        rescue => e
          puts "Error during IRB configuration: #{e.message}"
        end

        puts "IRB configuration complete"
        """

        target = self._get_target()

        try:
            tmux = self._tmux_path
            send_cmd = [tmux, "send-keys", "-t", target, config_cmd, "Enter"]
            subprocess.run(send_cmd, capture_output=True, text=True, check=True)
            logger.debug("IRB configuration commands sent successfully")
        except subprocess.SubprocessError as e:
            logger.exception("Failed to configure IRB settings")
            msg = f"Failed to configure IRB settings: {e}"
            raise TmuxSessionError(msg) from e

    @staticmethod
    def _has_fatal_console_error(output: str) -> bool:
        """Detect fatal IRB/Reline/console errors in tmux output."""
        if not output:
            return False
        fatal_terms = [
            "ungetbyte failed (IOError)",
            "Reline::ANSI#cursor_pos",
            "Reline::Core#readmultiline",
            "IRB::Irb#run",
            "SystemStackError",
            _RUBY_STACK_OVERFLOW,
        ]
        return any(term in output for term in fatal_terms)

    def _clear_pane(self) -> None:
        """Clear the tmux pane to prepare for command output.

        Raises:
            TmuxSessionError: If failed to clear tmux pane

        """
        target = self._get_target()

        try:
            tmux = self._tmux_path
            clear_cmd = [tmux, "send-keys", "-t", target, "C-l"]
            subprocess.run(clear_cmd, capture_output=True, text=True, check=True)
        except subprocess.SubprocessError as e:
            logger.warning("Failed to clear tmux pane: %s", e)
            msg = f"Failed to clear tmux pane: {e}"
            raise TmuxSessionError(msg) from e

    def _stabilize_console(self) -> None:
        """Send a harmless command to stabilize console state.

        Raises:
            ConsoleNotReadyError: If console cannot be stabilized

        """
        try:
            target = self._get_target()

            # Send a space and Enter to reset terminal state
            tmux = self._tmux_path
            send_cmd = [tmux, "send-keys", "-t", target, " ", "Enter"]
            subprocess.run(send_cmd, capture_output=True, text=True, check=True)
            time.sleep(0.3)

            # Clear the screen
            tmux = self._tmux_path
            clear_cmd = [tmux, "send-keys", "-t", target, "C-l"]
            subprocess.run(clear_cmd, capture_output=True, text=True, check=True)
            time.sleep(0.2)

            # Send Ctrl+C to abort any pending operation
            tmux = self._tmux_path
            subprocess.run(
                [tmux, "send-keys", "-t", target, "C-c"],
                capture_output=True,
                text=True,
                check=True,
            )
            time.sleep(0.2)

            logger.debug("Console state stabilized")
        except subprocess.SubprocessError as e:
            logger.exception("Failed to stabilize console")
            msg = f"Failed to stabilize console: {e}"
            raise ConsoleNotReadyError(msg) from e

    def _escape_command(self, command: str) -> str:
        """Escape a command for tmux send-keys.

        Args:
            command: Command to escape

        Returns:
            Escaped command

        """
        return command.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")

    def execute(
        self,
        command: str,
        timeout: int | None = None,
        *,
        suppress_output: bool = False,
    ) -> str:
        """Execute a command in the Rails console and wait for completion.

        Args:
            command: Ruby command to execute
            timeout: Command timeout in seconds (default: self.command_timeout)
            suppress_output: If True, don't print the result to console (for file-based operations)

        Returns:
            Extracted command output as a string

        Raises:
            CommandExecutionError: If command execution fails
            RubyError: If Ruby reports an error in the executed code

        """
        if timeout is None:
            timeout = self.command_timeout

        marker_id = self.file_manager.generate_unique_id()
        debug_session_dir = self.file_manager.create_debug_session(marker_id)

        self.file_manager.add_to_debug_log(
            debug_session_dir,
            f"COMMAND EXECUTION START: {time.strftime('%Y-%m-%d %H:%M:%S')}\nCommand: {command}\n",
        )

        # Construct markers with single literal strings to avoid IRB concatenation artifacts
        start_marker_cmd = f'puts "--EXEC_START--{marker_id}"'
        start_marker_out = f"--EXEC_START--{marker_id}"

        end_marker_cmd = f'puts "--EXEC_END--{marker_id}"'
        end_marker_out = f"--EXEC_END--{marker_id}"

        error_marker_cmd = f'puts "--EXEC_ERROR--{marker_id}"'
        error_marker_out = f"--EXEC_ERROR--{marker_id}"

        # Script end comment to delimit the end of echoed input
        script_end_comment_out = f"--SCRIPT_END--{marker_id}"

        # Conditionally include result printing based on suppress_output flag
        result_print_line = "" if suppress_output else "puts result.inspect"

        # Two execution templates:
        # - expression_template: expects `command` to be an expression; assigns to `result`
        # - script_template: executes arbitrary multi-line script (used when suppress_output=True)
        expression_template = """
        %s
        begin
          result = nil  # Initialize result variable
          result = %s  # Assign the actual result
          %s
        rescue => e
          begin; %s; rescue; end
          begin; puts "Ruby error: #{e.class}: #{e.message}"; rescue; end
          begin
            bt = e.backtrace || []
            puts bt.join("\\n")[0..2000]
          rescue
          end
        ensure
          begin; %s; rescue; end
        end # %s
        """

        script_template = """
        %s
        begin
          %s
        rescue => e
          begin; %s; rescue; end
          begin; puts "Ruby error: #{e.class}: #{e.message}"; rescue; end
          begin
            bt = e.backtrace || []
            puts bt.join("\\n")[0..2000]
          rescue
          end
        ensure
          begin; %s; rescue; end
        end # %s
        """

        end_with_comment = script_end_comment_out

        header_comment = self._console_provenance() + "\n"

        if suppress_output:
            body = script_template % (
                start_marker_cmd,
                command,
                error_marker_cmd,
                end_marker_cmd,
                end_with_comment,
            )
        else:
            body = expression_template % (
                start_marker_cmd,
                command,
                result_print_line,
                error_marker_cmd,
                end_marker_cmd,
                end_with_comment,
            )
        wrapped_command = header_comment + body

        command_path = self.file_manager.join(debug_session_dir, "ruby_command.rb")
        with command_path.open("w") as f:
            f.write(wrapped_command)

        # Execute in tmux
        if suppress_output:
            # For suppressed multi-line scripts, avoid marker waits entirely to reduce fragility/noise
            tmux_output = self._send_command_to_tmux(
                wrapped_command,
                timeout,
                wait_for_line=None,
                script_end_marker=None,
            )
        else:
            # Wait until EXEC_END is printed as an output line (not the echoed Ruby string literal)
            tmux_output = self._send_command_to_tmux(
                wrapped_command,
                timeout,
                wait_for_line=end_marker_out,
                script_end_marker=script_end_comment_out,
            )

        tmux_output_path = self.file_manager.join(debug_session_dir, "tmux_output.txt")
        with tmux_output_path.open("w") as f:
            f.write(tmux_output)

        self.file_manager.add_to_debug_log(
            debug_session_dir,
            f"TMUX OUTPUT RECEIVED: {time.strftime('%Y-%m-%d %H:%M:%S')}\nSize: {len(tmux_output)} bytes\n",
        )

        # For suppressed output calls, return early to avoid marker parsing and console noise
        if suppress_output:
            return ""

        return self._command_result(
            tmux_output, start_marker_out, end_marker_out, error_marker_out, script_end_comment_out
        )

    @staticmethod
    def _console_provenance() -> str:
        import sys

        ignored = ("openproject_client.py", "rails_console_client.py", "docker_client.py", "ssh_client.py")
        component = "rails/console"
        frame = sys._getframe(1)
        for _ in range(48):
            if frame is None:
                break
            path = frame.f_code.co_filename
            if "/src/" in path and not path.endswith(ignored):
                name = path.split("/src/")[-1].replace("migrations/", "migration/").replace("clients/", "client/")
                component = f"{name.removesuffix('.py').removesuffix('_migration')} func={frame.f_code.co_name}"
                break
            frame = frame.f_back
        try:
            project = (getattr(config, "jira_config", None) or {}).get("project_filter")
        except Exception:
            project = None
        project_hint = f" project={project}" if project else ""
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"# j2o: {component} ts={timestamp} pid={os.getpid()}{project_hint}"

    def _capture_pane(self, target: str, lines: int) -> str:
        return subprocess.run(
            [self._tmux_path, "capture-pane", "-p", "-S", f"-{lines}", "-t", target],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    @staticmethod
    def _output_marker(lines: list[str], marker: str, start: int = 0) -> int:
        # Lines echoed by IRB contain the Ruby string literal, including its closing quote.
        return next((i for i in range(start, len(lines)) if lines[i].strip().endswith(marker)), -1)

    @classmethod
    def _own_output_start(cls, lines: list[str], left: int, right: int, echo: str) -> int:
        # IRB prints the start marker before it echoes the wrapper's begin block, so this
        # command's own output begins after the last script-end echo line before `right`.
        echo_end = next((i for i in reversed(range(left + 1, right)) if echo in lines[i]), -1)
        return max(left, echo_end)

    def _check_ruby_failure(self, output: str, where: str, *, include_ruby_error: bool = True) -> None:
        fatal = ("SystemStackError", _RUBY_STACK_OVERFLOW, _RUBY_FULL_MESSAGE_MARKER)
        if any(term in output for term in fatal) or (include_ruby_error and _RUBY_ERROR_PREFIX in output):
            msg = f"Ruby console reported error {where}: {self._extract_error_summary(output)}"
            raise RubyError(msg)

    def _command_result(self, output: str, start: str, end: str, error: str, echo: str) -> str:
        lines = output.split("\n")
        left = self._output_marker(lines, start)
        right = self._output_marker(lines, end, left + 1)
        if left < 0:
            # Without this command's start marker there is no own output region; only a crash counts.
            self._check_ruby_failure(output, "with no markers", include_ruby_error=False)
        if left < 0 or right < 0:
            recovered = self._recapture_result(start, end, error, echo, recover_echo=left < 0)
            if recovered is not None:
                return recovered
        if left < 0:
            msg = f"Start marker '{start}' not found in output"
            raise CommandExecutionError(msg)
        if right < 0:
            own = lines[self._own_output_start(lines, left, len(lines), echo) + 1 :]
            self._check_ruby_failure("\n".join(own), "before end marker")
            if self._get_console_state(output[-50:])["ready"]:
                candidate = filter_console_output_lines(own)
                if candidate:
                    return candidate
            msg = f"End marker '{end}' not found in output"
            raise CommandExecutionError(msg)
        return self._marked_result(lines, left, right, error, echo)

    def _recapture_result(self, start: str, end: str, error: str, echo: str, *, recover_echo: bool) -> str | None:
        try:
            lines = self._capture_pane(self._get_target(), 1000).split("\n")
        except Exception:
            logger.debug("Could not recapture Rails console", exc_info=True)
            return None
        left = self._output_marker(lines, start)
        right = self._output_marker(lines, end, left + 1)
        if left >= 0 and right >= 0:
            return self._marked_result(lines, left, right, error, echo)
        if recover_echo and right >= 0:
            left = next((i for i in reversed(range(right)) if echo in lines[i]), -1)
            if left >= 0:
                return self._marked_result(lines, left, right, error, echo) or None
        return None

    def _marked_result(self, lines: list[str], left: int, right: int, error: str, echo: str) -> str:
        body = lines[self._own_output_start(lines, left, right, echo) + 1 : right]
        if self._output_marker(body, error) >= 0:
            # A wrapper IRB rejected still prints both markers; report IRB's error, not a stale `e`.
            self._check_ruby_failure("\n".join(body), "in command output", include_ruby_error=False)
            msg = next((line.strip() for line in body if _RUBY_ERROR_PREFIX in line), "Ruby error detected")
            raise RubyError(msg)
        output = filter_console_output_lines(body)
        errors = ("SyntaxError:", "NameError:", "NoMethodError:", "ArgumentError:", "TypeError:", "RuntimeError:")
        for line in output.splitlines():
            if any(pattern in line for pattern in errors):
                raise RubyError(line.strip())
        echo_end = next((i for i in reversed(range(len(lines))) if echo in lines[i]), -1)
        trailing = "\n".join(lines[max(right, echo_end) + 1 :])
        self._check_ruby_failure(trailing, "after end marker", include_ruby_error=False)
        return output

    def _get_console_state(self, output: str) -> dict[str, Any]:
        """Check if the Rails console is ready for input by looking for the prompt.

        Args:
            output: Current tmux pane output

        Returns:
            Dictionary with state information

        """
        ready_patterns = [_IRB_MAIN_PROMPT, ">", ">>", "irb>", "pry>"]
        awaiting_patterns = ["*"]
        string_patterns = ['"', "'"]

        result: dict[str, Any] = {"ready": False, "state": "unknown", "prompt": None}

        lines = [line.strip() for line in output.strip().split("\n")]
        non_empty_lines = [line for line in lines if line]

        if not non_empty_lines:
            return result

        last_line = non_empty_lines[-1]
        logger.debug("Last line: '%s'", last_line)

        if any(pattern in last_line for pattern in ready_patterns) or last_line.endswith(">"):
            result["prompt"] = last_line
            result["state"] = "ready"
            result["ready"] = True
            return result

        if any(pattern in last_line for pattern in awaiting_patterns):
            result["prompt"] = last_line
            result["state"] = "awaiting_input"
            return result

        if any(pattern in last_line for pattern in string_patterns):
            result["prompt"] = last_line
            result["state"] = "multiline_string"
            return result

        return result

    def _wait_for_console_ready(self, target: str, timeout: int = 5, *, reset_on_stall: bool = True) -> bool:
        """Wait for the console to be in a ready state.

        Args:
            target: tmux target (session:window.pane)
            timeout: Maximum time to wait in seconds
            reset_on_stall: When True, may send Ctrl+C on stalled/awaiting input states to recover

        Returns:
            bool: True if console is ready, False if timed out

        Raises:
            ConsoleNotReadyError: If console cannot be made ready

        """
        logger.debug("Waiting for console ready state (timeout: %ss)", timeout)
        tmux = self._tmux_path
        start_time = time.time()
        poll_interval = 0.05
        attempts = 0

        while time.time() - start_time < timeout:
            try:
                capture = subprocess.run(
                    [tmux, "capture-pane", "-p", "-S", "-10", "-t", target],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                current_output = capture.stdout

                # Hard fail on fatal console errors
                if self._has_fatal_console_error(current_output):
                    logger.error("Fatal console error detected while waiting for ready state")
                    return False

                console_state = self._get_console_state(current_output)
                if console_state["ready"]:
                    logger.debug("Console ready after %.2fs", time.time() - start_time)
                    return True

                if reset_on_stall and console_state["state"] in ["awaiting_input", "multiline_string"]:
                    logger.debug(
                        "Console in %s state, sending Ctrl+C to reset",
                        console_state["state"],
                    )
                    subprocess.run(
                        [tmux, "send-keys", "-t", target, "C-c"],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    time.sleep(0.3)
                    attempts += 1

                    if attempts >= 2:
                        logger.debug(
                            "Multiple Ctrl+C attempts failed, trying full stabilization",
                        )
                        self._stabilize_console()
                        attempts = 0

                logger.debug(
                    "Waiting %ss for ready state, current: %s",
                    poll_interval,
                    console_state["state"],
                )
                time.sleep(poll_interval)
                poll_interval *= 2
            except subprocess.SubprocessError as e:
                logger.exception("Error checking console state")
                msg = f"Error checking console state: {e}"
                raise ConsoleNotReadyError(msg) from e

        logger.error("Console not ready after %ss", timeout)
        return False

    def _wait_for_script_completion(self, target: str, end: str, timeout: int) -> None:
        """Wait until this command prints `end` as an output line.

        The echoed wrapper also contains `end`, but inside a Ruby string literal, so only
        a line ending in the bare marker counts. The wrapper's `ensure` prints it after
        errors too, including SystemStackError.
        """
        deadline = time.monotonic() + timeout
        poll_interval = 0.05
        while True:
            if self._output_marker(self._capture_pane(target, 1000).split("\n"), end) >= 0:
                return
            if time.monotonic() >= deadline:
                msg = f"End marker '{end}' not observed within {timeout}s"
                raise CommandExecutionError(msg)
            time.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.5, 0.5)

    def _send_command_to_tmux(
        self,
        command: str,
        timeout: int,
        wait_for_line: str | None = None,
        script_end_marker: str | None = None,
    ) -> str:
        """Send a command to the local tmux session and capture output.

        Args:
            command: Command to send
            timeout: Timeout in seconds
            wait_for_line: Optional end marker the script prints as a bare output line
            script_end_marker: Marker in the last echoed script line; together with
                wait_for_line it enables waiting for completion

        Returns:
            Command output as a string

        Raises:
            TmuxSessionError: If tmux command fails
            ConsoleNotReadyError: If console cannot be made ready
            CommandExecutionError: If command execution fails

        """
        target = self._get_target()

        if not self._wait_for_console_ready(target, timeout=10, reset_on_stall=False):
            logger.error("Console not ready, forcing full stabilization")
            self._stabilize_console()

            if not self._wait_for_console_ready(target, timeout=5, reset_on_stall=False):
                msg = "Console could not be made ready"
                raise ConsoleNotReadyError(msg)

        escaped_command = self._escape_command(command)

        try:
            # Removed TMUX_CMD_* markers; rely solely on EXEC_* markers from the script

            logger.debug("Sending command (length: %s bytes)", len(escaped_command))
            tmux = self._tmux_path
            subprocess.run(
                [tmux, "send-keys", "-t", target, escaped_command, "Enter"],
                capture_output=True,
                text=True,
                check=True,
            )

            # Give the command a brief moment to start producing output
            time.sleep(0.2)

            # Wait until this command's own end marker is printed as an output line
            if script_end_marker and wait_for_line:
                self._wait_for_script_completion(target, wait_for_line, timeout)

            # Now ensure prompt is ready before final capture
            self._wait_for_console_ready(target, timeout, reset_on_stall=False)

            # After script completes, capture a compact tail; outer parser will locate markers
            tmux = self._tmux_path
            cap = subprocess.run(
                [tmux, "capture-pane", "-p", "-S", "-1000", "-t", target],
                capture_output=True,
                text=True,
                check=True,
            )
            last_output = cap.stdout

            # drop all return lines from last_output
            last_output = "\n".join(
                [line.strip() for line in last_output.split("\n") if not line.strip().startswith("=> ")],
            )
            # drop all lines irb(main):30486>
            last_output = "\n".join(
                [line.strip() for line in last_output.split("\n") if not line.strip().startswith(_IRB_MAIN_PROMPT)],
            )
            # Do not slice out EXEC markers here; higher-level parser relies on them

            return last_output.strip()

        except subprocess.SubprocessError as e:
            logger.exception("Tmux command failed")
            self._stabilize_console()
            msg = f"Tmux command failed: {e}"
            raise TmuxSessionError(msg) from e
        except Exception as e:
            logger.exception("Error sending command to tmux")
            self._stabilize_console()
            msg = f"Error sending command to tmux: {e}"
            raise CommandExecutionError(msg) from e
