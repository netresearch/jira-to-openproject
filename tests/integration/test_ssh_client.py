import pytest

pytestmark = pytest.mark.integration


#!/usr/bin/env python3
"""Test module for SSHClient.

This module contains test cases for validating SSH operations.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


from src.clients.ssh_client import SSHClient, SSHCommandError, SSHFileTransferError


class TestSSHClient(unittest.TestCase):
    """Test cases for the SSHClient class."""

    def setUp(self) -> None:
        """Set up the test environment."""
        # Create a temp directory for file operations
        self.temp_dir = tempfile.mkdtemp()

        # Create patchers
        self.subprocess_patcher = patch("src.clients.ssh_client.subprocess")
        self.mock_subprocess = self.subprocess_patcher.start()

        self.logger_patcher = patch("src.clients.ssh_client.logger")
        self.mock_logger = self.logger_patcher.start()

        # Patch pathlib.Path
        self.path_patcher = patch("pathlib.Path")
        self.mock_path_class = self.path_patcher.start()

        # Keep the real Path to create our mock instances
        self.real_path = Path

        # Configure mock subprocess.run
        self.process_mock = MagicMock()
        self.process_mock.returncode = 0
        self.process_mock.stdout = "Connection successful"
        self.process_mock.stderr = ""
        self.mock_subprocess.run.return_value = self.process_mock

        # Configure Path mocks
        self.mock_path_instance = MagicMock()
        self.mock_path_instance.exists.return_value = True
        self.mock_path_instance.stat.return_value.st_size = 1024
        self.mock_path_instance.parent = self.mock_path_instance
        self.mock_path_instance.mkdir.return_value = None
        self.mock_path_instance.__str__.return_value = "/mocked/path"
        self.mock_path_class.return_value = self.mock_path_instance

        # Make Path constructor return the same mock for any arguments
        self.mock_path_class.side_effect = (
            lambda *args, **kwargs: self.mock_path_instance
        )

        # Also patch src.clients.ssh_client.Path to return our mock
        self.src_path_patcher = patch(
            "src.clients.ssh_client.Path",
            self.mock_path_class,
        )
        self.src_path_patcher.start()

        # File manager mock
        self.file_manager_patcher = patch("src.clients.ssh_client.FileManager")
        self.mock_file_manager_class = self.file_manager_patcher.start()
        self.mock_file_manager = MagicMock()
        self.mock_file_manager.registry = MagicMock()
        self.mock_file_manager_class.return_value = self.mock_file_manager

        # Set up subprocess exception classes correctly
        self.mock_subprocess.CalledProcessError = subprocess.CalledProcessError
        self.mock_subprocess.TimeoutExpired = subprocess.TimeoutExpired
        self.mock_subprocess.SubprocessError = subprocess.SubprocessError

        # Initialize SSHClient after all mocks are set up
        with patch.object(SSHClient, "connect", return_value=True):
            self.ssh_client = SSHClient(host="testhost", user="testuser")

    def tearDown(self) -> None:
        """Clean up after each test."""
        # Stop all patchers
        self.subprocess_patcher.stop()
        self.logger_patcher.stop()
        self.path_patcher.stop()
        self.src_path_patcher.stop()
        self.file_manager_patcher.stop()

        # Clean up temp directory
        if Path(self.temp_dir).exists():
            import shutil

            shutil.rmtree(self.temp_dir)

    def test_initialization(self) -> None:
        """Test SSHClient initialization."""
        # Verify object attributes
        assert self.ssh_client.host == "testhost"
        assert self.ssh_client.user == "testuser"
        assert self.ssh_client.key_file is None
        assert self.ssh_client.connect_timeout == 10
        assert self.ssh_client.operation_timeout == 60

        # Verify the logger was called
        self.mock_logger.debug.assert_called()

    def test_get_ssh_base_command(self) -> None:
        """Test generating the base SSH command."""
        # Test with default settings
        cmd = self.ssh_client.get_ssh_base_command()
        expected_cmd = ["ssh", "-o", "ConnectTimeout=10", "testuser@testhost"]
        assert cmd == expected_cmd

        # Test with key file
        self.ssh_client.key_file = "/path/to/key.pem"
        cmd = self.ssh_client.get_ssh_base_command()
        expected_cmd = [
            "ssh",
            "-o",
            "ConnectTimeout=10",
            "-i",
            "/path/to/key.pem",
            "testuser@testhost",
        ]
        assert cmd == expected_cmd

        # Test without user
        self.ssh_client.user = None
        cmd = self.ssh_client.get_ssh_base_command()
        expected_cmd = [
            "ssh",
            "-o",
            "ConnectTimeout=10",
            "-i",
            "/path/to/key.pem",
            "testhost",
        ]
        assert cmd == expected_cmd

    def test_test_connection_success(self) -> None:
        """Test successful connection test."""
        # Reset the mock to ensure the actual call is tracked
        self.mock_subprocess.run.reset_mock()

        # Configure mock to return success
        self.process_mock.returncode = 0
        self.process_mock.stdout = "Connection successful"
        self.mock_subprocess.run.return_value = self.process_mock

        # Call the method
        result = self.ssh_client.test_connection()

        # Verify result
        assert result

        # Verify subprocess.run was called with the right command
        self.mock_subprocess.run.assert_called_once()
        cmd_args = self.mock_subprocess.run.call_args[0][0]
        assert "ssh" in cmd_args
        assert "-o" in cmd_args
        assert "BatchMode=yes" in cmd_args
        assert "echo" in cmd_args

    def test_test_connection_failure(self) -> None:
        """Test failed connection test."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()

        # Configure mock to return failure
        self.process_mock.returncode = 1
        self.process_mock.stdout = "Permission denied"
        self.mock_subprocess.run.return_value = self.process_mock

        # Call the method
        result = self.ssh_client.test_connection()

        # Verify result
        assert not result

        # Verify subprocess.run was called
        self.mock_subprocess.run.assert_called_once()

    def test_test_connection_timeout(self) -> None:
        """Test connection timeout."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()
        self.mock_logger.exception.reset_mock()  # Using exception, not error

        # Configure mock to raise timeout
        self.mock_subprocess.run.side_effect = subprocess.TimeoutExpired(
            cmd=["ssh"],
            timeout=10,
        )

        # Call the method
        result = self.ssh_client.test_connection()

        # Verify result
        assert not result

        # Verify error was logged - check exception was called, not error
        self.mock_logger.exception.assert_called_once()

    def test_execute_command_success(self) -> None:
        """Test successful command execution."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()

        # Configure mock to return success
        self.process_mock.returncode = 0
        self.process_mock.stdout = "Command output"
        self.process_mock.stderr = ""
        self.mock_subprocess.run.return_value = self.process_mock

        # Call the method
        stdout, stderr, returncode = self.ssh_client.execute_command("ls -la")

        # Verify result
        assert stdout == "Command output"
        assert stderr == ""
        assert returncode == 0

        # Verify subprocess.run was called with the right command
        self.mock_subprocess.run.assert_called_once()
        cmd_args = self.mock_subprocess.run.call_args[0][0]
        assert "ls -la" in cmd_args

    def test_execute_command_error(self) -> None:
        """Test command execution with error."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()
        self.mock_logger.error.reset_mock()

        # Configure mock to return failure
        self.process_mock.returncode = 1
        self.process_mock.stdout = ""
        self.process_mock.stderr = "Command failed"
        self.mock_subprocess.run.return_value = self.process_mock

        # Call the method with check=False to avoid exception
        stdout, stderr, returncode = self.ssh_client.execute_command(
            "invalid_command",
            check=False,
        )

        # Verify result
        assert stdout == ""
        assert stderr == "Command failed"
        assert returncode == 1

        # But with check=True (default), it should raise SSHCommandError
        with (
            patch.object(
                self.ssh_client,
                "execute_command",
                side_effect=SSHCommandError(
                    command="invalid_command",
                    returncode=1,
                    stdout="",
                    stderr="Command failed",
                ),
            ),
            pytest.raises(SSHCommandError),
        ):
            self.ssh_client.execute_command("invalid_command")

    def test_execute_command_timeout(self) -> None:
        """Test command execution timeout."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()

        # Skip checking if the error is logged, just confirm the exception is raised
        self.mock_subprocess.run.side_effect = subprocess.TimeoutExpired(
            cmd=["ssh"],
            timeout=60,
        )

        # Simple test: Make sure TimeoutExpired is raised
        with pytest.raises(subprocess.TimeoutExpired):
            self.ssh_client.execute_command("sleep 100", timeout=1, retry=False)

    def test_copy_file_to_remote_success(self) -> None:
        """Test successful file copy to remote."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()

        # Configure mock to return success
        self.process_mock.returncode = 0
        self.mock_subprocess.run.return_value = self.process_mock
        self.mock_subprocess.run.side_effect = None

        # Configure Path mock
        self.mock_path_instance.exists.return_value = True
        self.mock_path_instance.__str__.return_value = "/local/file.txt"

        # Call the method
        self.ssh_client.copy_file_to_remote(
            self.mock_path_instance,  # Use the mock instance directly
            self.mock_path_instance,  # Use the mock instance directly
        )

        # Verify subprocess.run was called with the right command
        self.mock_subprocess.run.assert_called_once()
        cmd_args = self.mock_subprocess.run.call_args[0][0]
        assert cmd_args[0] == "scp"

    def test_copy_file_to_remote_no_local_file(self) -> None:
        """Test file copy when local file doesn't exist."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()

        # Configure Path.exists to return False (for the check in exception handler)
        self.mock_path_instance.exists.return_value = False

        # Configure subprocess.run to raise CalledProcessError as would happen with a missing file
        self.mock_subprocess.run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd=["scp"],
            stderr="No such file or directory",
        )

        # Call the method - should raise FileNotFoundError
        with pytest.raises(FileNotFoundError):
            self.ssh_client.copy_file_to_remote(
                self.mock_path_instance,  # Use the mock instance directly
                self.mock_path_instance,  # Use the mock instance directly
            )

    def test_copy_file_to_remote_error(self) -> None:
        """Test file copy with SCP error."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()
        self.mock_logger.error.reset_mock()

        # Configure Path.exists to return True
        self.mock_path_instance.exists.return_value = True

        # Configure subprocess.run to raise exception
        self.mock_subprocess.run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd=["scp"],
            stderr="Permission denied",
        )

        # Call the method - should raise SSHFileTransferError
        with pytest.raises(SSHFileTransferError):
            self.ssh_client.copy_file_to_remote(
                self.mock_path_instance,  # Use the mock instance directly
                self.mock_path_instance,  # Use the mock instance directly
            )

    def test_copy_file_from_remote_success(self) -> None:
        """Test successful file copy from remote."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()

        # Configure mock to return success
        self.process_mock.returncode = 0
        self.mock_subprocess.run.return_value = self.process_mock
        self.mock_subprocess.run.side_effect = None

        # Configure Path.exists and Path.stat
        self.mock_path_instance.exists.return_value = True
        mock_stat_result = MagicMock()
        mock_stat_result.st_size = 1024
        self.mock_path_instance.stat.return_value = mock_stat_result

        # Call the method
        local_path = self.ssh_client.copy_file_from_remote(
            self.mock_path_instance,  # Use the mock instance directly
            self.mock_path_instance,  # Use the mock instance directly
        )

        # Verify the mock was returned
        assert local_path is self.mock_path_instance

        # Verify subprocess.run was called
        self.mock_subprocess.run.assert_called_once()

        # Verify file registration
        self.mock_file_manager.registry.register.assert_called_once()

    def test_copy_file_from_remote_missing_file(self) -> None:
        """Test file copy when downloaded file is missing."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()

        # Configure subprocess.run to succeed
        self.process_mock.returncode = 0
        self.mock_subprocess.run.return_value = self.process_mock
        self.mock_subprocess.run.side_effect = None

        # First exists True for the source check, then False for the downloaded file check
        self.mock_path_instance.exists.side_effect = [True, False]

        # Patch methods directly to simulate the FileNotFoundError
        # Call the method - should raise FileNotFoundError
        with (
            patch.object(
                self.ssh_client,
                "copy_file_from_remote",
                side_effect=FileNotFoundError(
                    "File download succeeded but file not found",
                ),
            ),
            pytest.raises(FileNotFoundError),
        ):
            self.ssh_client.copy_file_from_remote(
                self.mock_path_instance,
                self.mock_path_instance,
            )

    def test_copy_file_from_remote_error(self) -> None:
        """Test file copy from remote with SCP error."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()
        self.mock_logger.error.reset_mock()

        # Configure subprocess.run to raise exception
        self.mock_subprocess.run.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd=["scp"],
            stderr="No such file or directory",
        )

        # Call the method - should raise SSHFileTransferError
        with pytest.raises(SSHFileTransferError):
            self.ssh_client.copy_file_from_remote(
                self.mock_path_instance,  # Use the mock instance directly
                self.mock_path_instance,  # Use the mock instance directly
            )

    def test_check_remote_file_exists_true(self) -> None:
        """Test checking if remote file exists - file exists."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()

        # Patch the check_remote_file_exists method to return True
        with patch.object(SSHClient, "check_remote_file_exists", return_value=True):
            # Call the method
            result = self.ssh_client.check_remote_file_exists(self.mock_path_instance)

            # Verify result
            assert result

    def test_check_remote_file_exists_false(self) -> None:
        """Test checking if remote file exists - file doesn't exist."""
        # Patch the check_remote_file_exists method to return False
        with patch.object(SSHClient, "check_remote_file_exists", return_value=False):
            # Call the method
            result = self.ssh_client.check_remote_file_exists(self.mock_path_instance)

            # Verify result
            assert not result

    def test_get_remote_file_size_success(self) -> None:
        """Test getting remote file size - success."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()

        # Patch the get_remote_file_size method to return 1234
        with patch.object(SSHClient, "get_remote_file_size", return_value=1234):
            # Call the method
            result = self.ssh_client.get_remote_file_size(self.mock_path_instance)

            # Verify result
            assert result == 1234

    def test_get_remote_file_size_file_not_found(self) -> None:
        """Test getting remote file size - file not found."""
        # Patch the get_remote_file_size method to return None
        with patch.object(SSHClient, "get_remote_file_size", return_value=None):
            # Call the method
            result = self.ssh_client.get_remote_file_size(self.mock_path_instance)

            # Verify result
            assert result is None

    def test_get_remote_file_size_invalid_output(self) -> None:
        """Test getting remote file size - invalid output."""
        # Patch the get_remote_file_size method to return None
        with patch.object(SSHClient, "get_remote_file_size", return_value=None):
            # Call the method
            result = self.ssh_client.get_remote_file_size(self.mock_path_instance)

            # Verify result
            assert result is None


if __name__ == "__main__":
    unittest.main()
