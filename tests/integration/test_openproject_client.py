#!/usr/bin/env python3
"""Test module for OpenProjectClient.

This module contains test cases for validating the OpenProjectClient as the top-level
component in the refactored client architecture, focusing on proper dependency injection,
delegation to underlying clients, file transfers, error handling, and command execution workflows.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.clients.docker_client import DockerClient
from src.clients.openproject_client import FileTransferError, OpenProjectClient
from src.clients.rails_console_client import CommandExecutionError, RailsConsoleClient
from src.clients.ssh_client import SSHClient


class TestOpenProjectClient(unittest.TestCase):
    """Test cases for the OpenProjectClient class."""

    def setUp(self) -> None:
        """Set up the test environment."""
        # Create a temp directory for file operations
        self.temp_dir = Path(tempfile.mkdtemp())
        # Create the temp_scripts directory to ensure it exists
        self.scripts_dir = self.temp_dir / "temp_scripts"
        self.scripts_dir.mkdir(parents=True, exist_ok=True)

        # Create patchers for all client dependencies
        # 1. SSHClient patcher
        self.ssh_client_patcher = patch("src.clients.openproject_client.SSHClient")
        self.mock_ssh_client_class = self.ssh_client_patcher.start()
        self.mock_ssh_client = MagicMock(spec=SSHClient)
        self.mock_ssh_client_class.return_value = self.mock_ssh_client

        # 2. DockerClient patcher
        self.docker_client_patcher = patch(
            "src.clients.openproject_client.DockerClient"
        )
        self.mock_docker_client_class = self.docker_client_patcher.start()
        self.mock_docker_client = MagicMock(spec=DockerClient)
        # Set the return value for the execute_command method to ensure it returns 3 values
        self.mock_docker_client.execute_command.return_value = ("stdout", "stderr", 0)
        # Set check_file_exists_in_container to return True
        self.mock_docker_client.check_file_exists_in_container.return_value = True
        self.mock_docker_client_class.return_value = self.mock_docker_client

        # 3. RailsConsoleClient patcher
        self.rails_client_patcher = patch(
            "src.clients.openproject_client.RailsConsoleClient"
        )
        self.mock_rails_client_class = self.rails_client_patcher.start()
        self.mock_rails_client = MagicMock(spec=RailsConsoleClient)
        self.mock_rails_client_class.return_value = self.mock_rails_client

        # Mock config module
        self.config_patcher = patch("src.clients.openproject_client.config")
        self.mock_config = self.config_patcher.start()
        self.mock_config.openproject_config = {
            "container": "test_container",
            "server": "test_server",
            "user": "test_user",
            "key_file": "/path/to/key.pem",
            "tmux_session_name": "test_session",
        }
        self.mock_config.logger = MagicMock()

        # Mock FileManager
        self.file_manager_patcher = patch("src.clients.openproject_client.FileManager")
        self.mock_file_manager_class = self.file_manager_patcher.start()
        self.mock_file_manager = MagicMock()
        self.mock_file_manager.data_dir = self.temp_dir
        self.mock_file_manager_class.return_value = self.mock_file_manager

        # Mock os functions
        self.os_patcher = patch("src.clients.openproject_client.os")
        self.mock_os = self.os_patcher.start()
        self.mock_os.path.exists.return_value = True
        self.mock_os.path.basename.side_effect = os.path.basename  # Use real basename
        self.mock_os.path.getsize.return_value = 1024
        self.mock_os.path.join.side_effect = os.path.join  # Use real join function
        self.mock_os.path.abspath.side_effect = (
            lambda x: x
        )  # Simplify abspath for testing
        self.mock_os.makedirs.side_effect = (
            lambda path, exist_ok=False: None
        )  # Do nothing but don't raise error
        self.mock_os.access.return_value = True
        self.mock_os.unlink = MagicMock()
        self.mock_os.urandom.return_value = (
            b"1234"  # Mock urandom for deterministic testing
        )

        # Initialize OpenProjectClient after all mocks are set up
        self.op_client = OpenProjectClient()

    def tearDown(self) -> None:
        """Clean up after each test."""
        # Stop all patchers
        self.ssh_client_patcher.stop()
        self.docker_client_patcher.stop()
        self.rails_client_patcher.stop()
        self.config_patcher.stop()
        self.file_manager_patcher.stop()
        self.os_patcher.stop()

        # Clean up temp directory
        if self.temp_dir.exists():
            import shutil

            shutil.rmtree(self.temp_dir)

    def test_initialization(self) -> None:
        """Test initialization with explicit parameters."""
        # Create a client with explicit parameters
        op_client = OpenProjectClient(
            container_name="test_container",
            ssh_host="test.example.com",
            ssh_user="test_user",
            tmux_session_name="test_session",
            command_timeout=60,
            ssh_client=MagicMock(spec=SSHClient),
            docker_client=MagicMock(spec=DockerClient),
            rails_client=MagicMock(spec=RailsConsoleClient),
        )

        # Check that the parameters were stored correctly
        assert op_client.container_name == "test_container"
        assert op_client.ssh_host == "test.example.com"
        assert op_client.ssh_user == "test_user"
        assert op_client.tmux_session_name == "test_session"
        assert op_client.command_timeout == 60

    def test_init_missing_container(self) -> None:
        """Test initialization with missing container name."""
        # Create a config with missing container name
        temp_config = self.mock_config.openproject_config.copy()
        temp_config["container"] = None
        self.mock_config.openproject_config = temp_config

        # Verify ValueError is raised
        with pytest.raises(ValueError) as context:
            OpenProjectClient()

        assert "Container name is required" in str(context.value)

    def test_init_missing_ssh_host(self) -> None:
        """Test initialization with missing SSH host."""
        # Create a config with missing SSH host
        temp_config = self.mock_config.openproject_config.copy()
        temp_config["server"] = None
        self.mock_config.openproject_config = temp_config

        # Verify ValueError is raised
        with pytest.raises(ValueError) as context:
            OpenProjectClient()

        assert "SSH host is required" in str(context.value)

    def test_create_script_file(self) -> None:
        """Test script file creation."""
        test_script = "puts 'Hello, World!'"

        # Mock the data_dir path and os.urandom for the random filename
        with patch("src.clients.openproject_client.os.urandom", return_value=b"abcd"):
            # Mock Path.open to return a mock file
            mock_file = MagicMock()
            mock_file.__enter__.return_value = mock_file

            with patch("pathlib.Path.mkdir") as mock_mkdir:
                with patch("pathlib.Path.open", return_value=mock_file) as mock_open:
                    # Call the method being tested
                    result = self.op_client._create_script_file(test_script)

                    # Verify the result is a Path with the expected name
                    assert str(result).endswith("openproject_script_61626364.rb")

                    # Verify directory creation
                    mock_mkdir.assert_called_with(parents=True, exist_ok=True)

                    # Verify file was opened
                    mock_open.assert_called_once()

                    # Verify content was written
                    mock_file.write.assert_called_once_with(test_script)

    def test_transfer_and_execute_script_success(self) -> None:
        """Test successful script transfer and execution."""
        # Initialize the expected result
        expected_result = '{"result": 42}'

        # Patch the rails_client to return the expected result
        with patch.object(
            self.op_client.rails_client,
            "_send_command_to_tmux",
            return_value=expected_result,
        ) as mock_send_command:
            # Execute a query (which now directly calls _send_command_to_tmux)
            result = self.op_client.execute_query("puts 'test'")

            # Verify the result
            assert result == expected_result

            # Verify the method calls
            mock_send_command.assert_called_once()

    def test_transfer_and_execute_script_ssh_failure(self) -> None:
        """Test script execution failing during SSH transfer."""
        # Mock the rails_client._send_command_to_tmux to throw a CommandExecutionError
        self.mock_rails_client._send_command_to_tmux.side_effect = (
            CommandExecutionError(
                "SSH transfer failed: Connection refused",
            )
        )

        # Execute the test with expected exception
        with pytest.raises(CommandExecutionError) as context:
            self.op_client.execute_query("puts 'test'")

        # Verify error message
        assert "Connection refused" in str(context.value)

    def test_transfer_and_execute_script_docker_failure(self) -> None:
        """Test script execution failing during Docker transfer."""
        # Mock the rails_client._send_command_to_tmux to throw a CommandExecutionError
        self.mock_rails_client._send_command_to_tmux.side_effect = (
            CommandExecutionError(
                "Failed to copy script to container",
            )
        )

        # Execute the test with expected exception
        with pytest.raises(CommandExecutionError) as context:
            self.op_client.execute_query("puts 'test'")

        # Verify error message
        assert "Failed to copy script to container" in str(context.value)

    def test_execute_query_success(self) -> None:
        """Test successful query execution."""
        # Mock rails_client._send_command_to_tmux to return a specific value
        self.mock_rails_client._send_command_to_tmux.return_value = "42"

        # Execute the query
        result = self.op_client.execute_query("puts 'hello world'")

        # Verify result
        assert result == "42"

        # Verify mock was called with properly wrapped query
        self.mock_rails_client._send_command_to_tmux.assert_called_once()
        command_arg = self.mock_rails_client._send_command_to_tmux.call_args[0][0]
        assert "puts (puts 'hello world')" in command_arg

    def test_execute_query_error(self) -> None:
        """Test query execution with error."""
        # Mock rails_client._send_command_to_tmux to raise an exception
        self.mock_rails_client._send_command_to_tmux.side_effect = (
            CommandExecutionError("Query execution failed")
        )

        # Execute the query with error expected
        with pytest.raises(CommandExecutionError) as context:
            self.op_client.execute_query("invalid syntax")

        # Verify error message
        assert "Query execution failed" in str(context.value)

    def test_execute_script(self) -> None:
        """Test direct script execution."""
        # Mock execute_query instead of _transfer_and_execute_script
        with patch.object(self.op_client, "execute_query") as mock_execute:
            # Return a string that can't be parsed as JSON
            mock_execute.return_value = "Script executed successfully"

            # Call the execute method (which delegates to execute_query)
            script_content = "puts 'Hello, World!'"
            result = self.op_client.execute(script_content)

            # Verify result - execute method returns dict with 'result' key when it can't parse as JSON
            assert isinstance(result, dict)
            assert "result" in result
            assert result["result"] == "Script executed successfully"

            # Verify mock was called with the right content
            mock_execute.assert_called_once_with(script_content)

    def test_file_transfer_methods(self) -> None:
        """Test file transfer methods."""
        # For transfer to container test
        local_path = Path("/tmp/test_script.rb")
        container_path = Path("/container/test_script.rb")

        # Test transfer_file_to_container
        self.op_client.transfer_file_to_container(local_path, container_path)

        # Verify docker_client.transfer_file_to_container was called with the correct paths
        self.mock_docker_client.transfer_file_to_container.assert_called_once_with(
            local_path, container_path
        )

        # Reset the mock for the next test
        self.mock_docker_client.transfer_file_to_container.reset_mock()

        # Test transfer_file_from_container
        self.op_client.transfer_file_from_container(container_path, local_path)

        # Verify docker_client.copy_file_from_container was called with the correct paths
        self.mock_docker_client.copy_file_from_container.assert_called_once_with(
            container_path, local_path
        )

        # Test failing transfer (to container)
        self.mock_docker_client.transfer_file_to_container.side_effect = Exception(
            "Connection refused"
        )

        with pytest.raises(FileTransferError):
            self.op_client.transfer_file_to_container(local_path, container_path)

    def test_is_connected(self) -> None:
        """Test connection check."""
        # Patch random.randint to return a fixed value
        with patch("src.clients.openproject_client.random.randint", return_value=12345):
            # Configure mock for successful validation
            self.mock_rails_client.execute.side_effect = (
                None  # Clear any previous side effects
            )
            self.mock_rails_client.execute.return_value = (
                "OPENPROJECT_CONNECTION_TEST_12345"
            )

            # Test is_connected
            result = self.op_client.is_connected()

            # Verify result
            assert result

            # Verify Rails client was called with the correct connection test command
            self.mock_rails_client.execute.assert_called_once()
            command = self.mock_rails_client.execute.call_args[0][0]
            assert "OPENPROJECT_CONNECTION_TEST_" in command

            # Test with a failed execution
            self.mock_rails_client.execute.reset_mock()
            self.mock_rails_client.execute.side_effect = Exception(
                "Connection to Rails console failed"
            )

            result = self.op_client.is_connected()

            # Verify result
            assert not result


if __name__ == "__main__":
    unittest.main()
