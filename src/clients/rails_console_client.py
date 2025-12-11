"""RailsConsoleClient.

Client for executing commands on a Rails console running in a tmux session.
Uses exception-based error handling for all operations.
"""

import inspect
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from typing import Any

from src.display import configure_logging

try:
    from src import config  # type: ignore
except Exception:  # noqa: BLE001
    config = None
from src.utils.file_manager import FileManager

logger = configure_logging("INFO", None)


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
        window: int = 0,
        pane: int = 0,
        command_timeout: int = 180,
        inactivity_timeout: int = 30,
    ) -> None:
        """Initialize the Rails console client.

        Args:
            tmux_session_name: tmux session name (default: "rails_console")
            window: tmux window number (default: 0)
            pane: tmux pane number (default: 0)
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

        if not self._session_exists():
            msg = f"tmux session '{self.tmux_session_name}' does not exist"
            raise TmuxSessionError(msg)

        logger.success("Connected to tmux session '%s'", self.tmux_session_name)

        try:
            self._configure_irb_settings()
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to configure IRB settings: %s", e)

    @staticmethod
    def _extract_error_summary(text: str) -> str:
        """Extract a concise, high-signal Ruby error summary from tmux output.

        Prefer specific exception lines like SystemStackError or 'stack level too deep'.
        Falls back to last non-marker lines.
        """
        try:
            lines = [ln.strip() for ln in text.split("\n")]

            # Filter out prompts, markers, and Ruby nil/inspects
            def is_noise(ln: str) -> bool:
                return not ln or ln.startswith(("--EXEC_", "TMUX_CMD_", "=> ", "irb(main):", "open-project("))

            candidates = [ln for ln in lines if not is_noise(ln)]

            # Targeted patterns
            key_preds = [
                lambda s: "SystemStackError" in s,
                lambda s: "stack level too deep" in s,
                lambda s: "full_message':" in s,
                lambda s: s.startswith("Ruby error:"),
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
        except Exception:  # noqa: BLE001
            return text.strip()[:300]

    def _session_exists(self) -> bool:
        """Check if the specified tmux session exists locally.

        Returns:
            True if session exists, False otherwise

        """
        try:
            tmux = shutil.which("tmux") or "tmux"
            cmd = [tmux, "has-session", "-t", self.tmux_session_name]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
        except subprocess.SubprocessError:
            logger.exception("Error checking tmux session")
            return False
        else:
            return result.returncode == 0

    def _get_target(self) -> str:
        """Get the tmux target string for the session, window, and pane.

        Returns:
            tmux target string

        """
        return f"{self.tmux_session_name}:{self.window}.{self.pane}"

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
            tmux = shutil.which("tmux") or "tmux"
            send_cmd = [tmux, "send-keys", "-t", target, config_cmd, "Enter"]
            subprocess.run(send_cmd, capture_output=True, text=True, check=True)  # noqa: S603
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
            "stack level too deep",
        ]
        return any(term in output for term in fatal_terms)

    def _clear_pane(self) -> None:
        """Clear the tmux pane to prepare for command output.

        Raises:
            TmuxSessionError: If failed to clear tmux pane

        """
        target = self._get_target()

        try:
            tmux = shutil.which("tmux") or "tmux"
            clear_cmd = [tmux, "send-keys", "-t", target, "C-l"]
            subprocess.run(clear_cmd, capture_output=True, text=True, check=True)  # noqa: S603
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
            tmux = shutil.which("tmux") or "tmux"
            send_cmd = [tmux, "send-keys", "-t", target, " ", "Enter"]
            subprocess.run(send_cmd, capture_output=True, text=True, check=True)  # noqa: S603
            time.sleep(0.3)

            # Clear the screen
            tmux = shutil.which("tmux") or "tmux"
            clear_cmd = [tmux, "send-keys", "-t", target, "C-l"]
            subprocess.run(clear_cmd, capture_output=True, text=True, check=True)  # noqa: S603
            time.sleep(0.2)

            # Send Ctrl+C to abort any pending operation
            tmux = shutil.which("tmux") or "tmux"
            subprocess.run(  # noqa: S603
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

    def execute(  # noqa: C901, PLR0912, PLR0915
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

        # Build provenance header for every console execution
        def _provenance_hint() -> str:
            try:
                stack = inspect.stack()
                path = None
                func = None
                # Prefer callers under migrations/, and skip client plumbing
                for fr in stack[2:50]:
                    filename = fr.filename
                    if "/src/" not in filename:
                        continue
                    if any(
                        skip in filename
                        for skip in (
                            "/src/clients/openproject_client.py",
                            "/src/clients/rails_console_client.py",
                            "/src/clients/docker_client.py",
                            "/src/clients/ssh_client.py",
                        )
                    ):
                        continue
                    path = filename.split("/src/")[-1]
                    func = fr.function
                    break
                parts: list[str] = ["j2o:"]
                if path:
                    parts.append(
                        path.replace("migrations/", "migration/")
                        .replace("clients/", "client/")
                        .replace("_migration.py", "")
                        .replace(".py", ""),
                    )
                else:
                    parts.append("rails/console")
                if func:
                    parts.append(f"func={func}")
                ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                parts.append(f"ts={ts}")
                try:
                    parts.append(f"pid={os.getpid()}")
                except Exception:
                    pass
                proj = None
                try:
                    if config and getattr(config, "jira_config", None):
                        proj = (config.jira_config or {}).get("project_filter")
                except Exception:
                    proj = None
                if proj:
                    parts.append(f"project={proj}")
                return "# " + " ".join(parts)
            except Exception:
                return "# j2o: rails/console"

        header_comment = _provenance_hint() + "\n"

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
            # Use state-machine semantics: wait for script-end echo, then ensure tail contains EXEC_END
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

        # Robust, line-anchored marker parsing to avoid false positives from echoed source lines
        all_lines = tmux_output.split("\n")
        start_line_index = -1
        for idx, line in enumerate(all_lines):
            stripped = line.strip()
            if start_marker_out in stripped:
                start_line_index = idx
                break
        if start_line_index == -1:
            # If start marker is missing, attempt a tolerant recovery:
            # 1) Detect obvious Ruby failures first
            severe_output = tmux_output
            if (
                "SystemStackError" in severe_output
                or "stack level too deep" in severe_output
                or "Ruby error:" in severe_output
                or "full_message':" in severe_output
            ):
                snippet = self._extract_error_summary(severe_output)
                msg = f"Ruby console reported error with no markers: {snippet}"
                raise RubyError(msg)

            # 2) Recapture a larger pane slice to find markers
            try:
                tmux = shutil.which("tmux") or "tmux"
                recapture = subprocess.run(  # noqa: S603
                    [tmux, "capture-pane", "-p", "-S", "-1000", "-t", self._get_target()],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                rec_output = recapture.stdout
                rec_lines = rec_output.split("\n")
                new_start = -1
                new_end = -1
                for i, ln in enumerate(rec_lines):
                    s = ln.strip()
                    if start_marker_out in s:
                        new_start = i
                        break
                if new_start != -1:
                    for j in range(new_start + 1, len(rec_lines)):
                        s = rec_lines[j].strip()
                        if end_marker_out in s:
                            new_end = j
                            break
                if new_start != -1 and new_end != -1:
                    between_lines = rec_lines[new_start + 1 : new_end]
                    out_lines = [ln for ln in between_lines if ln.strip() and not ln.strip().startswith("--EXEC_")]
                    return "\n".join(out_lines).strip()

                # 3) If start marker still missing, but we have end marker and the script-echo comment,
                #    extract output between the echoed script end and the end marker as a fallback.
                rec_end = -1
                for j in range(len(rec_lines)):
                    if end_marker_out in rec_lines[j]:
                        rec_end = j
                        break
                rec_script_echo = -1
                if script_end_comment_out:
                    for j in range(len(rec_lines) - 1, -1, -1):
                        if script_end_comment_out in rec_lines[j]:
                            rec_script_echo = j
                            break
                if rec_script_echo != -1 and rec_end != -1 and rec_end > rec_script_echo:
                    between_lines = rec_lines[rec_script_echo + 1 : rec_end]
                    out_lines = [ln for ln in between_lines if ln.strip() and not ln.strip().startswith("--EXEC_")]
                    if out_lines:
                        return "\n".join(out_lines).strip()
            except Exception:  # noqa: BLE001,S110
                # Fall through to strict error below
                pass

            # 4) Strict failure when no safe fallback was possible
            msg = f"Start marker '{start_marker_out}' not found in output"
            raise CommandExecutionError(msg)

        end_line_index = -1
        for idx in range(start_line_index + 1, len(all_lines)):
            stripped = all_lines[idx].strip()
            if end_marker_out in stripped:
                end_line_index = idx
                break
        if end_line_index == -1:
            # One more try: recapture a larger slice in case the marker landed after our first capture
            try:
                tmux = shutil.which("tmux") or "tmux"
                recapture = subprocess.run(  # noqa: S603
                    [tmux, "capture-pane", "-p", "-S", "-1000", "-t", self._get_target()],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                rec_output = recapture.stdout
                rec_lines = rec_output.split("\n")
                new_start = -1
                new_end = -1
                for idx, line in enumerate(rec_lines):
                    s = line.strip()
                    if start_marker_out in s:
                        new_start = idx
                        break
                if new_start != -1:
                    for idx in range(new_start + 1, len(rec_lines)):
                        s = rec_lines[idx].strip()
                        if end_marker_out in s:
                            new_end = idx
                            break
                if new_start != -1 and new_end != -1:
                    # Use recaptured range
                    between_lines = rec_lines[new_start + 1 : new_end]
                    out_lines = [ln for ln in between_lines if ln.strip() and not ln.strip().startswith("--EXEC_")]
                    return "\n".join(out_lines).strip()
            except Exception:  # noqa: BLE001,S110
                # Fall through to existing error handling
                pass

            logger.error("End marker '%s' not found in output", end_marker_out)
            # First, detect obvious Ruby failures even when end marker is missing
            severe_output = tmux_output
            if (
                "SystemStackError" in severe_output
                or "stack level too deep" in severe_output
                or "Ruby error:" in severe_output
                or "full_message':" in severe_output
            ):
                snippet = self._extract_error_summary(severe_output)
                msg = f"Ruby console reported error before end marker: {snippet}"
                raise RubyError(msg)
            console_state = self._get_console_state(tmux_output[-50:])
            if console_state["ready"]:
                logger.error(
                    "Console appears ready despite missing end marker - attempting to extract output",
                )
                candidate_lines = [
                    ln.strip()
                    for ln in all_lines[start_line_index + 1 :]
                    if ln.strip() and not ln.strip().startswith("--EXEC_")
                ]
                if candidate_lines:
                    logger.info(
                        "Extracted %s lines of output despite missing end marker",
                        len(candidate_lines),
                    )
                    return "\n".join(candidate_lines)
                msg = f"End marker '{end_marker_out}' not found in output and no clear output could be extracted"
                raise CommandExecutionError(msg)
            msg = f"End marker '{end_marker_out}' not found in output"
            raise CommandExecutionError(msg)

        between_lines = all_lines[start_line_index + 1 : end_line_index]
        # If Ruby printed our error marker, prefer it over other patterns
        if any((ln.strip() == error_marker_out) or (ln.strip().endswith(error_marker_out)) for ln in between_lines):
            logger.error("Error marker found in output, indicating a Ruby error")
            error_message = "Ruby error detected"
            for ln in between_lines:
                if "Ruby error:" in ln:
                    error_message = ln.strip()
                    break
            raise RubyError(error_message)

        # Build command output from lines strictly between markers, excluding any marker lines
        out_lines = [ln for ln in between_lines if ln.strip() and not ln.strip().startswith("--EXEC_")]
        command_output = "\n".join(out_lines).strip()

        error_patterns = [
            "SyntaxError:",
            "NameError:",
            "NoMethodError:",
            "ArgumentError:",
            "TypeError:",
            "RuntimeError:",
        ]

        for pattern in error_patterns:
            if pattern in command_output:
                logger.warning("Ruby error pattern '%s' detected in output", pattern)
                for line in command_output.split("\n"):
                    if pattern in line:
                        raise RubyError(line.strip())

        # Find script echo end (comment) to delimit where echoed code stops
        script_echo_end_index = -1
        for idx in range(len(all_lines) - 1, -1, -1):
            if script_end_comment_out in all_lines[idx]:
                script_echo_end_index = idx
                break

        # Defensive: detect Ruby errors when end marker may be missing (e.g., SystemStackError in IRB)
        try:
            scan_start = max(end_line_index, script_echo_end_index)
            trailing_segment_lines = all_lines[scan_start + 1 :]
            trailing_output = "\n".join(trailing_segment_lines)
            if (
                "SystemStackError" in trailing_output
                or "full_message':" in trailing_output
                or "stack level too deep" in trailing_output
            ):
                snippet = self._extract_error_summary(trailing_output)
                msg = f"Ruby console reported error after end marker: {snippet}"
                raise RubyError(msg)  # noqa: TRY301
        except RubyError:
            raise
        except Exception:  # noqa: BLE001,S110
            # If detection itself fails, ignore and continue
            pass

        return command_output

    def _get_console_state(self, output: str) -> dict[str, Any]:
        """Check if the Rails console is ready for input by looking for the prompt.

        Args:
            output: Current tmux pane output

        Returns:
            Dictionary with state information

        """
        ready_patterns = ["irb(main):", ">", ">>", "irb>", "pry>"]
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

    def _wait_for_console_output(
        self,
        target: str,
        marker: str | None,
        timeout: int,
    ) -> tuple[bool, str]:
        """Wait for specific marker to appear in the console output.

        Args:
            target: tmux target (session:window.pane)
            marker: Text to wait for in the output
            timeout: Maximum time to wait in seconds

        Returns:
            tuple: (marker_found, output)

        Raises:
            CommandExecutionError: If timeout waiting for marker

        """
        start_time = time.time()
        poll_interval = 0.05
        max_interval = 0.5

        if marker is None:
            logger.debug("Waiting for console output without specific marker (polling only)")
        else:
            logger.debug("Waiting for marker '%s' in console output", marker)

        while time.time() - start_time < timeout:
            try:
                tmux = shutil.which("tmux") or "tmux"
                capture = subprocess.run(  # noqa: S603
                    [tmux, "capture-pane", "-p", "-S", "-500", "-t", target],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                current_output = capture.stdout

                # Detect fatal console errors early (prefer fatal over marker)
                if self._has_fatal_console_error(current_output):
                    logger.error("Fatal console error detected while waiting for marker")
                    return False, current_output

                if marker is not None and marker in current_output:
                    logger.debug("Marker found after %.2fs", time.time() - start_time)
                    return True, current_output

                console_state = self._get_console_state(current_output)
                if console_state["ready"] and time.time() - start_time > 3:  # noqa: PLR2004
                    logger.debug("Console ready but marker not found yet")

                time.sleep(poll_interval)
                poll_interval = min(poll_interval * 1.5, max_interval)
            except subprocess.SubprocessError as e:
                logger.exception("Error capturing tmux pane")
                msg = f"Error capturing tmux pane: {e}"
                raise CommandExecutionError(msg) from e

        if marker is not None:
            logger.error("Marker '%s' not found after %ss", marker, timeout)
        else:
            logger.error("Console output wait timed out after %ss without marker", timeout)
        return False, current_output

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
        tmux = shutil.which("tmux") or "tmux"
        start_time = time.time()
        poll_interval = 0.05
        attempts = 0

        while time.time() - start_time < timeout:
            try:
                capture = subprocess.run(  # noqa: S603
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
                    subprocess.run(  # noqa: S603
                        [tmux, "send-keys", "-t", target, "C-c"],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    time.sleep(0.3)
                    attempts += 1

                    if attempts >= 2:  # noqa: PLR2004
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

    def _send_command_to_tmux(  # noqa: C901, PLR0915
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
            wait_for_line: Optional line that must appear after script end
            script_end_marker: Marker indicating end of script echo

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
            tmux = shutil.which("tmux") or "tmux"
            subprocess.run(  # noqa: S603
                [tmux, "send-keys", "-t", target, escaped_command, "Enter"],
                capture_output=True,
                text=True,
                check=True,
            )

            # Give the command a brief moment to start producing output
            time.sleep(0.2)

            # State-machine: first observe script-end echo, then require post-script output to contain EXEC_END
            if script_end_marker:
                found_script_end, pane_output = self._wait_for_console_output(
                    target,
                    script_end_marker,
                    timeout,
                )
                if not found_script_end:
                    if self._has_fatal_console_error(pane_output):
                        snippet = self._extract_error_summary(pane_output)
                        msg = f"Rails console crashed before script-end echo: {snippet}"
                        raise ConsoleNotReadyError(msg)  # noqa: TRY301
                    msg = "Script end echo not observed in console output"
                    raise CommandExecutionError(msg)  # noqa: TRY301

                # Wait for any new output beyond script-end echo
                baseline = pane_output
                start_wait = time.time()
                while time.time() - start_wait < max(2, min(timeout, 10)):
                    tmux = shutil.which("tmux") or "tmux"
                    cap = subprocess.run(  # noqa: S603
                        [tmux, "capture-pane", "-p", "-S", "-200", "-t", target],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    cur = cap.stdout
                    if cur != baseline:
                        break
                    time.sleep(0.1)

                # Inspect only the tail window for the end marker
                tail_lines = cur.strip().split("\n")[-200:]
                if wait_for_line and not any(wait_for_line in ln for ln in tail_lines):
                    # No further output with EXEC_END → error (nothing should print after EXEC_END)
                    msg = "End marker not found in tail after post-script output"
                    raise CommandExecutionError(msg)  # noqa: TRY301

            # Now ensure prompt is ready before final capture
            self._wait_for_console_ready(target, timeout, reset_on_stall=False)

            # After script completes, capture a compact tail; outer parser will locate markers
            tmux = shutil.which("tmux") or "tmux"
            cap = subprocess.run(  # noqa: S603
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
                [line.strip() for line in last_output.split("\n") if not line.strip().startswith("irb(main):")],
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
