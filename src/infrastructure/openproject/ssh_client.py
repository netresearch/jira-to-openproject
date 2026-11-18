# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

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

from src.display import configure_logging
from src.utils.file_manager import FileManager

logger = configure_logging("INFO", None)


class SSHConnectionError(ConnectionError):
    """Exception raised for SSH connection errors."""


class SSHCommandError(Exception):
    """Exception raised when an SSH command fails.

    Args:
        command: The command that was executed
        returncode: Process return code
        stdout: Captured stdout
        stderr: Captured stderr
        message: Optional base message

    """

    def __init__(
        self,
        command: str,
        returncode: int,
        stdout: str,
        stderr: str,
        message: str = "SSH command failed",
    ) -> None:
        """Initialize error with process details.

        Args:
            command: The SSH command executed
            returncode: Exit code
            stdout: Captured standard output
            stderr: Captured standard error
            message: Optional descriptive message

        """
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.message = f"{message}: {stderr}" if stderr else message
        super().__init__(self.message)


class SSHFileTransferError(Exception):
    """Exception raised when an SCP file transfer operation fails.

    Args:
        source: Source path
        destination: Destination path
        message: Optional base message

    """

    def __init__(
        self,
        source: str,
        destination: str,
        message: str = "File transfer failed",
    ) -> None:
        """Initialize file transfer error.

        Args:
            source: Source path
            destination: Destination path
            message: Optional message

        """
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

    SSH_CONTROL_PATH = "~/.ssh/j2o-%C"
    SSH_CONTROL_PERSIST_SECONDS = "300"

    def __init__(
        self,
        host: str,
        *,
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

    def _get_common_ssh_options(self) -> list[str]:
        """Get SSH options shared between ssh and scp commands.

        Returns:
            List of option flags for ConnectTimeout, multiplexing, and key file

        """
        opts = [
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
        ]
        try:
            (Path.home() / ".ssh").mkdir(mode=0o700, exist_ok=True)
        except OSError:
            # Read-only homes still support key and agent authentication.
            opts.extend(["-o", "ControlMaster=no", "-o", "ControlPath=none"])
        else:
            opts.extend(
                [
                    "-o",
                    "ControlMaster=auto",
                    "-o",
                    f"ControlPath={self.SSH_CONTROL_PATH}",
                    "-o",
                    f"ControlPersist={self.SSH_CONTROL_PERSIST_SECONDS}",
                ]
            )

        if self.key_file:
            opts.extend(["-i", self.key_file])

        return opts

    def get_ssh_base_command(self) -> list[str]:
        """Get the base SSH command with common options.

        Returns:
            List of command parts

        """
        cmd = ["ssh", *self._get_common_ssh_options()]

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
        return ["scp", *self._get_common_ssh_options()]

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

        except subprocess.SubprocessError:
            logger.exception("SSH connection test failed")
            return False
        except Exception:
            logger.exception("Unexpected error testing SSH connection")
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
        *,
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
        effective_timeout = self.operation_timeout if timeout is None else timeout

        def send() -> tuple[str, str, int]:
            try:
                result = subprocess.run(
                    [*self.get_ssh_base_command(), command],
                    capture_output=True,
                    text=True,
                    timeout=effective_timeout,
                    check=False,
                )
            except subprocess.CalledProcessError as exc:
                raise SSHCommandError(command, exc.returncode, exc.stdout, exc.stderr) from exc
            if check and result.returncode:
                raise SSHCommandError(command, result.returncode, result.stdout, result.stderr)
            return result.stdout, result.stderr, result.returncode

        return self._attempt(send, retry=retry, terminal=(SSHCommandError,) if not check else ())

    def _attempt[T](self, operation: Callable[[], T], *, retry: bool, terminal: tuple[type[Exception], ...] = ()) -> T:
        attempts = self.retry_count if retry else 1
        for index in range(attempts):
            if index:
                time.sleep(self.retry_delay * index)
            try:
                return operation()
            except Exception as exc:
                if index + 1 == attempts or isinstance(exc, terminal):
                    raise
                logger.debug("SSH operation failed, retrying attempt %d/%d", index + 2, attempts)
        msg = "No SSH operation attempts configured"
        raise RuntimeError(msg)

    def copy_file_to_remote(
        self,
        local_path: Path | str,
        remote_path: Path | str,
        *,
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
        local, remote = Path(local_path), Path(remote_path)
        self._attempt(lambda: self._copy(local, remote, download=False), retry=retry, terminal=(FileNotFoundError,))

    def copy_file_from_remote(
        self,
        remote_path: Path | str,
        local_path: Path | str,
        *,
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
        local, remote = Path(local_path), Path(remote_path)
        self._attempt(lambda: self._copy(local, remote, download=True), retry=retry, terminal=(FileNotFoundError,))
        return local

    def _copy(self, local: Path, remote: Path, *, download: bool) -> None:
        host = f"{self.user}@{self.host}" if self.user else self.host
        remote_spec = f"{host}:{remote}"
        source, destination = (remote_spec, str(local)) if download else (str(local), remote_spec)
        try:
            if download:
                local.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                [*self.get_scp_base_command(), source, destination],
                capture_output=True,
                text=True,
                timeout=self.operation_timeout,
                check=True,
            )
            if result.returncode:
                raise subprocess.CalledProcessError(result.returncode, result.args, stderr=result.stderr)
            if download:
                if not local.exists():
                    msg = f"File download succeeded but file not found at {local}"
                    raise FileNotFoundError(msg)
                self.file_manager.registry.register(local, "temp")
        except subprocess.CalledProcessError as exc:
            if not download and not local.exists():
                msg = f"Local file does not exist: {local}"
                raise FileNotFoundError(msg) from exc
            raise SSHFileTransferError(source, destination, f"SCP command failed: {exc.stderr}") from exc
        except FileNotFoundError, subprocess.TimeoutExpired:
            raise
        except Exception as exc:
            raise SSHFileTransferError(source, destination, f"Unexpected error: {exc}") from exc

    def check_remote_file_exists(self, remote_path: Path | str) -> bool:
        """Check if a file exists on the remote host.

        Args:
            remote_path: Path on remote host

        Returns:
            True if file exists, False otherwise

        """
        # Convert to string for the remote command
        remote_path_str = str(remote_path) if isinstance(remote_path, Path) else remote_path

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
        remote_path_str = str(remote_path) if isinstance(remote_path, Path) else remote_path

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
    ) -> object:
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
        return self._attempt(lambda: operation(*args, **kwargs), retry=True)

    def close(self) -> None:
        """Close the SSH connection."""
        logger.debug("SSH connection to %s closed", self.host)
