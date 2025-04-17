#!/usr/bin/env python3
"""
OpenProject Rails Client

A client that interacts with an OpenProject Rails console through tmux.
This allows for executing Rails commands in an existing tmux session
with an already running Rails console.
"""

import os
import sys
import json
import subprocess
import time
import re
from typing import Dict, List, Any, Union, Optional, Tuple

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src import config

logger = config.logger

class OpenProjectRailsClient:
    """
    Client for interacting with OpenProject Rails console via tmux.
    Implemented as a singleton to ensure only one instance exists.
    """

    # Singleton instance
    _instance = None

    def __new__(cls, *args, **kwargs):
        """Create a singleton instance of the OpenProjectRailsClient."""
        if cls._instance is None:
            cls._instance = super(OpenProjectRailsClient, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(
            self,
            window: int = 0,
            pane: int = 0,
            marker_prefix: str = "RAILSCMD_",
            debug: bool = False,
            command_timeout: int = 600,
            inactivity_timeout: int = 30
        ):
        """
        Initialize the Rails client.

        Args:
            window: tmux window number (default: 0)
            pane: tmux pane number (default: 0)
            marker_prefix: prefix for output markers (default: "RAILSCMD_")
            debug: whether to enable debug logging (default: False)
            command_timeout: maximum time in seconds to wait for a command to complete (default: 180)
            inactivity_timeout: time in seconds to wait after output stops changing before interrupting (default: 30)
        """
        # Skip initialization if already initialized
        if self._initialized:
            return

        self.session_name = config.openproject_config.get("tmux_session_name", "rails_console")
        logger.debug(f"Using tmux session name from config: {self.session_name}")

        self.window = window
        self.pane = pane
        self.marker_prefix = marker_prefix
        self.debug = debug
        self.command_counter = 0
        self._is_connected = False

        # Timeout settings
        self.command_timeout = command_timeout
        self.inactivity_timeout = inactivity_timeout

        # Validate that tmux is installed
        try:
            subprocess.run(["tmux", "-V"], check=True, capture_output=True)
            self._is_connected = self._session_exists()
        except (subprocess.SubprocessError, FileNotFoundError):
            logger.error("tmux is not installed or not available in PATH")
            raise RuntimeError("tmux is not installed or not available in PATH")

        # Check if specified tmux session exists
        if not self._is_connected:
            logger.error(f"tmux session '{self.session_name}' does not exist")
            raise ValueError(f"tmux session '{self.session_name}' does not exist")
        else:
            logger.success(f"Successfully connected to tmux session '{self.session_name}'")

        # Directly configure IRB settings during initialization
        try:
            self._configure_irb_settings()
        except Exception as e:
            logger.warning(f"Could not configure IRB settings: {str(e)}")

        # Mark as initialized
        self._initialized = True

    def _session_exists(self) -> bool:
        """Check if the specified tmux session exists."""
        try:
            result = subprocess.run(
                ["tmux", "has-session", "-t", self.session_name],
                check=False,
                capture_output=True
            )
            return result.returncode == 0
        except subprocess.SubprocessError:
            return False

    def _get_target(self) -> str:
        """Get the tmux target string for the session, window, and pane."""
        return f"{self.session_name}:{self.window}.{self.pane}"

    def execute(self, command: str, timeout: int = None) -> Dict[str, Any]:
        """
        Execute a command in the Rails console and wait for it to complete.

        Args:
            command: The Ruby command to execute
            timeout: Maximum time to wait for command completion (seconds).
                     If None, uses the instance's command_timeout.

        Returns:
            Dictionary with status ('success' or 'error') and output or error message
        """
        if not self._is_connected:
            return {
                'status': 'error',
                'error': 'Not connected to tmux session'
            }

        # Use the provided timeout or fall back to the instance's command_timeout
        if timeout is None:
            timeout = self.command_timeout

        target = self._get_target()
        marker_id = str(int(time.time()))
        start_marker = f"RAILSCMD_{marker_id}_START"
        end_marker = f"RAILSCMD_{marker_id}_END"

        # Create a file-based output command wrapper for large outputs
        output_file = f"/tmp/rails_output_{marker_id}.json"

        # Wrap the original command with code to write results to a file
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
            File.write('{output_file}', {{
              status: 'success',
              output: result.nil? ? "nil" : result.inspect
            }}.to_json)

            # Print a short confirmation to stdout
            puts "Command executed, results written to {output_file}"
          rescue => e
            # Write error information to the file
            File.write('{output_file}', {{
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

        # Send the command to the tmux session
        result = self._send_command(file_based_command, start_marker, end_marker, timeout)

        if result is None:
            logger.error("Command timed out or markers not found")
            return {
                'status': 'error',
                'error': 'Command timed out or markers not found'
            }

        # Check if the command execution finished successfully
        if end_marker not in result:
            logger.error(f"End marker not found in result: {result}")
            return {
                'status': 'error',
                'error': 'Command did not complete (end marker not found)'
            }

        # Check for error messages in the result
        # ERROR must be start of line
        if result.startswith("ERROR: "):
            error_line = next((line for line in result.splitlines() if "ERROR:" in line), "Unknown error")
            logger.error(f"Error executing command: {error_line}")
            return {
                'status': 'error',
                'error': error_line
            }

        # Now, transfer the output file from the container to local machine
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as temp_file:
            local_output_path = temp_file.name

        if self.transfer_file_from_container(output_file, local_output_path):
            try:
                # Read and parse the JSON output file
                with open(local_output_path, 'r') as f:
                    result_data = json.load(f)

                # Delete the temporary file
                os.unlink(local_output_path)

                # Return the parsed results
                if result_data.get('status') == 'success':
                    output = result_data.get('output')
                    # Explicitly handle nil values from Ruby
                    if output == "nil":
                        return {
                            'status': 'success',
                            'output': None
                        }
                    return {
                        'status': 'success',
                        'output': output
                    }
                else:
                    return {
                        'status': 'error',
                        'error': result_data.get('error', 'Unknown error')
                    }
            except json.JSONDecodeError as e:
                return {
                    'status': 'error',
                    'error': f'Error parsing JSON output file: {e}'
                }
            except Exception as e:
                return {
                    'status': 'error',
                    'error': f'Error reading result file: {str(e)}'
                }
        else:
            # If we couldn't retrieve the file, return as much from stdout as we have
            logger.warning("Couldn't retrieve output file, returning stdout output")
            return {
                'status': 'success',
                'output': result
            }

    def _send_command(self, command: str, start_marker: str, end_marker: str, timeout: int) -> Optional[str]:
        """
        Send a command to the tmux session and wait for completion.

        Args:
            command: The command to execute
            start_marker: The marker to identify start of output
            end_marker: The marker to identify end of output
            timeout: Maximum time to wait for completion in seconds

        Returns:
            String containing the captured output, or None if timed out
        """
        if not command:
            logger.error("Empty command")
            return None

        try:
            # Send the command to the tmux session
            target = self._get_target()

            logger.debug(f"Sending command to tmux (wrapped): {command}")

            send_cmd = ["tmux", "send-keys", "-t", target, command, "Enter"]
            subprocess.run(send_cmd, check=True)

            # Wait for the command to complete (look for end marker in output)
            start_time = time.time()
            found_start = False
            found_end = False

            # For checking inactivity
            last_output_change_time = start_time
            last_output = ""

            # Check more frequently at the beginning
            check_interval = 0.2
            attempts = 0
            last_log_time = start_time

            # For debugging long-running commands
            recovery_attempts = 0

            # Use command_timeout as the overall limit (default to passed timeout if not set)
            max_timeout = getattr(self, 'command_timeout', timeout)

            # Use inactivity_timeout for detecting stalled output (default 30s if not set)
            inactivity_timeout = getattr(self, 'inactivity_timeout', 30)

            while time.time() - start_time < max_timeout:
                attempts += 1
                current_time = time.time()

                # Capture the pane content
                capture_cmd = ["tmux", "capture-pane", "-p", "-t", target]
                result = subprocess.run(
                    capture_cmd,
                    check=True,
                    capture_output=True,
                    text=True
                )
                pane_content = result.stdout

                # Check if output has changed
                if pane_content != last_output:
                    last_output = pane_content
                    last_output_change_time = current_time

                # Log captured content periodically
                if current_time - last_log_time > 5:  # Log every 5 seconds for long-running commands
                    elapsed = int(current_time - start_time)
                    inactivity = int(current_time - last_output_change_time)
                    logger.debug(f"Still waiting after {elapsed}s (inactive: {inactivity}s), start={found_start}, end={found_end}, content sample: {pane_content[-200:] if pane_content else 'None'}")
                    last_log_time = current_time

                # Check for start marker
                if start_marker in pane_content:
                    if not found_start:
                        logger.debug(f"Found start marker after {int(time.time()-start_time)}s")
                    found_start = True

                # Check for end marker if start marker was found
                if found_start:
                    # Only match the end marker when it appears on its own line
                    lines = pane_content.splitlines()
                    for line in lines:
                        if line.strip() == end_marker:
                            logger.debug(f"Found end marker after {int(time.time()-start_time)}s")
                            found_end = True
                            return pane_content

                # Recovery strategy for potentially hanging commands
                elapsed_time = current_time - start_time
                inactivity_time = current_time - last_output_change_time

                # After 10 seconds without start marker, try recovery
                if not found_start and elapsed_time > 10 and recovery_attempts == 0:
                    logger.warning("Start marker not found after 10s, attempting recovery by sending Ctrl+C")
                    recovery_attempts += 1
                    ctrlc_cmd = ["tmux", "send-keys", "-t", target, "C-c"]
                    subprocess.run(ctrlc_cmd, check=True)
                    time.sleep(1)
                    # Retry the command
                    logger.debug("Retrying command after interruption")
                    subprocess.run(send_cmd, check=True)
                    continue

                # If start marker found but no end marker after inactivity timeout with no output change, try interrupting
                if found_start and not found_end and inactivity_time > inactivity_timeout and recovery_attempts == 0:
                    logger.warning(f"Command inactive for {inactivity_time:.1f}s without completion, attempting to interrupt...")
                    recovery_attempts += 1
                    # Send Ctrl+C to interrupt any running process
                    ctrlc_cmd = ["tmux", "send-keys", "-t", target, "C-c"]
                    subprocess.run(ctrlc_cmd, check=True)
                    # Return partial result
                    logger.warning("Returning partial result due to inactivity")
                    return pane_content

                # Gradually increase the check interval
                if attempts > 50:  # After ~10s, slow down polling
                    check_interval = 0.5

                # Wait before checking again
                time.sleep(check_interval)

            logger.warning(f"Command timeout after {int(time.time()-start_time)}s: start_found={found_start}, end_found={found_end}")
            return None

        except subprocess.SubprocessError as e:
            logger.exception(f"Error sending command: {str(e)}")
            return None
        except Exception as e:
            logger.exception(f"Unexpected error sending command: {type(e).__name__}: {str(e)}")
            return None

    def _clear_pane(self):
        """Clear the tmux pane to prepare for command output."""
        target = self._get_target()
        # Send Ctrl+L to clear the screen
        clear_cmd = ["tmux", "send-keys", "-t", target, "C-l"]
        subprocess.run(clear_cmd, check=True)
        time.sleep(0.1)  # Small delay to ensure screen clears

    # --- Convenience methods for migrations --- #

    def count_records(self, model: str) -> int:
        """
        Count records for a given Rails model.

        Args:
            model: Model name (e.g., "User", "Project")

        Returns:
            Number of records or -1 if error
        """
        result = self.execute(f"{model}.count")
        if result['status'] == 'success' and result['output'] is not None:
            return result['output']
        return -1

    def find_record(self, model: str, id_or_conditions: Union[int, Dict]) -> Optional[Dict]:
        """
        Find a record by ID or conditions.

        Args:
            model: Model name (e.g., "User", "Project")
            id_or_conditions: ID or conditions hash

        Returns:
            Record data or None if not found
        """
        if isinstance(id_or_conditions, int):
            command = f"{model}.find_by(id: {id_or_conditions})&.as_json"
        else:
            conditions = json.dumps(id_or_conditions).replace('"', '\'')
            command = f"{model}.find_by({conditions})&.as_json"

        result = self.execute(command)
        if result['status'] == 'success' and result['output']:
            try:
                # Handle the case where output is already parsed into Python types
                if isinstance(result['output'], dict):
                    return result['output']

                # Try to parse it as JSON if it's a string
                if isinstance(result['output'], str):
                    return json.loads(result['output'].replace("=>", ":").replace("nil", "null"))

                return None
            except (json.JSONDecodeError, TypeError):
                return None
        return None

    def create_record(self, model: str, attributes: Dict) -> Tuple[bool, Optional[Dict], Optional[str]]:
        """
        Create a record with given attributes.

        Args:
            model: Model name (e.g., "User", "Project")
            attributes: Record attributes

        Returns:
            Tuple of (success, record_data, error_message)
        """
        # Convert Python dict to Ruby hash format
        ruby_hash = json.dumps(attributes).replace('"', '\'')

        # Build command to create and return the record
        command = f"""
        record = {model}.new({ruby_hash})
        if record.save
          record.as_json
        else
          "ERROR: " + record.errors.full_messages.join(', ')
        end
        """

        result = self.execute(command)

        if result['status'] == 'success':
            output = result['output']

            # Check if the result indicates an error
            if isinstance(output, str) and output.startswith("ERROR:"):
                return False, None, output[6:].strip()  # Remove "ERROR: " prefix

            # Otherwise, parse the record data
            try:
                if isinstance(output, dict):
                    return True, output, None

                if isinstance(output, str):
                    record_data = json.loads(output.replace("=>", ":").replace("nil", "null"))
                    return True, record_data, None

                return True, None, None
            except (json.JSONDecodeError, TypeError):
                return False, None, "Failed to parse record data"

        return False, None, result.get('error', 'Unknown error')

    def update_record(self, model: str, id: int, attributes: Dict) -> Tuple[bool, Optional[str]]:
        """
        Update a record with given attributes.

        Args:
            model: Model name (e.g., "User", "Project")
            id: Record ID
            attributes: Record attributes to update

        Returns:
            Tuple of (success, error_message)
        """
        # Convert Python dict to Ruby hash format
        ruby_hash = json.dumps(attributes).replace('"', '\'')

        # Build command to update the record
        command = f"""
        record = {model}.find_by(id: {id})
        if record.nil?
          "ERROR: Record not found"
        elsif record.update({ruby_hash})
          "SUCCESS"
        else
          "ERROR: " + record.errors.full_messages.join(', ')
        end
        """

        result = self.execute(command)

        if result['status'] == 'success':
            output = result['output']

            if output == "SUCCESS":
                return True, None

            if isinstance(output, str) and output.startswith("ERROR:"):
                return False, output[6:].strip()  # Remove "ERROR: " prefix

            return True, None

        return False, result.get('error', 'Unknown error')

    def delete_record(self, model: str, id: int) -> Tuple[bool, Optional[str]]:
        """
        Delete a record.

        Args:
            model: Model name (e.g., "User", "Project")
            id: Record ID

        Returns:
            Tuple of (success, error_message)
        """
        command = f"""
        record = {model}.find_by(id: {id})
        if record.nil?
          "ERROR: Record not found"
        elsif record.destroy
          "SUCCESS"
        else
          "ERROR: " + record.errors.full_messages.join(', ')
        end
        """

        result = self.execute(command)

        if result['status'] == 'success':
            output = result['output']

            if output == "SUCCESS":
                return True, None

            if isinstance(output, str) and output.startswith("ERROR:"):
                return False, output[6:].strip()

            return True, None

        return False, result.get('error', 'Unknown error')

    def get_custom_field_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Find a custom field by name.

        Args:
            name: The name of the custom field to find

        Returns:
            The custom field ID or None if not found
        """
        # Use find_record method to get the custom field by name
        return self.find_record("CustomField", {"name": name})

    def get_custom_field_id_by_name(self, name: str) -> Optional[int]:
        """
        Find a custom field ID by name.

        Args:
            name: The name of the custom field to find

        Returns:
            The custom field ID or None if not found
        """
        command = f"CustomField.where(name: '{name}').first&.id"
        result = self.execute(command)

        if result['status'] == 'success' and result['output'] is not None:
            # Get the output and sanitize it if it's a string
            output = result['output']

            # Handle nil value from Ruby
            if output == "nil" or output is None:
                return None

            if isinstance(output, str):
                # Remove any Rails console output or whitespace
                # Extract just the numeric ID if present
                import re
                match = re.search(r'\b(\d+)\b', output)
                if match:
                    return int(match.group(1))
                # If no numeric ID found but it's not nil, log and return None
                if output.strip() != "nil":
                    logger.warning(f"Unexpected output format from Rails console: {output}")
                return None

            # If it's already a numeric type, return it directly
            if isinstance(output, (int, float)):
                return int(output)

            return None
        return None

    def execute_transaction(self, commands: List[str]) -> Dict[str, Any]:
        """
        Execute multiple commands in a transaction.

        Args:
            commands: List of Ruby/Rails commands to execute in transaction

        Returns:
            Dict with keys 'status', 'output', 'raw_output'
        """
        # Build transaction block
        transaction_commands = "\n".join(commands)
        transaction_block = f"""
        ActiveRecord::Base.transaction do
          {transaction_commands}
        end
        """

        return self.execute(transaction_block)

    @property
    def connected(self) -> bool:
        """
        Check if the client is connected to a valid tmux session.

        Returns:
            True if connected, False otherwise
        """
        return self._is_connected

    def _configure_irb_settings(self):
        """Configure IRB settings for better output and interaction."""
        # Disable color output and ensure large objects can be displayed
        config_cmd = """
        IRB.conf[:USE_COLORIZE] = false
        IRB.conf[:INSPECT_MODE] = :to_s
        puts "IRB configuration commands sent successfully"
        """

        # Send the command to the tmux session
        target = self._get_target()
        send_cmd = ["tmux", "send-keys", "-t", target, config_cmd, "Enter"]

        try:
            subprocess.run(send_cmd, check=True, capture_output=True)
            logger.debug("IRB configuration commands sent successfully")
        except subprocess.SubprocessError as e:
            logger.error(f"Failed to configure IRB settings: {str(e)}")
            raise

    # Utility methods for file transfer and script execution
    def transfer_file_to_container(self, local_path: str, remote_path: str) -> bool:
        """
        Transfer a file from the local machine to the OpenProject container.

        Args:
            local_path: Path to the file on the local machine
            remote_path: Path where the file should be placed in the container

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Get container and server info from config
            container_name = config.openproject_config.get("container")
            op_server = config.openproject_config.get("server")

            if not container_name or not op_server:
                logger.error("Missing container or server configuration")
                return False

            logger.info(f"Transferring file to {op_server} container {container_name}")

            # First, copy to server using SCP
            logger.info(f"Copying file to server {op_server}")
            scp_cmd = ["scp", local_path, f"{op_server}:/tmp/{os.path.basename(remote_path)}"]
            logger.debug(f"Running command: {' '.join(scp_cmd)}")

            subprocess.run(scp_cmd, check=True)

            # Then copy from server to container
            logger.info(f"Copying file from server to container {container_name}")
            docker_cp_cmd = ["ssh", op_server, "docker", "cp",
                          f"/tmp/{os.path.basename(remote_path)}",
                          f"{container_name}:{remote_path}"]
            logger.debug(f"Running command: {' '.join(docker_cp_cmd)}")

            subprocess.run(docker_cp_cmd, check=True)

            # Change permissions to make file readable by all
            logger.info(f"Setting file permissions to allow reading by all users")
            chmod_cmd = ["ssh", op_server, "docker", "exec", "-u", "root", container_name,
                       "chmod", "644", remote_path]
            logger.debug(f"Running command: {' '.join(chmod_cmd)}")

            subprocess.run(chmod_cmd, check=True)

            logger.success(f"Successfully copied file to container: {remote_path}")
            return True
        except subprocess.SubprocessError as e:
            logger.error(f"Error transferring file to container: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error during file transfer: {e}")
            return False

    def transfer_file_from_container(self, remote_path: str, local_path: str, retries: int = 3, delay: float = 1.0) -> bool:
        """
        Transfer a file from the container to the local machine, with retry logic if the file is not found.

        Args:
            remote_path: Path to file in container
            local_path: Path to save file locally
            retries: Number of times to retry if file is not found (default: 3)
            delay: Delay in seconds between retries (default: 1.0)

        Returns:
            True if successful, False otherwise
        """
        for attempt in range(retries):
            if attempt > 0:
                logger.warning(f"Retrying file transfer from container: attempt {attempt+1}/{retries}")
                # wait attempt * delay seconds
                time.sleep(delay * attempt)
            try:
                # Get container and server info from config
                container_name = config.openproject_config.get("container")
                op_server = config.openproject_config.get("server")

                if not container_name or not op_server:
                    logger.error("Missing container or server configuration")
                    return False

                # First check if the file exists in the container
                logger.debug(f"Checking if file exists in container: {remote_path}")
                check_cmd = ["ssh", op_server, "docker", "exec", container_name,
                           "bash", "-c", f"ls -la {remote_path} || echo 'File not found'"]

                result = subprocess.run(check_cmd, capture_output=True, text=True)
                output = result.stdout.strip()

                if "File not found" in output:
                    logger.error(f"File not found in container: {remote_path}")
                    if attempt < retries - 1:
                        continue
                    return False

                # Copy from container to server
                logger.debug(f"Copying file from container to server")
                docker_cp_cmd = ["ssh", op_server, "docker", "cp",
                              f"{container_name}:{remote_path}", "/tmp/"]

                subprocess.run(docker_cp_cmd, check=True)

                # Copy from server to local
                logger.debug(f"Copying file from server to local")
                scp_cmd = ["scp", f"{op_server}:/tmp/{os.path.basename(remote_path)}",
                         local_path]

                subprocess.run(scp_cmd, check=True)

                logger.debug(f"Successfully copied file from container to local: {local_path}")
                return True
            except subprocess.SubprocessError as e:
                logger.error(f"Error transferring file from container: {e}")
                if attempt < retries - 1:
                    continue
                return False
            except Exception as e:
                logger.error(f"Unexpected error during file transfer: {e}")
                if attempt < retries - 1:
                    continue
                return False
        return False

    def test_connection(self) -> bool:
        """
        Test if the Rails console connection is working.

        Returns:
            True if connection is successful, False otherwise
        """
        try:
            # Execute a simple Ruby command to verify connectivity
            result = self.execute("puts 'Rails console connection test'; true")
            return result.get('status') == 'success'
        except Exception as e:
            logger.error(f"Rails console connection test failed: {str(e)}")
            return False

    def read_json_from_container(self, remote_path: str) -> Optional[Any]:
        """
        Read a JSON file from the container.

        Args:
            remote_path: Path to JSON file in container

        Returns:
            Parsed JSON content or None if file doesn't exist or isn't valid JSON
        """
        try:
            # Get container and server info from config
            container_name = config.openproject_config.get("container")
            op_server = config.openproject_config.get("server")

            if not container_name or not op_server:
                logger.error("Missing container or server configuration")
                return None

            # Read the file content directly with cat
            logger.info(f"Reading JSON from container file: {remote_path}")
            cat_cmd = ["ssh", op_server, "docker", "exec", container_name,
                     "cat", remote_path]

            result = subprocess.run(cat_cmd, capture_output=True, text=True, check=True)
            content = result.stdout.strip()

            if not content:
                logger.error(f"Empty content from file: {remote_path}")
                return None

            # Parse the JSON content
            try:
                data = json.loads(content)
                logger.info(f"Successfully parsed JSON data from container file")
                return data
            except json.JSONDecodeError as e:
                logger.error(f"Error parsing JSON from container file: {e}")
                return None

        except subprocess.SubprocessError as e:
            logger.error(f"Error reading JSON from container: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error reading JSON from container: {e}")
            return None

    def execute_ruby_script(self, script_path: str, in_container: bool = True, output_as_json: bool = True) -> Dict[str, Any]:
        """
        Execute a Ruby script in the Rails console and optionally capture structured output.

        Args:
            script_path: Path to the Ruby script file
            in_container: Whether the path is inside the container or local
            output_as_json: Whether to expect and parse JSON output from the script

        Returns:
            Dictionary with execution results
        """
        try:
            # Read the content of the script file regardless of location
            if not in_container:
                # For local files, read the content directly
                try:
                    with open(script_path, 'r') as f:
                        script_content = f.read()
                except Exception as e:
                    logger.error(f"Error reading script file: {e}")
                    return {
                        'status': 'error',
                        'message': f'Failed to read script file: {str(e)}'
                    }
            else:
                # For container files, use docker exec to get content
                container_name = config.openproject_config.get("container")
                op_server = config.openproject_config.get("server")

                if not container_name or not op_server:
                    logger.error("Missing container or server configuration")
                    return {
                        'status': 'error',
                        'message': 'Missing container or server configuration'
                    }

                try:
                    cat_cmd = ["ssh", op_server, "docker", "exec", container_name, "cat", script_path]
                    result = subprocess.run(cat_cmd, capture_output=True, text=True, check=True)
                    script_content = result.stdout
                except subprocess.SubprocessError as e:
                    logger.error(f"Error reading script file from container: {e}")
                    return {
                        'status': 'error',
                        'message': f'Failed to read script from container: {str(e)}'
                    }

            # Now execute the script content directly
            logger.info(f"Executing Ruby script content directly in Rails console")

            # Format for JSON output if needed
            if output_as_json:
                # Header section with Python interpolation
                header = f"""
                begin
                  require 'json'
                  puts "JSON_OUTPUT_START"
                  # Execute the script content directly
                  result = (
                    {script_content}
                  )
                """

                # Main section without Python interpolation
                main_section = """
                  # Convert result to JSON if it's not already a string
                  if result.is_a?(String)
                    puts result
                  else
                    puts result.to_json
                  end
                  puts "JSON_OUTPUT_END"
                rescue => e
                  puts "ERROR: #{e.class.name}: #{e.message}"
                  puts e.backtrace.join("\\n")
                  puts "JSON_OUTPUT_START"
                  puts({ "error" => e.message, "backtrace" => e.backtrace }.to_json)
                  puts "JSON_OUTPUT_END"
                end
                """

                # Combine the sections
                command = header + main_section
            else:
                # Header section with Python interpolation
                header = f"""
                begin
                  # Execute the script content directly
                  {script_content}
                """

                # Main section without Python interpolation
                main_section = """
                rescue => e
                  puts "ERROR: #{e.class.name}: #{e.message}"
                  puts e.backtrace.join("\\n")
                end
                """

                # Combine the sections
                command = header + main_section

            # Execute the command in the Rails console
            result = self.execute(command)

            if result['status'] != 'success':
                return {
                    'status': 'error',
                    'message': result.get('error', 'Unknown error')
                }

            # Process JSON output if needed
            if output_as_json:
                output = result.get('output', '')
                start_marker = "JSON_OUTPUT_START"
                end_marker = "JSON_OUTPUT_END"

                start_idx = output.find(start_marker)
                end_idx = output.find(end_marker)

                if start_idx != -1 and end_idx != -1:
                    json_content = output[start_idx + len(start_marker):end_idx].strip()

                    try:
                        data = json.loads(json_content)
                        return {
                            'status': 'success',
                            'data': data
                        }
                    except json.JSONDecodeError as e:
                        return {
                            'status': 'error',
                            'message': f'Error parsing JSON output: {e}',
                            'content': json_content
                        }
                else:
                    return {
                        'status': 'error',
                        'message': 'JSON output markers not found in response',
                        'output': output
                    }
            else:
                return {
                    'status': 'success',
                    'output': result.get('output', '')
                }

        except Exception as e:
            logger.error(f"Error executing Ruby script: {e}")
            import traceback
            logger.debug(f"Traceback: {traceback.format_exc()}")
            return {
                'status': 'error',
                'message': str(e)
            }

    def execute_script_with_data(self, script_content: str, data: Any = None, output_as_json: bool = True) -> Dict[str, Any]:
        """
        Execute a Ruby script with provided data and capture the results.

        Args:
            script_content: Content of the Ruby script to execute
            data: Data to pass to the script (will be converted to JSON)
            output_as_json: Whether to expect and parse JSON output

        Returns:
            Dictionary with execution results
        """
        try:
            # Create a temporary script file
            import tempfile
            import os

            script_path = None
            data_path = None
            container_data_path = None

            # If data is provided, write it to a separate JSON file
            if data is not None:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as data_file:
                    # Ensure data is properly serializable
                    try:
                        json.dump(data, data_file, ensure_ascii=False, indent=2)
                        data_file.flush()  # Ensure all data is written to disk
                        data_path = data_file.name
                        logger.info(f"Created temporary JSON data file: {data_path}")

                        # Verify the JSON file is valid
                        with open(data_path, 'r') as verify_file:
                            json.load(verify_file)  # This will raise an exception if JSON is invalid
                            logger.debug("Verified JSON data is valid")
                    except Exception as e:
                        logger.error(f"Error creating JSON data file: {e}")
                        if os.path.exists(data_path):
                            os.unlink(data_path)
                        return {
                            'status': 'error',
                            'message': f'Failed to create valid JSON data: {str(e)}'
                        }

                    # Generate a timestamp-based path for the container
                    timestamp = int(time.time())
                    filename = os.path.basename(data_path)
                    container_data_path = f"/tmp/j2o_data_{timestamp}_{filename}"

                    # Transfer the JSON file to the container
                    logger.info(f"Transferring JSON data file to container at {container_data_path}")
                    if not self.transfer_file_to_container(data_path, container_data_path):
                        logger.error("Failed to transfer JSON data file to container")
                        return {
                            'status': 'error',
                            'message': 'Failed to transfer JSON data file to container'
                        }

                    # Verify the file exists in the container and has content
                    container_name = config.openproject_config.get("container")
                    op_server = config.openproject_config.get("server")
                    if container_name and op_server:
                        try:
                            verify_cmd = ["ssh", op_server, "docker", "exec", container_name,
                                        "bash", "-c", f"cat {container_data_path} | head -10"]
                            result = subprocess.run(verify_cmd, capture_output=True, text=True, check=True)
                            if not result.stdout.strip():
                                logger.error(f"Container file exists but appears to be empty: {container_data_path}")
                                return {
                                    'status': 'error',
                                    'message': 'Container data file is empty'
                                }
                            logger.debug(f"Verified container data file has content (first few lines): {result.stdout[:100]}...")
                        except Exception as e:
                            logger.error(f"Failed to verify container data file: {e}")
                            return {
                                'status': 'error',
                                'message': f'Failed to verify container data file: {str(e)}'
                            }

            # Create Ruby script with improved data loading and error handling
            with tempfile.NamedTemporaryFile(mode='w', suffix='.rb', delete=False) as f:
                # Header section with Python variable interpolation
                if data is not None:
                    # Python string interpolation for the path
                    data_file_path = container_data_path

                    # Write Ruby header with plain string instead of f-string to avoid interpolation issues
                    header = f"""
                    # Load data from separate JSON file
                    require "json"
                    begin
                      # Check if the file exists and has content
                      unless File.exist?("{data_file_path}")
                        raise "Data file not found at {data_file_path}"
                      end

                      file_content = File.read("{data_file_path}")
                      if file_content.nil? || file_content.empty?
                        raise "Data file is empty"
                      end

                      begin
                        input_data = JSON.parse(file_content)
                        puts "Successfully loaded data with " + (input_data.is_a?(Array) ? input_data.size.to_s : "1") + " records"
                      rescue JSON::ParserError => e
                        puts "JSON parse error: " + e.message
                        puts "First 100 chars of file: " + file_content[0..100].inspect
                        raise "Failed to parse JSON data: " + e.message
                      end

                    """
                    f.write(header)

                # Main section - just the script content without interpolation
                f.write(script_content)
                script_path = f.name

            # Execute the script
            logger.info(f"Executing Ruby script with data")
            result = self.execute_ruby_script(script_path, in_container=False, output_as_json=output_as_json)

            # Clean up the temporary files
            try:
                if script_path:
                    os.unlink(script_path)
                    logger.debug(f"Removed temporary script file: {script_path}")
                if data_path:
                    os.unlink(data_path)
                    logger.debug(f"Removed temporary JSON data file: {data_path}")
                if container_data_path:
                    # Clean up the remote file after execution
                    logger.debug(f"Attempting to remove container file: {container_data_path}")
                    container_name = config.openproject_config.get("container")
                    op_server = config.openproject_config.get("server")
                    if container_name and op_server:
                        try:
                            clean_cmd = ["ssh", op_server, "docker", "exec", container_name, "rm", "-f", container_data_path]
                            subprocess.run(clean_cmd, check=False, capture_output=True, timeout=10)
                            logger.debug(f"Container file cleanup attempted")
                        except Exception as e:
                            logger.debug(f"Error during container file cleanup: {e}")
            except Exception as e:
                logger.debug(f"Error cleaning up temporary files: {e}")

            return result

        except Exception as e:
            logger.error(f"Error executing script with data: {e}")
            import traceback
            logger.debug(f"Traceback: {traceback.format_exc()}")
            return {
                'status': 'error',
                'message': str(e)
            }

    def set_project_status(self, project_id: int, status_code: str) -> Dict[str, Any]:
        """
        Set the project status using Rails.

        Args:
            project_id: The ID of the project
            status_code: The status code to set ('on_track', 'off_track', 'at_risk', 'finished')

        Returns:
            Result dictionary
        """
        script = f"""
        begin
          project_id = {project_id}
          status_code = '{status_code}'

          if defined?(Status)  # Try Status model first (older OpenProject)
            begin
              status = Status.find_or_create_by(name: status_code.humanize)
              project = Project.find(project_id)
              if project.respond_to?(:status=)
                project.status = status
                project.save
                puts "Set project status using Status model"
                {{ status: 'success', message: "Status set to #{status_code}" }}
              else
                puts "Project does not respond to status="
                {{ status: 'warning', message: "Project does not support status" }}
              end
            rescue => e1
              puts "Status model error: #{{e1.message}}"

              # Try ProjectStatus instead
              if defined?(ProjectStatus)
                begin
                  ps = ProjectStatus.find_or_create_by(project_id: project_id)
                  ps.code = status_code
                  ps.save
                  puts "Set project status using ProjectStatus model"
                  {{ status: 'success', message: "Status set to #{status_code}" }}
                rescue => e2
                  puts "ProjectStatus error: #{{e2.message}}"
                  {{ status: 'error', message: e2.message }}
                end
              else
                puts "Neither Status nor ProjectStatus models available"
                {{ status: 'error', message: "Status models not available" }}
              end
            end
          elsif defined?(ProjectStatus)  # Try ProjectStatus model directly
            begin
              ps = ProjectStatus.find_or_create_by(project_id: project_id)
              ps.code = status_code
              ps.save
              puts "Set project status using ProjectStatus model"
              {{ status: 'success', message: "Status set to #{status_code}" }}
            rescue => e
              puts "ProjectStatus error: #{{e.message}}"
              {{ status: 'error', message: e.message }}
            end
          else
            puts "No status models available"
            {{ status: 'error', message: "Status models not available" }}
          end
        rescue => e
          puts "Error setting status: #{{e.message}}"
          {{ status: 'error', message: e.message }}
        end
        """
        return self.execute(script)
