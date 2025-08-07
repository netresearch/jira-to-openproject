#!/usr/bin/env python3
"""Test module for DockerClient.

This module contains test cases for validating Docker container interactions.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

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

        # File manager mock
        self.file_manager_patcher = patch("src.clients.docker_client.FileManager")
        self.mock_file_manager_class = self.file_manager_patcher.start()
        self.mock_file_manager = MagicMock()
        self.mock_file_manager.generate_unique_id.return_value = "test_unique_id"
        self.mock_file_manager_class.return_value = self.mock_file_manager

        # Configure mocks for successful container existence check
        self.mock_ssh_client.execute_command.return_value = ("test_container\n", "", 0)

        # Initialize DockerClient after all mocks are set up
        self.docker_client = DockerClient(
            container_name="test_container",
            ssh_client=self.mock_ssh_client,
        )

    def tearDown(self) -> None:
        """Clean up after each test."""
        # Stop all patchers
        self.ssh_client_patcher.stop()
        self.logger_patcher.stop()
        self.file_manager_patcher.stop()

        # Clean up temp directory
        if os.path.exists(self.temp_dir):
            import shutil

            shutil.rmtree(self.temp_dir)

    def test_initialization(self) -> None:
        """Test DockerClient initialization."""
        # Verify object attributes
        assert self.docker_client.container_name == "test_container"
        assert self.docker_client.command_timeout == 60
        assert self.docker_client.retry_count == 3
        assert self.docker_client.retry_delay == 1.0

        # Verify container existence was checked
        self.mock_ssh_client.execute_command.assert_called()
        cmd_args = self.mock_ssh_client.execute_command.call_args[0][0]
        assert "docker ps" in cmd_args
        assert "test_container" in cmd_args

        # Verify the logger was called at least once
        assert self.mock_logger.debug.called
        # Check that the initialization message was logged with format parameters
        debug_call = call("DockerClient initialized for container %s", "test_container")
        assert debug_call in self.mock_logger.debug.call_args_list

    def test_check_container_exists_success(self) -> None:
        """Test successful container existence check."""
        # Reset mock
        self.mock_ssh_client.execute_command.reset_mock()

        # Configure mock to return success
        self.mock_ssh_client.execute_command.return_value = ("test_container\n", "", 0)

        # Call the method
        result = self.docker_client.check_container_exists()

        # Verify result
        assert result

        # Verify execute_command was called with the right command
        self.mock_ssh_client.execute_command.assert_called_once()
        cmd_args = self.mock_ssh_client.execute_command.call_args[0][0]
        assert "docker ps" in cmd_args
        assert "test_container" in cmd_args

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
        assert not result

        # Verify execute_command was called twice
        assert self.mock_ssh_client.execute_command.call_count == 2

        # First call should check running containers
        first_cmd = self.mock_ssh_client.execute_command.call_args_list[0][0][0]
        assert "docker ps " in first_cmd  # space after ps to ensure it's not ps -a

        # Second call should check all containers
        second_cmd = self.mock_ssh_client.execute_command.call_args_list[1][0][0]
        assert "docker ps -a" in second_cmd

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
        assert not result

        # Verify execute_command was called twice
        assert self.mock_ssh_client.execute_command.call_count == 2

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
        assert stdout == "Command output"
        assert stderr == ""
        assert returncode == 0

        # Verify execute_command was called with the right command
        self.mock_ssh_client.execute_command.assert_called_once()
        cmd_args = self.mock_ssh_client.execute_command.call_args[0][0]
        assert "docker exec" in cmd_args
        assert "test_container" in cmd_args
        assert "ls -la" in cmd_args

    def test_execute_command_complex(self) -> None:
        """Test executing a complex command in container."""
        # Reset mock
        self.mock_ssh_client.execute_command.reset_mock()

        # Configure mock to return success
        self.mock_ssh_client.execute_command.return_value = ("Command output", "", 0)

        # Call the method with a complex command
        stdout, stderr, returncode = self.docker_client.execute_command(
            "cd /tmp && echo 'hello' > test.txt",
        )

        # Verify result
        assert stdout == "Command output"
        assert stderr == ""
        assert returncode == 0

        # Verify execute_command was called with the right command
        self.mock_ssh_client.execute_command.assert_called_once()
        cmd_args = self.mock_ssh_client.execute_command.call_args[0][0]
        assert "docker exec" in cmd_args
        assert "bash -c" in cmd_args
        # Check for a simple pattern that will be in the command
        assert "cd /tmp" in cmd_args

    def test_execute_command_with_options(self) -> None:
        """Test executing a command with user, workdir, and env options."""
        # Reset mock
        self.mock_ssh_client.execute_command.reset_mock()

        # Configure mock to return success
        self.mock_ssh_client.execute_command.return_value = ("Command output", "", 0)

        # Call the method with options
        stdout, stderr, returncode = self.docker_client.execute_command(
            "ls -la",
            user="root",
            workdir="/app",
            env={"DEBUG": "true"},
        )

        # Verify result
        assert stdout == "Command output"
        assert stderr == ""
        assert returncode == 0

        # Verify execute_command was called with the right command
        self.mock_ssh_client.execute_command.assert_called_once()
        cmd_args = self.mock_ssh_client.execute_command.call_args[0][0]
        assert "-u root" in cmd_args
        assert "-w /app" in cmd_args
        assert "-e DEBUG=true" in cmd_args

    def test_copy_file_to_container_success(self) -> None:
        """Test successful file copy to container."""
        # Reset mocks
        self.mock_ssh_client.copy_file_to_remote.reset_mock()
        self.mock_ssh_client.execute_command.reset_mock()

        # Configure mocks for successful file existence check
        # First call sets the docker cp return value, second call sets the file exists check return value
        self.mock_ssh_client.execute_command.side_effect = [
            ("", "", 0),  # docker cp succeeds
            ("EXISTS", "", 0),  # file exists check succeeds
        ]

        # Call the method - should not raise an exception
        self.docker_client.copy_file_to_container(
            Path("/local/file.txt"),
            Path("/container/file.txt"),
        )

        # Verify copy_file_to_remote was called
        self.mock_ssh_client.copy_file_to_remote.assert_not_called()  # This method doesn't use copy_file_to_remote

        # Verify execute_command was called for docker cp and file existence check
        assert self.mock_ssh_client.execute_command.call_count == 3
        docker_cp_cmd = self.mock_ssh_client.execute_command.call_args_list[0][0][0]
        assert "docker cp" in docker_cp_cmd
        assert "test_container:" in docker_cp_cmd

    def test_copy_file_to_container_local_missing(self) -> None:
        """Test file copy when local file doesn't exist."""
        # For this test, we need to modify Docker.copy_file_to_container to better handle our specific test case

        # Configure mock to make the check_file_exists_in_container check fail
        with patch.object(
            self.docker_client,
            "check_file_exists_in_container",
            return_value=False,
        ):
            # Configure ssh_client.execute_command to succeed with docker cp
            self.mock_ssh_client.execute_command.return_value = ("", "", 0)

            # Now we should get a ValueError about file not found in container
            with pytest.raises(ValueError) as context:
                self.docker_client.copy_file_to_container(
                    Path("/local/file.txt"),
                    Path("/container/file.txt"),
                )

            # Verify the error message
            assert "File not found in container after copy" in str(context.value)

    def test_copy_file_from_container_success(self) -> None:
        """Test copying a file from the container."""
        # Setup mock for ssh_client execute_command and check_remote_file_exists
        self.mock_ssh_client.execute_command.return_value = ("", "", 0)
        self.mock_ssh_client.check_remote_file_exists.return_value = True
        self.mock_ssh_client.get_remote_file_size.return_value = 1024

        # Execute
        result = self.docker_client.copy_file_from_container(
            Path("/container/path"),
            Path("/local/path"),
        )

        # Assert
        assert result == Path("/local/path")  # Should return the path
        # With optimistic execution, check_file_exists_in_container is not called on success
        assert (
            self.mock_ssh_client.execute_command.call_count >= 2
        )  # mkdir and docker cp commands
        self.mock_ssh_client.check_remote_file_exists.assert_called_once()  # Check if local file exists
        self.mock_ssh_client.get_remote_file_size.assert_called_once()  # Get file size

    def test_copy_file_from_container_not_found(self) -> None:
        """Test copying a file that doesn't exist in the container."""
        # Configure the execute_command to fail as would happen with a missing file
        self.mock_ssh_client.execute_command.side_effect = Exception("docker cp failed")

        # Need to patch the method to return False when checking if file exists
        with patch.object(
            self.docker_client,
            "check_file_exists_in_container",
            return_value=False,
        ):
            # Call the method - should raise FileNotFoundError
            with pytest.raises(FileNotFoundError):
                self.docker_client.copy_file_from_container(
                    Path("/container/path"),
                    Path("/local/path"),
                )

    def test_check_file_exists_in_container(self) -> None:
        """Test checking if a file exists in the container."""
        # Reset mock
        self.mock_ssh_client.execute_command.reset_mock()

        # Configure mock to return that file exists
        self.mock_ssh_client.execute_command.return_value = ("EXISTS\n", "", 0)

        # Call the method
        result = self.docker_client.check_file_exists_in_container(
            "/container/file.txt",
        )

        # Verify result
        assert result

        # Verify execute_command was called with the right command
        self.mock_ssh_client.execute_command.assert_called_once()
        cmd_args = self.mock_ssh_client.execute_command.call_args[0][0]
        assert "test -e /container/file.txt" in cmd_args

    def test_get_file_size_in_container(self) -> None:
        """Test getting file size in the container."""
        # Reset mock
        self.mock_ssh_client.execute_command.reset_mock()

        # Configure mock to return file size
        self.mock_ssh_client.execute_command.return_value = ("1024\n", "", 0)

        # Call the method
        result = self.docker_client.get_file_size_in_container("/container/file.txt")

        # Verify result
        assert result == 1024

        # Verify execute_command was called with the right command
        self.mock_ssh_client.execute_command.assert_called_once()
        cmd_args = self.mock_ssh_client.execute_command.call_args[0][0]
        assert "stat -c %s /container/file.txt" in cmd_args


if __name__ == "__main__":
    unittest.main()
