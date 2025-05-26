#!/usr/bin/env python3
"""OpenProjectClient.

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
import random
from pathlib import Path
from shlex import quote
from time import time
from typing import Any, cast

from src import config
from src.clients.docker_client import DockerClient
from src.clients.rails_console_client import (
    CommandExecutionError,
    RailsConsoleClient,
    RubyError,
)
from src.clients.ssh_client import (
    SSHClient,
    SSHCommandError,
    SSHConnectionError,
)
from src.utils.file_manager import FileManager

logger = config.logger


class OpenProjectError(Exception):
    """Base exception for all OpenProject client errors."""


class ConnectionError(OpenProjectError):
    """Error when connection to OpenProject fails."""


class FileTransferError(OpenProjectError):
    """Error when transferring files to/from OpenProject container."""


class QueryExecutionError(OpenProjectError):
    """Error when executing a query in OpenProject."""


class RecordNotFoundError(OpenProjectError):
    """Error when a record is not found in OpenProject."""


class JsonParseError(OpenProjectError):
    """Error when parsing JSON output from OpenProject."""


class OpenProjectClient:
    """Client for OpenProject operations.

    This is the top-level coordinator that orchestrates the client architecture:
    - SSHClient handles all SSH interactions
    - DockerClient (using SSHClient) handles container interactions
    - RailsConsoleClient handles Rails console interactions.

    All error handling uses exceptions rather than status dictionaries.
    """

    def __init__(
        self,
        container_name: str | None = None,
        ssh_host: str | None = None,
        ssh_user: str | None = None,
        tmux_session_name: str | None = None,
        command_timeout: int = 180,
        retry_count: int = 3,
        retry_delay: float = 1.0,
        ssh_client: SSHClient | None = None,
        docker_client: DockerClient | None = None,
        rails_client: RailsConsoleClient | None = None,
    ) -> None:
        """Initialize the OpenProject client with dependency injection.

        Args:
            container_name: Docker container name (default: from config)
            ssh_host: SSH host (default: from config)
            ssh_user: SSH username (default: from config)
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
        self._users_cache: list[dict[str, Any]] | None = None
        self._users_cache_time: float | None = None
        self._users_by_email_cache: dict[str, dict[str, Any]] = {}

        # Get config values
        op_config = config.openproject_config

        # Use provided values or defaults from config
        self.container_name = container_name or op_config.get("container")
        self.ssh_host = ssh_host or op_config.get("server")
        self.ssh_user = ssh_user or op_config.get("user")
        self.tmux_session_name = tmux_session_name or op_config.get("tmux_session_name", "rails_console")
        self.command_timeout = command_timeout
        self.retry_count = retry_count
        self.retry_delay = retry_delay

        # Verify required configuration
        if not self.container_name:
            msg = "Container name is required"
            raise ValueError(msg)
        if not self.ssh_host:
            msg = "SSH host is required"
            raise ValueError(msg)

        # Initialize file manager
        self.file_manager = FileManager()

        # Initialize clients in the correct order, respecting dependency injection
        # 1. First, create or use the SSH client which is the foundation
        self.ssh_client = ssh_client or SSHClient(
            host=str(self.ssh_host),
            user=self.ssh_user,
            operation_timeout=self.command_timeout,
            retry_count=self.retry_count,
            retry_delay=self.retry_delay,
        )
        logger.debug(
            f"{'Using provided' if ssh_client else 'Initialized'} SSHClient for host {self.ssh_host}",
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
            f"{'Using provided' if docker_client else 'Initialized'} DockerClient for container {self.container_name}",
        )

        # 3. Finally, create or use the Rails console client for executing commands
        self.rails_client = rails_client or RailsConsoleClient(
            tmux_session_name=self.tmux_session_name,
            command_timeout=self.command_timeout,
        )
        logger.debug(
            f"{'Using provided' if rails_client else 'Initialized'} "
            f"RailsConsoleClient with tmux session {self.tmux_session_name}",
        )

        logger.success(f"OpenProjectClient initialized for host {self.ssh_host}, container {self.container_name}")

    def _create_script_file(self, script_content: str) -> Path:
        """Create a temporary file with the script content.

        Args:
            script_content: Content to write to the file

        Returns:
            Path to the created file

        Raises:
            OSError: If unable to create or write to the script file

        """
        file_path = None
        try:
            # Create a temporary directory if needed
            temp_dir = Path(self.file_manager.data_dir) / "temp_scripts"
            temp_dir.mkdir(parents=True, exist_ok=True)

            # Generate a unique filename
            filename = f"openproject_script_{os.urandom(4).hex()}.rb"
            file_path = temp_dir / filename

            # Write the content directly instead of using tempfile module
            with file_path.open("w", encoding="utf-8") as f:
                f.write(script_content)

            # Log the absolute path for easier debugging
            logger.debug("Created temporary script file: %s", file_path.as_posix())
            return file_path
        except OSError:
            error_msg = f"Failed to create script file: {str(file_path)}"
            logger.exception(error_msg)
            raise OSError(error_msg)
        except Exception:
            error_msg = f"Failed to create script file: {str(file_path)}"
            logger.exception(error_msg)
            raise OSError(error_msg)

    def _transfer_rails_script(self, local_path: Path | str) -> Path:
        """Transfer a script to the Rails environment.

        Args:
            local_path: Path to the script file (Path object or string)

        Returns:
            Path to the script in the container

        Raises:
            FileTransferError: If transfer fails

        """
        try:
            # Convert string to Path if needed
            if isinstance(local_path, str):
                local_path = Path(local_path)

            # Get the absolute path for better error messages
            abs_path = local_path.absolute()
            logger.debug("Transferring script from: %s", abs_path)

            # Use just the base filename for the container path
            container_path = Path("/tmp") / local_path.name

            self.docker_client.transfer_file_to_container(abs_path, container_path)

            logger.debug("Successfully transferred file to container at %s", container_path)

        except Exception as e:
            # Verify the local file exists and is readable before attempting to transfer
            if not local_path.is_file():
                msg = f"Local file does not exist: {local_path}"
                raise FileTransferError(msg)

            if not os.access(local_path, os.R_OK):
                msg = f"Local file is not readable: {local_path}"
                raise FileTransferError(msg)

            msg = "Failed to transfer script."
            raise FileTransferError(msg) from e

        return container_path

    def _cleanup_script_files(self, local_path: Path, remote_path: Path) -> None:
        """Clean up script files after execution.

        Args:
            local_path: Path to the local script file
            remote_path: Path to the remote script file

        """
        # Clean up local file
        try:
            if local_path.exists():
                local_path.unlink()
                logger.debug("Cleaned up local script file: %s", local_path)
        except Exception as e:
            logger.warning("Non-critical error cleaning up local file: %s", e)

        # Clean up remote file
        try:
            command = [
                "rm",
                "-f",
                quote(remote_path.resolve(strict=True).as_posix()),
            ]
            self.ssh_client.execute_command(" ".join(command))
            logger.debug("Cleaned up remote script file: %s", remote_path)
        except Exception as e:
            logger.warning("Non-critical error cleaning up remote file: %s", e)

    def execute(self, script_content: str) -> dict[str, Any]:
        """Execute a Ruby script directly.

        Args:
            script_content: Ruby script content to execute

        Returns:
            Script execution result

        Raises:
            QueryExecutionError: If script execution fails

        """
        return self.execute_query(script_content)

    def transfer_file_to_container(self, local_path: Path, container_path: Path) -> None:
        """Transfer a file from local to the OpenProject container.

        Args:
            local_path: Path to local file
            container_path: Destination path in container

        Raises:
            FileTransferError: If the transfer fails for any reason

        """
        try:
            self.docker_client.transfer_file_to_container(local_path, container_path)
        except Exception as e:
            error_msg = "Failed to transfer file to container."
            logger.exception(error_msg)
            raise FileTransferError(error_msg) from e

    def is_connected(self) -> bool:
        """Test if connected to OpenProject.

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
        except Exception:
            logger.exception("Connection test failed.")
            return False

    def execute_query(self, query: str, timeout: int | None = None) -> str:
        """Execute a Rails query.

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

        return self.rails_client._send_command_to_tmux(f"puts ({query})", 5)

    def execute_query_to_json_file(self, query: str, timeout: int | None = None) -> Any:
        """Execute a Rails query and save the result to a JSON file, then transfer it back.

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
        container_result_file = Path("/tmp") / f"query_result_{uid}.json"
        # Use remote temp path instead of local temp path for docker_client.copy_file_from_container
        remote_result_file = Path("/tmp") / f"query_result_{uid}.json"
        local_result_file = self.file_manager.temp_dir / f"query_result_{uid}.json"

        # Create a simplified script that avoids variable name conflicts
        script = f"""
require 'json'

begin
  # Execute query
  result = {query}

  # Write to file
  file_content = result.nil? ? "null" : result.to_json
  File.write('{quote(container_result_file.as_posix())}', file_content)

  # Return true to indicate success
  true
rescue StandardError => error
  # Re-raise the error to trigger exception handling
  raise error
end
"""

        try:
            # Create and transfer script
            local_script_file = self._create_script_file(script)
            container_script_file = self._transfer_rails_script(local_script_file)

            # Run the script
            self.rails_client.execute(
                f"load '{container_script_file}'",
                timeout=timeout or self.command_timeout,
            )

            # First copy from container to remote server
            self.docker_client.copy_file_from_container(
                container_result_file,
                remote_result_file,
            )

            # Then copy from remote server to local machine
            self.ssh_client.copy_file_from_remote(remote_result_file, local_result_file)

            # Clean up script file
            if local_script_file.exists():
                local_script_file.unlink()

            # Clean up container file
            rm_command = ["rm", "-f", quote(container_result_file.as_posix())]
            self.docker_client.execute_command(" ".join(rm_command))

            # Clean up remote file
            self.ssh_client.execute_command(f"rm -f {quote(str(remote_result_file))}")

            # Parse and return
            try:
                with local_result_file.open("r") as f:
                    data = json.load(f)
                logger.debug("Successfully parsed JSON data from %s", local_result_file)
                return data
            except json.JSONDecodeError as e:
                msg = "Failed to parse JSON result file."
                raise JsonParseError(msg) from e
            finally:
                # Clean up local file
                if local_result_file.exists():
                    local_result_file.unlink()

        except (RubyError, CommandExecutionError) as e:
            msg = "Error executing query."
            raise QueryExecutionError(msg) from e
        except (SSHCommandError, SSHConnectionError) as e:
            msg = "SSH error during file operation."
            raise ConnectionError(msg) from e
        except FileTransferError:
            raise  # Just re-raise FileTransferError
        except Exception as e:
            msg = "Unexpected error during query execution."
            raise QueryExecutionError(msg) from e

    def execute_json_query(self, query: str, timeout: int | None = None) -> Any:
        """Execute a Rails query and return parsed JSON result.

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
        """Count records for a given Rails model.

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
        msg = "Unable to parse count result."
        raise QueryExecutionError(msg)

    def find_record(
        self,
        model: str,
        id_or_conditions: int | dict[str, Any],
    ) -> dict[str, Any]:
        """Find a record by ID or conditions.

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
                msg = f"No {model} found with {id_or_conditions}"
                raise RecordNotFoundError(msg)

            return result

        except (QueryExecutionError, JsonParseError) as e:
            msg = f"Error finding record for {model}."
            raise QueryExecutionError(msg) from e

    def create_record(self, model: str, attributes: dict[str, Any]) -> dict[str, Any]:
        """Create a record with given attributes.

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
            return self.execute_query(command)
        except RubyError as e:
            msg = f"Failed to create {model}."
            raise QueryExecutionError(msg) from e
        except Exception as e:
            msg = f"Error creating {model}."
            raise QueryExecutionError(msg) from e

    def update_record(self, model: str, id: int, attributes: dict[str, Any]) -> dict[str, Any]:
        """Update a record with given attributes.

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
            return self.execute_query(command)
        except RubyError as e:
            if "Record not found" in str(e):
                msg = f"{model} with ID {id} not found"
                raise RecordNotFoundError(msg) from e
            msg = f"Failed to update {model}."
            raise QueryExecutionError(msg) from e
        except Exception as e:
            msg = f"Error updating {model}."
            raise QueryExecutionError(msg) from e

    def delete_record(self, model: str, id: int) -> None:
        """Delete a record.

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
                msg = f"{model} with ID {id} not found"
                raise RecordNotFoundError(msg) from e
            msg = f"Failed to delete {model}."
            raise QueryExecutionError(msg) from e
        except Exception as e:
            msg = f"Error deleting {model}."
            raise QueryExecutionError(msg) from e

    def find_all_records(
        self,
        model: str,
        conditions: dict[str, Any] | None = None,
        limit: int | None = None,
        includes: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Find all records matching conditions.

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
            msg = f"Error finding records for {model}."
            raise QueryExecutionError(msg) from e

    def execute_transaction(self, commands: list[str]) -> Any:
        """Execute multiple commands in a transaction.

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
            msg = "Transaction failed."
            raise QueryExecutionError(msg) from e

    def transfer_file_from_container(
        self,
        container_path: Path,
        local_path: Path,
    ) -> Path:
        """Copy a file from the container to the local system.

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
            return self.docker_client.copy_file_from_container(
                container_path,
                local_path,
            )

        except Exception as e:
            msg = "Error transferring file from container."
            raise FileTransferError(msg) from e

    def get_users(self) -> list[dict[str, Any]]:
        """Get all users from OpenProject.

        Uses caching to avoid repeated Rails console queries.

        Returns:
            List of OpenProject users

        Raises:
            QueryExecutionError: If unable to retrieve users

        """
        # Check cache first (5 minutes validity)
        current_time = time()
        cache_valid = (
            hasattr(self, "_users_cache")
            and hasattr(self, "_users_cache_time")
            and self._users_cache is not None
            and self._users_cache_time is not None
            and current_time - self._users_cache_time < 300
        )

        if cache_valid:
            logger.debug("Using cached users data (%d users)", len(self._users_cache))
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
                    email = user.get("email", "").lower()
                    if email:
                        self._users_by_email_cache[email] = user

            logger.debug("Retrieved %d users from OpenProject", len(self._users_cache))
            return self._users_cache

        except Exception as e:
            msg = "Failed to retrieve users."
            raise QueryExecutionError(msg) from e

    def get_user_by_email(self, email: str) -> dict[str, Any]:
        """Get a user by email address.

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
        if hasattr(self, "_users_by_email_cache") and email_lower in self._users_by_email_cache:
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

            msg = f"User with email '{email}' not found"
            raise RecordNotFoundError(msg)

        except RecordNotFoundError:
            raise  # Re-raise RecordNotFoundError
        except Exception as e:
            msg = "Error finding user by email."
            raise QueryExecutionError(msg) from e

    def get_custom_field_by_name(self, name: str) -> dict[str, Any]:
        """Find a custom field by name.

        Args:
            name: The name of the custom field to find

        Returns:
            The custom field

        Raises:
            RecordNotFoundError: If custom field with given name is not found

        """
        return self.find_record("CustomField", {"name": name})

    def get_custom_field_id_by_name(self, name: str) -> int:
        """Find a custom field ID by name.

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
                msg = f"Custom field '{name}' not found"
                raise RecordNotFoundError(msg)

            # Handle integer result
            if isinstance(result, int):
                return result

            # Try to convert string to int
            if isinstance(result, str):
                try:
                    return int(result)
                except ValueError:
                    msg = f"Invalid ID format: {result}"
                    raise QueryExecutionError(msg)

            msg = f"Unexpected result type: {type(result)}"
            raise QueryExecutionError(msg)

        except RecordNotFoundError:
            raise  # Re-raise RecordNotFoundError
        except Exception as e:
            msg = "Error getting custom field ID."
            raise QueryExecutionError(msg) from e

    def get_statuses(self) -> list[dict[str, Any]]:
        """Get all statuses from OpenProject.

        Returns:
            List of status objects

        Raises:
            QueryExecutionError: If query fails

        """
        try:
            return self.execute_json_query("Status.all") or []
        except Exception as e:
            msg = "Failed to get statuses."
            raise QueryExecutionError(msg) from e

    def get_work_package_types(self) -> list[dict[str, Any]]:
        """Get all work package types from OpenProject.

        Returns:
            List of work package type objects

        Raises:
            QueryExecutionError: If query fails

        """
        try:
            return self.execute_json_query("Type.all") or []
        except Exception as e:
            msg = "Failed to get work package types."
            raise QueryExecutionError(msg) from e

    def get_projects(self) -> list[dict[str, Any]]:
        """Get all projects from OpenProject.

        Returns:
            List of project objects

        Raises:
            QueryExecutionError: If query fails

        """
        try:
            return self.execute_json_query("Project.all") or []
        except Exception as e:
            msg = "Failed to get projects."
            raise QueryExecutionError(msg) from e

    def get_project_by_identifier(self, identifier: str) -> dict[str, Any]:
        """Get a project by identifier.

        Args:
            identifier: Project identifier or slug

        Returns:
            Project object

        Raises:
            RecordNotFoundError: If project with given identifier is not found
            QueryExecutionError: If query fails

        """
        try:
            project = self.execute_json_query(
                f"Project.find_by(identifier: '{identifier}')",
            )
            if project is None:
                msg = f"Project with identifier '{identifier}' not found"
                raise RecordNotFoundError(msg)
            return project
        except RecordNotFoundError:
            raise  # Re-raise RecordNotFoundError
        except Exception as e:
            msg = "Failed to get project."
            raise QueryExecutionError(msg) from e

    def delete_all_work_packages(self) -> int:
        """Delete all work packages in bulk.

        Returns:
            Number of deleted work packages

        Raises:
            QueryExecutionError: If bulk deletion fails

        """
        try:
            count = self.execute_query("WorkPackage.delete_all")
            return count if isinstance(count, int) else 0
        except Exception as e:
            msg = "Failed to delete all work packages."
            raise QueryExecutionError(msg) from e

    def delete_all_projects(self) -> int:
        """Delete all projects in bulk.

        Returns:
            Number of deleted projects

        Raises:
            QueryExecutionError: If bulk deletion fails

        """
        try:
            count = self.execute_query("Project.delete_all")
            return count if isinstance(count, int) else 0
        except Exception as e:
            msg = "Failed to delete all projects."
            raise QueryExecutionError(msg) from e

    def delete_all_custom_fields(self) -> int:
        """Delete all custom fields in bulk.

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
            msg = "Failed to delete all custom fields."
            raise QueryExecutionError(msg) from e

    def delete_non_default_issue_types(self) -> int:
        """Delete non-default issue types (work package types).

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
            msg = "Failed to delete non-default issue types."
            raise QueryExecutionError(msg) from e

    def delete_non_default_issue_statuses(self) -> int:
        """Delete non-default issue statuses.

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
            msg = "Failed to delete non-default issue statuses."
            raise QueryExecutionError(msg) from e

    def create_users_in_bulk(
        self,
        users_data: list[dict[str, Any]],
    ) -> str:
        """Create multiple users in OpenProject with a single API call.

        Args:
            users_data: List of user data dictionaries

        Returns:
            List of created user objects

        Raises:
            QueryExecutionError: If bulk user creation fails

        """
        if not users_data:
            return ""

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
            return self.execute_query(script)

            # Return the results
        except Exception as e:
            msg = "Failed to create users in bulk."
            raise QueryExecutionError(msg) from e
