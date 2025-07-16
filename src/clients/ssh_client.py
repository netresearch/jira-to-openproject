#!/usr/bin/env python3
"""SSHClient.

Handles SSH operations to remote servers, including command execution and file transfers.
This is the foundation of the layered client architecture:
1. SSHClient - Base component for SSH operations
2. DockerClient - Uses SSHClient for remote Docker operations
3. RailsConsoleClient - Uses DockerClient for container interactions
4. OpenProjectClient - Coordinates all clients and operations
"""

import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from shlex import quote
from typing import Any

from src import config
from src.utils.file_manager import FileManager

logger = config.logger


class SSHConnectionError(ConnectionError):
    """Exception raised for SSH connection errors."""


class SSHCommandError(Exception):
    """Exception raised when an SSH command fails."""

    def __init__(
        self,
        command: str,
        returncode: int,
        stdout: str,
        stderr: str,
        message: str = "SSH command failed",
    ) -> None:
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.message = f"{message}: {stderr}" if stderr else message
        super().__init__(self.message)


class SSHFileTransferError(Exception):
    """Exception raised when an SCP file transfer operation fails."""

    def __init__(
        self,
        source: str,
        destination: str,
        message: str = "File transfer failed",
    ) -> None:
        self.source = source
        self.destination = destination
        self.message = message
        super().__init__(self.message)


class SSHClient:
    """Client for SSH operations to remote servers.

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
        """Initialize the SSH client.

        Args:
            host: SSH host (hostname or IP)
            user: SSH username (default: current user)
            key_file: Path to SSH key file (default: use system's SSH configuration)
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

        # Get file manager instance
        self.file_manager = FileManager()

        # Test connection
        if not self.connect():
            msg = f"Failed to connect to SSH host: {self.host}"
            raise SSHConnectionError(msg)

        logger.debug("SSHClient initialized for host %s", self.host)

    def connect(self) -> bool:
        """Establish connection to the SSH host.

        Returns:
            True if connection successful, False otherwise

        """
        return self.test_connection()

    def get_ssh_base_command(self) -> list[str]:
        """Get the base SSH command with common options.

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
        """Get the base SCP command with common options.

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
        """Test SSH connection to the remote host.

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
                timeout=self.connect_timeout,
                check=False,
            )

            return result.returncode == 0 and "Connection successful" in result.stdout

        except subprocess.SubprocessError as e:
            logger.exception("SSH connection test failed: %s", e)
            return False
        except Exception as e:
            logger.exception("Unexpected error testing SSH connection: %s", e)
            return False

    def is_connected(self) -> bool:
        """Check if the client is currently connected.

        Returns:
            True if connected, False otherwise

        """
        return True

    def execute_command(
        self,
        command: str,
        timeout: int | None = None,
        check: bool = True,
        retry: bool = True,
    ) -> tuple[str, str, int]:
        """Execute a command on the remote host.

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
        last_exception: Exception | None = None

        for attempt in range(max_attempts):
            if attempt > 0:
                logger.debug(
                    "Retrying command (attempt %d/%d): %s",
                    attempt + 1,
                    max_attempts,
                    command,
                )
                time.sleep(self.retry_delay * attempt)

            try:
                cmd = self.get_ssh_base_command()
                cmd.append(command)

                logger.debug("Executing SSH command: %s", " ".join(cmd))

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,  # We'll handle checking ourselves
                )

                # If check=True and command failed, raise our custom error
                if check and result.returncode != 0:
                    raise SSHCommandError(
                        command=command,
                        returncode=result.returncode,
                        stdout=result.stdout,
                        stderr=result.stderr,
                    )

                return result.stdout, result.stderr, result.returncode

            except subprocess.TimeoutExpired as e:
                logger.exception("SSH command timed out after %d seconds", timeout)
                last_exception = e
                if attempt < max_attempts - 1:
                    continue
                raise

            except subprocess.CalledProcessError as e:
                # This shouldn't be reached since we set check=False above
                logger.exception(
                    "SSH command failed with exit code %d: %s",
                    e.returncode,
                    e.stderr,
                )
                last_exception = SSHCommandError(
                    command=command,
                    returncode=e.returncode,
                    stdout=e.stdout,
                    stderr=e.stderr,
                )
                if check and attempt < max_attempts - 1:
                    continue
                raise last_exception from None

            except Exception as e:
                logger.exception("Unexpected error executing SSH command: %s", e)
                last_exception = e
                if attempt < max_attempts - 1:
                    continue
                raise

        if last_exception:
            raise last_exception  # This should never be reached due to the raise statements above
        msg = "Command failed after all retry attempts"
        raise RuntimeError(msg)  # Fallback in case of logic error

    def copy_file_to_remote(
        self,
        local_path: Path | str,
        remote_path: Path | str,
        retry: bool = True,
    ) -> None:
        """Copy a file from local to remote host using scp.

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
        # Convert to Path objects
        local_path = Path(local_path) if isinstance(local_path, str) else local_path
        remote_path = Path(remote_path) if isinstance(remote_path, str) else remote_path

        # Retry logic
        max_attempts = self.retry_count if retry else 1
        last_exception: Exception | None = None

        for attempt in range(max_attempts):
            if attempt > 0:
                logger.debug(
                    "Retrying file copy (attempt %d/%d): %s -> %s",
                    attempt + 1,
                    max_attempts,
                    local_path,
                    remote_path,
                )
                time.sleep(self.retry_delay * attempt)

            try:
                # Build scp command
                cmd = self.get_scp_base_command()

                # Add source and destination
                cmd.append(str(local_path))

                if self.user:
                    cmd.append(f"{self.user}@{self.host}:{remote_path}")
                else:
                    cmd.append(f"{self.host}:{remote_path}")

                logger.debug("Executing SCP command: %s", " ".join(cmd))

                # Execute scp command
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.operation_timeout,
                    check=True,
                )

                # Verify the command was successful
                if result.returncode == 0:
                    logger.debug(
                        "File copied successfully: %s -> %s",
                        local_path,
                        remote_path,
                    )
                    return
                # This shouldn't be reached due to check=True above
                raise SSHFileTransferError(
                    source=str(local_path),
                    destination=f"{self.host}:{remote_path}",
                    message=f"SCP failed with exit code {result.returncode}: {result.stderr}",
                )

            except subprocess.CalledProcessError as e:
                logger.exception("SCP command failed: %s", e.stderr)
                # Check if the error was due to missing local file
                if not local_path.exists():
                    msg = f"Local file does not exist: {local_path}"
                    raise FileNotFoundError(
                        msg,
                    ) from e
                last_exception = SSHFileTransferError(
                    source=str(local_path),
                    destination=f"{self.host}:{remote_path}",
                    message=f"SCP command failed: {e.stderr}",
                )
                if attempt < max_attempts - 1:
                    continue
                raise last_exception from None

            except FileNotFoundError:
                # Don't convert FileNotFoundError
                raise

            except subprocess.TimeoutExpired as e:
                logger.exception(
                    "SCP command timed out after %d seconds",
                    self.operation_timeout,
                )
                last_exception = e
                if attempt < max_attempts - 1:
                    continue
                raise

            except Exception as e:
                logger.exception("Unexpected error during file copy: %s", e)
                last_exception = SSHFileTransferError(
                    source=str(local_path),
                    destination=f"{self.host}:{remote_path}",
                    message=f"Unexpected error: {e!s}",
                )
                if attempt < max_attempts - 1:
                    continue
                raise last_exception from None

        if last_exception:
            raise last_exception  # This should never be reached due to the raise statements above
        msg = "File copy failed after all retry attempts"
        raise RuntimeError(msg)  # Fallback in case of logic error

    def copy_file_from_remote(
        self,
        remote_path: Path | str,
        local_path: Path | str,
        retry: bool = True,
    ) -> Path:
        """Copy a file from remote host to local using scp.

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
        # Convert to Path objects
        remote_path = Path(remote_path) if isinstance(remote_path, str) else remote_path
        local_path = Path(local_path) if isinstance(local_path, str) else local_path

        # Retry logic
        max_attempts = self.retry_count if retry else 1
        last_exception: Exception | None = None

        for attempt in range(max_attempts):
            if attempt > 0:
                logger.debug(
                    "Retrying file download (attempt %d/%d): %s -> %s",
                    attempt + 1,
                    max_attempts,
                    remote_path,
                    local_path,
                )
                time.sleep(self.retry_delay * attempt)

            try:
                # Ensure local directory exists
                local_path.parent.mkdir(parents=True, exist_ok=True)

                # Build scp command
                cmd = self.get_scp_base_command()

                # Add source and destination
                if self.user:
                    cmd.append(f"{self.user}@{self.host}:{remote_path}")
                else:
                    cmd.append(f"{self.host}:{remote_path}")

                cmd.append(str(local_path))

                logger.debug("Executing SCP command: %s", " ".join(cmd))

                # Execute scp command
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.operation_timeout,
                    check=True,
                )

                # Verify the command was successful and file exists
                if result.returncode == 0 and not local_path.exists():
                    msg = f"File download succeeded but file not found at {local_path}"
                    raise FileNotFoundError(msg)

                file_size = local_path.stat().st_size
                logger.debug(
                    "File downloaded successfully: %s -> %s (%d bytes)",
                    remote_path,
                    local_path,
                    file_size,
                )

                # Register the file with the file manager
                self.file_manager.registry.register(local_path, "temp")

                return local_path

            except subprocess.CalledProcessError as e:
                logger.exception("SCP command failed: %s", e.stderr)
                last_exception = SSHFileTransferError(
                    source=f"{self.host}:{remote_path}",
                    destination=str(local_path),
                    message=f"SCP command failed: {e.stderr}",
                )
                if attempt < max_attempts - 1:
                    continue
                raise last_exception from None

            except FileNotFoundError:
                # Don't convert FileNotFoundError
                raise

            except subprocess.TimeoutExpired as e:
                logger.exception(
                    "SCP command timed out after %d seconds",
                    self.operation_timeout,
                )
                last_exception = e
                if attempt < max_attempts - 1:
                    continue
                raise

            except Exception as e:
                logger.exception("Unexpected error during file download: %s", e)
                last_exception = SSHFileTransferError(
                    source=f"{self.host}:{remote_path}",
                    destination=str(local_path),
                    message=f"Unexpected error: {e!s}",
                )
                if attempt < max_attempts - 1:
                    continue
                raise last_exception from None

        if last_exception:
            raise last_exception  # This should never be reached due to the raise statements above
        msg = "File download failed after all retry attempts"
        raise RuntimeError(msg)  # Fallback in case of logic error

    def check_remote_file_exists(self, remote_path: Path | str) -> bool:
        """Check if a file exists on the remote host.

        Args:
            remote_path: Path on remote host

        Returns:
            True if file exists, False otherwise

        """
        # Convert to string for the remote command
        remote_path_str = (
            str(remote_path) if isinstance(remote_path, Path) else remote_path
        )

        try:
            cmd = f"test -e {quote(remote_path_str)} && echo 'EXISTS' || echo 'NOT_EXISTS'"
            stdout, _, returncode = self.execute_command(cmd, check=False)

            return "EXISTS" in stdout and returncode == 0
        except Exception:
            logger.exception("Error checking if remote file exists: %s", remote_path)
            return False

    def get_remote_file_size(self, remote_path: Path | str) -> int | None:
        """Get the size of a file on the remote host.

        Args:
            remote_path: Path on remote host

        Returns:
            File size in bytes or None if file doesn't exist or size can't be determined

        """
        # Convert to string for the remote command
        remote_path_str = (
            str(remote_path) if isinstance(remote_path, Path) else remote_path
        )

        try:
            cmd = f"stat -c '%s' {quote(remote_path_str)} 2>/dev/null || echo 'NOT_EXISTS'"
            stdout, _, returncode = self.execute_command(cmd, check=False)

            if "NOT_EXISTS" in stdout or returncode != 0:
                return None

            try:
                return int(stdout.strip())
            except ValueError:
                logger.exception(
                    "Invalid file size returned for %s: %s",
                    remote_path,
                    stdout.strip(),
                )
                return None

        except Exception:
            logger.exception("Error getting remote file size: %s", remote_path)
            return None

    def with_retry(
        self,
        operation: Callable[..., Any],
        *args: object,
        **kwargs: object,
    ) -> Any:
        """Execute an operation with retry logic.

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
        last_exception: Exception | None = None

        for attempt in range(max_attempts):
            if attempt > 0:
                logger.debug(
                    "Retrying operation (attempt %d/%d)",
                    attempt + 1,
                    max_attempts,
                )
                time.sleep(self.retry_delay * attempt)

            try:
                return operation(*args, **kwargs)
            except Exception as e:
                logger.exception("Operation failed: %s", e)
                last_exception = e
                if attempt == max_attempts - 1:
                    raise

        if last_exception:
            raise last_exception  # This should never be reached due to the raise statement above
        msg = "Operation failed after all retry attempts"
        raise RuntimeError(msg)  # Fallback in case of logic error

    def close(self) -> None:
        """Close the SSH connection."""
        logger.debug("SSH connection to %s closed", self.host)
