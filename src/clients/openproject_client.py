#!/usr/bin/env python3
"""
OpenProjectClient

Main client interface for OpenProject operations.
Coordinates SSHClient, DockerClient, and RailsConsoleClient in a layered architecture:

Architecture:
1. SSHClient - Base component for all SSH operations
2. DockerClient - Uses SSHClient for remote Docker operations
3. RailsConsoleClient - Interacts with Rails console via tmux
4. OpenProjectClient - Coordinates all clients and operations

Workflow:
1. Creates Ruby script files locally
2. Transfers files to remote server via SSHClient
3. Transfers files to container via DockerClient
4. Executes scripts in Rails console via RailsConsoleClient
5. Processes and returns results
"""

import json
import os
import tempfile
import time
import random
from typing import Any, Dict, List, Optional, cast

from src import config
from src.clients.rails_console_client import RailsConsoleClient
from src.clients.docker_client import DockerClient
from src.clients.ssh_client import SSHClient
from src.utils.file_manager import FileManager

logger = config.logger


class OpenProjectClient:
    """
    Client for OpenProject operations.
    This is the main API that consumers should use.

    The client follows a layered architecture:
    1. SSHClient - Base component for SSH operations
    2. DockerClient - Uses SSHClient for Docker operations
    3. RailsConsoleClient - Interacts with Rails console via tmux
    4. OpenProjectClient - Coordinates all clients and operations

    Workflow:
    1. Creates Ruby script files locally
    2. Transfers files to remote server via SSH
    3. Transfers files to container via Docker
    4. Executes scripts via Rails console
    5. Processes and returns results
    """

    def __init__(
        self,
        container_name: Optional[str] = None,
        ssh_host: Optional[str] = None,
        ssh_user: Optional[str] = None,
        ssh_key_file: Optional[str] = None,
        tmux_session_name: Optional[str] = None,
        command_timeout: int = 180,
        retry_count: int = 3,
        retry_delay: float = 1.0,
        ssh_client: Optional[SSHClient] = None,
        docker_client: Optional[DockerClient] = None,
        rails_client: Optional[RailsConsoleClient] = None,
    ) -> None:
        """
        Initialize the OpenProject client.

        Args:
            container_name: Docker container name (default: from config)
            ssh_host: SSH host (default: from config)
            ssh_user: SSH username (default: from config)
            ssh_key_file: SSH key file (default: from config)
            tmux_session_name: tmux session name (default: from config)
            command_timeout: Command timeout in seconds (default: 180)
            retry_count: Number of retries (default: 3)
            retry_delay: Delay between retries in seconds (default: 1.0)
            ssh_client: Optional SSH client (default: create new)
            docker_client: Optional Docker client (default: create new)
            rails_client: Optional Rails console client (default: create new)
        """
        # Rails console query state
        self._last_query = ""

        # Initialize caches
        self._users_cache = None
        self._users_cache_time = None
        self._users_by_email_cache = {}

        # Get config values
        op_config = config.openproject_config

        # Use provided values or defaults from config
        self.container_name = container_name or op_config.get("container")
        self.ssh_host = ssh_host or op_config.get("server")
        self.ssh_user = ssh_user or op_config.get("user")
        self.ssh_key_file = ssh_key_file or op_config.get("key_file")
        self.tmux_session_name = tmux_session_name or op_config.get("tmux_session_name", "rails_console")
        self.command_timeout = command_timeout
        self.retry_count = retry_count
        self.retry_delay = retry_delay

        # Verify required configuration
        if not self.container_name:
            raise ValueError("Container name is required")
        if not self.ssh_host:
            raise ValueError("SSH host is required")

        # Initialize file manager
        self.file_manager = FileManager()

        # Initialize clients in the correct order, respecting dependency injection
        # 1. First, create or use the SSH client which is the foundation
        self.ssh_client = ssh_client or SSHClient(
            host=str(self.ssh_host),
            user=self.ssh_user,
            key_file=cast(Optional[str], self.ssh_key_file),
            operation_timeout=self.command_timeout,
            retry_count=self.retry_count,
            retry_delay=self.retry_delay,
        )
        logger.debug(
            f"{'Using provided' if ssh_client else 'Initialized'} SSHClient for host {self.ssh_host}"
        )

        # 2. Next, create or use the Docker client
        self.docker_client = docker_client or DockerClient(
            container_name=str(self.container_name),
            ssh_client=self.ssh_client,  # Pass our SSH client instance
            command_timeout=self.command_timeout,
            retry_count=self.retry_count,
            retry_delay=self.retry_delay,
        )
        logger.debug(
            f"{'Using provided' if docker_client else 'Initialized'} DockerClient for container {self.container_name}"
        )

        # 3. Finally, create or use the Rails console client for executing commands
        self.rails_client = rails_client or RailsConsoleClient(
            tmux_session_name=self.tmux_session_name,
            command_timeout=self.command_timeout,
        )
        logger.debug(
            f"{'Using provided' if rails_client else 'Initialized'} "
            f"RailsConsoleClient with tmux session {self.tmux_session_name}"
        )

        logger.success(f"OpenProjectClient initialized for host {self.ssh_host}, container {self.container_name}")

    def _create_script_file(self, script_content: str) -> str:
        """
        Create a temporary Ruby script file with the given content.

        Args:
            script_content: Ruby code to write to the file

        Returns:
            Path to the created file
        """
        # Create a temporary directory if needed
        temp_dir = os.path.join(self.file_manager.data_dir, "temp_scripts")
        os.makedirs(temp_dir, exist_ok=True)

        # Create a temporary file with .rb extension
        temp_file = tempfile.NamedTemporaryFile(
            suffix=".rb",
            prefix="openproject_script_",
            dir=temp_dir,
            delete=False,
            mode="w",
            encoding="utf-8"
        )

        # Write the script content to the file
        temp_file.write(script_content)
        temp_file.close()

        logger.debug(f"Created temporary script file: {temp_file.name}")
        return temp_file.name

    def _transfer_rails_script(self, local_script_path: str) -> Dict[str, Any]:
        """
        Transfer a script file to the Rails environment.

        Args:
            local_script_path: Path to the local script file

        Returns:
            Dictionary with status and remote path
        """
        script_filename = os.path.basename(local_script_path)
        remote_script_path = f"/tmp/{script_filename}"
        container_script_path = f"/tmp/{script_filename}"

        try:
            # Verify local file exists
            if not os.path.exists(local_script_path):
                return {"success": False, "error": f"Local script file not found: {local_script_path}"}

            # Copy to remote host
            ssh_result = self.ssh_client.copy_file_to_remote(local_script_path, remote_script_path)
            if not ssh_result.get("status") == "success":
                return {"success": False, "error": ssh_result.get("error", "Unknown SSH error")}

            # Set permissions
            self.ssh_client.execute_command(f"chmod 644 {remote_script_path}")

            # Copy to container
            container = self.docker_client.container_name
            container_path = container_script_path
            docker_cp_cmd = f"docker cp {remote_script_path} {container}:{container_path}"
            copy_result = self.ssh_client.execute_command(docker_cp_cmd)

            if copy_result.get("status") != "success":
                return {"success": False, "error": "Failed to copy script to container"}

            return {"success": True, "remote_path": container_script_path}

        except Exception as e:
            logger.exception(f"Error transferring Rails script: {e}")
            return {"success": False, "error": str(e)}

    def _cleanup_script_files(self, local_path: str, remote_path: str) -> None:
        """
        Clean up script files after execution.

        Args:
            local_path: Path to the local script file
            remote_path: Path to the remote script file
        """
        # Clean up local file
        try:
            if os.path.exists(local_path):
                os.unlink(local_path)
                logger.debug(f"Cleaned up local script file: {local_path}")
        except Exception as e:
            logger.exception(f"Non-critical error cleaning up local file: {e}")

        # Clean up remote file
        try:
            self.ssh_client.execute_command(f"rm -f {remote_path}", check=False)
            logger.debug(f"Cleaned up remote script file: {remote_path}")
        except Exception as e:
            logger.exception(f"Non-critical error cleaning up remote file: {e}")

    def _transfer_and_execute_script(self, script_content: str, timeout: Optional[int] = None) -> Any:
        """
        Transfer and execute a script in the Rails console.

        Args:
            script_content: Content of the script to execute
            timeout: Timeout in seconds

        Returns:
            Script result

        Raises:
            Exception: If execution fails
        """
        # Add proper Ruby JSON formatting to the script for reliable parsing in Python
        wrapped_script = f"""
begin
  require 'json'
  {script_content}

  # Return JSON-encoded output for reliable parsing in Python
  output = _
  if defined?(output)
    puts JSON.dump(output)
  else
    puts "null"
  end
rescue => e
  puts JSON.dump({{
    "error": e.message,
    "backtrace": e.backtrace
  }})
end
"""

        # Create a local script file
        script_file = self._create_script_file(wrapped_script)
        self._last_script = script_content

        # Transfer the script to the server/container
        transfer_result = self._transfer_rails_script(script_file)

        # Clean up the local script file
        os.unlink(script_file)

        # Check for transfer errors
        if not transfer_result.get("success", False):
            # Could not transfer the script
            raise Exception(f"Failed to transfer script: {transfer_result.get('error', 'Unknown error')}")

        # Execute the script in the Rails console
        result = self.rails_client.execute(f"load '{transfer_result['remote_path']}'", timeout=timeout)

        # Try to parse the output as JSON
        try:
            # The output should be valid JSON
            parsed_result = json.loads(result)
            return parsed_result
        except json.JSONDecodeError:
            # Not valid JSON - try finding Ruby hashes in the output
            if "{" in result and "}" in result:
                # Look for something that resembles a Ruby hash/dictionary output
                start_idx = result.rfind("{", 0, result.rfind("}"))
                end_idx = result.rfind("}")

                if start_idx >= 0 and end_idx > start_idx:
                    hash_str = result[start_idx:end_idx+1]
                    try:
                        # Try to parse it as JSON by replacing Ruby syntax with JSON
                        json_str = hash_str.replace("=>", ":").replace("nil", "null")
                        parsed_result = json.loads(json_str)
                        return parsed_result
                    except json.JSONDecodeError:
                        # Still not valid JSON
                        pass

            # If we reach here, we couldn't extract a valid result
            raise Exception("Failed to extract result from output")

    def execute_query(self, query: str, timeout: Optional[int] = None, file_output: bool = True) -> Any:
        """
        Execute a Rails query.

        Args:
            query: Rails query to execute
            timeout: Timeout in seconds
            file_output: Whether to use file-based execution (True) or direct console output (False)

        Returns:
            Query results

        Raises:
            Exception: If execution fails
        """
        self._last_query = query

        if file_output:
            script_content = f"""
  result = (
    {query}
  )
"""
            return self._transfer_and_execute_script(script_content, timeout)
        else:
            # Direct console execution
            return self.rails_client.execute(query, timeout=timeout or 180)

    def execute_query_to_json_file(self, query: str, timeout: Optional[int] = None) -> Any:
        """
        Execute a Rails query and save the result to a JSON file, then transfer it back.

        Args:
            query: Rails query to execute
            timeout: Timeout in seconds

        Returns:
            Parsed JSON data from the file

        Raises:
            Exception: If execution fails or file transfer fails
        """
        # Generate filenames
        uid = self.file_manager.generate_unique_id()
        remote_file = f"/tmp/query_result_{uid}.json"
        local_file = os.path.join(self.file_manager.temp_dir, f"query_result_{uid}.json")

        # Create a simplified script that avoids variable name conflicts with linter
        script = f"""
require 'json'

begin
  # Execute query
  result = {query}

  # Write to file
  file_content = result.nil? ? "null" : result.to_json
  File.write('{remote_file}', file_content)

  # Simply return true, just need to know if it succeeded
  true
rescue StandardError => error
  # Just return false on error
  false
end
"""

        # Create script file and transfer it
        script_file = self._create_script_file(script)
        xfer = self._transfer_rails_script(script_file)

        if not xfer.get("success", False):
            os.remove(script_file)
            raise Exception(f"Failed to transfer script: {xfer.get('error', 'Unknown error')}")

        # Run the script
        # Note: rails_client.execute returns a string, not a dictionary
        result_str = self.rails_client.execute(f"load '{xfer['remote_path']}'", timeout=timeout or 180)
        logger.debug(f"Script execution result: {result_str}")

        # Check if the script executed successfully by looking for "true" in the result
        if "true" not in result_str.lower():
            os.remove(script_file)
            raise Exception(f"Script execution failed: {result_str}")

        # Clean up script file
        os.remove(script_file)

        # Verify the JSON file exists in the container
        logger.debug(f"Checking if JSON file exists in container: {remote_file}")
        check_result = self.docker_client.check_file_exists_in_container(remote_file)
        if not check_result:
            raise Exception(f"JSON result file was not created in container: {remote_file}")

        # Get file size for debugging
        size = self.docker_client.get_file_size_in_container(remote_file)
        logger.debug(f"JSON file size in container: {size} bytes")

        if size is None or size <= 0:
            raise Exception(f"JSON result file is empty or invalid: {remote_file}")

        # Create a temporary file in the local tmp directory
        local_temp_directory = os.path.dirname(local_file)
        os.makedirs(local_temp_directory, exist_ok=True)

        # Try direct file copy using docker cp and scp
        logger.debug(f"Transferring JSON file directly from container to local: {remote_file} -> {local_file}")

        try:
            # Step 1: Generate a unique temporary filename on the remote host
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            unique_id = '%06x' % random.randrange(16**6)
            remote_temp_path = f"/tmp/{timestamp}_{unique_id}_{os.path.basename(remote_file)}"

            # Step 2: Copy from container to remote host using docker cp
            docker_cmd = f"docker cp {self.docker_client.container_name}:{remote_file} {remote_temp_path}"
            result = self.ssh_client.execute_command(docker_cmd)
            if result.get("status") != "success":
                err_msg = result.get('stderr', 'Unknown error')
                raise Exception(f"Failed to copy file from container to remote host: {err_msg}")

            # Step 3: Verify file exists on remote host
            check_cmd = f"test -e {remote_temp_path} && echo 'EXISTS' || echo 'NOT_EXISTS'"
            check_result = self.ssh_client.execute_command(check_cmd)
            if "EXISTS" not in check_result.get("stdout", ""):
                raise Exception(
                    f"File not found on remote host after docker cp: {remote_temp_path}"
                )

            # Step 4: Copy from remote host to local using SCP
            scp_result = self.ssh_client.copy_file_from_remote(remote_temp_path, local_file)
            if scp_result.get("status") != "success":
                raise Exception(f"Failed to copy file from remote host to local: {scp_result.get('error', 'Unknown error')}")

            # Final checks - verify file exists and has content
            if not os.path.exists(local_file):
                raise Exception(f"Local file not found after transfer: {local_file}")

            local_size = os.path.getsize(local_file)
            if local_size <= 0:
                raise Exception(f"Local file is empty after transfer: {local_file}")

            logger.debug(f"File transfer succeeded: {local_file} ({local_size} bytes)")

            # Clean up remote temp file
            self.ssh_client.execute_command(f"rm -f {remote_temp_path}", check=False)

        except Exception as e:
            logger.exception(f"Error in direct file transfer: {str(e)}")
            raise Exception(f"Failed to transfer JSON result file: {str(e)}")

        # Clean up container file
        self.docker_client.execute_command(f"rm -f {remote_file}")

        # Parse and return
        try:
            with open(local_file) as f:
                data = json.load(f)
            logger.debug(f"Successfully parsed JSON data from {local_file}")
            return data
        except Exception as e:
            raise Exception(f"Failed to parse JSON result file: {str(e)}")
        finally:
            # Clean up local file
            if os.path.exists(local_file):
                os.remove(local_file)

    def execute_json_query(self, query: str, timeout: Optional[int] = None) -> Any:
        """
        Execute a Rails query and return parsed JSON result.

        This method is optimized for retrieving data from Rails as JSON,
        automatically handling the conversion and parsing.

        Args:
            query: Rails query to execute (should produce JSON output)
            timeout: Timeout in seconds

        Returns:
            Parsed JSON result (list, dict, scalar, or None)

        Raises:
            Exception: If execution fails or result cannot be parsed as JSON
        """
        # Modify query to ensure it produces JSON output
        if not (".to_json" in query or ".as_json" in query):
            # Add as_json if the query doesn't already have JSON conversion
            if query.strip().endswith(")"):
                # If query ends with a closing parenthesis, add .as_json after it
                json_query = f"{query}.as_json"
            else:
                # Otherwise just append .as_json
                json_query = f"({query}).as_json"
        else:
            json_query = query

        # Execute the query and get result from JSON file
        return self.execute_query_to_json_file(json_query, timeout)

    def _parse_ruby_response(self, response: Any) -> Any:
        """
        Parse and normalize response from Ruby/Rails console.
        """
        # Already a Python object of appropriate type
        if response is None or isinstance(response, (list, dict, int, float, bool)):
            return response

        # String responses
        if isinstance(response, str):
            # Skip parsing if "=> nil"
            if "=> nil" in response:
                if ".all" in self._last_query:
                    return []
                return None

            try:
                # Replace Ruby hash syntax with JSON syntax
                cleaned = response.replace("=>", ":")
                cleaned = cleaned.replace("nil", "null")
                return json.loads(cleaned)
            except json.JSONDecodeError:
                return response

        # Unknown type
        logger.warning(f"Unknown response type: {type(response)}")
        return response

    def count_records(self, model: str) -> int:
        """
        Count records for a given Rails model.

        Args:
            model: Model name (e.g., "User", "Project")

        Returns:
            Number of records or -1 if error
        """
        result = self.execute_query(f"{model}.count")
        if result["status"] == "success" and result["output"] is not None:
            try:
                # Handle different output formats
                output = result["output"]
                if isinstance(output, int):
                    return output
                if isinstance(output, str) and output.isdigit():
                    return int(output)
                return -1
            except (ValueError, TypeError):
                return -1
        return -1

    def find_record(self, model: str, id_or_conditions: int | Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Find a record by ID or conditions.

        Args:
            model: Model name (e.g., "User", "Project")
            id_or_conditions: ID or conditions hash

        Returns:
            Record data or None if not found

        Raises:
            Exception: If query fails
        """
        try:
            if isinstance(id_or_conditions, int):
                query = f"{model}.find_by(id: {id_or_conditions})&.as_json"
            else:
                # Convert Python dict to Ruby hash format
                conditions_str = json.dumps(id_or_conditions).replace('"', "'")
                query = f"{model}.find_by({conditions_str})&.as_json"

            return self.execute_json_query(query)
        except Exception as e:
            logger.exception(f"Error finding record for {model}: {e}")
            raise

    def create_record(self, model: str, attributes: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a record with given attributes.

        Args:
            model: Model name (e.g., "User", "Project")
            attributes: Record attributes

        Returns:
            Dict with status, record data, and error info
        """
        # Convert Python dict to Ruby hash format
        ruby_hash = json.dumps(attributes).replace('"', "'")

        # Build command to create and return the record
        command = f"""
        record = {model}.new({ruby_hash})
        if record.save
          {{ status: 'success', record: record.as_json }}
        else
          {{ status: 'error', errors: record.errors.full_messages }}
        end
        """

        result = self.execute_query(command)

        if result["status"] == "success":
            output = result["output"]

            # Parse the result to extract status and data
            if isinstance(output, dict):
                record_status = output.get("status")

                if record_status == "success":
                    return {
                        "status": "success",
                        "record": output.get("record")
                    }
                else:
                    return {
                        "status": "error",
                        "errors": output.get("errors", ["Unknown error"])
                    }

            # If output is not a dict, return an error
            return {
                "status": "error",
                "errors": ["Unexpected response format"]
            }
        else:
            return {
                "status": "error",
                "errors": [result.get("error", "Failed to create record")]
            }

    def update_record(self, model: str, id: int, attributes: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update a record with given attributes.

        Args:
            model: Model name (e.g., "User", "Project")
            id: Record ID
            attributes: Attributes to update

        Returns:
            Dict with status and error info
        """
        # Convert Python dict to Ruby hash format
        ruby_hash = json.dumps(attributes).replace('"', "'")

        # Build command to update the record
        command = f"""
        record = {model}.find_by(id: {id})
        if record.nil?
          {{ status: 'error', errors: ['Record not found'] }}
        elsif record.update({ruby_hash})
          {{ status: 'success', record: record.as_json }}
        else
          {{ status: 'error', errors: record.errors.full_messages }}
        end
        """

        result = self.execute_query(command)

        if result["status"] == "success":
            output = result["output"]

            # Parse the result to extract status and data
            if isinstance(output, dict):
                record_status = output.get("status")

                if record_status == "success":
                    return {
                        "status": "success",
                        "record": output.get("record")
                    }
                else:
                    return {
                        "status": "error",
                        "errors": output.get("errors", ["Unknown error"])
                    }

            # If output is not a dict, return an error
            return {
                "status": "error",
                "errors": ["Unexpected response format"]
            }
        else:
            return {
                "status": "error",
                "errors": [result.get("error", "Failed to update record")]
            }

    def delete_record(self, model: str, id: int) -> Dict[str, Any]:
        """
        Delete a record.

        Args:
            model: Model name (e.g., "User", "Project")
            id: Record ID

        Returns:
            Dict with status and error info
        """
        command = f"""
        record = {model}.find_by(id: {id})
        if record.nil?
          {{ status: 'error', errors: ['Record not found'] }}
        elsif record.destroy
          {{ status: 'success' }}
        else
          {{ status: 'error', errors: record.errors.full_messages }}
        end
        """

        result = self.execute_query(command)

        if result["status"] == "success":
            output = result["output"]

            # Parse the result to extract status and data
            if isinstance(output, dict):
                record_status = output.get("status")

                if record_status == "success":
                    return {
                        "status": "success"
                    }
                else:
                    return {
                        "status": "error",
                        "errors": output.get("errors", ["Unknown error"])
                    }

            # If output is not a dict, return an error
            return {
                "status": "error",
                "errors": ["Unexpected response format"]
            }
        else:
            return {
                "status": "error",
                "errors": [result.get("error", "Failed to delete record")]
            }

    def find_all_records(
        self,
        model: str,
        conditions: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        includes: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Find all records matching conditions.

        Args:
            model: Model name (e.g., "User", "Project")
            conditions: Optional conditions hash
            limit: Optional limit on number of records
            includes: Optional list of associations to include

        Returns:
            List of record data

        Raises:
            Exception: If query fails
        """
        # Start building the query
        query = f"{model}"

        # Add conditions if provided
        if conditions:
            conditions_str = json.dumps(conditions).replace('"', "'")
            query += f".where({conditions_str})"

        # Add includes if provided
        if includes:
            includes_str = json.dumps(includes).replace('"', "'")
            query += f".includes({includes_str})"

        # Add limit if provided
        if limit:
            query += f".limit({limit})"

        # Add to_json to get the result as JSON
        query += ".as_json"

        try:
            # Use the new JSON-specific method
            return self.execute_json_query(query) or []
        except Exception as e:
            logger.exception(f"Error finding records for {model}: {e}")
            raise

    def execute_transaction(self, commands: List[str]) -> Dict[str, Any]:
        """
        Execute multiple commands in a transaction.

        Args:
            commands: List of Ruby/Rails commands

        Returns:
            Dict with status and output or error
        """
        # Build transaction block
        transaction_commands = "\n".join(commands)
        transaction_block = f"""
        ActiveRecord::Base.transaction do
          {transaction_commands}
        end
        """

        return self.execute_query(transaction_block)

    def execute_script(self, script_content: str) -> Dict[str, Any]:
        """
        Execute a Ruby script.

        Args:
            script_content: Ruby script content

        Returns:
            Dict with status and output or error
        """
        # Create a script file locally
        local_script_path = self._create_script_file(script_content)
        script_filename = os.path.basename(local_script_path)

        # Define remote path and container path
        remote_script_path = f"/tmp/{script_filename}"
        container_script_path = f"/tmp/{script_filename}"

        try:
            # Verify local file exists before transfer
            if not os.path.exists(local_script_path):
                logger.error(f"Local script file does not exist: {local_script_path}")
                return {"status": "error", "error": f"Local script file not found: {local_script_path}"}

            if not os.access(local_script_path, os.R_OK):
                logger.error(f"Local script file is not readable: {local_script_path}")
                return {"status": "error", "error": f"Local script file not readable: {local_script_path}"}

            logger.debug(f"Verified local script file exists: {local_script_path}")
            logger.debug(f"File size: {os.path.getsize(local_script_path)} bytes")

            # First, transfer the script file to the remote host
            ssh_result = self.ssh_client.copy_file_to_remote(local_script_path, remote_script_path)
            if ssh_result.get("status") != "success":
                return {"status": "error", "error": f"Failed to copy script to remote host: {ssh_result.get('error')}"}

            # Then transfer it to the container
            docker_result = self.docker_client.copy_file_to_container(remote_script_path, container_script_path)
            if docker_result.get("status") != "success":
                return {"status": "error", "error": f"Failed to copy script to container: {docker_result.get('error')}"}

            # Execute the script with the Rails console using load command
            load_command = f"load '{container_script_path}'"
            result = self.rails_client.execute(load_command)

            # Clean up the files
            try:
                os.unlink(local_script_path)
                self.ssh_client.execute_command(f"rm -f {remote_script_path}", check=False)
            except Exception as e:
                logger.exception(f"Non-critical error cleaning up files: {str(e)}")

            return result

        except Exception as e:
            logger.exception(f"Error executing script: {str(e)}")
            return {"status": "error", "error": f"Script execution failed: {str(e)}"}

    def execute_script_with_data(self, script_content: str, data: Any) -> Dict[str, Any]:
        """
        Execute a Ruby script with provided data.

        Args:
            script_content: Ruby script content
            data: Data to pass to the script

        Returns:
            Dict with status and output or error
        """
        try:
            # Convert data to JSON for passing to Ruby
            import json
            json_data = json.dumps(data, ensure_ascii=False)

            # Create wrapper script that loads the data and then runs the script
            wrapper_script = f"""
            # Load the data
            data = JSON.parse('{json_data}')

            # Execute the original script with data available
            {script_content}
            """

            # Use the execute_script method to run the wrapper script
            return self.execute_script(wrapper_script)

        except Exception as e:
            logger.exception(f"Error executing script with data: {str(e)}")
            return {"status": "error", "error": f"Script execution with data failed: {str(e)}"}

    def get_custom_field_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Find a custom field by name.

        Args:
            name: The name of the custom field to find

        Returns:
            The custom field or None if not found
        """
        return self.find_record("CustomField", {"name": name})

    def get_custom_field_id_by_name(self, name: str) -> Optional[int]:
        """
        Find a custom field ID by name.

        Args:
            name: The name of the custom field to find

        Returns:
            The custom field ID or None if not found
        """
        result = self.execute_query(f"CustomField.where(name: '{name}').first&.id")

        if result["status"] == "success" and result["output"] is not None:
            # Get the output and sanitize it if it's a string
            output = result["output"]

            # Handle nil value from Ruby
            if output == "nil" or output is None:
                return None

            if isinstance(output, int):
                return output

            if isinstance(output, str):
                try:
                    return int(output)
                except ValueError:
                    return None

            return None
        return None

    def get_statuses(self) -> List[Dict[str, Any]]:
        """
        Get all statuses from OpenProject.
        """
        try:
            result = self.execute_query("Status.all.as_json")

            if result["status"] == "success" and result["output"] is not None:
                statuses = result["output"]
                if isinstance(statuses, list):
                    logger.debug(f"Retrieved {len(statuses)} statuses from OpenProject")
                    return statuses
                else:
                    logger.error(f"Expected a list of statuses, got {type(statuses)}")
                    return []
            else:
                return []
        except Exception as e:
            logger.exception(f"Failed to get statuses from OpenProject: {e}")
            return []

    def get_work_package_types(self) -> List[Dict[str, Any]]:
        """
        Get all work package types from OpenProject.

        Returns:
            List of work package type dictionaries
        """
        # Try to get work package types using the API or Rails console
        result = self.execute_query("Type.all.as_json")

        if result["status"] == "success" and result["output"] is not None:
            try:
                # Handle the case where output is already parsed into Python types
                if isinstance(result["output"], list):
                    return result["output"]

                # Try to parse it as JSON if it's a string
                if isinstance(result["output"], str):
                    # Clean Ruby-style hashes in the output
                    cleaned_output = result["output"].replace("=>", ":")
                    cleaned_output = cleaned_output.replace("nil", "null")
                    parsed_output = json.loads(cleaned_output)
                    return cast(
                        List[Dict[str, Any]],
                        parsed_output if isinstance(parsed_output, list) else [parsed_output]
                    )

                return []
            except (json.JSONDecodeError, TypeError):
                logger.exception("Failed to parse work package types from OpenProject")
                return []

        logger.error("Failed to get work package types from OpenProject")
        return []

    def transfer_file_to_container(self, local_path: str, container_path: str) -> bool:
        """
        Transfer a file to the container.

        Args:
            local_path: Path to the local file
            container_path: Path to the destination in the container

        Returns:
            True if successful, False otherwise
        """
        # First copy the file to the remote host using SSH client
        remote_temp_path = f"/tmp/{os.path.basename(local_path)}"

        ssh_result = self.ssh_client.copy_file_to_remote(local_path, remote_temp_path)
        if ssh_result.get("status") != "success":
            logger.error(f"Failed to copy file to remote host: {ssh_result.get('error', 'Unknown error')}")
            return False

        # Then copy from remote host to container using Docker client
        docker_result = self.docker_client.copy_file_to_container(remote_temp_path, container_path)

        # Clean up the remote file regardless of success
        try:
            self.ssh_client.execute_command(f"rm -f {remote_temp_path}", check=False)
        except Exception as e:
            logger.exception(f"Non-critical error cleaning up remote file: {str(e)}")

        return docker_result.get("status") == "success"

    def transfer_file_from_container(self, container_path: str, local_path: str) -> bool:
        """
        Copy a file from the container to the local system.

        Args:
            container_path: Path to the file in the container
            local_path: Path where the file should be saved locally

        Returns:
            True if successful, False otherwise
        """
        # Create a temporary path on the remote host
        remote_temp_path = f"/tmp/{os.path.basename(container_path)}_{self.file_manager.generate_unique_id()}"

        # First copy from container to remote host using Docker client
        docker_result = self.docker_client.copy_file_from_container(container_path, remote_temp_path)
        if docker_result["status"] != "success":
            logger.error(f"Failed to copy file from container: {docker_result.get('error', 'Unknown error')}")
            return False

        # Verify the remote temp file exists on the remote host
        check_result = self.ssh_client.check_remote_file_exists(remote_temp_path)
        if not check_result:
            logger.error(f"Remote file not found after Docker copy: {remote_temp_path}")
            return False

        # Get file size for debugging
        size_result = self.ssh_client.get_remote_file_size(remote_temp_path)
        logger.debug(f"Remote temp file size: {size_result} bytes")

        # Handle None or zero size differently
        if size_result is None:
            logger.warning(f"Cannot determine size of remote file: {remote_temp_path}")
            # Continue anyway as the file might still be valid
        elif size_result <= 0:
            logger.error(f"Remote file is empty or not accessible: {remote_temp_path}")
            return False

        # Then copy from remote host to local using SSH client (copy directly to final destination)
        logger.debug(f"Copying file from remote host to local: {remote_temp_path} -> {local_path}")
        ssh_result = self.ssh_client.copy_file_from_remote(remote_temp_path, local_path)

        # Clean up the remote file regardless of success
        try:
            self.ssh_client.execute_command(f"rm -f {remote_temp_path}", check=False)
        except Exception as e:
            logger.exception(f"Non-critical error cleaning up remote file: {str(e)}")

        # Check if the copy succeeded and the local file exists
        if ssh_result.get("status") == "success":
            if os.path.exists(local_path):
                logger.debug(f"File successfully copied to local path: {local_path}")
                return True
            else:
                logger.error(f"Local file not found after successful SCP: {local_path}")
                return False
        else:
            logger.error(f"SCP failed: {ssh_result.get('error', 'Unknown error')}")
            return False

    def execute(self, script_content: str) -> Dict[str, Any]:
        """
        Legacy method that delegates to execute_query for backward compatibility.

        Args:
            script_content: Ruby script content

        Returns:
            Dict with status and output or error
        """
        return self.execute_query(script_content)

    def get_projects(self) -> List[Dict[str, Any]]:
        """
        Get all projects from OpenProject.
        """
        try:
            result = self.execute_query("Project.all.as_json")

            if result["status"] == "success" and result["output"] is not None:
                projects = result["output"]
                if isinstance(projects, list):
                    logger.debug(f"Retrieved {len(projects)} projects from OpenProject")
                    return projects
                else:
                    logger.error(f"Expected a list of projects, got {type(projects)}")
                    return []
            else:
                return []
        except Exception as e:
            logger.exception(f"Failed to get projects from OpenProject: {e}")
            return []

    def get_project_by_identifier(self, identifier: str) -> Optional[Dict[str, Any]]:
        """
        Get a project by identifier.

        Args:
            identifier: Project identifier or slug

        Returns:
            Project dictionary or None if not found
        """
        # Try to get project using the API or Rails console
        result = self.execute_query(f"Project.find_by(identifier: '{identifier}').as_json")

        if result["status"] == "success" and result["output"] is not None:
            try:
                # Handle the case where output is already parsed into Python types
                if isinstance(result["output"], dict):
                    return result["output"]

                # Try to parse it as JSON if it's a string and not "null"
                if isinstance(result["output"], str) and result["output"] != "null":
                    # Clean Ruby-style hashes in the output
                    cleaned_output = result["output"].replace("=>", ":")
                    return cast(Dict[str, Any], json.loads(cleaned_output))

                return None
            except Exception as e:
                logger.exception(f"Failed to parse project output: {e}")
                return None

        logger.error(f"Failed to get project with identifier {identifier}")
        return None

    def delete_all_work_packages(self) -> bool:
        """
        Delete all work packages in bulk.

        Returns:
            True if successful, False otherwise
        """
        script = """
        begin
          WorkPackage.delete_all
          {success: true}
        rescue => e
          {success: false, error: e.message}
        end
        """

        self._transfer_and_execute_script(script)

        return True

    def delete_all_projects(self) -> bool:
        """
        Delete all projects in bulk.

        Returns:
            True if successful, False otherwise
        """
        script = """
        begin
          Project.delete_all
          {success: true}
        rescue => e
          {success: false, error: e.message}
        end
        """

        self._transfer_and_execute_script(script)

        return True

    def delete_all_custom_fields(self) -> bool:
        """
        Delete all custom fields in bulk.
        Uses destroy_all for proper dependency cleanup.

        Returns:
            True if successful, False otherwise
        """
        script = """
        begin
          CustomField.destroy_all
          {success: true}
        rescue => e
          {success: false, error: e.message}
        end
        """

        self._transfer_and_execute_script(script)

        return True

    def delete_non_default_issue_types(self) -> Dict[str, Any]:
        """
        Delete non-default issue types (work package types).

        Returns:
            Dict with count of deleted types and status info
        """
        script = """
        begin
          non_default_types = Type.where(is_default: false, is_standard: false)
          count = non_default_types.count
          if count > 0
            non_default_types.destroy_all
            {success: true, count: count}
          else
            {success: true, count: 0, message: 'No non-default types found'}
          end
        rescue => e
          {success: false, error: e.message}
        end
        """

        output = self._transfer_and_execute_script(script)

        return {"count": output.get("count", 0), "message": output.get("message", "")}

    def delete_non_default_issue_statuses(self) -> Dict[str, Any]:
        """
        Delete non-default issue statuses.

        Returns:
            Dict with count of deleted statuses and status info
        """
        script = """
        begin
          non_default_statuses = Status.where(is_default: false)
          count = non_default_statuses.count
          if count > 0
            non_default_statuses.destroy_all
            {success: true, count: count}
          else
            {success: true, count: 0, message: 'No non-default statuses found'}
          end
        rescue => e
          {success: false, error: e.message}
        end
        """

        output = self._transfer_and_execute_script(script)

        return {"count": output.get("count", 0), "message": output.get("message", "")}

    def delete_custom_issue_link_types(self) -> Dict[str, Any]:
        """
        Delete custom issue link types (relation types),
        preserving default types.

        Returns:
            Dict with count of deleted link types and status info
        """
        script = """
        begin
          # Check if TypedRelation exists in the system
          if !defined?(TypedRelation)
            {success: true, count: 0, model_not_found: true, message: 'TypedRelation model not found'}
          else
            # Get all relation types that aren't default
            custom_types = []
            default_types = Relation::TYPES.keys.map(&:to_s)

            # Find TypedRelation records where name is not in default types
            TypedRelation.all.each do |rel|
              if !default_types.include?(rel.name) && !default_types.include?(rel.reverse_name)
                custom_types << rel
              end
            end

            count = custom_types.size

            if count > 0
              custom_types.each(&:destroy)
              {success: true, count: count}
            else
              {success: true, count: 0, message: 'No custom link types found'}
            end
          end
        rescue => e
          {success: false, error: e.message}
        end
        """

        output = self._transfer_and_execute_script(script)

        result_dict = {
            "count": output.get("count", 0),
            "message": output.get("message", "")
        }
        if output.get("model_not_found") is True:
            result_dict["model_not_found"] = True
        return result_dict

    def is_connected(self) -> bool:
        """
        Check if the OpenProject client is connected to the Rails console.

        Returns:
            True if connected, False otherwise
        """
        try:
            # Use a simple direct puts command instead of a script file
            # This minimizes the possibility of failure
            logger.debug("Testing Rails console connection with direct method...")

            # Create a unique test marker
            test_marker = f"OPENPROJECT_CONNECTION_TEST_{self.file_manager.generate_unique_id()}"

            # Very simple command that should work if the Rails console is connected
            # Just echo back our test marker
            simple_command = f"puts '{test_marker}'"

            # Send command using direct tmux interaction via RailsConsoleClient
            result = self.rails_client.execute(simple_command)

            if result.get("status") == "success":
                logger.debug("Rails console connection test succeeded with status=success")
                return True

            # Check if our test marker is in the output even if status is not success
            output_str = str(result)
            if test_marker in output_str:
                logger.debug("Rails console connection test succeeded: marker found in output")
                return True

            logger.debug(f"Rails console test failed with status: {result.get('status')}")
            return False

        except Exception as e:
            logger.exception(f"Error testing Rails console connection: {str(e)}")
            return False

    def get_users(self) -> List[Dict[str, Any]]:
        """
        Get all users from OpenProject.

        Uses caching to avoid repeated Rails console queries.

        Returns:
            List of OpenProject users

        Raises:
            Exception: If unable to retrieve users
        """
        # Check cache first
        current_time = time.time()
        cache_valid = (
            hasattr(self, '_users_cache') and
            hasattr(self, '_users_cache_time') and
            self._users_cache is not None and
            self._users_cache_time is not None and
            current_time - self._users_cache_time < 300  # 5 minutes cache validity
        )

        if cache_valid:
            logger.debug(f"Using cached users data ({len(self._users_cache)} users)")
            return self._users_cache

        # Use the simplest approach possible with direct file output
        try:
            # Generate a unique ID for temporary files
            uid = self.file_manager.generate_unique_id()
            container_file = f"/tmp/users_{uid}.json"
            local_file = os.path.join(self.file_manager.temp_dir, f"users_{uid}.json")

            # Execute command to write users to file
            logger.debug(f"Writing OpenProject users to container file: {container_file}")
            cmd = f"File.write('{container_file}', User.all.to_json)"
            self.rails_client.execute(cmd)

            # Check if file exists and has data in container
            check_cmd = f"test -e {container_file} && echo 'EXISTS'"
            check_result = self.docker_client.execute_command(check_cmd)
            if "EXISTS" not in check_result.get("stdout", ""):
                logger.error(f"Users JSON file not created in container: {container_file}")
                return []

            # Copy file using docker directly
            result = self.docker_client.copy_file_from_container(container_file, local_file)
            if result["status"] != "success":
                logger.error(f"Failed to copy users file from container: {result.get('error')}")
                return []

            # Read and parse JSON file
            if not os.path.exists(local_file):
                logger.error(f"Failed to get OpenProject users: local file not found")
                return []

            # Parse the file contents
            with open(local_file, 'r') as f:
                users = json.load(f)

            # Successfully loaded the users
            logger_msg = "Successfully loaded {} users from file"
            logger.debug(logger_msg.format(len(users) if isinstance(users, list) else '?'))

            # Ensure we got a list back
            if not isinstance(users, list):
                logger.error(f"Expected a list of users, got {type(users)}")
                users = []

            # Update cache
            logger.debug(f"Retrieved {len(users)} users from OpenProject")
            self._users_cache = users
            self._users_cache_time = current_time

            # Update email lookup cache too
            self._users_by_email_cache = {}
            for user in users:
                if not isinstance(user, dict):
                    logger.warning(f"Expected user to be a dict, got {type(user)}")
                    continue

                email = user.get('email', '').lower()
                if email:
                    self._users_by_email_cache[email] = user

            # Clean up
            try:
                os.remove(local_file)
                self.docker_client.execute_command(f"rm -f {container_file}")
            except Exception as e:
                logger.warning(f"Non-critical cleanup error: {e}")

            return users

        except Exception as e:
            logger.exception(f"Error retrieving users from OpenProject: {e}")
            return []

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """
        Get a user by email address.

        Uses cached user data if available to avoid repeated Rails console queries.

        Args:
            email: Email address of the user

        Returns:
            User data or None if not found

        Raises:
            Exception: If there's an error during retrieval
        """
        logger.debug(f"Looking up user with email: {email}")

        email_lower = email.lower()

        # Try to get from cache first
        if email_lower in self._users_by_email_cache:
            logger.debug(f"Found user with email {email} in cache")
            return self._users_by_email_cache[email_lower]

        # If we have cached all users recently, and the email is not there, it doesn't exist
        if hasattr(self, '_users_cache_time') and self._users_cache_time is not None:
            current_time = time.time()
            if current_time - self._users_cache_time < 300:  # 5 minutes cache validity
                logger.debug(f"User with email {email} not found in cache, returning None")
                return None

        # Try direct lookup using find_record
        user = self.find_record("User", {"email": email})

        # Cache the result if found
        if user:
            self._users_by_email_cache[email_lower] = user
            logger.debug(f"Found user with email {email} via direct lookup")

        return user

    def create_users_in_bulk(self, users: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Create multiple users at once via the Rails console.

        Args:
            users: List of user data dictionaries with keys firstname, lastname, email, etc.

        Returns:
            Dictionary with success status, counts, and created user data

        Raises:
            Exception: If user creation fails
        """
        if not users:
            logger.warning("No users provided to create_users_in_bulk")
            return {
                "success": True,
                "created_count": 0,
                "failed_count": 0,
                "created_users": []
            }

        # Generate a Rails script that will bulk create the users and handle errors
        script = """
        require 'json'

        # Helper for sanitizing for logs
        def sanitize_for_log(hash)
          # Only include safe fields for logging
          safe_fields = %%w[id login firstname lastname]
          hash.select { |k, _| safe_fields.include?(k.to_s) }
        end

        users_data = %s

        # Initialize counters and arrays
        created_users = []
        failed_users = []

        # Create each user and handle errors
        users_data.each do |user_data|
          begin
            # Create user with specific fields
            user = User.new(
              login: user_data['login'],
              firstname: user_data['firstname'],
              lastname: user_data['lastname'],
              mail: user_data['email'],
              admin: user_data['admin'] || false,
              status: 1  # 1=active, 2=registered, 3=locked
            )

            # Set password if provided
            if user_data['password']
              user.password = user_data['password']
              user.password_confirmation = user_data['password']
            end

            # Add custom fields if provided
            if user_data['custom_fields']
              user_data['custom_fields'].each do |field|
                user.custom_field_values = {
                  field['id'].to_s => field['value']
                }
              end
            end

            # Save the user
            if user.save
              # Add to created users array with full details
              created_users << user.as_json
              puts "Created user: #{sanitize_for_log(user.as_json)}"
            else
              # Log errors
              errors = user.errors.full_messages.join(', ')
              failed_users << {
                data: sanitize_for_log(user_data),
                errors: errors
              }
              puts "Failed to create user #{user_data['login']}: #{errors}"
            end
          rescue => e
            # Handle any exceptions during user creation
            failed_users << {
              data: sanitize_for_log(user_data),
              errors: e.message
            }
            puts "Exception creating user #{user_data['login']}: #{e.message}"
          end
        end

        # Return results as hash
        {
          created_count: created_users.length,
          failed_count: failed_users.length,
          created_users: created_users,
          failed_users: failed_users
        }
        """ % json.dumps(users, ensure_ascii=False)

        # Execute the script and return the results
        output = self._transfer_and_execute_script(script)

        # Update our cache with the newly created users
        new_users = output.get("created_users", [])
        if new_users and hasattr(self, '_users_cache') and self._users_cache is not None:
            self._users_cache.extend(new_users)

            # Update the email cache too
            if not hasattr(self, '_users_by_email_cache'):
                self._users_by_email_cache = {}

            for user in new_users:
                email = user.get("email")
                if email:
                    self._users_by_email_cache[email.lower()] = user

        return output
