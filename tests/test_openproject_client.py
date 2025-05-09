#!/usr/bin/env python3
"""
Test module for OpenProjectClient.

This module contains test cases for validating the OpenProjectClient as the top-level
component in the refactored client architecture, focusing on proper dependency injection,
delegation to underlying clients, file transfers, error handling, and command execution workflows.
"""

import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock, Mock

from src.clients.openproject_client import OpenProjectClient
from src.clients.ssh_client import SSHClient
from src.clients.docker_client import DockerClient
from src.clients.rails_console_client import RailsConsoleClient


class TestOpenProjectClient(unittest.TestCase):
    """Test cases for the OpenProjectClient class."""

    def setUp(self) -> None:
        """Set up the test environment."""
        # Create a temp directory for file operations
        self.temp_dir = tempfile.mkdtemp()

        # Create patchers for all client dependencies
        # 1. SSHClient patcher
        self.ssh_client_patcher = patch('src.clients.openproject_client.SSHClient')
        self.mock_ssh_client_class = self.ssh_client_patcher.start()
        self.mock_ssh_client = MagicMock(spec=SSHClient)
        self.mock_ssh_client_class.return_value = self.mock_ssh_client

        # 2. DockerClient patcher
        self.docker_client_patcher = patch('src.clients.openproject_client.DockerClient')
        self.mock_docker_client_class = self.docker_client_patcher.start()
        self.mock_docker_client = MagicMock(spec=DockerClient)
        self.mock_docker_client_class.return_value = self.mock_docker_client

        # 3. RailsConsoleClient patcher
        self.rails_client_patcher = patch('src.clients.openproject_client.RailsConsoleClient')
        self.mock_rails_client_class = self.rails_client_patcher.start()
        self.mock_rails_client = MagicMock(spec=RailsConsoleClient)
        self.mock_rails_client_class.return_value = self.mock_rails_client

        # Mock config module
        self.config_patcher = patch('src.clients.openproject_client.config')
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
        self.file_manager_patcher = patch('src.clients.openproject_client.FileManager')
        self.mock_file_manager_class = self.file_manager_patcher.start()
        self.mock_file_manager = MagicMock()
        self.mock_file_manager.data_dir = self.temp_dir
        self.mock_file_manager_class.return_value = self.mock_file_manager

        # Mock os functions
        self.os_patcher = patch('src.clients.openproject_client.os')
        self.mock_os = self.os_patcher.start()
        self.mock_os.path.exists.return_value = True
        self.mock_os.path.basename.return_value = "test_script.rb"
        self.mock_os.path.getsize.return_value = 1024
        self.mock_os.path.join = os.path.join  # Use real join function
        self.mock_os.makedirs = MagicMock()
        self.mock_os.access.return_value = True
        self.mock_os.unlink = MagicMock()

        # Mock tempfile
        self.tempfile_patcher = patch('src.clients.openproject_client.tempfile')
        self.mock_tempfile = self.tempfile_patcher.start()
        self.mock_named_tempfile = MagicMock()
        self.mock_named_tempfile.name = os.path.join(self.temp_dir, "openproject_script_123456.rb")
        self.mock_tempfile.NamedTemporaryFile.return_value = self.mock_named_tempfile

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
        self.tempfile_patcher.stop()

        # Clean up temp directory
        if os.path.exists(self.temp_dir):
            import shutil
            shutil.rmtree(self.temp_dir)

    def test_initialization(self) -> None:
        """Test OpenProjectClient initialization and proper dependency injection."""
        # Verify client attributes
        self.assertEqual(self.op_client.container_name, "test_container")
        self.assertEqual(self.op_client.ssh_host, "test_server")
        self.assertEqual(self.op_client.ssh_user, "test_user")
        self.assertEqual(self.op_client.ssh_key_file, "/path/to/key.pem")
        self.assertEqual(self.op_client.tmux_session_name, "test_session")
        self.assertEqual(self.op_client.command_timeout, 180)
        self.assertEqual(self.op_client.retry_count, 3)
        self.assertEqual(self.op_client.retry_delay, 1.0)

        # Verify each client was properly initialized with correct parameters
        # 1. SSHClient initialization
        self.mock_ssh_client_class.assert_called_once()
        ssh_args = self.mock_ssh_client_class.call_args
        self.assertEqual(ssh_args[1]['host'], "test_server")
        self.assertEqual(ssh_args[1]['user'], "test_user")
        self.assertEqual(ssh_args[1]['key_file'], "/path/to/key.pem")
        self.assertEqual(ssh_args[1]['operation_timeout'], 180)
        self.assertEqual(ssh_args[1]['retry_count'], 3)
        self.assertEqual(ssh_args[1]['retry_delay'], 1.0)

        # 2. DockerClient initialization with SSHClient dependency
        self.mock_docker_client_class.assert_called_once()
        docker_args = self.mock_docker_client_class.call_args
        self.assertEqual(docker_args[1]['container_name'], "test_container")
        self.assertEqual(docker_args[1]['ssh_client'], self.mock_ssh_client)  # Verify dependency injection
        self.assertEqual(docker_args[1]['command_timeout'], 180)
        self.assertEqual(docker_args[1]['retry_count'], 3)
        self.assertEqual(docker_args[1]['retry_delay'], 1.0)

        # 3. RailsConsoleClient initialization
        self.mock_rails_client_class.assert_called_once()
        rails_args = self.mock_rails_client_class.call_args
        self.assertEqual(rails_args[1]['tmux_session_name'], "test_session")
        self.assertEqual(rails_args[1]['command_timeout'], 180)

    def test_init_missing_container(self) -> None:
        """Test initialization with missing container name."""
        # Create a config with missing container name
        temp_config = self.mock_config.openproject_config.copy()
        temp_config["container"] = None
        self.mock_config.openproject_config = temp_config

        # Verify ValueError is raised
        with self.assertRaises(ValueError) as context:
            OpenProjectClient()

        self.assertIn("Container name is required", str(context.exception))

    def test_init_missing_ssh_host(self) -> None:
        """Test initialization with missing SSH host."""
        # Create a config with missing SSH host
        temp_config = self.mock_config.openproject_config.copy()
        temp_config["server"] = None
        self.mock_config.openproject_config = temp_config

        # Verify ValueError is raised
        with self.assertRaises(ValueError) as context:
            OpenProjectClient()

        self.assertIn("SSH host is required", str(context.exception))

    def test_create_script_file(self) -> None:
        """Test script file creation."""
        test_script = "puts 'Hello, World!'"
        script_path = self.op_client._create_script_file(test_script)

        # Verify temp directory was created
        self.mock_os.makedirs.assert_called_once()
        makedirs_args = self.mock_os.makedirs.call_args
        self.assertEqual(os.path.basename(makedirs_args[0][0]), "temp_scripts")

        # Verify NamedTemporaryFile was called with correct parameters
        self.mock_tempfile.NamedTemporaryFile.assert_called_once()
        tempfile_args = self.mock_tempfile.NamedTemporaryFile.call_args
        self.assertEqual(tempfile_args[1]['suffix'], ".rb")
        self.assertEqual(tempfile_args[1]['prefix'], "openproject_script_")
        self.assertEqual(tempfile_args[1]['mode'], "w")
        self.assertEqual(tempfile_args[1]['encoding'], "utf-8")

        # Verify script content was written to file
        self.mock_named_tempfile.write.assert_called_once_with(test_script)
        self.mock_named_tempfile.close.assert_called_once()

        # Verify correct path was returned
        self.assertEqual(script_path, self.mock_named_tempfile.name)

    def test_transfer_and_execute_script_success(self):
        """Test successful script transfer and execution."""
        # Mock file_manager's create_script_file
        self.mock_file_manager.create_script_file.return_value = "/tmp/test_script.rb"

        # Mock _transfer_rails_script to succeed
        with patch.object(self.op_client, '_transfer_rails_script') as mock_transfer:
            mock_transfer.return_value = {
                "success": True,
                "remote_path": "/tmp/remote_script.rb"
            }

            # Mock rails_client execute to return sample JSON result
            with patch.object(self.op_client.rails_client, 'execute') as mock_execute:
                mock_execute.return_value = '{"result": 42}'

                # Execute the test
                result = self.op_client._transfer_and_execute_script("puts 'hello'")

                # Verify the result
                self.assertEqual(result, {"result": 42})

    def test_transfer_and_execute_script_ssh_failure(self):
        """Test script execution failing during SSH transfer."""
        # Mock file_manager's create_script_file
        self.mock_file_manager.create_script_file.return_value = "/tmp/test_script.rb"

        # Mock _transfer_rails_script to fail
        self.op_client._transfer_rails_script = Mock(return_value={
            "success": False,
            "error": "Connection refused"
        })

        # Execute the test with expected exception
        with self.assertRaises(Exception) as context:
            self.op_client._transfer_and_execute_script("puts 'test'")

        # Verify error message
        self.assertIn("Connection refused", str(context.exception))

    def test_transfer_and_execute_script_docker_failure(self):
        """Test script execution failing during Docker transfer."""
        # Mock file_manager's create_script_file
        self.mock_file_manager.create_script_file.return_value = "/tmp/test_script.rb"

        # Mock _transfer_rails_script to fail with Docker error
        self.op_client._transfer_rails_script = Mock(return_value={
            "success": False,
            "error": "Failed to copy script to container"
        })

        # Execute the test with expected exception
        with self.assertRaises(Exception) as context:
            self.op_client._transfer_and_execute_script("puts 'test'")

        # Verify error message
        self.assertIn("Failed to copy script to container", str(context.exception))

    def test_execute_query_success(self):
        """Test successful query execution."""
        # Directly patch _transfer_and_execute_script
        with patch.object(self.op_client, '_transfer_and_execute_script', return_value=42) as mock_transfer:
            # Execute the query
            result = self.op_client.execute_query("puts 'hello world'")

            # Verify result
            self.assertEqual(result, 42)

            # Verify mock was called with properly wrapped query
            mock_transfer.assert_called_once()
            script_content = mock_transfer.call_args[0][0]
            self.assertIn("puts 'hello world'", script_content)

    def test_execute_query_error(self):
        """Test query execution with error."""
        # Directly patch _transfer_and_execute_script
        with patch.object(self.op_client, '_transfer_and_execute_script') as mock_transfer:
            mock_transfer.side_effect = Exception("Query execution failed")

            # Execute the query with error expected
            with self.assertRaises(Exception) as context:
                self.op_client.execute_query("invalid syntax")

            # Verify error message
            self.assertIn("Query execution failed", str(context.exception))

    def test_execute_script(self) -> None:
        """Test direct script execution."""
        # Mock execute_query instead of _transfer_and_execute_script
        with patch.object(self.op_client, 'execute_query') as mock_execute:
            mock_execute.return_value = {
                "status": "success",
                "output": "Script executed successfully"
            }

            # Call the execute method (which delegates to execute_query)
            script_content = "puts 'Hello, World!'"
            result = self.op_client.execute(script_content)

            # Verify result
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["output"], "Script executed successfully")

            # Verify mock was called with the right content
            mock_execute.assert_called_once_with(script_content)

    def test_file_transfer_methods(self) -> None:
        """Test file transfer methods."""
        # Mock the underlying client methods
        self.mock_ssh_client.copy_file_to_remote.return_value = {"status": "success"}
        self.mock_docker_client.copy_file_to_container.return_value = {"status": "success"}
        self.mock_docker_client.check_file_exists_in_container.return_value = True

        # Test transfer_file_to_container
        result = self.op_client.transfer_file_to_container("/local/file.txt", "/container/file.txt")

        # Verify result
        self.assertTrue(result)

        # Verify SSH client was called
        self.mock_ssh_client.copy_file_to_remote.assert_called_once()
        local_path = self.mock_ssh_client.copy_file_to_remote.call_args[0][0]
        self.assertEqual(local_path, "/local/file.txt")

        # Verify Docker client was called
        self.mock_docker_client.copy_file_to_container.assert_called_once()

        # Test with a failing SSH transfer
        self.mock_ssh_client.copy_file_to_remote.reset_mock()
        self.mock_docker_client.copy_file_to_container.reset_mock()
        self.mock_ssh_client.copy_file_to_remote.return_value = {"status": "error", "error": "Connection refused"}

        result = self.op_client.transfer_file_to_container("/local/file.txt", "/container/file.txt")

        # Verify result
        self.assertFalse(result)

        # Verify SSH client was called but Docker client was not
        self.mock_ssh_client.copy_file_to_remote.assert_called_once()
        self.mock_docker_client.copy_file_to_container.assert_not_called()

    def test_is_connected(self) -> None:
        """Test connection check."""
        # Mock generate_unique_id to return a predictable value
        self.mock_file_manager.generate_unique_id.return_value = "test_id"

        # Configure mock for successful validation
        self.mock_rails_client.execute.return_value = {
            "status": "success",
            "output": "OPENPROJECT_CONNECTION_TEST_test_id"
        }

        # Test is_connected
        result = self.op_client.is_connected()

        # Verify result
        self.assertTrue(result)

        # Verify Rails client was called with the correct connection test command
        self.mock_rails_client.execute.assert_called_once()
        command = self.mock_rails_client.execute.call_args[0][0]
        self.assertIn("OPENPROJECT_CONNECTION_TEST_test_id", command)

        # Test with a failed execution
        self.mock_rails_client.execute.reset_mock()
        self.mock_rails_client.execute.return_value = {
            "status": "error",
            "error": "Connection to Rails console failed"
        }

        result = self.op_client.is_connected()

        # Verify result
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
