#!/usr/bin/env python3
"""
RailsConsoleClient

Client for interacting with Rails console in a tmux session.
This client is part of the layered client architecture where:
1. RailsConsoleClient interacts only with the tmux session running Rails console
2. It does not know about Docker or SSH - it purely handles sending commands to and
   capturing output from the tmux session
3. OpenProjectClient coordinates all clients, including file transfers via SSHClient/DockerClient
   and command execution via RailsConsoleClient
"""

import os
import time
import subprocess
from typing import Any, Dict, Optional

from src import config
from src.utils.file_manager import FileManager

logger = config.logger


class RailsConsoleClient:
    """
    Client for interacting with Rails console via a local tmux session.
    This client only knows how to:
    1. Send commands to a tmux session containing a Rails console
    2. Capture and parse the output from these commands
    3. It does not directly interact with Docker or SSH
    """

    def __init__(
        self,
        tmux_session_name: str = "rails_console",
        window: int = 0,
        pane: int = 0,
        command_timeout: int = 180,
        inactivity_timeout: int = 30,
    ):
        """
        Initialize the Rails console client.

        Args:
            tmux_session_name: tmux session name (default: "rails_console")
            window: tmux window number (default: 0)
            pane: tmux pane number (default: 0)
            command_timeout: Command timeout in seconds (default: 180)
            inactivity_timeout: Inactivity timeout in seconds (default: 30)
        """
        self.tmux_session_name = tmux_session_name
        self.window = window
        self.pane = pane
        self.command_timeout = command_timeout
        self.inactivity_timeout = inactivity_timeout

        # Initialize file manager
        self.file_manager = FileManager()

        # Rails console command
        self._rails_command = "bundle exec rails console"

        # Check if tmux session exists
        if not self._session_exists():
            raise ValueError(f"tmux session '{self.tmux_session_name}' does not exist")

        logger.success(f"Connected to tmux session '{self.tmux_session_name}'")

        # Configure IRB settings during initialization
        try:
            self._configure_irb_settings()
        except Exception as e:
            logger.warning(f"Could not configure IRB settings: {str(e)}")

    def _session_exists(self) -> bool:
        """
        Check if the specified tmux session exists locally.

        Returns:
            True if session exists, False otherwise
        """
        try:
            # Check if the tmux session exists locally
            cmd = ["tmux", "has-session", "-t", self.tmux_session_name]
            result = subprocess.run(cmd, capture_output=True, text=True)
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Error checking local tmux session: {str(e)}")
            return False

    def _get_target(self) -> str:
        """
        Get the tmux target string for the session, window, and pane.

        Returns:
            tmux target string
        """
        return f"{self.tmux_session_name}:{self.window}.{self.pane}"

    def _configure_irb_settings(self) -> None:
        """
        Configure IRB settings for better output and interaction.
        """
        # Disable color output, ensure large objects can be displayed,
        # and configure readline settings to avoid IO errors
        config_cmd = """
        IRB.conf[:USE_COLORIZE] = false
        IRB.conf[:INSPECT_MODE] = :to_s

        # Additional settings to prevent IO errors with Ruby 3.4's Reline library
        begin
          # Handle non-interactive terminals better
          if defined?(Reline)
            Reline.output_modifier_proc = nil
            Reline.completion_proc = nil
            Reline.prompt_proc = nil
            puts "Applied Reline configuration"
          end

          # Configure history behavior to avoid file access issues
          IRB.conf[:SAVE_HISTORY] = nil
          IRB.conf[:HISTORY_FILE] = nil
        rescue => e
          puts "Error during IRB configuration: #{e.message}"
        end

        puts "IRB configuration commands sent successfully"
        """

        # Send the command to the local tmux session
        target = self._get_target()

        try:
            # Use local tmux command to send keys to the session
            send_cmd = ["tmux", "send-keys", "-t", target, config_cmd, "Enter"]
            subprocess.run(send_cmd, capture_output=True, text=True, check=True)

            logger.debug("IRB configuration commands sent successfully")
            time.sleep(1)  # Give more time for settings to apply
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to configure IRB settings: {str(e)}")
            raise RuntimeError("Failed to configure IRB settings")
        except Exception as e:
            logger.error(f"Unexpected error configuring IRB: {str(e)}")
            raise RuntimeError(f"Failed to configure IRB settings: {str(e)}")

    def _clear_pane(self) -> None:
        """
        Clear the tmux pane to prepare for command output.
        """
        target = self._get_target()

        try:
            # Send Ctrl+L to clear the screen using local tmux
            clear_cmd = ["tmux", "send-keys", "-t", target, "C-l"]
            subprocess.run(clear_cmd, capture_output=True, text=True, check=True)
        except Exception as e:
            logger.warning(f"Failed to clear tmux pane: {str(e)}")

    def _stabilize_console(self) -> bool:
        """
        Send a harmless command to stabilize console state.

        Returns:
            True if successful, False otherwise
        """
        try:
            target = self._get_target()

            # Send a space and Enter to reset terminal state using local tmux
            send_cmd = ["tmux", "send-keys", "-t", target, " ", "Enter"]
            subprocess.run(send_cmd, capture_output=True, text=True, check=True)
            time.sleep(0.5)  # Small delay

            # Clear the screen
            clear_cmd = ["tmux", "send-keys", "-t", target, "C-l"]
            subprocess.run(clear_cmd, capture_output=True, text=True, check=True)

            logger.debug("Console state stabilized")
            return True
        except Exception as e:
            logger.debug(f"Non-critical error when stabilizing console: {str(e)}")
            return False

    def _escape_command(self, command: str) -> str:
        """
        Escape a command for tmux send-keys.

        When sending to tmux, we don't need to escape double quotes the same way as shell.
        This was causing syntax errors in the Ruby code.

        Args:
            command: Command to escape

        Returns:
            Escaped command
        """
        # Only escape characters that would interact with the shell
        # Do NOT escape double quotes - they should be passed through as-is to Ruby
        escaped = command.replace("\\", "\\\\") \
                         .replace('`', '\\`') \
                         .replace('$', '\\$')
        return escaped

    def execute(self, command: str, timeout: Optional[int] = None) -> Dict[str, Any]:
        """
        Execute a command in the Rails console and wait for completion.

        This method uses a direct output capture approach:
        1. Sends the Ruby command to tmux with unique markers
        2. Captures output from tmux session
        3. Extracts the output between markers

        Args:
            command: Ruby command to execute
            timeout: Command timeout in seconds (default: self.command_timeout)

        Returns:
            Dict with status and output or error
        """
        # Use the provided timeout or the default
        if timeout is None:
            timeout = self.command_timeout

        # Generate a unique marker ID
        marker_id = self.file_manager.generate_unique_id()

        # Create debug session directory
        debug_session_dir = self.file_manager.create_debug_session(marker_id)

        # Log the command to the debug log
        self.file_manager.add_to_debug_log(
            debug_session_dir,
            f"COMMAND EXECUTION START: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Command: {command}\n"
        )

        # Create unique marker strings that will be rendered differently by puts
        # When puts receives multiple string arguments, it concatenates them without spaces
        start_marker_cmd = f"puts \"START\" \"{marker_id}\""  # Command looks like: puts "START" "abc123"
        start_marker_out = f"START{marker_id}"  # Output looks like: STARTabc123

        end_marker_cmd = f"puts \"END\" \"{marker_id}\""  # Command looks like: puts "END" "abc123"
        end_marker_out = f"END{marker_id}"  # Output looks like: ENDabc123

        error_marker_cmd = f"puts \"ERROR\" \"{marker_id}\""  # Command looks like: puts "ERROR" "abc123"
        error_marker_out = f"ERROR{marker_id}"  # Output looks like: ERRORadc123

        # Use a template string to execute the command with markers
        template = """
        # Print start marker
        %s

        # Execute the actual command
        begin
          result = %s

          # Print the result and end marker
          puts result.inspect
          %s
          nil  # Return nil to avoid printing result twice
        rescue => e
          # Print error marker and details
          %s
          puts "Ruby error: #{e.class}: #{e.message}"
          %s
          nil
        end
        """

        # Format the template with our values
        wrapped_command = template % (
            start_marker_cmd,
            command,
            end_marker_cmd,
            error_marker_cmd,
            end_marker_cmd
        )

        # Save the command to the debug session
        command_path = os.path.join(debug_session_dir, "ruby_command.rb")
        with open(command_path, 'w') as f:
            f.write(wrapped_command)

        # Execute the command via tmux
        tmux_output = self._send_command_to_tmux(wrapped_command, timeout)

        # Save tmux output to debug session
        tmux_output_path = os.path.join(debug_session_dir, "tmux_output.txt")
        with open(tmux_output_path, 'w') as f:
            f.write(tmux_output)

        # Add debug log entry
        self.file_manager.add_to_debug_log(
            debug_session_dir,
            f"TMUX OUTPUT RECEIVED: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Size: {len(tmux_output)} bytes\n"
        )

        # Extract the output between markers
        try:
            # Look for our output markers (not the commands)
            start_idx = tmux_output.find(start_marker_out)
            if start_idx == -1:
                logger.error(f"Start marker '{start_marker_out}' not found in output")
                return {"status": "error", "error": "Start marker not found in command output"}

            # Extract output after start marker
            start_idx += len(start_marker_out)
            remainder = tmux_output[start_idx:]

            # Find the end marker
            end_idx = remainder.find(end_marker_out)
            if end_idx == -1:
                logger.error(f"End marker '{end_marker_out}' not found in output")
                return {"status": "error", "error": "End marker not found in command output"}

            # Extract the content between start and end markers
            command_output = remainder[:end_idx].strip()

            # First priority: Check for error marker in the extracted section
            # This must be checked before success to catch actual errors
            error_idx = remainder.find(error_marker_out)
            if error_idx != -1 and error_idx < end_idx:
                # Found an error marker in the valid output range
                logger.error("Error marker found in output, indicating a Ruby error")

                # Extract the error message
                if "Ruby error:" in command_output:
                    error_content = command_output[command_output.find("Ruby error:"):]
                    error_line = error_content.split("\n")[0].strip()
                    return {"status": "error", "error": error_line}

                return {"status": "error", "error": "Ruby error detected"}

            # If no error was found, check for success keywords in the extracted content
            # Note: Only check the actual command output, not the entire tmux buffer
            if "SUCCESS" in command_output:
                logger.debug("SUCCESS keyword found in command output")
                return {"status": "success", "output": {"success": True, "raw_output": command_output}}

            # No explicit error or success indicator, return the content as success
            logger.debug(f"Command executed successfully, extracted {len(command_output)} chars of output")
            return {"status": "success", "output": command_output}

        except Exception as e:
            logger.error(f"Error processing command output: {str(e)}")
            return {"status": "error", "error": f"Error processing command output: {str(e)}"}

    def _parse_ruby_output(self, ruby_output: str) -> Any:
        """
        Parse Ruby output into appropriate Python data structures.

        Args:
            ruby_output: The raw output from the Ruby console

        Returns:
            Parsed Python data structure or the original string if parsing fails
        """
        # Handle success indicators
        if "SUCCESS" in ruby_output:
            return {"success": True, "raw_output": ruby_output}

        # If empty or nil, return None
        if not ruby_output or ruby_output == "nil":
            return None

        # Return the raw output as a fallback
        return ruby_output

    def _send_command_to_tmux(self, command: str, timeout: int) -> str:
        """
        Send a command to the local tmux session and capture output.

        Args:
            command: Command to send
            timeout: Timeout in seconds

        Returns:
            Command output as a string
        """
        target = self._get_target()

        # Stabilize the console first
        logger.debug("Stabilizing console before command execution")
        self._stabilize_console()

        # Escape the command for tmux send-keys
        escaped_command = self._escape_command(command)

        try:
            # Add markers to the command for easier parsing
            # We'll wrap the command with puts statements to mark the beginning and end
            marker_id = self.file_manager.generate_unique_id()
            start_marker = f"TMUX_CMD_START_{marker_id}"
            end_marker = f"TMUX_CMD_END_{marker_id}"

            # First, clear current line and any partial input
            logger.debug("Clearing current input line in tmux session")
            subprocess.run(
                ["tmux", "send-keys", "-t", target, "C-c"],
                capture_output=True,
                text=True
            )
            time.sleep(0.5)

            # Send a unique start marker so we can identify where our output begins
            logger.debug(f"Sending start marker to tmux session: {start_marker}")
            subprocess.run(
                ["tmux", "send-keys", "-t", target, f"puts '{start_marker}'", "Enter"],
                capture_output=True,
                text=True
            )
            time.sleep(0.5)

            # Execute the command
            logger.debug("Sending command to tmux session: (truncated for brevity)")
            logger.debug(f"Command length: {len(escaped_command)} bytes")
            subprocess.run(
                ["tmux", "send-keys", "-t", target, escaped_command, "Enter"],
                capture_output=True,
                text=True
            )

            # Send an end marker after the command
            time.sleep(1.0)  # Give more time for the main command to start executing
            logger.debug(f"Sending end marker to tmux session: {end_marker}")
            subprocess.run(
                ["tmux", "send-keys", "-t", target, f"puts '{end_marker}'", "Enter"],
                capture_output=True,
                text=True
            )

            # Wait for result and capture output
            logger.debug(f"Waiting for command completion (timeout: {timeout}s)")
            start_time = time.time()
            last_output = ""
            found_end_marker = False

            # Loop until we find the end marker or timeout
            while time.time() - start_time < timeout:
                # Capture the current pane content
                capture = subprocess.run(
                    ["tmux", "capture-pane", "-p", "-S", "-100", "-t", target],
                    capture_output=True,
                    text=True,
                    check=True
                )
                current_output = capture.stdout

                # Check if the end marker is in the output
                if end_marker in current_output:
                    logger.debug(f"Found end marker after {time.time() - start_time:.1f}s")
                    last_output = current_output
                    found_end_marker = True
                    break

                # Update last output if it has changed
                if current_output != last_output:
                    last_output = current_output

                # Small delay to prevent hammering the tmux session
                time.sleep(0.5)

            if not found_end_marker:
                logger.warning(f"End marker not found after {timeout}s")
                logger.debug("Capturing final output")
                # One final capture
                capture = subprocess.run(
                    ["tmux", "capture-pane", "-p", "-S", "-200", "-t", target],
                    capture_output=True,
                    text=True,
                    check=True
                )
                last_output = capture.stdout

            # Save the output for debugging
            return last_output

        except subprocess.SubprocessError as e:
            logger.error(f"Tmux command failed: {str(e)}")
            return f"ERROR: {str(e)}"
        except Exception as e:
            logger.error(f"Error sending command to tmux: {str(e)}")
            return f"ERROR: {str(e)}"
