#!/usr/bin/env python3
"""
SSHClient

Handles SSH operations to remote servers, including command execution and file transfers.
"""

import os
import subprocess
from typing import Any

from src import config
from src.utils.file_manager import FileManager

logger = config.logger


class SSHClient:
    """
    Client for SSH operations to remote servers.
    """

    def __init__(self, host: str, user: str | None = None,
                 key_file: str | None = None,
                 connect_timeout: int = 10,
                 operation_timeout: int = 60) -> None:
        """
        Initialize the SSH client.

        Args:
            host: SSH host (hostname or IP)
            user: SSH username (default: current user)
            key_file: Path to SSH key file (default: use SSH agent)
            connect_timeout: Connection timeout in seconds
            operation_timeout: Default timeout for operations
        """
        self.host = host
        self.user = user
        self.key_file = key_file
        self.connect_timeout = connect_timeout
        self.operation_timeout = operation_timeout

        # Get file manager instance
        self.file_manager = FileManager()

        # Test connection
        if not self.test_connection():
            raise ConnectionError(f"Failed to connect to SSH host: {self.host}")

        logger.debug(f"SSHClient initialized for host {self.host}")

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

            return result.returncode == 0 and "Connection successful" in result.stdout

        except subprocess.SubprocessError as e:
            logger.error(f"SSH connection test failed: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error testing SSH connection: {str(e)}")
            return False

    def execute_command(self, command: str, timeout: int | None = None,
                        check: bool = True) -> dict[str, Any]:
        """
        Execute a command on the remote host.

        Args:
            command: Command to execute
            timeout: Command timeout in seconds (default: self.operation_timeout)
            check: Whether to check the return code

        Returns:
            Dict with keys: status, stdout, stderr, returncode
        """
        if timeout is None:
            timeout = self.operation_timeout

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

            return {
                "status": "success" if result.returncode == 0 else "error",
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode
            }

        except subprocess.TimeoutExpired:
            logger.error(f"SSH command timed out after {timeout} seconds")
            return {
                "status": "error",
                "error": f"Command timed out after {timeout} seconds",
                "returncode": -1
            }
        except subprocess.CalledProcessError as e:
            logger.error(f"SSH command failed with exit code {e.returncode}: {e.stderr}")
            return {
                "status": "error",
                "error": str(e),
                "stdout": e.stdout,
                "stderr": e.stderr,
                "returncode": e.returncode
            }
        except Exception as e:
            logger.error(f"Unexpected error executing SSH command: {str(e)}")
            return {
                "status": "error",
                "error": str(e),
                "returncode": -1
            }

    def copy_file_to_remote(self, local_path: str, remote_path: str) -> dict[str, Any]:
        """
        Copy a file from local to remote host using scp.

        Args:
            local_path: Path to local file
            remote_path: Path on remote host

        Returns:
            Dict with status and error information
        """
        try:
            # Validate local file exists
            if not os.path.exists(local_path):
                return {
                    "status": "error",
                    "error": f"Local file does not exist: {local_path}"
                }

            # Build scp command
            cmd = ["scp"]

            # Add connect timeout
            cmd.extend(["-o", f"ConnectTimeout={self.connect_timeout}"])

            # Add key file if specified
            if self.key_file:
                cmd.extend(["-i", self.key_file])

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
                return {
                    "status": "success"
                }
            else:
                return {
                    "status": "error",
                    "error": f"SCP failed with exit code {result.returncode}",
                    "stderr": result.stderr
                }

        except subprocess.CalledProcessError as e:
            logger.error(f"SCP command failed: {e.stderr}")
            return {
                "status": "error",
                "error": str(e),
                "stderr": e.stderr
            }
        except Exception as e:
            logger.error(f"Unexpected error during file copy: {str(e)}")
            return {
                "status": "error",
                "error": str(e)
            }

    def copy_file_from_remote(self, remote_path: str, local_path: str) -> dict[str, Any]:
        """
        Copy a file from remote host to local using scp.

        Args:
            remote_path: Path on remote host
            local_path: Path to save file locally

        Returns:
            Dict with status and error information
        """
        try:
            # Ensure local directory exists
            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            # Build scp command
            cmd = ["scp"]

            # Add connect timeout
            cmd.extend(["-o", f"ConnectTimeout={self.connect_timeout}"])

            # Add key file if specified
            if self.key_file:
                cmd.extend(["-i", self.key_file])

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

                    return {
                        "status": "success",
                        "local_path": local_path,
                        "file_size": file_size
                    }
                else:
                    return {
                        "status": "error",
                        "error": f"File download succeeded but file not found at {local_path}"
                    }
            else:
                return {
                    "status": "error",
                    "error": f"SCP failed with exit code {result.returncode}",
                    "stderr": result.stderr
                }

        except subprocess.CalledProcessError as e:
            logger.error(f"SCP command failed: {e.stderr}")
            return {
                "status": "error",
                "error": str(e),
                "stderr": e.stderr
            }
        except Exception as e:
            logger.error(f"Unexpected error during file download: {str(e)}")
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

    def get_remote_file_size(self, remote_path: str) -> int | None:
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
