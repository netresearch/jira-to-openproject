#!/usr/bin/env python3
"""
RailsConsoleClient

Client for interacting with Rails console in a tmux session.
Uses Docker client for container operations.
"""

import os
import json
import time
from typing import Any

from src import config
from src.clients.docker_client import DockerClient
from src.utils.file_manager import FileManager

logger = config.logger


class RailsConsoleClient:
    """
    Client for interacting with Rails console via tmux in a Docker container.
    """

    def __init__(
        self,
        container_name: str,
        ssh_host: str,
        ssh_user: str | None = None,
        ssh_key_file: str | None = None,
        tmux_session_name: str = "rails_console",
        window: int = 0,
        pane: int = 0,
        command_timeout: int = 180,
        inactivity_timeout: int = 30,
    ):
        """
        Initialize the Rails console client.

        Args:
            container_name: Name of the Docker container
            ssh_host: SSH host where Docker is running
            ssh_user: SSH username (default: current user)
            ssh_key_file: Path to SSH key file (default: use SSH agent)
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

        # Initialize Docker client
        self.docker_client = DockerClient(
            container_name=container_name,
            ssh_host=ssh_host,
            ssh_user=ssh_user,
            ssh_key_file=ssh_key_file,
            command_timeout=command_timeout
        )

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
        Check if the specified tmux session exists.

        Returns:
            True if session exists, False otherwise
        """
        cmd = f"tmux has-session -t {self.tmux_session_name} 2>/dev/null && echo 'EXISTS' || echo 'NOT_EXISTS'"
        result = self.docker_client.ssh_client.execute_command(cmd)

        return result["status"] == "success" and "EXISTS" in result["stdout"]

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

        # Send the command to the tmux session
        target = self._get_target()
        send_cmd = f"tmux send-keys -t {target} '{config_cmd}' Enter"

        result = self.docker_client.ssh_client.execute_command(send_cmd)

        if result["status"] == "success":
            logger.debug("IRB configuration commands sent successfully")
            time.sleep(1)  # Give more time for settings to apply
        else:
            logger.error(f"Failed to configure IRB settings: {result.get('error', 'Unknown error')}")
            raise RuntimeError("Failed to configure IRB settings")

    def _clear_pane(self) -> None:
        """
        Clear the tmux pane to prepare for command output.
        """
        target = self._get_target()
        # Send Ctrl+L to clear the screen
        clear_cmd = f"tmux send-keys -t {target} C-l"

        result = self.docker_client.ssh_client.execute_command(clear_cmd)

        if result["status"] != "success":
            logger.warning("Failed to clear tmux pane")

    def _stabilize_console(self) -> bool:
        """
        Send a harmless command to stabilize console state.

        Returns:
            True if successful, False otherwise
        """
        try:
            target = self._get_target()
            # Send a space and Enter to reset terminal state
            send_cmd = f"tmux send-keys -t {target} ' ' Enter"

            self.docker_client.ssh_client.execute_command(send_cmd)
            time.sleep(0.5)  # Small delay

            # Clear the screen
            clear_cmd = f"tmux send-keys -t {target} C-l"
            clear_result = self.docker_client.ssh_client.execute_command(clear_cmd)

            if clear_result["status"] != "success":
                logger.debug("Non-critical error when clearing screen")

            logger.debug("Console state stabilized")
            return True
        except Exception as e:
            logger.debug(f"Non-critical error when stabilizing console: {str(e)}")
            return False

    def _escape_command(self, command: str) -> str:
        """
        Escape a command for shell execution.

        Args:
            command: Command to escape

        Returns:
            Escaped command
        """
        # Replace all double quotes with escaped double quotes
        # Also escape backticks, dollar signs and backslashes
        escaped = command.replace("\\", "\\\\") \
                         .replace('"', '\\"') \
                         .replace('`', '\\`') \
                         .replace('$', '\\$')
        return escaped

    def _create_result_file_path(self, marker_id: str) -> tuple[str, str]:
        """
        Create paths for result files.

        Args:
            marker_id: Unique marker ID

        Returns:
            Tuple of (container_path, local_path)
        """
        # Path in the container
        container_path = f"/tmp/{marker_id}_result.json"

        # Local path
        local_path = os.path.join(self.file_manager.data_dir, f"{marker_id}_result.json")

        return container_path, local_path

    def execute(self, command: str, timeout: int | None = None) -> dict[str, Any]:
        """
        Execute a command in the Rails console and wait for completion.

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

        # Define markers for output
        start_marker = f"RAILSCMD_{marker_id}_START"
        end_marker = f"RAILSCMD_{marker_id}_END"

        # Create paths for result file
        container_result_path, local_result_path = self._create_result_file_path(marker_id)

        # Wrap the command with code to write results to a file
        file_based_command = f"""
        begin
          puts "{start_marker}"
          result = nil
          begin
            # Execute the original command and capture the result
            result = (
              {command}
            )

            # Write the result to a file to handle large outputs
            File.write('{container_result_path}', {{
              status: 'success',
              output: result.nil? ? "nil" : result.inspect
            }}.to_json)

            # Print a short confirmation to stdout
            puts "Command executed, results written to {container_result_path}"
          rescue => e
            # Write error information to the file
            File.write('{container_result_path}', {{
              status: 'error',
              error: e.message,
              backtrace: e.backtrace
            }}.to_json)
            puts "ERROR: #{{e.class.name}}: #{{e.message}}"
          end
          puts "{end_marker}"
          nil
        end
        """

        # Save the command to the debug session
        command_path = os.path.join(debug_session_dir, "ruby_command.rb")
        with open(command_path, 'w') as f:
            f.write(file_based_command)

        # Execute the command via tmux
        tmux_output = self._send_command_to_tmux(file_based_command, timeout)

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

        # Check if the command executed successfully
        if end_marker not in tmux_output:
            error_msg = "Command did not complete (end marker not found)"
            logger.error(error_msg)
            self.file_manager.add_to_debug_log(debug_session_dir, f"ERROR: {error_msg}")
            return {"status": "error", "error": error_msg}

        # Check for error messages in the tmux output
        for line in tmux_output.splitlines():
            if line.strip().startswith("ERROR: "):
                error_line = line.strip()
                logger.error(f"Error executing command: {error_line}")
                self.file_manager.add_to_debug_log(debug_session_dir, f"ERROR: {error_line}")
                return {"status": "error", "error": error_line}

        # Copy the result file from the container
        self.file_manager.add_to_debug_log(
            debug_session_dir,
            f"COPYING RESULT FILE: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Container path: {container_result_path}\n"
            f"Local path: {local_result_path}\n"
        )

        result = self.docker_client.copy_file_from_container(container_result_path, local_result_path)

        if result["status"] != "success":
            error_msg = f"Failed to copy result file: {result.get('error', 'Unknown error')}"
            logger.error(error_msg)
            self.file_manager.add_to_debug_log(debug_session_dir, f"ERROR: {error_msg}")
            return {"status": "error", "error": error_msg}

        # Read and parse the JSON result file
        try:
            with open(local_result_path) as f:
                result_data = json.load(f)

            # Add debug log entry
            self.file_manager.add_to_debug_log(
                debug_session_dir,
                f"RESULT PARSED: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Status: {result_data.get('status')}\n"
            )

            # Return the result data
            if result_data.get("status") == "success":
                output = result_data.get("output")
                # Handle nil values from Ruby
                if output == "nil":
                    return {"status": "success", "output": None}
                return {"status": "success", "output": output}
            else:
                error_msg = result_data.get("error", "Unknown error")
                backtrace = result_data.get("backtrace", [])
                error_with_trace = f"{error_msg}\n" + "\n".join(backtrace) if backtrace else error_msg

                # Log the error
                self.file_manager.add_to_debug_log(
                    debug_session_dir,
                    f"ERROR IN RESULT: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"{error_with_trace}\n"
                )

                return {"status": "error", "error": error_with_trace}

        except json.JSONDecodeError as e:
            error_msg = f"Error parsing JSON result: {str(e)}"
            logger.error(error_msg)
            self.file_manager.add_to_debug_log(debug_session_dir, f"ERROR: {error_msg}")
            return {"status": "error", "error": error_msg}
        except Exception as e:
            error_msg = f"Error processing result: {str(e)}"
            logger.error(error_msg)
            self.file_manager.add_to_debug_log(debug_session_dir, f"ERROR: {error_msg}")
            return {"status": "error", "error": error_msg}

    def _send_command_to_tmux(self, command: str, timeout: int) -> str:
        """
        Send a command to the tmux session and capture output.

        Args:
            command: Command to send
            timeout: Timeout in seconds

        Returns:
            Command output as a string
        """
        target = self._get_target()

        # Stabilize the console first
        self._stabilize_console()

        # Escape the command for tmux send-keys
        escaped_command = self._escape_command(command)

        # Build the tmux command
        send_cmd = f"tmux send-keys -t {target} '{escaped_command}' Enter"

        # Execute the command
        result = self.docker_client.ssh_client.execute_command(send_cmd)

        if result["status"] != "success":
            logger.error(f"Failed to send command to tmux: {result.get('error', 'Unknown error')}")
            return ""

        # Wait a bit for the command to start execution
        time.sleep(1)

        # Capture the output with timeout
        start_time = time.time()
        last_output = ""
        last_change_time = start_time

        while time.time() - start_time < timeout:
            # Capture the current pane content
            capture_cmd = f"tmux capture-pane -p -t {target}"
            result = self.docker_client.ssh_client.execute_command(capture_cmd)

            if result["status"] != "success":
                logger.error(f"Failed to capture tmux pane: {result.get('error', 'Unknown error')}")
                return last_output

            current_output = result["stdout"]

            # Check if output has changed
            if current_output != last_output:
                last_output = current_output
                last_change_time = time.time()
            # Check for inactivity timeout
            elif time.time() - last_change_time > self.inactivity_timeout:
                logger.debug("Output inactivity timeout reached")
                break

            # Small delay before next capture
            time.sleep(0.5)

        # Stabilize the console after command
        self._stabilize_console()

        return last_output

    def execute_script(self, script_content: str, timeout: int | None = None) -> dict[str, Any]:
        """
        Execute a Ruby script in the Rails console.

        Args:
            script_content: Ruby script content
            timeout: Command timeout in seconds (default: self.command_timeout)

        Returns:
            Dict with status and output or error
        """
        # Generate a unique marker ID
        marker_id = self.file_manager.generate_unique_id()

        # Create a script file
        script_path = self.file_manager.create_script_file(
            script_content,
            filename=f"{marker_id}_script.rb"
        )

        # Create paths for the script in the container
        container_script_path = f"/tmp/{marker_id}_script.rb"

        # Copy the script to the container
        result = self.docker_client.copy_file_to_container(script_path, container_script_path)

        if result["status"] != "success":
            return {
                "status": "error",
                "error": f"Failed to copy script to container: {result.get('error', 'Unknown error')}"
            }

        # Execute the script with load command
        load_command = f"load '{container_script_path}'"

        return self.execute(load_command, timeout)

    def execute_with_data(
        self, script_content: str, data: Any, timeout: int | None = None
    ) -> dict[str, Any]:
        """
        Execute a Ruby script with provided data.

        Args:
            script_content: Ruby script content
            data: Data to provide to the script (will be converted to JSON)
            timeout: Command timeout in seconds (default: self.command_timeout)

        Returns:
            Dict with status and output or error
        """
        # Generate a unique marker ID
        marker_id = self.file_manager.generate_unique_id()

        # Create a debug session
        debug_session_dir = self.file_manager.create_debug_session(marker_id)

        # Create a data file
        data_path = self.file_manager.create_data_file(
            data,
            filename=f"{marker_id}_data.json",
            session_dir=debug_session_dir
        )

        # Create paths for container files
        container_script_path = f"/tmp/{marker_id}_script.rb"
        container_data_path = f"/tmp/{marker_id}_data.json"

        # Copy the data file to the container
        result = self.docker_client.copy_file_to_container(data_path, container_data_path)

        if result["status"] != "success":
            return {
                "status": "error",
                "error": f"Failed to copy data file to container: {result.get('error', 'Unknown error')}"
            }

        # Create a script file with code to load the data
        script_with_data = f"""
        # Load data from JSON file
        require 'json'
        begin
          puts "Loading data from container path: {container_data_path}"
          file_content = File.read('{container_data_path}')
          input_data = JSON.parse(file_content)
          puts "Successfully loaded data with #{{input_data.size}} records"
        rescue => err
          puts "Error loading data: #{{err.message}}"
          raise err
        end

        # Main script
        {script_content}
        """

        script_path = self.file_manager.create_script_file(
            script_with_data,
            filename=f"{marker_id}_script.rb",
            session_dir=debug_session_dir
        )

        # Copy the script to the container
        result = self.docker_client.copy_file_to_container(script_path, container_script_path)

        if result["status"] != "success":
            return {
                "status": "error",
                "error": f"Failed to copy script to container: {result.get('error', 'Unknown error')}"
            }

        # Execute the script with load command
        load_command = f"load '{container_script_path}'"

        return self.execute(load_command, timeout)
