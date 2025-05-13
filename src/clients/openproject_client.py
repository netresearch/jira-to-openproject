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
import time
import random
import re
from typing import Any, cast

from src import config
from src.clients.rails_console_client import (
    RailsConsoleClient,
    CommandExecutionError,
    RubyError
)
from src.clients.docker_client import DockerClient
from src.clients.ssh_client import (
    SSHClient,
    SSHConnectionError,
    SSHCommandError,
    SSHFileTransferError
)
from src.utils.file_manager import FileManager

logger = config.logger


class OpenProjectError(Exception):
    """Base exception for all OpenProject client errors."""
    pass


class ConnectionError(OpenProjectError):
    """Error when connection to OpenProject fails."""
    pass


class FileTransferError(OpenProjectError):
    """Error when transferring files to/from OpenProject container."""
    pass


class QueryExecutionError(OpenProjectError):
    """Error when executing a query in OpenProject."""
    pass


class RecordNotFoundError(OpenProjectError):
    """Error when a record is not found in OpenProject."""
    pass


class JsonParseError(OpenProjectError):
    """Error when parsing JSON output from OpenProject."""
    pass


class OpenProjectClient:
    """
    Client for OpenProject operations.
    This is the top-level coordinator that orchestrates the client architecture:
    - SSHClient handles all SSH interactions
    - DockerClient (using SSHClient) handles container interactions
    - RailsConsoleClient handles Rails console interactions

    All error handling uses exceptions rather than status dictionaries.
    """

    def __init__(
        self,
        container_name: str | None = None,
        ssh_host: str | None = None,
        ssh_user: str | None = None,
        ssh_key_file: str | None = None,
        tmux_session_name: str | None = None,
        command_timeout: int = 180,
        retry_count: int = 3,
        retry_delay: float = 1.0,
        ssh_client: SSHClient | None = None,
        docker_client: DockerClient | None = None,
        rails_client: RailsConsoleClient | None = None,
    ) -> None:
        """
        Initialize the OpenProject client with dependency injection.

        Args:
            container_name: Docker container name (default: from config)
            ssh_host: SSH host (default: from config)
            ssh_user: SSH username (default: from config)
            ssh_key_file: SSH key file (default: from config)
            tmux_session_name: tmux session name (default: from config)
            command_timeout: Command timeout in seconds (default: 180)
            retry_count: Number of retries (default: 3)
            retry_delay: Delay between retries in seconds (default: 1.0)
            ssh_client: Optional SSH client (dependency injection)
            docker_client: Optional Docker client (dependency injection)
            rails_client: Optional Rails console client (dependency injection)

        Raises:
            ValueError: If required configuration values are missing
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
            key_file=cast(str | None, self.ssh_key_file),
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

        Raises:
            OSError: If unable to create or write to the script file
        """
        try:
            # Create a temporary directory if needed
            temp_dir = os.path.join(self.file_manager.data_dir, "temp_scripts")
            os.makedirs(temp_dir, exist_ok=True)

            # Generate a unique filename
            filename = f"openproject_script_{os.urandom(4).hex()}.rb"
            file_path = os.path.join(temp_dir, filename)

            # Write the content directly instead of using tempfile module
            with open(file_path, mode="w", encoding="utf-8") as f:
                f.write(script_content)

            # Verify the file was created and is readable
            if not os.path.exists(file_path):
                raise OSError("Failed to create script file: File not found after creation")

            if not os.access(file_path, os.R_OK):
                raise OSError("Failed to create script file: File is not readable")

            # Log the absolute path for easier debugging
            logger.debug(f"Created temporary script file: {os.path.abspath(file_path)}")
            return file_path
        except OSError as e:
            logger.error(f"Failed to create script file: {str(e)}")
            raise OSError(f"Failed to create script file: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error creating script file: {str(e)}")
            raise OSError(f"Failed to create script file: {str(e)}")

    def _transfer_rails_script(self, local_path: str) -> str:
        """
        Transfer a script to the Rails environment.

        Args:
            local_path: Path to the script file

        Returns:
            Path to the script in the container

        Raises:
            FileTransferError: If transfer fails
        """
        try:
            # Verify the local file exists and is readable before attempting to transfer
            if not os.path.isfile(local_path):
                raise FileTransferError(f"Local file does not exist: {local_path}")

            if not os.access(local_path, os.R_OK):
                raise FileTransferError(f"Local file is not readable: {local_path}")

            # Get the absolute path for better error messages
            abs_path = os.path.abspath(local_path)
            logger.debug(f"Transferring script from: {abs_path}")

            # Use just the base filename for the container path
            container_path = f"/tmp/{os.path.basename(local_path)}"

            # Use Docker client to handle the entire transfer process
            # This should internally:
            # 1. Copy from local to remote (via SSH)
            # 2. Copy from remote to container
            # 3. Set permissions as root user in the container
            self.docker_client.transfer_file_to_container(abs_path, container_path)

            # Verify file exists and is readable in container
            if not self.docker_client.check_file_exists_in_container(container_path):
                raise FileTransferError(f"File not found in container after transfer: {container_path}")

            # Check if we can read the file content
            stdout, stderr, rc = self.docker_client.execute_command(f"head -1 {container_path}")
            if rc != 0:
                raise FileTransferError(f"File in container is not readable: {stderr}")

            logger.debug(f"Successfully transferred file to container at {container_path}")
            return container_path

        except Exception as e:
            raise FileTransferError(f"Failed to transfer script: {str(e)}")

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
            logger.warning(f"Non-critical error cleaning up local file: {str(e)}")

        # Clean up remote file
        try:
            self.ssh_client.execute_command(f"rm -f {remote_path}")
            logger.debug(f"Cleaned up remote script file: {remote_path}")
        except Exception as e:
            logger.warning(f"Non-critical error cleaning up remote file: {str(e)}")

    def _transfer_and_execute_script(self, script_content: str) -> dict[str, Any]:
        """
        Create, transfer and execute a script.

        Args:
            script_content: Script content to execute

        Returns:
            Execution result

        Raises:
            FileTransferError: If file transfer fails
            QueryExecutionError: If script execution fails
            JsonParseError: If result parsing fails
        """
        local_path = None
        container_path = None
        result = None  # Initialize result variable

        try:
            # Create a local script file
            local_path = self._create_script_file(script_content)
            logger.debug(f"Created script file at: {local_path}")

            # Transfer the script to the container
            container_path = self._transfer_rails_script(local_path)
            logger.debug(f"Transferred script to container: {container_path}")

            # We don't try to modify permissions, as we can't in this environment
            # Just check if the file exists
            file_exists = self.docker_client.check_file_exists_in_container(container_path)
            logger.debug(f"Container script file exists: {file_exists}")

            if not file_exists:
                raise FileTransferError(f"Script file not found in container after transfer: {container_path}")

            # Check permissions
            stdout, stderr, rc = self.docker_client.execute_command(f"ls -la {container_path}")
            logger.debug(f"File permissions in container: {stdout}")

            # Print contents for debugging
            stdout, stderr, rc = self.docker_client.execute_command(f"cat {container_path} | head -5")
            logger.debug(f"First few lines of script: {stdout}")

            # Execute the script in Rails console and get result
            rails_output = self.rails_client.execute(f'load "{container_path}"')
            logger.debug(f"Rails script execution output: {rails_output}")

            # Parse the result from JSON format
            try:
                result = json.loads(rails_output)
                logger.debug(f"Parsed result: {result}")
            except json.JSONDecodeError as e:
                # If JSON parsing fails, try to extract a hash from the Ruby output
                logger.warning(f"JSON parse error: {str(e)}")
                logger.warning(f"Attempting to extract hash from Rails output: {rails_output}")

                # Try to parse Ruby hash format into Python dict
                hash_match = re.search(r'\{([^{}]*)\}', rails_output)
                if hash_match:
                    hash_str = hash_match.group(0)
                    logger.debug(f"Found hash string: {hash_str}")

                    # Very simple Ruby hash to Python dict conversion
                    # This is not a general solution but works for our simple case
                    try:
                        # Replace Ruby symbols with strings
                        dict_str = re.sub(r':([a-zA-Z_]\w*)\s*=>', r'"\1":', hash_str)
                        # Replace => with :
                        dict_str = dict_str.replace("=>", ":")
                        # Try to parse resulting string as JSON
                        result = json.loads(dict_str)
                        logger.debug(f"Converted hash to dict: {result}")
                    except Exception as e:
                        logger.error(f"Failed to convert Ruby hash to dict: {str(e)}")
                        # Use the output as a string if we can't parse it
                        result = {"raw_output": rails_output}
                else:
                    # Fallback to raw output if no hash found
                    result = {"raw_output": rails_output}

            # Return the result
            return result if result is not None else {"raw_output": rails_output}
        except FileTransferError as e:
            raise e  # Re-raise FileTransferError
        except Exception as e:
            raise QueryExecutionError(f"Failed to execute script: {str(e)}")
        finally:
            # Clean up local script file
            if local_path and os.path.exists(local_path):
                try:
                    os.unlink(local_path)
                except Exception as e:
                    logger.warning(f"Failed to clean up local script file: {str(e)}")

            # Clean up container script file
            if container_path:
                try:
                    # Use root user to remove the file, to handle permission issues
                    self.docker_client.execute_command(f"rm -f {container_path}", user="root")
                except Exception as e:
                    logger.warning(f"Failed to clean up container script file: {str(e)}")

    def execute(self, script_content: str) -> dict[str, Any]:
        """
        Execute a Ruby script directly.

        Args:
            script_content: Ruby script content to execute

        Returns:
            Script execution result

        Raises:
            QueryExecutionError: If script execution fails
        """
        return self.execute_query(script_content)

    def transfer_file_to_container(self, local_path: str, container_path: str) -> bool:
        """
        Transfer a file from local to the OpenProject container.

        Args:
            local_path: Path to local file
            container_path: Destination path in container

        Returns:
            True if successful

        Raises:
            FileTransferError: If the transfer fails for any reason
        """
        try:
            # Use just the base filename for the remote path
            remote_filename = os.path.basename(local_path)
            remote_path = f"/tmp/{remote_filename}"

            # First copy to remote server's /tmp directory
            self.ssh_client.copy_file_to_remote(local_path, remote_path)

            # Then copy from remote server to container
            self.docker_client.copy_file_to_container(remote_path, container_path)

            # Clean up the temporary file on the remote server
            self.ssh_client.execute_command(f"rm -f {remote_path}")

            return True
        except Exception as e:
            error_msg = f"Failed to transfer file to container: {str(e)}"
            logger.error(error_msg)
            raise FileTransferError(error_msg)

    def is_connected(self) -> bool:
        """
        Test if connected to OpenProject.

        Returns:
            True if connected, False otherwise
        """
        try:
            # Generate a unique ID to verify connection
            unique_id = str(random.randint(10000, 99999))

            # Simple command to echo the ID back
            command = f'puts "OPENPROJECT_CONNECTION_TEST_{unique_id}"'

            # Execute the command
            result = self.rails_client.execute(command)

            # Check if the unique ID is in the response
            return f"OPENPROJECT_CONNECTION_TEST_{unique_id}" in result
        except Exception as e:
            logger.error(f"Connection test failed: {str(e)}")
            return False

    def execute_query(self, query: str, timeout: int | None = None) -> str:
        """
        Execute a Rails query.

        Args:
            query: Rails query to execute
            timeout: Timeout in seconds

        Returns:
            Query results

        Raises:
            QueryExecutionError: If execution fails
            JsonParseError: If result parsing fails
        """
        self._last_query = query

        result = self.rails_client._send_command_to_tmux(
            f"puts ({query})",
            5
        )
        
        return result

    def execute_query_to_json_file(self, query: str, timeout: int | None = None) -> Any:
        """
        Execute a Rails query and save the result to a JSON file, then transfer it back.

        Args:
            query: Rails query to execute
            timeout: Timeout in seconds

        Returns:
            Parsed JSON data from the file

        Raises:
            QueryExecutionError: If execution fails
            FileTransferError: If file transfer fails
            JsonParseError: If result parsing fails
        """
        # Generate filenames
        uid = self.file_manager.generate_unique_id()
        remote_file = f"/tmp/query_result_{uid}.json"
        local_file = os.path.join(self.file_manager.temp_dir, f"query_result_{uid}.json")

        # Create a simplified script that avoids variable name conflicts
        script = f"""
require 'json'

begin
  # Execute query
  result = {query}

  # Write to file
  file_content = result.nil? ? "null" : result.to_json
  File.write('{remote_file}', file_content)

  # Return true to indicate success
  true
rescue StandardError => error
  # Re-raise the error to trigger exception handling
  raise error
end
"""

        try:
            # Create and transfer script
            script_file = self._create_script_file(script)
            container_path = self._transfer_rails_script(script_file)

            # Run the script
            self.rails_client.execute(f"load '{container_path}'", timeout=timeout or self.command_timeout)

            # Clean up script file
            if os.path.exists(script_file):
                os.remove(script_file)

            # Verify the JSON file exists in the container
            if not self.docker_client.check_file_exists_in_container(remote_file):
                raise FileTransferError(f"JSON result file was not created in container: {remote_file}")

            # Get file size for debugging
            size = self.docker_client.get_file_size_in_container(remote_file)
            if size is None or size <= 0:
                raise FileTransferError(f"JSON result file is empty or invalid: {remote_file}")

            logger.debug(f"JSON file size in container: {size} bytes")

            # Create a temporary file in the local tmp directory
            local_temp_directory = os.path.dirname(local_file)
            os.makedirs(local_temp_directory, exist_ok=True)

            # Step 1: Generate a unique temporary filename on the remote host
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            unique_id = f"{random.randrange(16**6):06x}"
            remote_temp_path = f"/tmp/{timestamp}_{unique_id}_{os.path.basename(remote_file)}"

            # Step 2: Copy from container to remote host
            docker_cmd = f"docker cp {self.docker_client.container_name}:{remote_file} {remote_temp_path}"
            stdout, stderr, returncode = self.ssh_client.execute_command(docker_cmd)

            if returncode != 0:
                raise FileTransferError(f"Failed to copy file from container to remote host: {stderr}")

            # Step 3: Verify file exists on remote host
            if not self.ssh_client.check_remote_file_exists(remote_temp_path):
                raise FileTransferError(f"File not found on remote host after docker cp: {remote_temp_path}")

            # Step 4: Copy from remote host to local
            local_file = self.ssh_client.copy_file_from_remote(remote_temp_path, local_file)

            # Final checks - verify file exists and has content
            if not os.path.exists(local_file):
                raise FileTransferError(f"Local file not found after transfer: {local_file}")

            local_size = os.path.getsize(local_file)
            if local_size <= 0:
                raise FileTransferError(f"Local file is empty after transfer: {local_file}")

            logger.debug(f"File transfer succeeded: {local_file} ({local_size} bytes)")

            # Clean up remote temp file
            self.ssh_client.execute_command(f"rm -f {remote_temp_path}")

            # Clean up container file
            self.docker_client.execute_command(f"rm -f {remote_file}")

            # Parse and return
            try:
                with open(local_file) as f:
                    data = json.load(f)
                logger.debug(f"Successfully parsed JSON data from {local_file}")
                return data
            except json.JSONDecodeError as e:
                raise JsonParseError(f"Failed to parse JSON result file: {str(e)}")
            finally:
                # Clean up local file
                if os.path.exists(local_file):
                    os.remove(local_file)

        except (RubyError, CommandExecutionError) as e:
            raise QueryExecutionError(f"Error executing query: {str(e)}")
        except (SSHCommandError, SSHConnectionError) as e:
            raise ConnectionError(f"SSH error during file operation: {str(e)}")
        except FileTransferError as e:
            raise e  # Just re-raise FileTransferError
        except Exception as e:
            raise QueryExecutionError(f"Unexpected error during query execution: {str(e)}")

    def execute_json_query(self, query: str, timeout: int | None = None) -> Any:
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
            QueryExecutionError: If execution fails
            JsonParseError: If result cannot be parsed as JSON
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

    def count_records(self, model: str) -> int:
        """
        Count records for a given Rails model.

        Args:
            model: Model name (e.g., "User", "Project")

        Returns:
            Number of records

        Raises:
            QueryExecutionError: If the count query fails
        """
        result = self.execute_query(f"{model}.count")

        if isinstance(result, str) and result.isdigit():
            return int(result)
        raise QueryExecutionError(f"Unable to parse count result: {result}")

    def find_record(self, model: str, id_or_conditions: int | dict[str, Any]) -> dict[str, Any]:
        """
        Find a record by ID or conditions.

        Args:
            model: Model name (e.g., "User", "Project")
            id_or_conditions: ID or conditions hash

        Returns:
            Record data

        Raises:
            RecordNotFoundError: If no record is found
            QueryExecutionError: If query fails
        """
        try:
            if isinstance(id_or_conditions, int):
                query = f"{model}.find_by(id: {id_or_conditions})&.as_json"
            else:
                # Convert Python dict to Ruby hash format
                conditions_str = json.dumps(id_or_conditions).replace('"', "'")
                query = f"{model}.find_by({conditions_str})&.as_json"

            result = self.execute_json_query(query)

            if result is None:
                raise RecordNotFoundError(f"No {model} found with {id_or_conditions}")

            return result

        except (QueryExecutionError, JsonParseError) as e:
            raise QueryExecutionError(f"Error finding record for {model}: {str(e)}")

    def create_record(self, model: str, attributes: dict[str, Any]) -> dict[str, Any]:
        """
        Create a record with given attributes.

        Args:
            model: Model name (e.g., "User", "Project")
            attributes: Record attributes

        Returns:
            Created record data

        Raises:
            QueryExecutionError: If record creation fails
        """
        # Convert Python dict to Ruby hash format
        ruby_hash = json.dumps(attributes).replace('"', "'")

        # Build command to create and return the record
        command = f"""
        record = {model}.new({ruby_hash})
        if record.save
          record.as_json
        else
          raise "Failed to create record: #{{record.errors.full_messages.join(', ')}}"
        end
        """

        try:
            record = self.execute_query(command)
            return record
        except RubyError as e:
            raise QueryExecutionError(f"Failed to create {model}: {str(e)}")
        except Exception as e:
            raise QueryExecutionError(f"Error creating {model}: {str(e)}")

    def update_record(self, model: str, id: int, attributes: dict[str, Any]) -> dict[str, Any]:
        """
        Update a record with given attributes.

        Args:
            model: Model name (e.g., "User", "Project")
            id: Record ID
            attributes: Attributes to update

        Returns:
            Updated record data

        Raises:
            RecordNotFoundError: If record doesn't exist
            QueryExecutionError: If update fails
        """
        # Convert Python dict to Ruby hash format
        ruby_hash = json.dumps(attributes).replace('"', "'")

        # Build command to update the record
        command = f"""
        record = {model}.find_by(id: {id})
        if record.nil?
          raise "Record not found"
        elsif record.update({ruby_hash})
          record.as_json
        else
          raise "Failed to update record: #{{record.errors.full_messages.join(', ')}}"
        end
        """

        try:
            updated_record = self.execute_query(command)
            return updated_record
        except RubyError as e:
            if "Record not found" in str(e):
                raise RecordNotFoundError(f"{model} with ID {id} not found")
            raise QueryExecutionError(f"Failed to update {model}: {str(e)}")
        except Exception as e:
            raise QueryExecutionError(f"Error updating {model}: {str(e)}")

    def delete_record(self, model: str, id: int) -> None:
        """
        Delete a record.

        Args:
            model: Model name (e.g., "User", "Project")
            id: Record ID

        Raises:
            RecordNotFoundError: If record doesn't exist
            QueryExecutionError: If deletion fails
        """
        command = f"""
        record = {model}.find_by(id: {id})
        if record.nil?
          raise "Record not found"
        elsif record.destroy
          true
        else
          raise "Failed to delete record: #{{record.errors.full_messages.join(', ')}}"
        end
        """

        try:
            self.execute_query(command)
        except RubyError as e:
            if "Record not found" in str(e):
                raise RecordNotFoundError(f"{model} with ID {id} not found")
            raise QueryExecutionError(f"Failed to delete {model}: {str(e)}")
        except Exception as e:
            raise QueryExecutionError(f"Error deleting {model}: {str(e)}")

    def find_all_records(
        self,
        model: str,
        conditions: dict[str, Any] | None = None,
        limit: int | None = None,
        includes: list[str] | None = None,
    ) -> list[dict[str, Any]]:
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
            QueryExecutionError: If query fails
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
            # Use the JSON-specific method
            result = self.execute_json_query(query)
            # Make sure we always return a list, even for empty results
            if result is None:
                return []
            return result
        except Exception as e:
            raise QueryExecutionError(f"Error finding records for {model}: {str(e)}")

    def execute_transaction(self, commands: list[str]) -> Any:
        """
        Execute multiple commands in a transaction.

        Args:
            commands: List of Ruby/Rails commands

        Returns:
            Result of the transaction

        Raises:
            QueryExecutionError: If transaction fails
        """
        # Build transaction block
        transaction_commands = "\n".join(commands)
        transaction_block = f"""
        ActiveRecord::Base.transaction do
          {transaction_commands}
        end
        """

        try:
            return self.execute_query(transaction_block)
        except Exception as e:
            raise QueryExecutionError(f"Transaction failed: {str(e)}")

    def transfer_file_from_container(self, container_path: str, local_path: str) -> str:
        """
        Copy a file from the container to the local system.

        Args:
            container_path: Path to the file in the container
            local_path: Path where the file should be saved locally

        Returns:
            Path to the local file

        Raises:
            FileTransferError: If transfer fails
            FileNotFoundError: If container file doesn't exist
        """
        try:
            # Verify the container file exists
            if not self.docker_client.check_file_exists_in_container(container_path):
                raise FileNotFoundError(f"Container file not found: {container_path}")

            # Create a temporary path on the remote host
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            unique_id = f"{random.randrange(16**6):06x}"
            remote_temp_path = f"/tmp/{timestamp}_{unique_id}_{os.path.basename(container_path)}"

            # Copy from container to remote host
            self.docker_client.copy_file_from_container(container_path, remote_temp_path)

            # Create local directory if needed
            local_dir = os.path.dirname(local_path)
            if local_dir:
                os.makedirs(local_dir, exist_ok=True)

            # Copy from remote host to local
            local_file = self.ssh_client.copy_file_from_remote(remote_temp_path, local_path)

            # Clean up remote temp file
            self.ssh_client.execute_command(f"rm -f {remote_temp_path}")

            return local_file

        except (SSHFileTransferError, SSHCommandError) as e:
            raise FileTransferError(f"SSH error transferring file: {str(e)}")
        except FileNotFoundError as e:
            raise e  # Re-raise FileNotFoundError
        except Exception as e:
            raise FileTransferError(f"Error transferring file from container: {str(e)}")

    def get_users(self) -> list[dict[str, Any]]:
        """
        Get all users from OpenProject.

        Uses caching to avoid repeated Rails console queries.

        Returns:
            List of OpenProject users

        Raises:
            QueryExecutionError: If unable to retrieve users
        """
        # Check cache first (5 minutes validity)
        current_time = time.time()
        cache_valid = (
            hasattr(self, '_users_cache') and
            hasattr(self, '_users_cache_time') and
            self._users_cache is not None and
            self._users_cache_time is not None and
            current_time - self._users_cache_time < 300
        )

        if cache_valid:
            logger.debug(f"Using cached users data ({len(self._users_cache)} users)")
            return self._users_cache

        try:
            # Use direct JSON query for better performance
            users = self.execute_json_query("User.all")

            # Update cache
            self._users_cache = users or []
            self._users_cache_time = current_time

            # Update email lookup cache too
            self._users_by_email_cache = {}
            for user in self._users_cache:
                if isinstance(user, dict):
                    email = user.get('email', '').lower()
                    if email:
                        self._users_by_email_cache[email] = user

            logger.debug(f"Retrieved {len(self._users_cache)} users from OpenProject")
            return self._users_cache

        except Exception as e:
            raise QueryExecutionError(f"Failed to retrieve users: {str(e)}")

    def get_user_by_email(self, email: str) -> dict[str, Any]:
        """
        Get a user by email address.

        Uses cached user data if available.

        Args:
            email: Email address of the user

        Returns:
            User data

        Raises:
            RecordNotFoundError: If user with given email is not found
            QueryExecutionError: If query fails
        """
        # Normalize email to lowercase
        email_lower = email.lower()

        # Check cache first
        if hasattr(self, '_users_by_email_cache') and email_lower in self._users_by_email_cache:
            return self._users_by_email_cache[email_lower]

        # Try to load all users to populate cache
        try:
            # Load all users - we ignore the returned value because we just
            # want to populate the cache
            self.get_users()

            # Check if we got the user in the newly populated cache
            if email_lower in self._users_by_email_cache:
                return self._users_by_email_cache[email_lower]

            # If not in cache, try direct query
            user = self.find_record("User", {"email": email})
            if user:
                # Cache the result
                self._users_by_email_cache[email_lower] = user
                return user

            raise RecordNotFoundError(f"User with email '{email}' not found")

        except RecordNotFoundError as e:
            raise e  # Re-raise RecordNotFoundError
        except Exception as e:
            raise QueryExecutionError(f"Error finding user by email: {str(e)}")

    def get_custom_field_by_name(self, name: str) -> dict[str, Any]:
        """
        Find a custom field by name.

        Args:
            name: The name of the custom field to find

        Returns:
            The custom field

        Raises:
            RecordNotFoundError: If custom field with given name is not found
        """
        return self.find_record("CustomField", {"name": name})

    def get_custom_field_id_by_name(self, name: str) -> int:
        """
        Find a custom field ID by name.

        Args:
            name: The name of the custom field to find

        Returns:
            The custom field ID

        Raises:
            RecordNotFoundError: If custom field with given name is not found
            QueryExecutionError: If query fails
        """
        try:
            result = self.execute_query(f"CustomField.where(name: '{name}').first&.id")

            # Handle nil value from Ruby
            if result is None:
                raise RecordNotFoundError(f"Custom field '{name}' not found")

            # Handle integer result
            if isinstance(result, int):
                return result

            # Try to convert string to int
            if isinstance(result, str):
                try:
                    return int(result)
                except ValueError:
                    raise QueryExecutionError(f"Invalid ID format: {result}")

            raise QueryExecutionError(f"Unexpected result type: {type(result)}")

        except RecordNotFoundError as e:
            raise e  # Re-raise RecordNotFoundError
        except Exception as e:
            raise QueryExecutionError(f"Error getting custom field ID: {str(e)}")

    def get_statuses(self) -> list[dict[str, Any]]:
        """
        Get all statuses from OpenProject.

        Returns:
            List of status objects

        Raises:
            QueryExecutionError: If query fails
        """
        try:
            return self.execute_json_query("Status.all") or []
        except Exception as e:
            raise QueryExecutionError(f"Failed to get statuses: {str(e)}")

    def get_work_package_types(self) -> list[dict[str, Any]]:
        """
        Get all work package types from OpenProject.

        Returns:
            List of work package type objects

        Raises:
            QueryExecutionError: If query fails
        """
        try:
            return self.execute_json_query("Type.all") or []
        except Exception as e:
            raise QueryExecutionError(f"Failed to get work package types: {str(e)}")

    def get_projects(self) -> list[dict[str, Any]]:
        """
        Get all projects from OpenProject.

        Returns:
            List of project objects

        Raises:
            QueryExecutionError: If query fails
        """
        try:
            return self.execute_json_query("Project.all") or []
        except Exception as e:
            raise QueryExecutionError(f"Failed to get projects: {str(e)}")

    def get_project_by_identifier(self, identifier: str) -> dict[str, Any]:
        """
        Get a project by identifier.

        Args:
            identifier: Project identifier or slug

        Returns:
            Project object

        Raises:
            RecordNotFoundError: If project with given identifier is not found
            QueryExecutionError: If query fails
        """
        try:
            project = self.execute_json_query(f"Project.find_by(identifier: '{identifier}')")
            if project is None:
                raise RecordNotFoundError(f"Project with identifier '{identifier}' not found")
            return project
        except RecordNotFoundError as e:
            raise e  # Re-raise RecordNotFoundError
        except Exception as e:
            raise QueryExecutionError(f"Failed to get project: {str(e)}")

    def delete_all_work_packages(self) -> int:
        """
        Delete all work packages in bulk.

        Returns:
            Number of deleted work packages

        Raises:
            QueryExecutionError: If bulk deletion fails
        """
        try:
            count = self.execute_query("WorkPackage.delete_all")
            return count if isinstance(count, int) else 0
        except Exception as e:
            raise QueryExecutionError(f"Failed to delete all work packages: {str(e)}")

    def delete_all_projects(self) -> int:
        """
        Delete all projects in bulk.

        Returns:
            Number of deleted projects

        Raises:
            QueryExecutionError: If bulk deletion fails
        """
        try:
            count = self.execute_query("Project.delete_all")
            return count if isinstance(count, int) else 0
        except Exception as e:
            raise QueryExecutionError(f"Failed to delete all projects: {str(e)}")

    def delete_all_custom_fields(self) -> int:
        """
        Delete all custom fields in bulk.
        Uses destroy_all for proper dependency cleanup.

        Returns:
            Number of deleted custom fields

        Raises:
            QueryExecutionError: If bulk deletion fails
        """
        try:
            # Get count before deletion for return value
            count = self.execute_query("CustomField.count")
            self.execute_query("CustomField.destroy_all")
            return count if isinstance(count, int) else 0
        except Exception as e:
            raise QueryExecutionError(f"Failed to delete all custom fields: {str(e)}")

    def delete_non_default_issue_types(self) -> int:
        """
        Delete non-default issue types (work package types).

        Returns:
            Number of deleted types

        Raises:
            QueryExecutionError: If deletion fails
        """
        script = """
        non_default_types = Type.where(is_default: false, is_standard: false)
        count = non_default_types.count
        non_default_types.destroy_all
        count
        """

        try:
            count = self.execute_query(script)
            return count if isinstance(count, int) else 0
        except Exception as e:
            raise QueryExecutionError(f"Failed to delete non-default issue types: {str(e)}")

    def delete_non_default_issue_statuses(self) -> int:
        """
        Delete non-default issue statuses.

        Returns:
            Number of deleted statuses

        Raises:
            QueryExecutionError: If deletion fails
        """
        script = """
        non_default_statuses = Status.where(is_default: false)
        count = non_default_statuses.count
        non_default_statuses.destroy_all
        count
        """

        try:
            count = self.execute_query(script)
            return count if isinstance(count, int) else 0
        except Exception as e:
            raise QueryExecutionError(f"Failed to delete non-default issue statuses: {str(e)}")

    def create_users_in_bulk(self, users_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Create multiple users in OpenProject with a single API call.

        Args:
            users_data: List of user data dictionaries

        Returns:
            List of created user objects

        Raises:
            QueryExecutionError: If bulk user creation fails
        """
        if not users_data:
            return []

        # Format the user data for Ruby
        ruby_user_data = json.dumps(users_data)

        # Create a Ruby script to create the users in bulk
        script = f"""
        users_data = {ruby_user_data}
        results = []

        users_data.each do |user_data|
          begin
            user = User.new
            user.login = user_data['login']
            user.firstname = user_data['firstname']
            user.lastname = user_data['lastname']
            user.mail = user_data['mail']
            user.admin = user_data['admin'] || false
            user.status = user_data['status'] || :active
            user.password = user_data['password'] || SecureRandom.hex(8)

            if user.save
              results << {{
                'status' => 'success',
                'id' => user.id,
                'login' => user.login,
                'mail' => user.mail
              }}
            else
              results << {{
                'status' => 'error',
                'login' => user.login,
                'mail' => user.mail,
                'errors' => user.errors.full_messages
              }}
            end
          rescue => e
            results << {{
              'status' => 'error',
              'login' => user_data['login'],
              'mail' => user_data['mail'],
              'errors' => [e.message]
            }}
          end
        end

        results.as_json
        """

        try:
            # Execute the script
            result = self.execute_query(script)

            # Return the results
            return result
        except Exception as e:
            raise QueryExecutionError(f"Failed to create users in bulk: {str(e)}")
