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
            retry_count: Number of retries for operations (default: 3)
            retry_delay: Delay between retries in seconds (default: 1.0)
        """
        # Load configuration
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

        # Initialize clients in the correct order
        # 1. First, create the SSH client which is the foundation
        self.ssh_client = SSHClient(
            host=str(self.ssh_host),
            user=self.ssh_user,
            key_file=cast(Optional[str], self.ssh_key_file),
            operation_timeout=self.command_timeout,
            retry_count=self.retry_count,
            retry_delay=self.retry_delay,
        )
        logger.debug(f"Initialized SSHClient for host {self.ssh_host}")

        # 2. Next, create the Docker client using the SSH client
        self.docker_client = DockerClient(
            container_name=str(self.container_name),
            ssh_client=self.ssh_client,  # Pass our SSH client instance
            command_timeout=self.command_timeout,
            retry_count=self.retry_count,
            retry_delay=self.retry_delay,
        )
        logger.debug(f"Initialized DockerClient for container {self.container_name}")

        # 3. Finally, create the Rails console client for executing commands
        self.rails_client = RailsConsoleClient(
            tmux_session_name=self.tmux_session_name,
            command_timeout=self.command_timeout,
        )
        logger.debug(f"Initialized RailsConsoleClient with tmux session {self.tmux_session_name}")

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

    def _transfer_and_execute_script(self, script_content: str, timeout: Optional[int] = None) -> Dict[str, Any]:
        """
        Transfer a Ruby script to the remote server and execute it.

        Args:
            script_content: Ruby script content
            timeout: Command timeout in seconds

        Returns:
            Dict with execution results
        """
        # Create a script file locally
        local_script_path = self._create_script_file(script_content)
        script_filename = os.path.basename(local_script_path)

        # Define remote path
        remote_script_path = f"/tmp/{script_filename}"

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

            # Transfer file to remote server
            logger.debug(f"Transferring script {script_filename} to remote server")

            # First, use SSH client to transfer to remote host
            ssh_result = self.ssh_client.copy_file_to_remote(
                local_script_path,
                remote_script_path
            )

            if ssh_result.get("status") != "success":
                error_msg = ssh_result.get("error", "Unknown error transferring file")
                logger.error(f"Failed to transfer script to remote host: {error_msg}")
                return {"status": "error", "error": f"File transfer to remote host failed: {error_msg}"}

            # Now copy from remote host to container using Docker client
            docker_container_path = f"/tmp/{script_filename}"
            copy_result = self.docker_client.copy_file_to_container(
                remote_script_path,
                docker_container_path
            )

            if copy_result.get("status") != "success":
                error_msg = copy_result.get("error", "Unknown error transferring file")
                logger.error(f"Failed to transfer script to container: {error_msg}")
                return {"status": "error", "error": f"File transfer to container failed: {error_msg}"}

            logger.debug(f"Successfully transferred script to {docker_container_path}")

            # Verify the file exists in the container
            if not self.docker_client.check_file_exists_in_container(docker_container_path):
                error_msg = f"Script file not found in container at {docker_container_path}"
                logger.error(error_msg)
                return {"status": "error", "error": error_msg}

            # Execute the script in Rails console
            logger.debug(f"Executing script via Rails console: {docker_container_path}")

            # Build load command for the script file
            load_command = f"load '{docker_container_path}'"

            # Execute the command in Rails console
            result = self.rails_client.execute(load_command, timeout)

            logger.debug(f"Rails console execution completed with status: {result.get('status', 'unknown')}")

            # Clean up the local file
            try:
                os.unlink(local_script_path)
                logger.debug(f"Cleaned up local script file: {local_script_path}")
            except Exception as e:
                logger.debug(f"Non-critical error cleaning up temporary file: {str(e)}")

            # Clean up the remote file
            try:
                self.ssh_client.execute_command(f"rm -f {remote_script_path}", check=False)
                logger.debug(f"Cleaned up remote script file: {remote_script_path}")
            except Exception as e:
                logger.debug(f"Non-critical error cleaning up remote file: {str(e)}")

            return result

        except Exception as e:
            logger.error(f"Error in script execution: {str(e)}")
            # Clean up the local file on error
            try:
                if os.path.exists(local_script_path):
                    os.unlink(local_script_path)
                    logger.debug(f"Cleaned up local script file after error: {local_script_path}")
            except Exception:
                pass
            return {"status": "error", "error": f"Script execution failed: {str(e)}"}

    def execute_query(self, query: str, timeout: Optional[int] = None) -> Dict[str, Any]:
        """
        Execute a Rails query using the file-based approach.

        Args:
            query: Rails query to execute
            timeout: Query timeout in seconds (default: self.command_timeout)

        Returns:
            Dict with status and result or error
        """
        # Wrap the query in a script with result handling
        script_content = f"""
        begin
          result = (
            {query}
          )

          # Return the result in a structured format
          {{
            success: true,
            result: result
          }}
        rescue => e
          # Return error information
          {{
            success: false,
            error: e.message,
            backtrace: e.backtrace
          }}
        end
        """

        result = self._transfer_and_execute_script(script_content, timeout)

        if result.get("status") != "success":
            return {"status": "error", "error": result.get("error", "Unknown error")}

        output = result.get("output")
        if isinstance(output, dict):
            if output.get("success") is True:
                return {"status": "success", "output": output.get("result")}
            else:
                error_msg = output.get("error", "Unknown execution error")
                return {"status": "error", "error": error_msg}

        # If output format is unexpected
        return {"status": "error", "error": "Unexpected response format"}

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
        """
        if isinstance(id_or_conditions, int):
            command = f"{model}.find_by(id: {id_or_conditions})&.as_json"
        else:
            # Convert Python dict to Ruby hash format
            conditions_str = json.dumps(id_or_conditions).replace('"', "'")
            command = f"{model}.find_by({conditions_str})&.as_json"

        result = self.execute_query(command)

        if result["status"] == "success" and result["output"] is not None:
            try:
                # Handle the case where output is already parsed into Python types
                if isinstance(result["output"], dict):
                    return result["output"]

                # Try to parse it as JSON if it's a string
                if isinstance(result["output"], str):
                    # Handle Ruby hash format
                    cleaned_output = result["output"].replace("=>", ":")
                    cleaned_output = cleaned_output.replace("nil", "null")
                    return cast(Dict[str, Any], json.loads(cleaned_output))

                return None
            except (json.JSONDecodeError, TypeError):
                return None
        return None

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

        result = self.execute_query(query)

        if result["status"] == "success" and result["output"] is not None:
            try:
                # Handle the case where output is already parsed into Python types
                if isinstance(result["output"], list):
                    return result["output"]

                # Try to parse it as JSON if it's a string
                if isinstance(result["output"], str):
                    # Handle Ruby hash format
                    cleaned_output = result["output"].replace("=>", ":")
                    cleaned_output = cleaned_output.replace("nil", "null")
                    return cast(List[Dict[str, Any]], json.loads(cleaned_output))

                return []
            except (json.JSONDecodeError, TypeError):
                return []
        return []

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
                logger.debug(f"Non-critical error cleaning up files: {str(e)}")

            return result

        except Exception as e:
            logger.error(f"Error executing script: {str(e)}")
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
            logger.error(f"Error executing script with data: {str(e)}")
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

        Returns:
            List of status dictionaries
        """
        # Try to get statuses using the Status model
        result = self.execute_query("Status.all.as_json")

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
                logger.error("Failed to parse statuses from OpenProject")
                return []

        logger.error("Failed to get statuses from OpenProject")
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
                logger.error("Failed to parse work package types from OpenProject")
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
            logger.debug(f"Non-critical error cleaning up remote file: {str(e)}")

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
        if docker_result.get("status") != "success":
            logger.error(f"Failed to copy file from container: {docker_result.get('error', 'Unknown error')}")
            return False

        # Then copy from remote host to local using SSH client
        ssh_result = self.ssh_client.copy_file_from_remote(remote_temp_path, local_path)

        # Clean up the remote file regardless of success
        try:
            self.ssh_client.execute_command(f"rm -f {remote_temp_path}", check=False)
        except Exception as e:
            logger.debug(f"Non-critical error cleaning up remote file: {str(e)}")

        return ssh_result.get("status") == "success"

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

        Returns:
            List of project dictionaries
        """
        # Try to get projects using the API or Rails console
        result = self.execute_query("Project.all.as_json")

        if result["status"] == "success" and result["output"] is not None:
            try:
                # Handle the case where output is already parsed into Python types
                if isinstance(result["output"], list):
                    return result["output"]

                # Try to parse it as JSON if it's a string
                if isinstance(result["output"], str):
                    # Clean Ruby-style hashes in the output
                    cleaned_output = result["output"].replace("=>", ":")
                    return cast(List[Dict[str, Any]], json.loads(cleaned_output))

                return []
            except Exception as e:
                logger.error(f"Failed to parse projects output: {e}")
                return []

        logger.error("Failed to get projects from OpenProject")
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
                logger.error(f"Failed to parse project output: {e}")
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

        result = self._transfer_and_execute_script(script)

        if result.get("status") == "success":
            output = result.get("output")
            if isinstance(output, dict) and output.get("success") is True:
                return True

        return False

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

        result = self._transfer_and_execute_script(script)

        if result.get("status") == "success":
            output = result.get("output")
            if isinstance(output, dict) and output.get("success") is True:
                return True

        return False

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

        result = self._transfer_and_execute_script(script)

        if result.get("status") == "success":
            output = result.get("output")
            if isinstance(output, dict) and output.get("success") is True:
                return True

        return False

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

        result = self._transfer_and_execute_script(script)

        if result.get("status") == "success":
            output = result.get("output", {})
            if isinstance(output, dict):
                if output.get("success") is True:
                    return {"count": output.get("count", 0), "message": output.get("message", "")}

        return {"count": 0, "error": "Failed to delete issue types"}

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

        result = self._transfer_and_execute_script(script)

        if result.get("status") == "success":
            output = result.get("output", {})
            if isinstance(output, dict):
                if output.get("success") is True:
                    return {"count": output.get("count", 0), "message": output.get("message", "")}

        return {"count": 0, "error": "Failed to delete issue statuses"}

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

        result = self._transfer_and_execute_script(script)

        if result.get("status") == "success":
            output = result.get("output", {})
            if isinstance(output, dict):
                if output.get("success") is True:
                    result_dict = {
                        "count": output.get("count", 0),
                        "message": output.get("message", "")
                    }
                    if output.get("model_not_found") is True:
                        result_dict["model_not_found"] = True
                    return result_dict

        return {"count": 0, "error": "Failed to delete custom issue link types"}

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
            logger.error(f"Error testing Rails console connection: {str(e)}")
            return False
