#!/usr/bin/env python3
"""RailsConsoleClient.

Client for executing commands on a Rails console running in a tmux session.
Uses exception-based error handling for all operations.
"""

import subprocess
import time
from typing import Any

from src import config
from src.utils.file_manager import FileManager

logger = config.logger


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
        except Exception as e:
            logger.warning("Failed to configure IRB settings: %s", e)

    def _session_exists(self) -> bool:
        """Check if the specified tmux session exists locally.

        Returns:
            True if session exists, False otherwise

        """
        try:
            cmd = ["tmux", "has-session", "-t", self.tmux_session_name]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            return result.returncode == 0
        except subprocess.SubprocessError as e:
            logger.exception("Error checking tmux session: %s", e)
            return False

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

        # Handle non-interactive terminals better
        begin
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
            send_cmd = ["tmux", "send-keys", "-t", target, config_cmd, "Enter"]
            subprocess.run(send_cmd, capture_output=True, text=True, check=True)
            logger.debug("IRB configuration commands sent successfully")
        except subprocess.SubprocessError as e:
            logger.exception("Failed to configure IRB settings: %s", e)
            msg = f"Failed to configure IRB settings: {e}"
            raise TmuxSessionError(msg) from e

    def _clear_pane(self) -> None:
        """Clear the tmux pane to prepare for command output.

        Raises:
            TmuxSessionError: If failed to clear tmux pane

        """
        target = self._get_target()

        try:
            clear_cmd = ["tmux", "send-keys", "-t", target, "C-l"]
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
            send_cmd = ["tmux", "send-keys", "-t", target, " ", "Enter"]
            subprocess.run(send_cmd, capture_output=True, text=True, check=True)
            time.sleep(0.3)

            # Clear the screen
            clear_cmd = ["tmux", "send-keys", "-t", target, "C-l"]
            subprocess.run(clear_cmd, capture_output=True, text=True, check=True)
            time.sleep(0.2)

            # Send Ctrl+C to abort any pending operation
            subprocess.run(["tmux", "send-keys", "-t", target, "C-c"], capture_output=True, text=True, check=True)
            time.sleep(0.2)

            logger.debug("Console state stabilized")
        except subprocess.SubprocessError as e:
            logger.exception("Failed to stabilize console: %s", e)
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

    def execute(self, command: str, timeout: int | None = None, suppress_output: bool = False) -> str:
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

        start_marker_cmd = f'puts "--EXEC_START--" "{marker_id}"'
        start_marker_out = f"--EXEC_START--{marker_id}"

        end_marker_cmd = f'puts "--EXEC_END--" "{marker_id}"'
        end_marker_out = f"--EXEC_END--{marker_id}"

        error_marker_cmd = f'puts "--EXEC_ERROR--" "{marker_id}"'
        error_marker_out = f"--EXEC_ERROR--{marker_id}"

        # Conditionally include result printing based on suppress_output flag
        result_print_line = "" if suppress_output else "puts result.inspect"

        template = """
        # Print start marker
        %s

        # Execute the actual command
        begin
          result = nil  # Initialize result variable
          result = %s  # Assign the actual result

          # Print the result and end marker (conditionally)
          %s
          %s
        rescue => e
          # Print error marker and details
          %s
          puts "Ruby error: #{e.class}: #{e.message}"
          puts e.backtrace.join("\\n")[0..500] rescue nil  # Print limited backtrace
          %s
        end
        """

        wrapped_command = template % (start_marker_cmd, command, result_print_line, end_marker_cmd, error_marker_cmd, end_marker_cmd)

        command_path = self.file_manager.join(debug_session_dir, "ruby_command.rb")
        with command_path.open("w") as f:
            f.write(wrapped_command)

        tmux_output = self._send_command_to_tmux(wrapped_command, timeout)

        tmux_output_path = self.file_manager.join(debug_session_dir, "tmux_output.txt")
        with tmux_output_path.open("w") as f:
            f.write(tmux_output)

        self.file_manager.add_to_debug_log(
            debug_session_dir,
            f"TMUX OUTPUT RECEIVED: {time.strftime('%Y-%m-%d %H:%M:%S')}\nSize: {len(tmux_output)} bytes\n",
        )

        start_idx = tmux_output.find(start_marker_out)
        if start_idx == -1:
            # If we can't find the start marker, but the output shows the command execution already happened,
            # we can try to parse what's available
            console_state = self._get_console_state(tmux_output[-50:])
            if console_state["ready"]:
                logger.warning("Console appears ready despite missing start marker - attempting to extract output")

                # Look for any output that resembles our command's expected output
                if "load" in tmux_output:
                    logger.info("Found load command execution in output, proceeding with extraction")

                    # Try to find the result in the output directly
                    return tmux_output

            # If we can't extract anything useful, we have to fail
            msg = f"Start marker '{start_marker_out}' not found in output"
            raise CommandExecutionError(msg)

        start_idx += len(start_marker_out)
        remainder = tmux_output[start_idx:]

        end_idx = remainder.find(end_marker_out)
        if end_idx == -1:
            logger.error("End marker '%s' not found in output", end_marker_out)

            # Check if the console is in a ready state, which indicates the command may have completed
            console_state = self._get_console_state(tmux_output[-50:])
            if console_state["ready"]:
                logger.warning("Console appears ready despite missing end marker - attempting to extract output")

                # Try to find the result in the output - it usually appears between the start marker and the prompt
                # Look for either a nil, a hash output like {:key=>value}, or other standard Ruby output formats
                lines = remainder.split("\n")
                filtered_lines = []
                for line in lines:
                    line = line.strip()
                    # Skip empty lines and lines containing our potential markers
                    if not line or "--EXEC_" in line:
                        continue
                    # Keep lines that look like command output
                    filtered_lines.append(line)

                if filtered_lines:
                    logger.info("Extracted %s lines of output despite missing end marker", len(filtered_lines))
                    return "\n".join(filtered_lines)
                # If we can't extract useful output, we'll have to fail
                msg = f"End marker '{end_marker_out}' not found in output and no clear output could be extracted"
                raise CommandExecutionError(
                    msg,
                )
            # Console is not ready, command execution might be stuck
            msg = f"End marker '{end_marker_out}' not found in output"
            raise CommandExecutionError(msg)

        command_output = remainder[:end_idx].strip()

        error_idx = remainder.find(error_marker_out)
        if error_idx != -1 and error_idx < end_idx:
            logger.error("Error marker found in output, indicating a Ruby error")

            error_message = "Ruby error detected"
            if "Ruby error:" in command_output:
                for line in command_output.split("\n"):
                    if "Ruby error:" in line:
                        error_message = line.strip()
                        break

            raise RubyError(error_message)

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

    def _wait_for_console_output(self, target: str, marker: str, timeout: int) -> tuple[bool, str]:
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

        logger.debug("Waiting for marker '%s' in console output", marker)

        while time.time() - start_time < timeout:
            try:
                capture = subprocess.run(
                    ["tmux", "capture-pane", "-p", "-S", "-500", "-t", target],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                current_output = capture.stdout

                if marker in current_output:
                    logger.debug("Marker found after %.2fs", time.time() - start_time)
                    return True, current_output

                console_state = self._get_console_state(current_output)
                if console_state["ready"] and time.time() - start_time > 3:
                    logger.debug("Console ready but marker not found yet")

                time.sleep(poll_interval)
                poll_interval = min(poll_interval * 1.5, max_interval)
            except subprocess.SubprocessError as e:
                logger.exception("Error capturing tmux pane: %s", e)
                msg = f"Error capturing tmux pane: {e}"
                raise CommandExecutionError(msg) from e

        logger.warning("Marker '%s' not found after %ss", marker, timeout)
        return False, current_output

    def _wait_for_console_ready(self, target: str, timeout: int = 5) -> bool:
        """Wait for the console to be in a ready state.

        Args:
            target: tmux target (session:window.pane)
            timeout: Maximum time to wait in seconds

        Returns:
            bool: True if console is ready, False if timed out

        Raises:
            ConsoleNotReadyError: If console cannot be made ready

        """
        logger.debug("Waiting for console ready state (timeout: %ss)", timeout)
        start_time = time.time()
        poll_interval = 0.05
        attempts = 0

        while time.time() - start_time < timeout:
            try:
                capture = subprocess.run(
                    ["tmux", "capture-pane", "-p", "-S", "-10", "-t", target],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                current_output = capture.stdout

                console_state = self._get_console_state(current_output)
                if console_state["ready"]:
                    logger.debug("Console ready after %.2fs", time.time() - start_time)
                    return True

                if console_state["state"] in ["awaiting_input", "multiline_string"]:
                    logger.debug("Console in %s state, sending Ctrl+C to reset", console_state["state"])
                    subprocess.run(
                        ["tmux", "send-keys", "-t", target, "C-c"],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    time.sleep(0.3)
                    attempts += 1

                    if attempts >= 2:
                        logger.debug("Multiple Ctrl+C attempts failed, trying full stabilization")
                        self._stabilize_console()
                        attempts = 0

                logger.debug("Waiting %ss for ready state, current: %s", poll_interval, console_state["state"])
                time.sleep(poll_interval)
                poll_interval *= 2
            except subprocess.SubprocessError as e:
                logger.exception("Error checking console state: %s", e)
                msg = f"Error checking console state: {e}"
                raise ConsoleNotReadyError(msg) from e

        logger.warning("Console not ready after %ss", timeout)
        return False

    def _send_command_to_tmux(self, command: str, timeout: int) -> str:
        """Send a command to the local tmux session and capture output.

        Args:
            command: Command to send
            timeout: Timeout in seconds

        Returns:
            Command output as a string

        Raises:
            TmuxSessionError: If tmux command fails
            ConsoleNotReadyError: If console cannot be made ready
            CommandExecutionError: If command execution fails

        """
        target = self._get_target()

        if not self._wait_for_console_ready(target, timeout=10):
            logger.warning("Console not ready, forcing full stabilization")
            self._stabilize_console()

            if not self._wait_for_console_ready(target, timeout=5):
                msg = "Console could not be made ready"
                raise ConsoleNotReadyError(msg)

        escaped_command = self._escape_command(command)

        try:
            marker_id = self.file_manager.generate_unique_id()
            start_marker = f"TMUX_CMD_START_{marker_id}"
            end_marker = f"TMUX_CMD_END_{marker_id}"

            logger.debug("Sending start marker to tmux session: %s", start_marker)
            subprocess.run(
                ["tmux", "send-keys", "-t", target, f"puts '{start_marker}'", "Enter"],
                capture_output=True,
                text=True,
                check=True,
            )

            logger.debug("Sending command (length: %s bytes)", len(escaped_command))
            subprocess.run(
                ["tmux", "send-keys", "-t", target, escaped_command, "Enter"],
                capture_output=True,
                text=True,
                check=True,
            )

            # Give the command time to execute before checking if console is ready
            # This prevents sending the end marker before the command output appears
            time.sleep(0.5)
            self._wait_for_console_ready(target, timeout)

            logger.debug("Sending end marker to tmux session: %s", end_marker)
            subprocess.run(
                ["tmux", "send-keys", "-t", target, f"puts '{end_marker}'", "Enter"],
                capture_output=True,
                text=True,
                check=True,
            )

            found_end, last_output = self._wait_for_console_output(target, end_marker, timeout)

            if not found_end:
                msg = "End marker not found in tmux output"
                raise CommandExecutionError(msg)

            # drop all return lines from last_output
            last_output = "\n".join(
                [line.strip() for line in last_output.split("\n") if not line.strip().startswith("=> ")],
            )
            # drop all lines irb(main):30486>
            last_output = "\n".join(
                [line.strip() for line in last_output.split("\n") if not line.strip().startswith("irb(main):")],
            )
            # extract lines between markers
            start_idx = last_output.find(start_marker) + len(start_marker)
            end_idx = last_output.find(end_marker)
            if start_idx != -1 and end_idx != -1:
                last_output = last_output[start_idx:end_idx]
            else:
                msg = "Start or end marker not found in tmux output"
                raise CommandExecutionError(msg)

            return last_output.strip()

        except subprocess.SubprocessError as e:
            logger.exception("Tmux command failed: %s", e)
            self._stabilize_console()
            msg = f"Tmux command failed: {e}"
            raise TmuxSessionError(msg) from e
        except Exception as e:
            logger.exception("Error sending command to tmux: %s", e)
            self._stabilize_console()
            msg = f"Error sending command to tmux: {e}"
            raise CommandExecutionError(msg) from e
