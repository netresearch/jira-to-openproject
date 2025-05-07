#!/usr/bin/env python3
"""
DockerClient

Manages Docker container interactions, including command execution and file transfers.
Uses SSHClient for remote operations.
Part of the layered client architecture where:
1. SSHClient is the foundation for all SSH operations
2. DockerClient uses SSHClient for remote Docker operations
3. RailsConsoleClient uses DockerClient for container interactions
4. OpenProjectClient coordinates all clients and operations
"""

import os
import time
from typing import Any, Optional

from src import config
from src.clients.ssh_client import SSHClient
from src.utils.file_manager import FileManager

logger = config.logger


class DockerClient:
    """
    Client for interacting with Docker containers on remote servers.
    Part of the layered client architecture where:
    1. OpenProjectClient owns SSHClient, DockerClient, and RailsConsoleClient
    2. DockerClient uses SSHClient for remote Docker operations
    3. RailsConsoleClient uses DockerClient for container interactions
    """

    def __init__(
        self,
        container_name: str,
        ssh_client: Optional[SSHClient] = None,
        ssh_host: Optional[str] = None,
        ssh_user: Optional[str] = None,
        ssh_key_file: Optional[str] = None,
        command_timeout: int = 60,
        retry_count: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        """
        Initialize the Docker client.

        Args:
            container_name: Name or ID of the Docker container
            ssh_client: Existing SSHClient instance (preferred method)
            ssh_host: SSH host where Docker is running (used only if ssh_client is None)
            ssh_user: SSH username (used only if ssh_client is None)
            ssh_key_file: Path to SSH key file (used only if ssh_client is None)
            command_timeout: Default timeout for commands in seconds
            retry_count: Number of retries for operations
            retry_delay: Delay between retries in seconds
        """
        self.container_name = container_name
        self.command_timeout = command_timeout
        self.retry_count = retry_count
        self.retry_delay = retry_delay

        # Use provided SSHClient or create a new one
        if ssh_client:
            self.ssh_client = ssh_client
            logger.debug("Using provided SSHClient")
        else:
            # Require ssh_host parameter if ssh_client not provided
            if not ssh_host:
                raise ValueError("Either ssh_client or ssh_host must be provided")

            # Initialize SSH client
            self.ssh_client = SSHClient(
                host=ssh_host,
                user=ssh_user,
                key_file=ssh_key_file,
                operation_timeout=command_timeout,
                retry_count=retry_count,
                retry_delay=retry_delay
            )
            logger.debug(f"Created new SSHClient for host {ssh_host}")

        # Get file manager instance
        self.file_manager = FileManager()

        # Verify container exists
        if not self.check_container_exists():
            raise ValueError(f"Docker container not found or not running: {container_name}")

        logger.debug(f"DockerClient initialized for container {container_name}")

    def check_container_exists(self) -> bool:
        """
        Check if the specified container exists and is running.

        Returns:
            True if container exists and is running, False otherwise
        """
        result = self.ssh_client.execute_command(
            f"docker ps --filter name={self.container_name} --format '{{{{.Names}}}}'"
        )

        if result["status"] == "success" and self.container_name in result["stdout"]:
            logger.debug(f"Container verified: {self.container_name}")
            return True

        # Check if container exists but is not running
        result = self.ssh_client.execute_command(
            f"docker ps -a --filter name={self.container_name} --format '{{{{.Names}}}}'"
        )

        if result["status"] == "success" and self.container_name in result["stdout"]:
            logger.warning(f"Container exists but is not running: {self.container_name}")
            return False

        logger.error(f"Container not found: {self.container_name}")
        return False

    def execute_command(
        self,
        command: str,
        user: str | None = None,
        workdir: str | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Execute a command in the Docker container.

        Args:
            command: Command to execute
            user: User to run as (default: container's default user)
            workdir: Working directory (default: container's default)
            timeout: Command timeout in seconds (default: self.command_timeout)
            env: Environment variables to set

        Returns:
            Dict with keys: status, stdout, stderr, returncode
        """
        if timeout is None:
            timeout = self.command_timeout

        # Build docker exec command
        docker_cmd = ["docker", "exec"]

        # Add user if specified
        if user:
            docker_cmd.extend(["-u", user])

        # Add working directory if specified
        if workdir:
            docker_cmd.extend(["-w", workdir])

        # Add environment variables if specified
        if env:
            for key, value in env.items():
                docker_cmd.extend(["-e", f"{key}={value}"])

        # Add container name and command
        docker_cmd.append(self.container_name)

        # For complex commands, use bash -c
        if " " in command or ";" in command or "|" in command or ">" in command:
            # Escape quotes in the command
            escaped_command = command.replace('"', '\\"')
            docker_cmd.extend(["bash", "-c", f'"{escaped_command}"'])
        else:
            # Simple command can be passed directly
            docker_cmd.append(command)

        # Convert list to space-separated string
        docker_cmd_str = " ".join(docker_cmd)

        # Execute via SSH
        return self.ssh_client.execute_command(docker_cmd_str, timeout=timeout)

    def copy_file_to_container(self, local_path: str, container_path: str) -> dict[str, Any]:
        """
        Copy a file from local machine to the Docker container.

        Args:
            local_path: Path to local file
            container_path: Path in container

        Returns:
            Dict with status and error information
        """
        # Strategy: first copy to remote host, then to container
        try:
            # Validate local file exists
            if not os.path.exists(local_path):
                return {
                    "status": "error",
                    "error": f"Local file does not exist: {local_path}"
                }

            # Generate a unique filename for the intermediate file on the remote host
            unique_id = self.file_manager.generate_unique_id()
            remote_temp_path = f"/tmp/{unique_id}_{os.path.basename(local_path)}"

            # Step 1: Copy file to remote host
            logger.debug(f"Copying file to remote host: {local_path} -> {remote_temp_path}")

            result = self.ssh_client.copy_file_to_remote(local_path, remote_temp_path)
            if result["status"] != "success":
                return {
                    "status": "error",
                    "error": f"Failed to copy file to remote host: {result.get('error', 'Unknown error')}"
                }

            # Step 2: Copy from remote host to container
            logger.debug(f"Copying file from remote host to container: {remote_temp_path} -> {container_path}")

            # Create docker cp command
            docker_cp_cmd = f"docker cp {remote_temp_path} {self.container_name}:{container_path}"

            # Execute via SSH
            result = self.ssh_client.execute_command(docker_cp_cmd)

            # Step 3: Cleanup remote temp file (in background, don't care about result)
            cleanup_cmd = f"rm -f {remote_temp_path} &>/dev/null"
            self.ssh_client.execute_command(cleanup_cmd, check=False)

            if result["status"] == "success":
                logger.debug(f"File copied successfully to container: {container_path}")
                return {"status": "success"}
            else:
                return {
                    "status": "error",
                    "error":
                        "Failed to copy file to container:"
                        f" {result.get('error', result.get('stderr', 'Unknown error'))}"
                }

        except Exception as e:
            logger.error(f"Error copying file to container: {str(e)}")
            return {
                "status": "error",
                "error": str(e)
            }

    def copy_file_from_container(
        self,
        container_path: str,
        local_path: str,
        retries: int | None = None,
    ) -> dict[str, Any]:
        """
        Copy a file from the Docker container to the local machine.

        Args:
            container_path: Path in container
            local_path: Path to save file locally
            retries: Number of retries (default: self.retry_count)

        Returns:
            Dict with status and error information
        """
        if retries is None:
            retries = self.retry_count

        # Strategy: first copy from container to remote host, then to local
        for attempt in range(retries):
            if attempt > 0:
                logger.warning(f"Retrying file copy from container (attempt {attempt+1}/{retries})")
                time.sleep(self.retry_delay * attempt)

            try:
                # Generate a unique filename for the intermediate file on the remote host
                unique_id = self.file_manager.generate_unique_id()
                remote_temp_path = f"/tmp/{unique_id}_{os.path.basename(container_path)}"

                # Step 1: Check if file exists in container
                check_result = self.execute_command(f"test -e {container_path} && echo 'EXISTS' || echo 'NOT_EXISTS'")

                if "NOT_EXISTS" in check_result.get("stdout", ""):
                    # Skip retry if file definitely doesn't exist
                    return {
                        "status": "error",
                        "error": f"File does not exist in container: {container_path}"
                    }

                # Step 2: Copy from container to remote host
                logger.debug(f"Copying file from container to remote host: {container_path} -> {remote_temp_path}")

                # Create docker cp command
                docker_cp_cmd = f"docker cp {self.container_name}:{container_path} {remote_temp_path}"

                # Execute via SSH
                result = self.ssh_client.execute_command(docker_cp_cmd)

                if result["status"] != "success":
                    if attempt < retries - 1:
                        continue
                    return {
                        "status": "error",
                        "error":
                            "Failed to copy file from container:"
                            f" {result.get('error', result.get('stderr', 'Unknown error'))}"
                    }

                # Step 3: Check if file was copied to remote host
                check_result = self.ssh_client.execute_command(
                    f"test -e {remote_temp_path} && echo 'EXISTS' || echo 'NOT_EXISTS'"
                )

                if "NOT_EXISTS" in check_result.get("stdout", ""):
                    if attempt < retries - 1:
                        continue
                    return {
                        "status": "error",
                        "error": f"File was not copied to remote host: {remote_temp_path}"
                    }

                # Step 4: Copy from remote host to local
                logger.debug(f"Copying file from remote host to local: {remote_temp_path} -> {local_path}")

                result = self.ssh_client.copy_file_from_remote(remote_temp_path, local_path)

                # Step 5: Cleanup remote temp file (in background, don't care about result)
                cleanup_cmd = f"rm -f {remote_temp_path} &>/dev/null"
                self.ssh_client.execute_command(cleanup_cmd, check=False)

                if result["status"] == "success":
                    if os.path.exists(local_path):
                        file_size = os.path.getsize(local_path)
                        logger.debug(
                            "File copied successfully from container:"
                            f" {container_path} -> {local_path} ({file_size} bytes)"
                        )
                        return {
                            "status": "success",
                            "local_path": local_path,
                            "file_size": file_size
                        }

                if attempt < retries - 1:
                    continue
                return {
                    "status": "error",
                    "error": f"Failed to copy file from remote host: {result.get('error', 'Unknown error')}"
                }

            except Exception as e:
                logger.error(f"Error copying file from container (attempt {attempt+1}): {str(e)}")
                if attempt < retries - 1:
                    continue
                return {
                    "status": "error",
                    "error": str(e)
                }

        # This should never be reached, but just in case
        return {
            "status": "error",
            "error": "Failed to copy file after all retry attempts"
        }

    def check_file_exists_in_container(self, container_path: str) -> bool:
        """
        Check if a file exists in the container.

        Args:
            container_path: Path in container

        Returns:
            True if file exists, False otherwise
        """
        result = self.execute_command(f"test -e {container_path} && echo 'EXISTS' || echo 'NOT_EXISTS'")

        if result["status"] == "success" and "EXISTS" in result["stdout"]:
            return True
        return False

    def get_file_size_in_container(self, container_path: str) -> int | None:
        """
        Get the size of a file in the container.

        Args:
            container_path: Path in container

        Returns:
            File size in bytes or None if file doesn't exist
        """
        result = self.execute_command(f"stat -c %s {container_path} 2>/dev/null || echo 'NOT_EXISTS'")

        if result["status"] == "success" and "NOT_EXISTS" not in result["stdout"]:
            try:
                return int(result["stdout"].strip())
            except ValueError:
                return None
        return None
