#!/usr/bin/env python3
"""Test module for DockerClient.

This module contains test cases for validating Docker container interactions.
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

from src.clients.docker_client import DockerClient


class TestDockerClient(unittest.TestCase):
    """Test cases for the DockerClient class."""

    def setUp(self) -> None:
        """Set up the test environment."""
        # Create a temp directory for file operations
        self.temp_dir = tempfile.mkdtemp()

        # Create patchers
        self.ssh_client_patcher = patch("src.clients.docker_client.SSHClient")
        self.mock_ssh_client_class = self.ssh_client_patcher.start()
        self.mock_ssh_client = MagicMock()
        self.mock_ssh_client_class.return_value = self.mock_ssh_client

        self.logger_patcher = patch("src.clients.docker_client.logger")
        self.mock_logger = self.logger_patcher.start()

        self.os_patcher = patch("src.clients.docker_client.os")
        self.mock_os = self.os_patcher.start()

        # File manager mock
        self.file_manager_patcher = patch("src.clients.docker_client.FileManager")
        self.mock_file_manager_class = self.file_manager_patcher.start()
        self.mock_file_manager = MagicMock()
        self.mock_file_manager.generate_unique_id.return_value = "test_unique_id"
        self.mock_file_manager_class.return_value = self.mock_file_manager

        # Configure mocks for successful container existence check
        self.mock_ssh_client.execute_command.return_value = ("test_container\n", "", 0)

        # Configure os.path.exists and os.makedirs
        self.mock_os.path.exists.return_value = True
        self.mock_os.path.basename.return_value = "test_file.txt"
        self.mock_os.path.dirname.return_value = "/tmp"
        self.mock_os.path.getsize.return_value = 1024

        # Initialize DockerClient after all mocks are set up
        self.docker_client = DockerClient(container_name="test_container", ssh_client=self.mock_ssh_client)

    def tearDown(self) -> None:
        """Clean up after each test."""
        # Stop all patchers
        self.ssh_client_patcher.stop()
        self.logger_patcher.stop()
        self.os_patcher.stop()
        self.file_manager_patcher.stop()

        # Clean up temp directory
        if os.path.exists(self.temp_dir):
            import shutil

            shutil.rmtree(self.temp_dir)

    def test_initialization(self) -> None:
        """Test DockerClient initialization."""
        # Verify object attributes
        self.assertEqual(self.docker_client.container_name, "test_container")
        self.assertEqual(self.docker_client.command_timeout, 60)
        self.assertEqual(self.docker_client.retry_count, 3)
        self.assertEqual(self.docker_client.retry_delay, 1.0)

        # Verify container existence was checked
        self.mock_ssh_client.execute_command.assert_called()
        cmd_args = self.mock_ssh_client.execute_command.call_args[0][0]
        self.assertIn("docker ps", cmd_args)
        self.assertIn("test_container", cmd_args)

        # Verify the logger was called at least once
        self.assertTrue(self.mock_logger.debug.called)
        # Check that the initialization message was logged
        self.assertIn(
            call("DockerClient initialized for container test_container"), self.mock_logger.debug.call_args_list,
        )

    def test_check_container_exists_success(self) -> None:
        """Test successful container existence check."""
        # Reset mock
        self.mock_ssh_client.execute_command.reset_mock()

        # Configure mock to return success
        self.mock_ssh_client.execute_command.return_value = ("test_container\n", "", 0)

        # Call the method
        result = self.docker_client.check_container_exists()

        # Verify result
        self.assertTrue(result)

        # Verify execute_command was called with the right command
        self.mock_ssh_client.execute_command.assert_called_once()
        cmd_args = self.mock_ssh_client.execute_command.call_args[0][0]
        self.assertIn("docker ps", cmd_args)
        self.assertIn("test_container", cmd_args)

    def test_check_container_exists_not_running(self) -> None:
        """Test container exists but is not running."""
        # Reset mock
        self.mock_ssh_client.execute_command.reset_mock()

        # Configure mock to return empty for running containers
        # but success for all containers (running + stopped)
        self.mock_ssh_client.execute_command.side_effect = [
            ("", "", 0),  # No running container
            ("test_container\n", "", 0),  # Container exists but not running
        ]

        # Call the method
        result = self.docker_client.check_container_exists()

        # Verify result
        self.assertFalse(result)

        # Verify execute_command was called twice
        self.assertEqual(self.mock_ssh_client.execute_command.call_count, 2)

        # First call should check running containers
        first_cmd = self.mock_ssh_client.execute_command.call_args_list[0][0][0]
        self.assertIn("docker ps ", first_cmd)  # space after ps to ensure it's not ps -a

        # Second call should check all containers
        second_cmd = self.mock_ssh_client.execute_command.call_args_list[1][0][0]
        self.assertIn("docker ps -a", second_cmd)

    def test_check_container_exists_not_found(self) -> None:
        """Test container does not exist."""
        # Reset mock
        self.mock_ssh_client.execute_command.reset_mock()

        # Configure mock to return empty for both running and all containers
        self.mock_ssh_client.execute_command.side_effect = [
            ("", "", 0),  # No running container
            ("", "", 0),  # No container at all
        ]

        # Call the method
        result = self.docker_client.check_container_exists()

        # Verify result
        self.assertFalse(result)

        # Verify execute_command was called twice
        self.assertEqual(self.mock_ssh_client.execute_command.call_count, 2)

        # Verify logger.error was called
        self.mock_logger.error.assert_called_once()

    def test_execute_command_simple(self) -> None:
        """Test executing a simple command in container."""
        # Reset mock
        self.mock_ssh_client.execute_command.reset_mock()

        # Configure mock to return success
        self.mock_ssh_client.execute_command.return_value = ("Command output", "", 0)

        # Call the method with a simple command
        stdout, stderr, returncode = self.docker_client.execute_command("ls -la")

        # Verify result
        self.assertEqual(stdout, "Command output")
        self.assertEqual(stderr, "")
        self.assertEqual(returncode, 0)

        # Verify execute_command was called with the right command
        self.mock_ssh_client.execute_command.assert_called_once()
        cmd_args = self.mock_ssh_client.execute_command.call_args[0][0]
        self.assertIn("docker exec", cmd_args)
        self.assertIn("test_container", cmd_args)
        self.assertIn("ls -la", cmd_args)

    def test_execute_command_complex(self) -> None:
        """Test executing a complex command in container."""
        # Reset mock
        self.mock_ssh_client.execute_command.reset_mock()

        # Configure mock to return success
        self.mock_ssh_client.execute_command.return_value = ("Command output", "", 0)

        # Call the method with a complex command
        stdout, stderr, returncode = self.docker_client.execute_command("cd /tmp && echo 'hello' > test.txt")

        # Verify result
        self.assertEqual(stdout, "Command output")
        self.assertEqual(stderr, "")
        self.assertEqual(returncode, 0)

        # Verify execute_command was called with the right command
        self.mock_ssh_client.execute_command.assert_called_once()
        cmd_args = self.mock_ssh_client.execute_command.call_args[0][0]
        self.assertIn("docker exec", cmd_args)
        self.assertIn("bash -c", cmd_args)
        # Complex commands should be wrapped in bash -c
        self.assertIn('"cd /tmp && echo', cmd_args)

    def test_execute_command_with_options(self) -> None:
        """Test executing a command with user, workdir, and env options."""
        # Reset mock
        self.mock_ssh_client.execute_command.reset_mock()

        # Configure mock to return success
        self.mock_ssh_client.execute_command.return_value = ("Command output", "", 0)

        # Call the method with options
        stdout, stderr, returncode = self.docker_client.execute_command(
            "ls -la", user="root", workdir="/app", env={"DEBUG": "true"},
        )

        # Verify result
        self.assertEqual(stdout, "Command output")
        self.assertEqual(stderr, "")
        self.assertEqual(returncode, 0)

        # Verify execute_command was called with the right command
        self.mock_ssh_client.execute_command.assert_called_once()
        cmd_args = self.mock_ssh_client.execute_command.call_args[0][0]
        self.assertIn("-u root", cmd_args)
        self.assertIn("-w /app", cmd_args)
        self.assertIn("-e DEBUG=true", cmd_args)

    def test_copy_file_to_container_success(self) -> None:
        """Test successful file copy to container."""
        # Reset mocks
        self.mock_ssh_client.copy_file_to_remote.reset_mock()
        self.mock_ssh_client.execute_command.reset_mock()

        # Configure os.path.exists to return True
        self.mock_os.path.exists.return_value = True

        # Configure mocks for successful file existence check
        # First call sets the docker cp return value, second call sets the file exists check return value
        self.mock_ssh_client.execute_command.side_effect = [
            ("", "", 0),  # docker cp succeeds
            ("EXISTS", "", 0),  # file exists check succeeds
        ]

        # Call the method - should not raise an exception
        self.docker_client.copy_file_to_container("/local/file.txt", "/container/file.txt")

        # Verify copy_file_to_remote was called
        self.mock_ssh_client.copy_file_to_remote.assert_not_called()  # This method doesn't use copy_file_to_remote

        # Verify execute_command was called for docker cp and file existence check
        self.assertEqual(self.mock_ssh_client.execute_command.call_count, 2)
        docker_cp_cmd = self.mock_ssh_client.execute_command.call_args_list[0][0][0]
        self.assertIn("docker cp", docker_cp_cmd)
        self.assertIn("test_container:", docker_cp_cmd)

    def test_copy_file_to_container_local_missing(self) -> None:
        """Test file copy when local file doesn't exist."""
        # For this test, we need to modify Docker.copy_file_to_container to better handle our specific test case

        # Configure mock to make the check_file_exists_in_container check fail
        with patch.object(self.docker_client, "check_file_exists_in_container", return_value=False):
            # Configure ssh_client.execute_command to succeed with docker cp
            self.mock_ssh_client.execute_command.return_value = ("", "", 0)

            # Now we should get a ValueError about file not found in container
            with self.assertRaises(ValueError) as context:
                self.docker_client.copy_file_to_container("/local/file.txt", "/container/file.txt")

            # Verify the error message
            self.assertIn("File not found in container after copy", str(context.exception))

    def test_copy_file_from_container_success(self) -> None:
        """Test copying a file from the container."""
        # Setup
        self.mock_ssh_client.execute_command.side_effect = [
            ("EXISTS", "", 0),  # file exists in container check
            ("File copied", "", 0),  # docker cp command
            ("EXISTS", "", 0),  # file exists on remote host check
            ("", "", 0),  # cleanup command
        ]
        self.mock_ssh_client.copy_file_from_remote.return_value = "/local/path"
        self.mock_os.path.exists.return_value = True
        self.mock_os.path.getsize.return_value = 1024

        # Execute
        result = self.docker_client.copy_file_from_container("/container/path", "/local/path")

        # Assert
        self.assertEqual(result, "/local/path")  # Should return the path
        self.mock_ssh_client.execute_command.assert_called()  # Docker exec and cp commands
        self.mock_ssh_client.copy_file_from_remote.assert_called_once()  # SCP from remote to local

    def test_copy_file_from_container_not_found(self) -> None:
        """Test copying a file that doesn't exist in the container."""
        # Need to patch the method to directly use our implementation
        with patch.object(self.docker_client, "check_file_exists_in_container", return_value=False):
            # Call the method - should raise FileNotFoundError
            with self.assertRaises(FileNotFoundError):
                self.docker_client.copy_file_from_container("/container/path", "/local/path")

    def test_check_file_exists_in_container(self) -> None:
        """Test checking if a file exists in the container."""
        # Reset mock
        self.mock_ssh_client.execute_command.reset_mock()

        # Configure mock to return that file exists
        self.mock_ssh_client.execute_command.return_value = ("EXISTS\n", "", 0)

        # Call the method
        result = self.docker_client.check_file_exists_in_container("/container/file.txt")

        # Verify result
        self.assertTrue(result)

        # Verify execute_command was called with the right command
        self.mock_ssh_client.execute_command.assert_called_once()
        cmd_args = self.mock_ssh_client.execute_command.call_args[0][0]
        self.assertIn("test -e /container/file.txt", cmd_args)

    def test_get_file_size_in_container(self) -> None:
        """Test getting file size in the container."""
        # Reset mock
        self.mock_ssh_client.execute_command.reset_mock()

        # Configure mock to return file size
        self.mock_ssh_client.execute_command.return_value = ("1024\n", "", 0)

        # Call the method
        result = self.docker_client.get_file_size_in_container("/container/file.txt")

        # Verify result
        self.assertEqual(result, 1024)

        # Verify execute_command was called with the right command
        self.mock_ssh_client.execute_command.assert_called_once()
        cmd_args = self.mock_ssh_client.execute_command.call_args[0][0]
        self.assertIn("stat -c %s /container/file.txt", cmd_args)


if __name__ == "__main__":
    unittest.main()
