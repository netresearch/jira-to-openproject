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
from collections.abc import Callable
from typing import Any

from src import config
from src.utils.file_manager import FileManager

logger = config.logger


class SSHConnectionError(ConnectionError):
    """Exception raised for SSH connection errors."""

    pass


class SSHCommandError(Exception):
    """Exception raised when an SSH command fails."""

    def __init__(self, command: str, returncode: int, stdout: str, stderr: str, message: str = "SSH command failed"):
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.message = f"{message}: {stderr}" if stderr else message
        super().__init__(self.message)


class SSHFileTransferError(Exception):
    """Exception raised when an SCP file transfer operation fails."""

    def __init__(self, source: str, destination: str, message: str = "File transfer failed"):
        self.source = source
        self.destination = destination
        self.message = message
        super().__init__(self.message)


class SSHClient:
    """
    Client for SSH operations to remote servers.
    Provides the foundation for all remote operations in the client architecture.

    This client implements exception-based error handling:
    - Raises SSHConnectionError for connection issues
    - Raises SSHCommandError for command execution failures
    - Raises SSHFileTransferError for file transfer problems
    - Propagates standard subprocess exceptions for timeouts and process errors
    """

    def __init__(
        self,
        host: str,
        user: str | None = None,
        key_file: str | None = None,
        connect_timeout: int = 10,
        operation_timeout: int = 60,
        retry_count: int = 3,
        retry_delay: float = 1.0,
        auto_reconnect: bool = True,
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
            raise SSHConnectionError(f"Failed to connect to SSH host: {self.host}")

        logger.debug(f"SSHClient initialized for host {self.host}")

    def connect(self) -> bool:
        """
        Establish connection to the SSH host.

        Returns:
            True if connection successful, False otherwise
        """
        return self.test_connection()

    def get_ssh_base_command(self) -> list[str]:
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

    def get_scp_base_command(self) -> list[str]:
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

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.connect_timeout)

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
        self, command: str, timeout: int | None = None, check: bool = True, retry: bool = True
    ) -> tuple[str, str, int]:
        """
        Execute a command on the remote host.

        Args:
            command: Command to execute
            timeout: Command timeout in seconds (default: self.operation_timeout)
            check: Whether to check the return code
            retry: Whether to retry on failure

        Returns:
            Tuple of (stdout, stderr, returncode)

        Raises:
            SSHConnectionError: If unable to connect to the remote host
            SSHCommandError: If check=True and the command fails
            subprocess.TimeoutExpired: If the command times out
            Exception: For other unexpected errors
        """
        if timeout is None:
            timeout = self.operation_timeout

        # Retry logic
        max_attempts = self.retry_count if retry else 1
        last_exception = None

        for attempt in range(max_attempts):
            if attempt > 0:
                logger.debug(f"Retrying command (attempt {attempt+1}/{max_attempts}): {command}")
                time.sleep(self.retry_delay * attempt)

                # If auto-reconnect is enabled and we're not connected, try to reconnect
                if self.auto_reconnect and not self._connected:
                    logger.debug("Attempting to reconnect before retry")
                    if not self.connect():
                        raise SSHConnectionError(f"Failed to reconnect to SSH host: {self.host}")

            try:
                cmd = self.get_ssh_base_command()
                cmd.append(command)

                logger.debug(f"Executing SSH command: {' '.join(cmd)}")

                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=timeout, check=False  # We'll handle checking ourselves
                )

                # Update connection status based on command success
                self._connected = True

                # If check=True and command failed, raise our custom error
                if check and result.returncode != 0:
                    raise SSHCommandError(
                        command=command, returncode=result.returncode, stdout=result.stdout, stderr=result.stderr
                    )

                return result.stdout, result.stderr, result.returncode

            except subprocess.TimeoutExpired as e:
                logger.exception(f"SSH command timed out after {timeout} seconds")
                self._connected = False
                last_exception = e
                if attempt < max_attempts - 1:
                    continue
                raise

            except subprocess.CalledProcessError as e:
                # This shouldn't be reached since we set check=False above
                logger.exception(f"SSH command failed with exit code {e.returncode}: {e.stderr}")
                last_exception = SSHCommandError(
                    command=command, returncode=e.returncode, stdout=e.stdout, stderr=e.stderr
                )
                if check and attempt < max_attempts - 1:
                    continue
                raise last_exception

            except Exception as e:
                logger.exception(f"Unexpected error executing SSH command: {str(e)}")
                self._connected = False
                last_exception = e
                if attempt < max_attempts - 1:
                    continue
                raise

        if last_exception:
            raise last_exception  # This should never be reached due to the raise statements above
        raise RuntimeError("Command failed after all retry attempts")  # Fallback in case of logic error

    def copy_file_to_remote(self, local_path: str, remote_path: str, retry: bool = True) -> None:
        """
        Copy a file from local to remote host using scp.

        Args:
            local_path: Path to local file
            remote_path: Path on remote host
            retry: Whether to retry on failure

        Raises:
            FileNotFoundError: If the local file does not exist
            SSHConnectionError: If unable to connect to the remote host
            SSHFileTransferError: If the file transfer fails
            subprocess.TimeoutExpired: If the command times out
        """
        # Retry logic
        max_attempts = self.retry_count if retry else 1
        last_exception = None

        for attempt in range(max_attempts):
            if attempt > 0:
                logger.debug(f"Retrying file copy (attempt {attempt+1}/{max_attempts}): {local_path} -> {remote_path}")
                time.sleep(self.retry_delay * attempt)

                # If auto-reconnect is enabled and we're not connected, try to reconnect
                if self.auto_reconnect and not self._connected:
                    logger.debug("Attempting to reconnect before retry")
                    if not self.connect():
                        raise SSHConnectionError(f"Failed to reconnect to SSH host: {self.host}")

            try:
                # Validate local file exists
                if not os.path.exists(local_path):
                    raise FileNotFoundError(f"Local file does not exist: {local_path}")

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
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.operation_timeout, check=True)

                # Verify the command was successful
                if result.returncode == 0:
                    logger.debug(f"File copied successfully: {local_path} -> {remote_path}")
                    self._connected = True
                    return
                else:
                    # This shouldn't be reached due to check=True above
                    raise SSHFileTransferError(
                        source=local_path,
                        destination=f"{self.host}:{remote_path}",
                        message=f"SCP failed with exit code {result.returncode}: {result.stderr}",
                    )

            except subprocess.CalledProcessError as e:
                logger.error(f"SCP command failed: {e.stderr}")
                last_exception = SSHFileTransferError(
                    source=local_path,
                    destination=f"{self.host}:{remote_path}",
                    message=f"SCP command failed: {e.stderr}",
                )
                if attempt < max_attempts - 1:
                    continue
                raise last_exception

            except FileNotFoundError:
                # Don't convert FileNotFoundError
                raise

            except subprocess.TimeoutExpired as e:
                logger.error(f"SCP command timed out after {self.operation_timeout} seconds")
                last_exception = e
                if attempt < max_attempts - 1:
                    continue
                raise

            except Exception as e:
                logger.error(f"Unexpected error during file copy: {str(e)}")
                self._connected = False
                last_exception = SSHFileTransferError(
                    source=local_path, destination=f"{self.host}:{remote_path}", message=f"Unexpected error: {str(e)}"
                )
                if attempt < max_attempts - 1:
                    continue
                raise last_exception

        if last_exception:
            raise last_exception  # This should never be reached due to the raise statements above
        raise RuntimeError("File copy failed after all retry attempts")  # Fallback in case of logic error

    def copy_file_from_remote(self, remote_path: str, local_path: str, retry: bool = True) -> str:
        """
        Copy a file from remote host to local using scp.

        Args:
            remote_path: Path on remote host
            local_path: Path to save file locally
            retry: Whether to retry on failure

        Returns:
            Path to the downloaded file

        Raises:
            FileNotFoundError: If the remote file does not exist or local file is not created
            SSHConnectionError: If unable to connect to the remote host
            SSHFileTransferError: If the file transfer fails
            subprocess.TimeoutExpired: If the command times out
        """
        # Retry logic
        max_attempts = self.retry_count if retry else 1
        last_exception = None

        for attempt in range(max_attempts):
            if attempt > 0:
                logger.debug(
                    f"Retrying file download (attempt {attempt+1}/{max_attempts}): " f"{remote_path} -> {local_path}"
                )
                time.sleep(self.retry_delay * attempt)

                # If auto-reconnect is enabled and we're not connected, try to reconnect
                if self.auto_reconnect and not self._connected:
                    logger.debug("Attempting to reconnect before retry")
                    if not self.connect():
                        raise SSHConnectionError(f"Failed to reconnect to SSH host: {self.host}")

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
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.operation_timeout, check=True)

                # Verify the command was successful and file exists
                if result.returncode == 0 and not os.path.exists(local_path):
                    raise FileNotFoundError(f"File download succeeded but file not found at {local_path}")

                file_size = os.path.getsize(local_path)
                logger.debug(f"File downloaded successfully: {remote_path} -> {local_path} ({file_size} bytes)")

                # Register the file with the file manager
                self.file_manager.registry.register(local_path, "temp")

                self._connected = True
                return local_path

            except subprocess.CalledProcessError as e:
                logger.error(f"SCP command failed: {e.stderr}")
                last_exception = SSHFileTransferError(
                    source=f"{self.host}:{remote_path}",
                    destination=local_path,
                    message=f"SCP command failed: {e.stderr}",
                )
                if attempt < max_attempts - 1:
                    continue
                raise last_exception

            except FileNotFoundError:
                # Don't convert FileNotFoundError
                raise

            except subprocess.TimeoutExpired as e:
                logger.error(f"SCP command timed out after {self.operation_timeout} seconds")
                last_exception = e
                if attempt < max_attempts - 1:
                    continue
                raise

            except Exception as e:
                logger.error(f"Unexpected error during file download: {str(e)}")
                self._connected = False
                last_exception = SSHFileTransferError(
                    source=f"{self.host}:{remote_path}", destination=local_path, message=f"Unexpected error: {str(e)}"
                )
                if attempt < max_attempts - 1:
                    continue
                raise last_exception

        if last_exception:
            raise last_exception  # This should never be reached due to the raise statements above
        raise RuntimeError("File download failed after all retry attempts")  # Fallback in case of logic error

    def check_remote_file_exists(self, remote_path: str) -> bool:
        """
        Check if a file exists on the remote host.

        Args:
            remote_path: Path to check

        Returns:
            True if file exists, False otherwise
        """
        try:
            # Use single quotes around the command to prevent shell expansion
            # and escape any existing single quotes in the path
            escaped_path = remote_path.replace("'", "'\\''")
            command = f"test -e '{escaped_path}' && echo 'EXISTS' || echo 'NOT_EXISTS'"

            stdout, stderr, returncode = self.execute_command(command)

            # Check if the command returned the EXISTS marker
            if returncode == 0 and "EXISTS" in stdout:
                return True

            # Log the error for debugging
            if returncode != 0:
                logger.debug(f"Error checking if remote file exists: {stderr}")

            return False
        except Exception as e:
            logger.warning(f"Failed to check if remote file exists ({remote_path}): {str(e)}")
            return False

    def get_remote_file_size(self, remote_path: str) -> int | None:
        """
        Get the size of a file on the remote host.

        Args:
            remote_path: Path on remote host

        Returns:
            File size in bytes or None if file doesn't exist

        Raises:
            SSHConnectionError: If unable to connect to the remote host
            SSHCommandError: If the command fails
            subprocess.TimeoutExpired: If the command times out
        """
        try:
            stdout, _, returncode = self.execute_command(
                f"stat -c %s {remote_path} 2>/dev/null || echo 'NOT_EXISTS'", check=False
            )

            if returncode == 0 and "NOT_EXISTS" not in stdout:
                try:
                    return int(stdout.strip())
                except ValueError:
                    logger.warning(f"Invalid file size returned for {remote_path}: {stdout.strip()}")
                    return None
            return None
        except (SSHConnectionError, subprocess.TimeoutExpired):
            # Re-raise these exceptions directly
            raise
        except Exception as e:
            # For any other exceptions, log and return None
            logger.error(f"Error getting remote file size: {str(e)}")
            return None

    def with_retry(self, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """
        Execute an operation with retry logic.

        Args:
            operation: Function to execute
            *args: Arguments to pass to the operation
            **kwargs: Keyword arguments to pass to the operation

        Returns:
            Result of the operation

        Raises:
            Exception: Any exception raised by the operation after all retries
        """
        max_attempts = self.retry_count
        last_exception = None

        for attempt in range(max_attempts):
            if attempt > 0:
                logger.debug(f"Retrying operation (attempt {attempt+1}/{max_attempts})")
                time.sleep(self.retry_delay * attempt)

                # If auto-reconnect is enabled and we're not connected, try to reconnect
                if self.auto_reconnect and not self._connected:
                    logger.debug("Attempting to reconnect before retry")
                    self.connect()

            try:
                return operation(*args, **kwargs)
            except Exception as e:
                logger.exception(f"Operation failed: {str(e)}")
                last_exception = e
                if attempt == max_attempts - 1:
                    raise

        if last_exception:
            raise last_exception  # This should never be reached due to the raise statement above
        raise RuntimeError("Operation failed after all retry attempts")  # Fallback in case of logic error

    def close(self) -> None:
        """
        Close the SSH connection.
        """
        self._connected = False
        logger.debug(f"SSH connection to {self.host} closed")
