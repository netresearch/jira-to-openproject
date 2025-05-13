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
import random

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
        ssh_client: SSHClient,
        command_timeout: int = 60,
        retry_count: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        """
        Initialize the Docker client.

        Args:
            container_name: Name or ID of the Docker container
            ssh_client: SSHClient instance for remote operations (required)
            command_timeout: Default timeout for commands in seconds
            retry_count: Number of retries for operations
            retry_delay: Delay between retries in seconds
        """
        self.container_name = container_name
        self.command_timeout = command_timeout
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        self.ssh_client = ssh_client

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

        Raises:
            Exception: If the SSH command fails
        """
        try:
            stdout, _, returncode = self.ssh_client.execute_command(
                f"docker ps --filter name={self.container_name} --format '{{{{.Names}}}}'"
            )

            if returncode == 0 and self.container_name in stdout:
                logger.debug(f"Container verified: {self.container_name}")
                return True

            # Check if container exists but is not running
            stdout, _, returncode = self.ssh_client.execute_command(
                f"docker ps -a --filter name={self.container_name} --format '{{{{.Names}}}}'"
            )

            if returncode == 0 and self.container_name in stdout:
                logger.warning(f"Container exists but is not running: {self.container_name}")
                return False

            logger.error(f"Container not found: {self.container_name}")
            return False

        except Exception as e:
            logger.error(f"Error checking if container exists: {str(e)}")
            raise

    def execute_command(
        self,
        command: str,
        user: str | None = None,
        workdir: str | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[str, str, int]:
        """
        Execute a command in the Docker container.

        Args:
            command: Command to execute
            user: User to run as (default: container's default user)
            workdir: Working directory (default: container's default)
            timeout: Command timeout in seconds (default: self.command_timeout)
            env: Environment variables to set

        Returns:
            Tuple of (stdout, stderr, returncode)

        Raises:
            Exception: If the SSH command fails
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

    def copy_file_to_container(self, local_path: str, container_path: str) -> None:
        """
        Copy a file from local machine to the Docker container.

        Args:
            local_path: Path to local file on the remote server
            container_path: Path in container

        Returns:
            None on success

        Raises:
            ValueError: If the file transfer fails
            Exception: For other errors during command execution or file transfer
        """
        # We don't validate if the remote file exists, since this method is called
        # with a path on the remote server, not on the local machine

        try:
            # Copy from remote host to container (we assume the file exists on remote host)
            logger.debug(f"Copying file from remote host to container: {local_path} -> {container_path}")

            # Create docker cp command
            docker_cp_cmd = f"docker cp {local_path} {self.container_name}:{container_path}"

            # Execute via SSH
            stdout, stderr, returncode = self.ssh_client.execute_command(docker_cp_cmd)

            if returncode != 0:
                logger.error(f"Docker cp failed: {stderr}")
                raise ValueError(f"Failed to copy file to container: {stderr}")

            # Verify the file exists in the container
            exists_cmd = (
                f"docker exec {self.container_name} bash -c "
                f"\"test -e {container_path} && echo 'EXISTS' || echo 'NOT_EXISTS'\""
            )
            stdout, _, _ = self.ssh_client.execute_command(exists_cmd)

            if "EXISTS" not in stdout:
                logger.error(f"File not found in container after docker cp: {container_path}")
                raise ValueError(f"File not found in container after copy: {container_path}")

            logger.debug(f"File copied successfully to container: {container_path}")
            return

        except Exception as e:
            logger.error(f"Error copying file to container: {str(e)}")
            raise ValueError(f"Failed to copy file to container: {str(e)}")

    def copy_file_from_container(self, container_path: str, local_path: str) -> str:
        """
        Copy a file from the Docker container to the local system.
        This uses a direct approach to copy the file from container to local.

        Args:
            container_path: Path to the file in the container
            local_path: Path where to save the file locally

        Returns:
            Path to the downloaded file

        Raises:
            FileNotFoundError: If the file doesn't exist in container or isn't created locally
            ValueError: If the file transfer fails
            Exception: For other errors during command execution or file transfer
        """
        # Create a unique temporary filename on the remote host
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        unique_id = f"{random.randrange(16**6):06x}"
        remote_temp_path = f"/tmp/{timestamp}_{unique_id}_{os.path.basename(container_path)}"

        try:
            # Step 1: Check if file exists in container
            exists_cmd = (
                f"docker exec {self.container_name} bash -c "
                f"\"test -e {container_path} && echo 'EXISTS' || echo 'NOT_EXISTS'\""
            )
            stdout, _, _ = self.ssh_client.execute_command(exists_cmd)

            if "EXISTS" not in stdout:
                logger.error(f"File not found in container: {container_path}")
                raise FileNotFoundError(f"File not found in container: {container_path}")

            # Step 2: Copy from container to remote host
            docker_cmd = f"docker cp {self.container_name}:{container_path} {remote_temp_path}"
            logger.debug(f"Running docker cp command: {docker_cmd}")
            stdout, stderr, returncode = self.ssh_client.execute_command(docker_cmd)

            if returncode != 0:
                logger.error(f"Docker cp failed: {stderr}")
                raise ValueError(f"Failed to copy file from container: {stderr}")

            # Step 3: Verify the file exists on remote host
            verify_cmd = f"test -e {remote_temp_path} && echo 'EXISTS' || echo 'NOT_EXISTS'"
            stdout, _, _ = self.ssh_client.execute_command(verify_cmd)

            if "EXISTS" not in stdout:
                logger.error(f"File not found on remote host after docker cp: {remote_temp_path}")
                raise FileNotFoundError(f"File not found on remote host after docker cp: {remote_temp_path}")

            # Step 4: Use SCP to copy the file from remote to local
            logger.debug(f"Copying file from remote to local: {remote_temp_path} -> {local_path}")
            local_file_path = self.ssh_client.copy_file_from_remote(remote_temp_path, local_path)

            # Step 5: Verify local file exists and has content
            if not os.path.exists(local_path):
                logger.error(f"Local file not found after copy: {local_path}")
                raise FileNotFoundError(f"Local file not found after copy: {local_path}")

            local_size = os.path.getsize(local_path)
            logger.debug(f"File copied successfully: {local_path} ({local_size} bytes)")

            return local_file_path

        finally:
            # Clean up the temporary file on the remote host
            try:
                self.ssh_client.execute_command(f"rm -f {remote_temp_path}", check=False)
            except Exception:
                # Non-critical error, just log it
                logger.warning(f"Failed to clean up temporary file: {remote_temp_path}")

    def check_file_exists_in_container(self, container_path: str) -> bool:
        """
        Check if a file exists in the container.

        Args:
            container_path: Path in container

        Returns:
            True if file exists, False otherwise
        """
        try:
            stdout, _, _ = self.execute_command(f"test -e {container_path} && echo 'EXISTS' || echo 'NOT_EXISTS'")
            return "EXISTS" in stdout
        except Exception as e:
            logger.error(f"Error checking if file exists in container: {str(e)}")
            return False

    def get_file_size_in_container(self, container_path: str) -> int | None:
        """
        Get the size of a file in the container.

        Args:
            container_path: Path in container

        Returns:
            File size in bytes or None if file doesn't exist
        """
        try:
            stdout, _, _ = self.execute_command(f"stat -c %s {container_path} 2>/dev/null || echo 'NOT_EXISTS'")

            if "NOT_EXISTS" not in stdout:
                try:
                    return int(stdout.strip())
                except ValueError:
                    logger.warning(f"Invalid file size returned for {container_path}: {stdout.strip()}")
                    return None
            return None
        except Exception as e:
            logger.error(f"Error getting file size in container: {str(e)}")
            return None

    def transfer_file_to_container(self, local_path: str, container_path: str) -> None:
        """
        Transfer a file directly from the local machine to the Docker container in one operation.
        This method handles:
        1. Copying the file from local machine to the remote server (via SSH)
        2. Copying the file from remote server to the container
        3. Setting proper permissions in the container using root user

        Args:
            local_path: Path to file on local machine
            container_path: Destination path in container

        Raises:
            ValueError: If transfer fails at any step
        """
        # Use a temporary path on the remote server
        remote_temp_path = f"/tmp/{os.path.basename(local_path)}"

        try:
            # Step 1: Transfer from local to remote server via SSH
            logger.debug(f"Copying file from local to remote: {local_path} -> {remote_temp_path}")
            self.ssh_client.copy_file_to_remote(local_path, remote_temp_path)

            # Verify remote file exists
            if not self.ssh_client.check_remote_file_exists(remote_temp_path):
                raise ValueError(f"File not found on remote server after transfer: {remote_temp_path}")

            # Step 2: Transfer from remote server to container
            logger.debug(f"Copying file from remote to container: {remote_temp_path} -> {container_path}")
            self.copy_file_to_container(remote_temp_path, container_path)

            # Step 3: Set proper permissions as root to ensure Rails can read it
            logger.debug(f"Setting permissions on file in container: {container_path}")
            stdout, stderr, rc = self.execute_command(
                f"chmod 644 {container_path}",
                user="root"  # Execute as root user
            )

            if rc != 0:
                logger.warning(f"Failed to set permissions in container: {stderr}")
                # We continue anyway since the file might still be usable

            # Step 4: Verify file exists and is readable in container
            if not self.check_file_exists_in_container(container_path):
                raise ValueError(f"File not found in container after transfer: {container_path}")

            logger.debug(f"Successfully transferred file from local to container: {local_path} -> {container_path}")

        finally:
            # Clean up the temporary file on the remote server
            try:
                self.ssh_client.execute_command(f"rm -f {remote_temp_path}")
            except Exception as e:
                # Non-critical error, just log it
                logger.warning(f"Failed to clean up temporary file: {remote_temp_path}, Error: {str(e)}")
