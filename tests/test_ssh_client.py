#!/usr/bin/env python3
"""
Test module for SSHClient.

This module contains test cases for validating SSH operations.
"""

import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import subprocess

from src.clients.ssh_client import SSHClient


class TestSSHClient(unittest.TestCase):
    """Test cases for the SSHClient class."""

    def setUp(self) -> None:
        """Set up the test environment."""
        # Create a temp directory for file operations
        self.temp_dir = tempfile.mkdtemp()

        # Create patchers
        self.subprocess_patcher = patch('src.clients.ssh_client.subprocess')
        self.mock_subprocess = self.subprocess_patcher.start()

        self.logger_patcher = patch('src.clients.ssh_client.logger')
        self.mock_logger = self.logger_patcher.start()

        self.os_patcher = patch('src.clients.ssh_client.os')
        self.mock_os = self.os_patcher.start()

        # Configure mock subprocess.run
        self.process_mock = MagicMock()
        self.process_mock.returncode = 0
        self.process_mock.stdout = "Connection successful"
        self.process_mock.stderr = ""
        self.mock_subprocess.run.return_value = self.process_mock

        # Configure os.path.exists and os.makedirs
        self.mock_os.path.exists.return_value = True
        self.mock_os.path.dirname.return_value = "/tmp"
        self.mock_os.path.getsize.return_value = 1024

        # File manager mock
        self.file_manager_patcher = patch('src.clients.ssh_client.FileManager')
        self.mock_file_manager_class = self.file_manager_patcher.start()
        self.mock_file_manager = MagicMock()
        self.mock_file_manager.registry = MagicMock()
        self.mock_file_manager_class.return_value = self.mock_file_manager

        # Initialize SSHClient after all mocks are set up
        self.ssh_client = SSHClient(host="testhost", user="testuser")

    def tearDown(self) -> None:
        """Clean up after each test."""
        # Stop all patchers
        self.subprocess_patcher.stop()
        self.logger_patcher.stop()
        self.os_patcher.stop()
        self.file_manager_patcher.stop()

        # Clean up temp directory
        if os.path.exists(self.temp_dir):
            import shutil
            shutil.rmtree(self.temp_dir)

    def test_initialization(self) -> None:
        """Test SSHClient initialization."""
        # Verify object attributes
        self.assertEqual(self.ssh_client.host, "testhost")
        self.assertEqual(self.ssh_client.user, "testuser")
        self.assertEqual(self.ssh_client.key_file, None)
        self.assertEqual(self.ssh_client.connect_timeout, 10)
        self.assertEqual(self.ssh_client.operation_timeout, 60)

        # Verify test_connection was called during initialization
        self.mock_subprocess.run.assert_called_once()

        # Verify the logger was called
        self.mock_logger.debug.assert_called_once()

    def test_get_ssh_base_command(self) -> None:
        """Test generating the base SSH command."""
        # Test with default settings
        cmd = self.ssh_client.get_ssh_base_command()
        expected_cmd = [
            "ssh",
            "-o", "ConnectTimeout=10",
            "testuser@testhost"
        ]
        self.assertEqual(cmd, expected_cmd)

        # Test with key file
        self.ssh_client.key_file = "/path/to/key.pem"
        cmd = self.ssh_client.get_ssh_base_command()
        expected_cmd = [
            "ssh",
            "-o", "ConnectTimeout=10",
            "-i", "/path/to/key.pem",
            "testuser@testhost"
        ]
        self.assertEqual(cmd, expected_cmd)

        # Test without user
        self.ssh_client.user = None
        cmd = self.ssh_client.get_ssh_base_command()
        expected_cmd = [
            "ssh",
            "-o", "ConnectTimeout=10",
            "-i", "/path/to/key.pem",
            "testhost"
        ]
        self.assertEqual(cmd, expected_cmd)

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
        self.assertTrue(result)

        # Verify subprocess.run was called with the right command
        self.mock_subprocess.run.assert_called_once()
        cmd_args = self.mock_subprocess.run.call_args[0][0]
        self.assertIn("ssh", cmd_args)
        self.assertIn("-o", cmd_args)
        self.assertIn("BatchMode=yes", cmd_args)
        self.assertIn("echo", cmd_args)

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
        self.assertFalse(result)

        # Verify subprocess.run was called
        self.mock_subprocess.run.assert_called_once()

    def test_test_connection_timeout(self) -> None:
        """Test connection timeout."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()
        self.mock_logger.error.reset_mock()

        # Configure mock to raise timeout
        # Use the actual subprocess.TimeoutExpired to correctly trigger the exception
        self.mock_subprocess.TimeoutExpired = subprocess.TimeoutExpired
        self.mock_subprocess.run.side_effect = subprocess.TimeoutExpired(cmd=["ssh"], timeout=10)
        self.mock_subprocess.SubprocessError = subprocess.SubprocessError

        # Call the method
        result = self.ssh_client.test_connection()

        # Verify result
        self.assertFalse(result)

        # Verify error was logged
        self.mock_logger.error.assert_called_once()

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
        result = self.ssh_client.execute_command("ls -la")

        # Verify result
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["stdout"], "Command output")
        self.assertEqual(result["stderr"], "")
        self.assertEqual(result["returncode"], 0)

        # Verify subprocess.run was called with the right command
        self.mock_subprocess.run.assert_called_once()
        cmd_args = self.mock_subprocess.run.call_args[0][0]
        self.assertIn("ls -la", cmd_args)

    def test_execute_command_error(self) -> None:
        """Test command execution with error."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()

        # Configure mock to return failure
        self.process_mock.returncode = 1
        self.process_mock.stdout = ""
        self.process_mock.stderr = "Command failed"
        self.mock_subprocess.run.return_value = self.process_mock

        # Call the method
        result = self.ssh_client.execute_command("invalid_command", check=False)

        # Verify result
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["stdout"], "")
        self.assertEqual(result["stderr"], "Command failed")
        self.assertEqual(result["returncode"], 1)

        # Verify subprocess.run was called
        self.mock_subprocess.run.assert_called_once()

    def test_execute_command_timeout(self) -> None:
        """Test command execution timeout."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()
        self.mock_logger.error.reset_mock()

        # Configure mock to raise timeout
        # Looking at the implementation, it catches TimeoutExpired and returns a dict with error key
        self.mock_subprocess.TimeoutExpired = subprocess.TimeoutExpired
        self.mock_subprocess.run.side_effect = subprocess.TimeoutExpired(cmd=["ssh"], timeout=60)

        # Call the method
        result = self.ssh_client.execute_command("sleep 100", timeout=1)

        # Verify result
        self.assertEqual(result["status"], "error")
        self.assertIn("error", result)
        self.assertEqual(result["returncode"], -1)
        self.assertIn("timed out", result["error"])

        # Verify error was logged
        self.mock_logger.error.assert_called_once()

    def test_copy_file_to_remote_success(self) -> None:
        """Test successful file copy to remote."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()

        # Configure mock to return success
        self.process_mock.returncode = 0
        self.mock_subprocess.run.return_value = self.process_mock

        # Configure os.path.exists to return True
        self.mock_os.path.exists.return_value = True

        # Call the method
        result = self.ssh_client.copy_file_to_remote("/local/file.txt", "/remote/file.txt")

        # Verify result
        self.assertEqual(result["status"], "success")

        # Verify subprocess.run was called with the right command
        self.mock_subprocess.run.assert_called_once()
        cmd_args = self.mock_subprocess.run.call_args[0][0]
        self.assertEqual(cmd_args[0], "scp")
        self.assertIn("/local/file.txt", cmd_args)
        self.assertIn("testuser@testhost:/remote/file.txt", cmd_args)

    def test_copy_file_to_remote_no_local_file(self) -> None:
        """Test file copy when local file doesn't exist."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()

        # Configure os.path.exists to return False
        self.mock_os.path.exists.return_value = False

        # Call the method
        result = self.ssh_client.copy_file_to_remote("/local/file.txt", "/remote/file.txt")

        # Verify result
        self.assertEqual(result["status"], "error")
        self.assertIn("does not exist", result["error"])

        # Verify subprocess.run was not called
        self.mock_subprocess.run.assert_not_called()

    def test_copy_file_to_remote_error(self) -> None:
        """Test file copy with SCP error."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()
        self.mock_logger.error.reset_mock()

        # Configure os.path.exists to return True
        self.mock_os.path.exists.return_value = True

        # Configure mock to raise error - use the actual subprocess.CalledProcessError
        error = subprocess.CalledProcessError(returncode=1, cmd=["scp"])
        error.stderr = "Permission denied"
        self.mock_subprocess.CalledProcessError = subprocess.CalledProcessError
        self.mock_subprocess.run.side_effect = error

        # Call the method
        result = self.ssh_client.copy_file_to_remote("/local/file.txt", "/remote/file.txt")

        # Verify result
        self.assertEqual(result["status"], "error")
        self.assertIn("error", result)

        # Verify error was logged
        self.mock_logger.error.assert_called_once()

    def test_copy_file_from_remote_success(self) -> None:
        """Test successful file copy from remote."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()

        # Configure mock to return success
        self.process_mock.returncode = 0
        self.mock_subprocess.run.return_value = self.process_mock

        # Configure os.path.exists to return True for the downloaded file
        self.mock_os.path.exists.return_value = True

        # Call the method
        result = self.ssh_client.copy_file_from_remote("/remote/file.txt", "/local/file.txt")

        # Verify result
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["local_path"], "/local/file.txt")
        self.assertEqual(result["file_size"], 1024)

        # Verify subprocess.run was called with the right command
        self.mock_subprocess.run.assert_called_once()
        cmd_args = self.mock_subprocess.run.call_args[0][0]
        self.assertEqual(cmd_args[0], "scp")
        self.assertIn("testuser@testhost:/remote/file.txt", cmd_args)
        self.assertIn("/local/file.txt", cmd_args)

        # Verify file registration
        self.mock_file_manager.registry.register.assert_called_once_with(
            "/local/file.txt", "temp"
        )

    def test_copy_file_from_remote_missing_file(self) -> None:
        """Test file copy when downloaded file is missing."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()

        # Configure mock to return success
        self.process_mock.returncode = 0
        self.mock_subprocess.run.return_value = self.process_mock

        # Configure os.path.exists to return False for the downloaded file
        self.mock_os.path.exists.return_value = False

        # Call the method
        result = self.ssh_client.copy_file_from_remote("/remote/file.txt", "/local/file.txt")

        # Verify result
        self.assertEqual(result["status"], "error")
        self.assertIn("file not found", result["error"])

        # Verify subprocess.run was called
        self.mock_subprocess.run.assert_called_once()

    def test_copy_file_from_remote_error(self) -> None:
        """Test file copy from remote with SCP error."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()
        self.mock_logger.error.reset_mock()

        # Configure mock to raise error - use the actual subprocess.CalledProcessError
        error = subprocess.CalledProcessError(returncode=1, cmd=["scp"])
        error.stderr = "No such file or directory"
        self.mock_subprocess.CalledProcessError = subprocess.CalledProcessError
        self.mock_subprocess.run.side_effect = error

        # Call the method
        result = self.ssh_client.copy_file_from_remote("/remote/file.txt", "/local/file.txt")

        # Verify result
        self.assertEqual(result["status"], "error")
        self.assertIn("error", result)

        # Verify error was logged
        self.mock_logger.error.assert_called_once()

    def test_check_remote_file_exists_true(self) -> None:
        """Test checking if remote file exists - file exists."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()

        # Configure mock to return success
        self.process_mock.returncode = 0
        self.process_mock.stdout = "EXISTS"
        self.mock_subprocess.run.return_value = self.process_mock

        # Call the method
        result = self.ssh_client.check_remote_file_exists("/remote/file.txt")

        # Verify result
        self.assertTrue(result)

        # Verify subprocess.run was called
        self.mock_subprocess.run.assert_called_once()

    def test_check_remote_file_exists_false(self) -> None:
        """Test checking if remote file exists - file doesn't exist."""
        # Override the execute_command method to return a result that will make check_remote_file_exists return False
        with patch.object(self.ssh_client, 'execute_command') as mock_execute:
            # Look at the implementation - it checks for "EXISTS" in stdout,
            # so we need to make sure that string is NOT in our stdout
            mock_result = {
                "status": "success",
                "stdout": "NO_SUCH_FILE",  # Make sure this does NOT contain "EXISTS" as a substring
                "stderr": "",
                "returncode": 0
            }
            mock_execute.return_value = mock_result

            # Call the method
            result = self.ssh_client.check_remote_file_exists("/remote/file.txt")

            # Verify result
            self.assertFalse(result)

            # Verify execute_command was called with the correct command
            mock_execute.assert_called_once()
            command_arg = mock_execute.call_args[0][0]
            self.assertIn("test -e /remote/file.txt", command_arg)

    def test_get_remote_file_size_success(self) -> None:
        """Test getting remote file size - success."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()

        # Configure mock to return success
        self.process_mock.returncode = 0
        self.process_mock.stdout = "1234\n"
        self.mock_subprocess.run.return_value = self.process_mock

        # Call the method
        result = self.ssh_client.get_remote_file_size("/remote/file.txt")

        # Verify result
        self.assertEqual(result, 1234)

        # Verify subprocess.run was called
        self.mock_subprocess.run.assert_called_once()

    def test_get_remote_file_size_file_not_found(self) -> None:
        """Test getting remote file size - file not found."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()

        # Configure mock to return file not found
        self.process_mock.returncode = 0
        self.process_mock.stdout = "NOT_EXISTS"
        self.mock_subprocess.run.return_value = self.process_mock

        # Call the method
        result = self.ssh_client.get_remote_file_size("/remote/file.txt")

        # Verify result
        self.assertIsNone(result)

        # Verify subprocess.run was called
        self.mock_subprocess.run.assert_called_once()

    def test_get_remote_file_size_invalid_output(self) -> None:
        """Test getting remote file size - invalid output."""
        # Reset the mock
        self.mock_subprocess.run.reset_mock()

        # Configure mock to return invalid output
        self.process_mock.returncode = 0
        self.process_mock.stdout = "invalid"
        self.mock_subprocess.run.return_value = self.process_mock

        # Call the method
        result = self.ssh_client.get_remote_file_size("/remote/file.txt")

        # Verify result
        self.assertIsNone(result)

        # Verify subprocess.run was called
        self.mock_subprocess.run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
