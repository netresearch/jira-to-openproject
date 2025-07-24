#!/usr/bin/env python3
"""DockerClient.

Manages Docker container interactions, including command execution and file transfers.
Uses SSHClient for remote operations.
Part of the layered client architecture where:
1. SSHClient is the foundation for all SSH operations
2. DockerClient uses SSHClient for remote Docker operations
3. RailsConsoleClient uses DockerClient for container interactions
4. OpenProjectClient coordinates all clients and operations
"""

from pathlib import Path
from shlex import quote

from src.display import configure_logging
from src.clients.ssh_client import SSHClient
from src.utils.file_manager import FileManager

logger = configure_logging("INFO", None)


class DockerClient:
    """Client for interacting with Docker containers on remote servers.

    Part of the layered client architecture where:
    1. OpenProjectClient owns SSHClient, DockerClient, and RailsConsoleClient
    2. DockerClient uses SSHClient for remote Docker operations
    3. RailsConsoleClient uses DockerClient for container interactions.
    """

    def __init__(
        self,
        container_name: str,
        ssh_client: SSHClient,
        command_timeout: int = 60,
        retry_count: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        """Initialize the Docker client.

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
            msg = f"Docker container not found or not running: {container_name}"
            raise ValueError(msg)

        logger.debug("DockerClient initialized for container %s", container_name)

    def check_container_exists(self) -> bool:
        """Check if the specified container exists and is running.

        Returns:
            True if container exists and is running, False otherwise

        Raises:
            Exception: If the SSH command fails

        """
        try:
            # SECURITY FIX: Quote container name to prevent injection
            safe_container_name = quote(self.container_name)
            stdout, _, returncode = self.ssh_client.execute_command(
                f"docker ps --filter name={safe_container_name} --format '{{{{.Names}}}}'",
            )

            if returncode == 0 and self.container_name in stdout:
                logger.debug("Container verified: %s", self.container_name)
                return True

            # Check if container exists but is not running
            stdout, _, returncode = self.ssh_client.execute_command(
                f"docker ps -a --filter name={safe_container_name} --format '{{{{.Names}}}}'",
            )

            if returncode == 0 and self.container_name in stdout:
                logger.warning(
                    "Container exists but is not running: %s",
                    self.container_name,
                )
                return False

            logger.error("Container not found: %s", self.container_name)
            return False

        except Exception as e:
            logger.exception("Error checking if container exists: %s", e)
            raise

    def execute_command(
        self,
        command: str,
        user: str | None = None,
        workdir: Path | str | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[str, str, int]:
        """Execute a command in the Docker container.

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
            docker_cmd.extend(["-u", quote(user)])

        # Add working directory if specified
        if workdir:
            # Convert to Path object if it is a string
            if isinstance(workdir, str):
                workdir = Path(workdir)
            docker_cmd.extend(["-w", quote(workdir.resolve(strict=False).as_posix())])

        # Add environment variables if specified
        if env:
            for key, value in env.items():
                docker_cmd.extend(["-e", f"{quote(key)}={quote(value)}"])

        # Add container name and command
        docker_cmd.append(quote(self.container_name))

        # For complex commands, use bash -c
        docker_cmd.extend(["bash", "-c", quote(command)])

        # Convert list to space-separated string
        docker_cmd_str = " ".join(docker_cmd)

        # Execute via SSH
        return self.ssh_client.execute_command(docker_cmd_str, timeout=timeout)

    def copy_file_to_container(
        self,
        local_path: Path | str,
        container_path: Path | str,
    ) -> None:
        """Copy a file from local machine to the Docker container.

        Args:
            local_path: Path to local file on the remote server
            container_path: Path in container

        Returns:
            None on success

        Raises:
            FileNotFoundError: If the file does not exist or cannot be accessed
            ValueError: If the file transfer fails
            Exception: For other errors during command execution or file transfer

        """
        # Convert to Path objects if they're strings
        local_path = Path(local_path) if isinstance(local_path, str) else local_path
        container_path = (
            Path(container_path) if isinstance(container_path, str) else container_path
        )

        try:
            # Build docker cp command on remote
            cmd = f"docker cp {quote(str(local_path))} {quote(self.container_name)}:{quote(str(container_path))}"

            # Execute the command
            stdout, stderr, returncode = self.ssh_client.execute_command(
                cmd,
                check=True,
                timeout=self.command_timeout,
            )

            # Verify that the file was copied successfully
            if returncode == 0:
                # Check if the file exists in the container
                if not self.check_file_exists_in_container(container_path):
                    logger.error(
                        "File not found in container after copy: %s",
                        container_path,
                    )
                    msg = f"File not found in container after copy: {container_path}"
                    raise ValueError(
                        msg,
                    )

                # Get file size in container
                size = self.get_file_size_in_container(container_path)
                logger.debug(
                    f"Successfully copied file to container: "
                    f"{local_path} -> {container_path} (size: {size} bytes)",
                )
            else:
                logger.error("Failed to copy file to container: %s", stderr)
                msg = f"Failed to copy file to container: {stderr}"
                raise ValueError(msg)

        except FileNotFoundError:
            # Re-raise file not found errors
            raise
        except Exception as e:
            # Check if the error was due to missing local file
            local_file_exists = self.ssh_client.check_remote_file_exists(local_path)
            if not local_file_exists:
                logger.exception("Local file not found: %s", local_path)
                msg = f"Local file does not exist: {local_path}"
                raise FileNotFoundError(
                    msg,
                ) from e

            logger.exception("Error copying file to container: %s", e)
            msg = f"Failed to copy file to container: {e}"
            raise ValueError(msg) from e

    def copy_file_from_container(
        self,
        container_path: Path | str,
        local_path: Path | str,
    ) -> Path:
        """Copy a file from Docker container to local machine.

        Args:
            container_path: Path in container
            local_path: Path to save file locally

        Returns:
            Path to the file on success

        Raises:
            FileNotFoundError: If the file does not exist in the container
            ValueError: If the file transfer fails
            Exception: For other errors during command execution or file transfer

        """
        # Convert to Path objects if they're strings
        container_path = (
            Path(container_path) if isinstance(container_path, str) else container_path
        )
        local_path = Path(local_path) if isinstance(local_path, str) else local_path

        try:
            # Use a temporary file on the remote server for intermediate storage
            import uuid
            temp_filename = f"docker_transfer_{uuid.uuid4().hex}.tmp"
            remote_temp_path = f"/tmp/{temp_filename}"

            # Step 1: Copy from container to temporary location on remote server
            cmd = f"docker cp {quote(self.container_name)}:{quote(str(container_path))} {quote(remote_temp_path)}"

            stdout, stderr, returncode = self.ssh_client.execute_command(
                cmd,
                check=True,
                timeout=self.command_timeout,
            )

            if returncode != 0:
                logger.error("Failed to copy file from container: %s", stderr)
                msg = f"Failed to copy file from container: {stderr}"
                raise ValueError(msg)

            # Step 2: Copy from remote server to actual local path
            try:
                # Ensure local directory exists
                local_path.parent.mkdir(parents=True, exist_ok=True)
                
                # Copy from remote to local
                result_path = self.ssh_client.copy_file_from_remote(
                    remote_temp_path, local_path
                )
                
                logger.debug(
                    f"Successfully copied file from container: "
                    f"{container_path} -> {local_path}",
                )
                
                return result_path
                
            finally:
                # Step 3: Clean up temporary file on remote server
                try:
                    cleanup_cmd = f"rm -f {quote(remote_temp_path)}"
                    self.ssh_client.execute_command(cleanup_cmd, check=False)
                except Exception as cleanup_error:
                    logger.warning(f"Failed to clean up temporary file {remote_temp_path}: {cleanup_error}")

        except FileNotFoundError:
            # Re-raise file not found errors
            raise
        except Exception as e:
            # Check if file exists in container only after failure
            if not self.check_file_exists_in_container(container_path):
                logger.exception("File not found in container: %s", container_path)
                msg = f"File not found in container: {container_path}"
                raise FileNotFoundError(
                    msg,
                ) from e

            logger.exception("Error copying file from container: %s", e)
            msg = f"Failed to copy file from container: {e}"
            raise ValueError(msg) from e

    def check_file_exists_in_container(self, container_path: Path | str) -> bool:
        """Check if a file exists in the Docker container.

        Args:
            container_path: Path to check in container

        Returns:
            True if the file exists, False otherwise

        """
        container_path_str = str(container_path)

        try:
            # Use stat to check if file exists
            # SECURITY FIX: Quote container name to prevent injection
            safe_container_name = quote(self.container_name)
            cmd = (
                f"docker exec {safe_container_name} bash -c "
                f'\'test -e {quote(container_path_str)} && echo "EXISTS" || echo "NOT_EXISTS"\''
            )
            stdout, _, returncode = self.ssh_client.execute_command(cmd, check=False)

            return "EXISTS" in stdout and returncode == 0
        except Exception as e:
            logger.warning("Error checking if file exists in container: %s", e)
            return False

    def get_file_size_in_container(self, container_path: Path | str) -> int | None:
        """Get the size of a file in the Docker container.

        Args:
            container_path: Path to the file in the container

        Returns:
            Size of the file in bytes, or None if the file does not exist or cannot be accessed

        """
        container_path_str = str(container_path)

        try:
            # Use stat to get file size
            # SECURITY FIX: Quote container name to prevent injection
            safe_container_name = quote(self.container_name)
            cmd = (
                f"docker exec {safe_container_name} bash -c "
                f"'stat -c %s {quote(container_path_str)} 2>/dev/null || echo \"NOT_EXISTS\"'"
            )
            stdout, _, returncode = self.ssh_client.execute_command(cmd, check=False)

            if "NOT_EXISTS" in stdout or returncode != 0:
                return None

            try:
                return int(stdout.strip())
            except ValueError:
                logger.warning(
                    "Invalid file size returned for %s: %s",
                    container_path,
                    stdout.strip(),
                )
                return None
        except Exception as e:
            logger.warning("Error getting file size in container: %s", e)
            return None

    def run_container(
        self,
        image: str,
        name: str | None = None,
        user: str | None = None,
        environment: dict[str, str] | None = None,
        volumes: dict[str, str] | None = None,
        ports: dict[str, str] | None = None,
        cpu_limit: str | None = None,
        memory_limit: str | None = None,
        network: str | None = None,
        detach: bool = True,
        remove: bool = False,
        command: str | None = None,
    ) -> tuple[str, str, int]:
        """Run a new Docker container with security and resource constraints.

        Args:
            image: Docker image to run
            name: Container name (optional)
            user: User to run as (e.g., "1000:1000" for UID:GID)
            environment: Environment variables
            volumes: Volume mappings (host_path: container_path)
            ports: Port mappings (host_port: container_port)
            cpu_limit: CPU limit (e.g., "0.5" for half core)
            memory_limit: Memory limit (e.g., "512m" for 512MB)
            network: Network to connect to
            detach: Run in detached mode
            remove: Remove container when it exits
            command: Command to run in container

        Returns:
            Tuple of (stdout, stderr, returncode)

        Raises:
            Exception: If the container fails to start

        """
        # INPUT VALIDATION: Validate critical parameters to prevent errors
        import re
        
        if user and not re.match(r"^[\w]+:?[\w]*$", user):
            raise ValueError(f"Invalid user format: {user}. Expected format: 'user' or 'uid:gid'")
        
        if memory_limit and not re.match(r"^\d+[mgMGT]?$", memory_limit):
            raise ValueError(f"Invalid memory_limit format: {memory_limit}. Expected format: '512m', '1g', etc.")
        
        if cpu_limit and not re.match(r"^\d*\.?\d+$", cpu_limit):
            raise ValueError(f"Invalid cpu_limit format: {cpu_limit}. Expected format: '0.5', '1', '2.0', etc.")
        
        # Build docker run command
        docker_cmd = ["docker", "run"]

        # Add user if specified
        if user:
            docker_cmd.extend(["--user", quote(user)])

        # Add resource limits
        if cpu_limit:
            docker_cmd.extend(["--cpus", quote(cpu_limit)])
        if memory_limit:
            docker_cmd.extend(["--memory", quote(memory_limit)])

        # Add container name
        if name:
            docker_cmd.extend(["--name", quote(name)])

        # Add environment variables
        if environment:
            for key, value in environment.items():
                docker_cmd.extend(["-e", f"{quote(key)}={quote(value)}"])

        # Add volume mappings
        if volumes:
            for host_path, container_path in volumes.items():
                docker_cmd.extend(["-v", f"{quote(host_path)}:{quote(container_path)}"])

        # Add port mappings
        if ports:
            for host_port, container_port in ports.items():
                docker_cmd.extend(["-p", f"{quote(host_port)}:{quote(container_port)}"])

        # Add network
        if network:
            docker_cmd.extend(["--network", quote(network)])

        # Add flags
        if detach:
            docker_cmd.append("-d")
        if remove:
            docker_cmd.append("--rm")

        # Add image
        docker_cmd.append(quote(image))

        # Add command if specified
        if command:
            # SECURITY FIX: Quote command to prevent injection
            docker_cmd.extend(["bash", "-c", quote(command)])

        # Convert list to space-separated string
        docker_cmd_str = " ".join(docker_cmd)

        # Execute via SSH
        return self.ssh_client.execute_command(docker_cmd_str, timeout=self.command_timeout)

    def get_container_info(self) -> dict[str, str]:
        """Get information about the container including user and resource settings.

        Returns:
            Dictionary with container information

        Raises:
            Exception: If the inspection fails

        """
        try:
            # Get container inspection info
            # SECURITY FIX: Quote container name to prevent injection
            cmd = f"docker inspect {quote(self.container_name)}"
            stdout, stderr, returncode = self.ssh_client.execute_command(cmd)

            if returncode != 0:
                logger.error("Failed to inspect container: %s", stderr)
                msg = f"Failed to inspect container: {stderr}"
                raise ValueError(msg)

            import json
            inspect_data = json.loads(stdout)
            if not inspect_data:
                msg = f"No data returned for container: {self.container_name}"
                raise ValueError(msg)

            container_info = inspect_data[0]

            # Extract relevant security and resource information
            config = container_info.get("Config", {})
            host_config = container_info.get("HostConfig", {})

            return {
                "user": config.get("User", "root"),
                "image": config.get("Image", "unknown"),
                "cpu_shares": str(host_config.get("CpuShares", 0)),
                "memory": str(host_config.get("Memory", 0)),
                "memory_swap": str(host_config.get("MemorySwap", 0)),
                "cpu_quota": str(host_config.get("CpuQuota", 0)),
                "cpu_period": str(host_config.get("CpuPeriod", 0)),
                "security_opt": host_config.get("SecurityOpt", []),
                "readonly_rootfs": str(host_config.get("ReadonlyRootfs", False)),
            }

        except Exception as e:
            logger.exception("Error getting container info: %s", e)
            msg = f"Failed to get container info: {e}"
            raise ValueError(msg) from e

    def transfer_file_to_container(
        self,
        local_path: Path | str,
        container_path: Path | str,
    ) -> None:
        """Transfer a file directly from the local machine to the Docker container in one operation.

        This method handles:
        1. Copying the file from local machine to the remote server (via SSH)
        2. Copying the file from remote server to the container
        3. Setting proper permissions in the container using root user.

        Args:
            local_path: Path to file on local machine
            container_path: Destination path in container

        Raises:
            ValueError: If transfer fails at any step

        """
        # Convert to Path objects if they are strings
        local_path = Path(local_path) if isinstance(local_path, str) else local_path
        container_path = (
            Path(container_path) if isinstance(container_path, str) else container_path
        )

        # Use a temporary path on the remote server
        remote_temp_path = Path(f"/tmp/{local_path.name}")

        try:
            # Step 1: Transfer from local to remote server via SSH
            logger.debug(
                "Copying file from local to remote: %s -> %s",
                local_path,
                remote_temp_path,
            )
            self.ssh_client.copy_file_to_remote(local_path, remote_temp_path)

            # Verify remote file exists
            if not self.ssh_client.check_remote_file_exists(remote_temp_path):
                msg = f"File not found on remote server after transfer: {remote_temp_path}"
                raise ValueError(msg)

            # Step 2: Transfer from remote server to container
            logger.debug(
                "Copying file from remote to container: %s -> %s",
                remote_temp_path,
                container_path,
            )
            self.copy_file_to_container(remote_temp_path, container_path)

            # Step 3: Set proper permissions as root to ensure Rails can read it
            logger.debug("Setting permissions on file in container: %s", container_path)
            stdout, stderr, rc = self.execute_command(
                f"chmod 644 {container_path}",
                user="root",  # Execute as root user
            )

            if rc != 0:
                logger.warning("Failed to set permissions in container: %s", stderr)
                # We continue anyway since the file might still be usable

            # Step 4: Verify file exists and is readable in container
            if not self.check_file_exists_in_container(container_path):
                msg = f"File not found in container after transfer: {container_path}"
                raise ValueError(msg)

            logger.debug(
                "Successfully transferred file from local to container: %s -> %s",
                local_path,
                container_path,
            )

        finally:
            # Clean up the temporary file on the remote server
            try:
                self.ssh_client.execute_command(f"rm -f {remote_temp_path.as_posix()}")
            except Exception:
                # Non-critical error, just log it
                logger.exception(
                    "Failed to clean up temporary file: %s",
                    remote_temp_path,
                )
