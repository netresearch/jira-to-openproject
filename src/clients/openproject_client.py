#!/usr/bin/env python3
"""
OpenProjectClient

Main client interface for OpenProject operations.
Uses RailsConsoleClient for Rails console interactions.
"""

import json
from typing import Any, Dict, List, Optional, cast

from src import config
from src.clients.rails_console_client import RailsConsoleClient
from src.utils.file_manager import FileManager

logger = config.logger


class OpenProjectClient:
    """
    Client for OpenProject operations.
    This is the main API that consumers should use.
    """

    def __init__(
        self,
        container_name: Optional[str] = None,
        ssh_host: Optional[str] = None,
        ssh_user: Optional[str] = None,
        ssh_key_file: Optional[str] = None,
        tmux_session_name: Optional[str] = None,
        command_timeout: int = 180,
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

        # Verify required configuration
        if not self.container_name:
            raise ValueError("Container name is required")
        if not self.ssh_host:
            raise ValueError("SSH host is required")

        # Initialize file manager
        self.file_manager = FileManager()

        # Initialize Rails console client
        self.rails_client = RailsConsoleClient(
            container_name=str(self.container_name),
            ssh_host=str(self.ssh_host),
            ssh_user=self.ssh_user,
            ssh_key_file=cast(Optional[str], self.ssh_key_file),
            tmux_session_name=self.tmux_session_name,
            command_timeout=self.command_timeout
        )

        logger.success(f"OpenProjectClient initialized for host {self.ssh_host}, container {self.container_name}")

    def execute_query(self, query: str, timeout: Optional[int] = None) -> Dict[str, Any]:
        """
        Execute a Rails query.

        Args:
            query: Rails query to execute
            timeout: Query timeout in seconds (default: self.command_timeout)

        Returns:
            Dict with status and result or error
        """
        return self.rails_client.execute(query, timeout)

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

        if result["status"] == "success" and result["output"]:
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

        if result["status"] == "success" and result["output"]:
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
        if self.rails_client:
            return self.rails_client.execute_script(script_content)

        logger.error("Rails client not available for script execution")
        return {"status": "error", "error": "Rails client not available"}

    def execute_script_with_data(self, script_content: str, data: Any) -> Dict[str, Any]:
        """
        Execute a Ruby script with provided data.

        Args:
            script_content: Ruby script content
            data: Data to pass to the script

        Returns:
            Dict with status and output or error
        """
        if self.rails_client:
            return self.rails_client.execute_with_data(script_content, data)

        logger.error("Rails client not available for script execution with data")
        return {"status": "error", "error": "Rails client not available"}

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
        if self.rails_client and hasattr(self.rails_client, 'transfer_file_to_container'):
            # The method exists on Rails client, delegate to it
            result = self.rails_client.transfer_file_to_container(local_path, container_path)
            return bool(result)  # Ensure boolean return

        # Method doesn't exist, use parent's DockerClient via rails_client
        if self.rails_client and hasattr(self.rails_client, 'docker_client'):
            docker_client = getattr(self.rails_client, 'docker_client')
            if hasattr(docker_client, 'copy_file_to_container'):
                result = docker_client.copy_file_to_container(local_path, container_path)
                return bool(result)

        logger.error("Rails client not available for file transfer")
        return False

    def transfer_file_from_container(self, container_path: str, local_path: str) -> bool:
        """
        Copy a file from the container to the local system.

        Args:
            container_path: Path to the file in the container
            local_path: Path where the file should be saved locally

        Returns:
            True if successful, False otherwise
        """
        if self.rails_client and hasattr(self.rails_client, 'transfer_file_from_container'):
            # The method exists on Rails client, delegate to it
            result = self.rails_client.transfer_file_from_container(container_path, local_path)
            return bool(result)  # Ensure boolean return

        # Method doesn't exist, use parent's DockerClient via rails_client
        if self.rails_client and hasattr(self.rails_client, 'docker_client'):
            docker_client = getattr(self.rails_client, 'docker_client')
            if hasattr(docker_client, 'copy_file_from_container'):
                result = docker_client.copy_file_from_container(container_path, local_path)
                return bool(result)

        logger.error("Rails client not available for file transfer")
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
