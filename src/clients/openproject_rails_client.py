#!/usr/bin/env python3
"""
OpenProject Rails Client

A client that interacts with an OpenProject Rails console through tmux.
This allows for executing Rails commands in an existing tmux session
with an already running Rails console.
"""

import subprocess
import time
import re
import json
import logging
from typing import Dict, List, Any, Union, Optional, Tuple

logger = logging.getLogger(__name__)

class OpenProjectRailsClient:
    """Client for interacting with OpenProject Rails console via tmux."""

    def __init__(
            self,
            session_name: str = "rails_console",
            window: int = 0,
            pane: int = 0,
            marker_prefix: str = "RAILSCMD_",
            debug: bool = False
        ):
        """
        Initialize the Rails client.

        Args:
            session_name: tmux session name containing the Rails console (default: "rails_console")
            window: tmux window number (default: 0)
            pane: tmux pane number (default: 0)
            marker_prefix: prefix for output markers (default: "RAILSCMD_")
            debug: whether to enable debug logging (default: False)
        """
        self.session_name = session_name
        self.window = window
        self.pane = pane
        self.marker_prefix = marker_prefix
        self.debug = debug
        self.command_counter = 0
        self._is_connected = False

        # Validate that tmux is installed
        try:
            subprocess.run(["tmux", "-V"], check=True, capture_output=True)
            self._is_connected = self._session_exists()
        except (subprocess.SubprocessError, FileNotFoundError):
            raise RuntimeError("tmux is not installed or not available in PATH")

        # Check if specified tmux session exists
        if not self._is_connected:
            raise ValueError(f"tmux session '{session_name}' does not exist")

        # Directly disable the pager during initialization to prevent hanging
        self._disable_pager_direct()

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

    def execute(self, command: str, timeout: int = 180) -> Dict[str, Any]:
        """
        Execute a Rails command in the tmux session and capture the output.

        Args:
            command: The Ruby/Rails command to execute
            timeout: Maximum time to wait for command to complete in seconds (default 180s)

        Returns:
            Dict with keys 'status', 'output', 'raw_output'
        """
        if not command:
            return {
                'status': 'error',
                'error': 'Empty command',
                'output': None,
                'raw_output': None
            }

        try:
            # Create unique markers for this command execution
            cmd_id = f"{self.marker_prefix}{int(time.time())}_{self.command_counter}"
            start_marker = f"{cmd_id}_START"
            end_marker = f"{cmd_id}_END"
            self.command_counter += 1

            logger.debug(f"Executing command with markers: start='{start_marker}', end='{end_marker}'")

            # Format the command with markers for clear output identification
            # Use explicit nil return to prevent additional output
            wrapped_command = f"""
            begin
              puts "{start_marker}"
              result = nil
              begin
                # Log before execution for potentially long-running commands
                puts "Starting command execution..."
                result = {command}
                puts "Command execution completed"
              rescue => e
                puts "ERROR: #{{e.class.name}}: #{{e.message}}"
                result = nil
              end
              puts result.inspect
              puts "{end_marker}"
              nil
            end
            """

            # Clear any existing output in the pane before executing
            self._clear_pane()

            # Send the command to the tmux session
            target = self._get_target()
            wrapped_command = wrapped_command.replace("'", "\"")  # Replace single quotes with double quotes for tmux

            logger.debug(f"Sending command to tmux (wrapped): {wrapped_command[:100]}...")

            send_cmd = ["tmux", "send-keys", "-t", target, wrapped_command, "Enter"]
            subprocess.run(send_cmd, check=True)

            # Log that we're waiting for completion
            logger.debug(f"Waiting for markers: start='{start_marker}', end='{end_marker}'")

            # Wait for the command to complete (look for end marker in output)
            output = ""
            start_time = time.time()
            found_start = False
            found_end = False

            # Check more frequently at the beginning
            check_interval = 0.2
            attempts = 0
            last_log_time = start_time

            # For debugging long-running commands
            recovery_attempts = 0

            while time.time() - start_time < timeout:
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

                # Log captured content periodically
                if current_time - last_log_time > 5:  # Log every 5 seconds for long-running commands
                    elapsed = int(current_time - start_time)
                    logger.debug(f"Still waiting after {elapsed}s, start={found_start}, end={found_end}, content sample: {pane_content[-200:] if pane_content else 'None'}")
                    last_log_time = current_time

                # Check for start marker
                if start_marker in pane_content:
                    if not found_start:
                        logger.debug(f"Found start marker after {int(time.time()-start_time)}s")
                    found_start = True

                # Check for end marker if start marker was found
                if found_start and end_marker in pane_content:
                    logger.debug(f"Found end marker after {int(time.time()-start_time)}s")
                    found_end = True
                    output = pane_content
                    break

                # Recovery strategy for potentially hanging commands
                elapsed_time = current_time - start_time

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

                # If start marker found but no end marker after 30 seconds, try interrupting
                if found_start and not found_end and elapsed_time > 30 and recovery_attempts == 0:
                    logger.warning("Command running for 30s without completion, attempting to interrupt...")
                    recovery_attempts += 1
                    # Send Ctrl+C to interrupt any running process
                    ctrlc_cmd = ["tmux", "send-keys", "-t", target, "C-c"]
                    subprocess.run(ctrlc_cmd, check=True)

                    # Return partial result
                    logger.warning("Returning partial result due to long-running command")
                    return {
                        'status': 'error',
                        'error': 'Command execution interrupted after 30s',
                        'output': None,
                        'raw_output': pane_content
                    }

                # After a while, press Enter to try to recover if stuck
                if not found_end and attempts % 50 == 0:  # Periodically try to recover
                    logger.debug(f"Sending Enter to try to recover prompt (attempt #{attempts})")
                    enter_cmd = ["tmux", "send-keys", "-t", target, "Enter"]
                    subprocess.run(enter_cmd, check=True)

                # Gradually increase the check interval
                if attempts > 50:  # After ~10s, slow down polling
                    check_interval = 0.5

                # Wait before checking again
                time.sleep(check_interval)

            if not found_start or not found_end:
                logger.warning(f"Command timeout after {int(time.time()-start_time)}s: start_found={found_start}, end_found={found_end}")
                return {
                    'status': 'error',
                    'error': 'Command execution timeout',
                    'output': None,
                    'raw_output': pane_content if 'pane_content' in locals() else None
                }

            # Extract the result from between the markers
            result_value = "No output captured"
            raw_output = output

            # Get text between start and end markers
            if start_marker in output and end_marker in output:
                start_idx = output.find(start_marker) + len(start_marker)
                end_idx = output.find(end_marker)
                if start_idx < end_idx:
                    extracted_output = output[start_idx:end_idx].strip()
                    lines = [line.strip() for line in extracted_output.splitlines() if line.strip()]
                    if lines:
                        # The last line before the end marker should be the result.inspect output
                        result_value = lines[-1]

                        # If result is an error message, return an error status
                        if any(line.startswith("ERROR:") for line in lines):
                            error_line = next(line for line in lines if line.startswith("ERROR:"))
                            return {
                                'status': 'error',
                                'error': error_line[6:].strip(),  # Remove "ERROR: " prefix
                                'output': None,
                                'raw_output': raw_output
                            }

            logger.debug(f"Command completed successfully in {int(time.time()-start_time)}s")

            # Handle Ruby nil value
            if result_value == "nil":
                result_value = None

            # Handle Ruby string values (remove quotes)
            elif result_value.startswith('"') and result_value.endswith('"'):
                result_value = result_value[1:-1]

            # Handle Ruby numeric values - with improved parsing to extract numeric values from console output
            elif re.search(r'\b\d+\b', result_value):
                # Try to find and extract just the numeric part
                match = re.search(r'\b(\d+)\b', result_value)
                if match:
                    result_value = int(match.group(1))
                elif result_value.isdigit():  # Fallback for simple numeric strings
                    result_value = int(result_value)

            # Handle Ruby float values with improved parsing
            elif re.search(r'\b\d+\.\d+\b', result_value):
                match = re.search(r'\b(\d+\.\d+)\b', result_value)
                if match:
                    result_value = float(match.group(1))
                # Old pattern as fallback
                elif re.match(r'^-?\d+\.\d+$', result_value):
                    result_value = float(result_value)

            # Handle Ruby true/false values
            elif result_value == "true":
                result_value = True
            elif result_value == "false":
                result_value = False

            return {
                'status': 'success',
                'output': result_value,
                'raw_output': raw_output
            }

        except subprocess.SubprocessError as e:
            logger.exception("Error executing command")
            return {
                'status': 'error',
                'error': str(e),
                'output': None,
                'raw_output': None
            }
        except Exception as e:
            logger.exception("Unexpected error executing command")
            return {
                'status': 'error',
                'error': f"{type(e).__name__}: {str(e)}",
                'output': None,
                'raw_output': None
            }

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
            if isinstance(output, str):
                # Remove any Rails console output or whitespace
                # Extract just the numeric ID if present
                import re
                match = re.search(r'\b(\d+)\b', output)
                if match:
                    return int(match.group(1))
                # If no numeric ID found, just return the string itself
                return output
            return output
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

    def _disable_pager_direct(self):
        """Configure IRB settings for better machine parsing directly using tmux send-keys."""
        if self.debug:
            logger.debug("Configuring IRB for machine interaction...")

        try:
            # Use direct tmux command to configure IRB settings
            target = self._get_target()
            config_commands = [
                "require 'irb'",                    # Ensure IRB module is loaded
                "IRB.conf[:USE_PAGER] = false",     # Disable pager
                "IRB.conf[:USE_COLORIZE] = false",  # Disable color
                "IRB.conf[:INSPECT_MODE] = false",  # Simpler object inspection
                "IRB.conf[:PROMPT_MODE] = :SIMPLE", # Use simpler prompt
                "IRB.conf[:AUTO_INDENT] = false",   # Disable auto-indentation
                "IRB.conf[:SAVE_HISTORY] = nil",    # Disable history saving
                "IRB.conf[:USE_READLINE] = false",  # Disable readline
                "IRB.conf[:USE_TRACER] = false",    # Disable tracing
                "IRB.conf[:ECHO] = false",          # Disable echoing
                "IRB.conf[:VERBOSE] = false",       # Disable verbose output
                "IRB.conf[:ECHO_ON_ASSIGNMENT] = false", # Disable echo on assignment
                "IRB.conf[:TERM_LENGTH] = 130"      # Set larger terminal width to avoid wrapping
            ]

            # Send each configuration command
            for cmd in config_commands:
                disable_cmd = ["tmux", "send-keys", "-t", target, cmd, "Enter"]
                subprocess.run(disable_cmd, check=True)
                # Small delay between commands
                time.sleep(0.2)

            # Give it a moment to take effect
            time.sleep(0.5)

            logger.debug("IRB configuration commands sent successfully")
        except Exception as e:
            logger.warning(f"Failed to configure IRB settings: {str(e)}")

# --- Example usage --- #

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)

    # Usage examples
    client = OpenProjectRailsClient(session_name="rails_console", debug=True)

    # Count users
    user_count = client.count_records("User")
    print(f"User count: {user_count}")

    # Get a project
    project = client.find_record("Project", 1)
    if project:
        print(f"Found project: {project.get('name')}")

    # Execute a custom query
    result = client.execute("Project.all.pluck(:name).take(3)")
    if result['status'] == 'success':
        print(f"Project names: {result['output']}")
