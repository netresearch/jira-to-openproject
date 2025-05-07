#!/usr/bin/env python3
"""
SSHClient

Handles SSH operations to remote servers, including command execution and file transfers.
This is the foundation of the layered client architecture:
1. SSHClient - Base component for SSH operations
2. DockerClient - Uses SSHClient for remote Docker operations
3. RailsConsoleClient - Uses DockerClient for container interactions
4. OpenProjectClient - Coordinates all clients and operations
"""

import os
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional

from src import config
from src.utils.file_manager import FileManager

logger = config.logger


class SSHClient:
    """
    Client for SSH operations to remote servers.
    Provides the foundation for all remote operations in the client architecture.
    """

    def __init__(
        self,
        host: str,
        user: Optional[str] = None,
        key_file: Optional[str] = None,
        connect_timeout: int = 10,
        operation_timeout: int = 60,
        retry_count: int = 3,
        retry_delay: float = 1.0,
        auto_reconnect: bool = True
    ) -> None:
        """
        Initialize the SSH client.

        Args:
            host: SSH host (hostname or IP)
            user: SSH username (default: current user)
            key_file: Path to SSH key file (default: use SSH agent)
            connect_timeout: Connection timeout in seconds
            operation_timeout: Default timeout for operations
            retry_count: Number of retries for operations
            retry_delay: Delay between retries in seconds
            auto_reconnect: Automatically reconnect if connection fails
        """
        self.host = host
        self.user = user
        self.key_file = key_file
        self.connect_timeout = connect_timeout
        self.operation_timeout = operation_timeout
        self.retry_count = retry_count
        self.retry_delay = retry_delay
        self.auto_reconnect = auto_reconnect
        self._connected = False

        # Get file manager instance
        self.file_manager = FileManager()

        # Test connection
        if not self.connect():
            raise ConnectionError(f"Failed to connect to SSH host: {self.host}")

        logger.debug(f"SSHClient initialized for host {self.host}")

    def connect(self) -> bool:
        """
        Establish connection to the SSH host.

        Returns:
            True if connection successful, False otherwise
        """
        return self.test_connection()

    def get_ssh_base_command(self) -> List[str]:
        """
        Get the base SSH command with common options.

        Returns:
            List of command parts
        """
        cmd = ["ssh"]

        # Add connect timeout
        cmd.extend(["-o", f"ConnectTimeout={self.connect_timeout}"])

        # Add key file if specified
        if self.key_file:
            cmd.extend(["-i", self.key_file])

        # Add user and host
        if self.user:
            cmd.append(f"{self.user}@{self.host}")
        else:
            cmd.append(f"{self.host}")

        return cmd

    def get_scp_base_command(self) -> List[str]:
        """
        Get the base SCP command with common options.

        Returns:
            List of command parts
        """
        cmd = ["scp"]

        # Add connect timeout
        cmd.extend(["-o", f"ConnectTimeout={self.connect_timeout}"])

        # Add key file if specified
        if self.key_file:
            cmd.extend(["-i", self.key_file])

        return cmd

    def test_connection(self) -> bool:
        """
        Test SSH connection to the remote host.

        Returns:
            True if connection successful, False otherwise
        """
        try:
            cmd = self.get_ssh_base_command()
            cmd.extend(["-o", "BatchMode=yes", "echo", "Connection successful"])

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.connect_timeout
            )

            self._connected = result.returncode == 0 and "Connection successful" in result.stdout
            return self._connected

        except subprocess.SubprocessError as e:
            logger.error(f"SSH connection test failed: {str(e)}")
            self._connected = False
            return False
        except Exception as e:
            logger.error(f"Unexpected error testing SSH connection: {str(e)}")
            self._connected = False
            return False

    def is_connected(self) -> bool:
        """
        Check if the client is currently connected.

        Returns:
            True if connected, False otherwise
        """
        return self._connected

    def execute_command(
        self,
        command: str,
        timeout: Optional[int] = None,
        check: bool = True,
        retry: bool = True
    ) -> Dict[str, Any]:
        """
        Execute a command on the remote host.

        Args:
            command: Command to execute
            timeout: Command timeout in seconds (default: self.operation_timeout)
            check: Whether to check the return code
            retry: Whether to retry on failure

        Returns:
            Dict with keys: status, stdout, stderr, returncode
        """
        if timeout is None:
            timeout = self.operation_timeout

        # Retry logic
        max_attempts = self.retry_count if retry else 1
        for attempt in range(max_attempts):
            if attempt > 0:
                logger.debug(f"Retrying command (attempt {attempt+1}/{max_attempts}): {command}")
                time.sleep(self.retry_delay * attempt)

                # If auto-reconnect is enabled and we're not connected, try to reconnect
                if self.auto_reconnect and not self._connected:
                    logger.debug("Attempting to reconnect before retry")
                    self.connect()

            try:
                cmd = self.get_ssh_base_command()
                cmd.append(command)

                logger.debug(f"Executing SSH command: {' '.join(cmd)}")

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=check
                )

                # Update connection status based on command success
                self._connected = True

                return {
                    "status": "success" if result.returncode == 0 else "error",
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode
                }

            except subprocess.TimeoutExpired:
                logger.exception(f"SSH command timed out after {timeout} seconds")
                self._connected = False
                if attempt < max_attempts - 1:
                    continue
                return {
                    "status": "error",
                    "error": f"Command timed out after {timeout} seconds",
                    "returncode": -1
                }
            except subprocess.CalledProcessError as e:
                logger.exception(f"SSH command failed with exit code {e.returncode}: {e.stderr}")
                if check and attempt < max_attempts - 1:
                    continue
                return {
                    "status": "error",
                    "error": str(e),
                    "stdout": e.stdout,
                    "stderr": e.stderr,
                    "returncode": e.returncode
                }
            except Exception as e:
                logger.exception(f"Unexpected error executing SSH command: {str(e)}")
                self._connected = False
                if attempt < max_attempts - 1:
                    continue
                return {
                    "status": "error",
                    "error": str(e),
                    "returncode": -1
                }

    def copy_file_to_remote(
        self,
        local_path: str,
        remote_path: str,
        retry: bool = True
    ) -> Dict[str, Any]:
        """
        Copy a file from local to remote host using scp.

        Args:
            local_path: Path to local file
            remote_path: Path on remote host
            retry: Whether to retry on failure

        Returns:
            Dict with status and error information
        """
        # Retry logic
        max_attempts = self.retry_count if retry else 1
        for attempt in range(max_attempts):
            if attempt > 0:
                logger.debug(f"Retrying file copy (attempt {attempt+1}/{max_attempts}): {local_path} -> {remote_path}")
                time.sleep(self.retry_delay * attempt)

                # If auto-reconnect is enabled and we're not connected, try to reconnect
                if self.auto_reconnect and not self._connected:
                    logger.debug("Attempting to reconnect before retry")
                    self.connect()

            try:
                # Validate local file exists
                if not os.path.exists(local_path):
                    return {
                        "status": "error",
                        "error": f"Local file does not exist: {local_path}"
                    }

                # Build scp command
                cmd = self.get_scp_base_command()

                # Add source and destination
                cmd.append(local_path)

                if self.user:
                    cmd.append(f"{self.user}@{self.host}:{remote_path}")
                else:
                    cmd.append(f"{self.host}:{remote_path}")

                logger.debug(f"Executing SCP command: {' '.join(cmd)}")

                # Execute scp command
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.operation_timeout,
                    check=True
                )

                # Verify the command was successful
                if result.returncode == 0:
                    logger.debug(f"File copied successfully: {local_path} -> {remote_path}")
                    self._connected = True
                    return {
                        "status": "success"
                    }
                else:
                    if attempt < max_attempts - 1:
                        continue
                    return {
                        "status": "error",
                        "error": f"SCP failed with exit code {result.returncode}",
                        "stderr": result.stderr
                    }

            except subprocess.CalledProcessError as e:
                logger.error(f"SCP command failed: {e.stderr}")
                if attempt < max_attempts - 1:
                    continue
                return {
                    "status": "error",
                    "error": str(e),
                    "stderr": e.stderr
                }
            except Exception as e:
                logger.error(f"Unexpected error during file copy: {str(e)}")
                self._connected = False
                if attempt < max_attempts - 1:
                    continue
                return {
                    "status": "error",
                    "error": str(e)
                }

    def copy_file_from_remote(
        self,
        remote_path: str,
        local_path: str,
        retry: bool = True
    ) -> Dict[str, Any]:
        """
        Copy a file from remote host to local using scp.

        Args:
            remote_path: Path on remote host
            local_path: Path to save file locally
            retry: Whether to retry on failure

        Returns:
            Dict with status and error information
        """
        # Retry logic
        max_attempts = self.retry_count if retry else 1
        for attempt in range(max_attempts):
            if attempt > 0:
                logger.debug(
                    f"Retrying file download (attempt {attempt+1}/{max_attempts}): "
                    f"{remote_path} -> {local_path}"
                )
                time.sleep(self.retry_delay * attempt)

                # If auto-reconnect is enabled and we're not connected, try to reconnect
                if self.auto_reconnect and not self._connected:
                    logger.debug("Attempting to reconnect before retry")
                    self.connect()

            try:
                # Ensure local directory exists
                os.makedirs(os.path.dirname(local_path), exist_ok=True)

                # Build scp command
                cmd = self.get_scp_base_command()

                # Add source and destination
                if self.user:
                    cmd.append(f"{self.user}@{self.host}:{remote_path}")
                else:
                    cmd.append(f"{self.host}:{remote_path}")

                cmd.append(local_path)

                logger.debug(f"Executing SCP command: {' '.join(cmd)}")

                # Execute scp command
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.operation_timeout,
                    check=True
                )

                # Verify the command was successful
                if result.returncode == 0:
                    # Verify the file was downloaded
                    if os.path.exists(local_path):
                        file_size = os.path.getsize(local_path)
                        logger.debug(f"File downloaded successfully: {remote_path} -> {local_path} ({file_size} bytes)")

                        # Register the file with the file manager
                        self.file_manager.registry.register(local_path, "temp")

                        self._connected = True
                        return {
                            "status": "success",
                            "local_path": local_path,
                            "file_size": file_size
                        }
                    else:
                        if attempt < max_attempts - 1:
                            continue
                        return {
                            "status": "error",
                            "error": f"File download succeeded but file not found at {local_path}"
                        }
                else:
                    if attempt < max_attempts - 1:
                        continue
                    return {
                        "status": "error",
                        "error": f"SCP failed with exit code {result.returncode}",
                        "stderr": result.stderr
                    }

            except subprocess.CalledProcessError as e:
                logger.error(f"SCP command failed: {e.stderr}")
                if attempt < max_attempts - 1:
                    continue
                return {
                    "status": "error",
                    "error": str(e),
                    "stderr": e.stderr
                }
            except Exception as e:
                logger.error(f"Unexpected error during file download: {str(e)}")
                self._connected = False
                if attempt < max_attempts - 1:
                    continue
                return {
                    "status": "error",
                    "error": str(e)
                }

    def check_remote_file_exists(self, remote_path: str) -> bool:
        """
        Check if a file exists on the remote host.

        Args:
            remote_path: Path on remote host

        Returns:
            True if file exists, False otherwise
        """
        result = self.execute_command(f"test -e {remote_path} && echo 'EXISTS' || echo 'NOT_EXISTS'")

        if result["status"] == "success" and "EXISTS" in result["stdout"]:
            return True
        return False

    def get_remote_file_size(self, remote_path: str) -> Optional[int]:
        """
        Get the size of a file on the remote host.

        Args:
            remote_path: Path on remote host

        Returns:
            File size in bytes or None if file doesn't exist
        """
        result = self.execute_command(f"stat -c %s {remote_path} 2>/dev/null || echo 'NOT_EXISTS'")

        if result["status"] == "success" and "NOT_EXISTS" not in result["stdout"]:
            try:
                return int(result["stdout"].strip())
            except ValueError:
                return None
        return None

    def with_retry(
        self,
        operation: Callable[..., Dict[str, Any]],
        *args: Any,
        **kwargs: Any
    ) -> Dict[str, Any]:
        """
        Execute an operation with retry logic.

        Args:
            operation: Function to execute
            *args: Arguments to pass to the operation
            **kwargs: Keyword arguments to pass to the operation

        Returns:
            Result of the operation
        """
        max_attempts = self.retry_count
        for attempt in range(max_attempts):
            if attempt > 0:
                logger.debug(f"Retrying operation (attempt {attempt+1}/{max_attempts})")
                time.sleep(self.retry_delay * attempt)

                # If auto-reconnect is enabled and we're not connected, try to reconnect
                if self.auto_reconnect and not self._connected:
                    logger.debug("Attempting to reconnect before retry")
                    self.connect()

            try:
                result = operation(*args, **kwargs)
                if result.get("status") == "success":
                    return result
            except Exception as e:
                logger.exception(f"Operation failed: {str(e)}")
                if attempt == max_attempts - 1:
                    return {
                        "status": "error",
                        "error": str(e)
                    }

        # This should never be reached, but just in case
        return {
            "status": "error",
            "error": "Operation failed after all retry attempts"
        }

    def close(self) -> None:
        """
        Close the SSH connection.
        """
        self._connected = False
        logger.debug(f"SSH connection to {self.host} closed")
