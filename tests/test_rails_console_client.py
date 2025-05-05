#!/usr/bin/env python3
"""
Test module for RailsConsoleClient.

This module contains test cases for validating Rails console interactions.
"""

import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from src.clients.rails_console_client import RailsConsoleClient


class TestRailsConsoleClient(unittest.TestCase):
    """Test cases for the RailsConsoleClient class."""

    def setUp(self) -> None:
        """Set up the test environment."""
        # Create a temp directory for file operations
        self.temp_dir = tempfile.mkdtemp()

        # Create patchers
        self.docker_client_patcher = patch('src.clients.rails_console_client.DockerClient')
        self.mock_docker_client_class = self.docker_client_patcher.start()
        self.mock_docker_client = MagicMock()
        self.mock_docker_client_class.return_value = self.mock_docker_client

        # Set up SSH client mock within Docker client
        self.mock_ssh_client = MagicMock()
        self.mock_docker_client.ssh_client = self.mock_ssh_client

        self.logger_patcher = patch('src.clients.rails_console_client.logger')
        self.mock_logger = self.logger_patcher.start()

        self.time_patcher = patch('src.clients.rails_console_client.time')
        self.mock_time = self.time_patcher.start()

        # Mock file operations
        self.os_patcher = patch('src.clients.rails_console_client.os')
        self.mock_os = self.os_patcher.start()
        self.mock_os.path.join = os.path.join  # Use real path join

        # File manager mock
        self.file_manager_patcher = patch('src.clients.rails_console_client.FileManager')
        self.mock_file_manager_class = self.file_manager_patcher.start()
        self.mock_file_manager = MagicMock()
        self.mock_file_manager.generate_unique_id.return_value = "test_unique_id"
        self.mock_file_manager.create_debug_session.return_value = "/path/to/debug/session"
        self.mock_file_manager.create_data_file.return_value = "/path/to/data.json"
        self.mock_file_manager.create_script_file.return_value = "/path/to/script.rb"
        self.mock_file_manager.data_dir = "/data/dir"
        self.mock_file_manager_class.return_value = self.mock_file_manager

        # Configure mocks for successful tmux session check for the default instance
        self.mock_ssh_client.execute_command.return_value = {
            "status": "success",
            "stdout": "EXISTS\n",
            "stderr": "",
            "returncode": 0
        }

        # Initialize RailsConsoleClient after all mocks are set up
        self.rails_client = RailsConsoleClient(
            container_name="test_container",
            ssh_host="testhost",
            ssh_user="testuser",
            tmux_session_name="test_session"
        )

    def tearDown(self) -> None:
        """Clean up after each test."""
        # Stop all patchers
        self.docker_client_patcher.stop()
        self.logger_patcher.stop()
        self.time_patcher.stop()
        self.os_patcher.stop()
        self.file_manager_patcher.stop()

        # Clean up temp directory
        if os.path.exists(self.temp_dir):
            import shutil
            shutil.rmtree(self.temp_dir)

    def test_initialization(self) -> None:
        """Test RailsConsoleClient initialization."""
        # Test that default parameters are set correctly
        self.assertEqual(self.rails_client.tmux_session_name, "test_session")
        self.assertEqual(self.rails_client.window, 0)
        self.assertEqual(self.rails_client.pane, 0)
        self.assertEqual(self.rails_client.command_timeout, 180)
        self.assertEqual(self.rails_client.inactivity_timeout, 30)

        # Verify DockerClient was initialized with correct parameters
        self.mock_docker_client_class.assert_called_once_with(
            container_name="test_container",
            ssh_host="testhost",
            ssh_user="testuser",
            ssh_key_file=None,
            command_timeout=180
        )

        # Verify session existence was checked
        self.mock_ssh_client.execute_command.assert_any_call(
            "tmux has-session -t test_session 2>/dev/null && echo 'EXISTS' || echo 'NOT_EXISTS'"
        )

        # Verify success message was logged
        self.mock_logger.success.assert_called_once()

    def test_execute_script(self) -> None:
        """Test executing a Ruby script."""
        # Reset mocks
        self.mock_file_manager.create_script_file.reset_mock()
        self.mock_docker_client.copy_file_to_container.reset_mock()

        # Mock script file creation
        self.mock_file_manager.create_script_file.return_value = "/path/to/script.rb"

        # Mock successful file copy to container
        self.mock_docker_client.copy_file_to_container.return_value = {"status": "success"}

        # Setup execute method mock with success result
        with patch.object(
            self.rails_client, 'execute',
            return_value={"status": "success", "output": "script result"}
        ) as mock_execute:
            # Execute the script
            result = self.rails_client.execute_script("puts 'Hello from script'")

            # Verify execute was called with the load command
            mock_execute.assert_called_once()
            execute_cmd = mock_execute.call_args[0][0]
            self.assertIn("load '/tmp/test_unique_id_script.rb'", execute_cmd)

        # Verify the script file was created
        self.mock_file_manager.create_script_file.assert_called_once()
        script_content = self.mock_file_manager.create_script_file.call_args[0][0]
        self.assertIn("puts 'Hello from script'", script_content)

        # Verify the script was copied to the container
        self.mock_docker_client.copy_file_to_container.assert_called_once_with(
            "/path/to/script.rb", "/tmp/test_unique_id_script.rb"
        )

        # Verify the result is correct
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"], "script result")

    def test_execute_with_data(self) -> None:
        """Test executing a script with data."""
        # Reset mocks
        self.mock_file_manager.create_data_file.reset_mock()
        self.mock_file_manager.create_script_file.reset_mock()
        self.mock_docker_client.copy_file_to_container.reset_mock()

        # Mock file creation
        self.mock_file_manager.create_data_file.return_value = "/path/to/data.json"
        self.mock_file_manager.create_script_file.return_value = "/path/to/script.rb"

        # Mock successful file copies to container
        self.mock_docker_client.copy_file_to_container.side_effect = [
            {"status": "success"},  # data file
            {"status": "success"}   # script file
        ]

        # Setup execute method mock with success result
        with patch.object(
            self.rails_client, 'execute',
            return_value={"status": "success", "output": "data script result"}
        ) as mock_execute:
            # Execute the script with data
            data = {"key": "value", "items": [1, 2, 3]}
            result = self.rails_client.execute_with_data(
                "puts input_data.inspect",
                data
            )

            # Verify execute was called
            mock_execute.assert_called_once()

        # Verify the data file was created
        self.mock_file_manager.create_data_file.assert_called_once_with(
            data,
            filename="test_unique_id_data.json",
            session_dir="/path/to/debug/session"
        )

        # Verify the script file was created
        self.mock_file_manager.create_script_file.assert_called_once()
        script_content = self.mock_file_manager.create_script_file.call_args[0][0]
        # Script should include data loading code
        self.assertIn("require 'json'", script_content)
        self.assertIn("input_data = JSON.parse", script_content)
        # Original script should be included
        self.assertIn("puts input_data.inspect", script_content)

        # Verify both files were copied to the container
        self.assertEqual(self.mock_docker_client.copy_file_to_container.call_count, 2)

        # First call should copy data file
        first_call = self.mock_docker_client.copy_file_to_container.call_args_list[0]
        self.assertEqual(first_call[0][0], "/path/to/data.json")
        self.assertEqual(first_call[0][1], "/tmp/test_unique_id_data.json")

        # Second call should copy script file
        second_call = self.mock_docker_client.copy_file_to_container.call_args_list[1]
        self.assertEqual(second_call[0][0], "/path/to/script.rb")
        self.assertEqual(second_call[0][1], "/tmp/test_unique_id_script.rb")

        # Verify the result is correct
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"], "data script result")


if __name__ == "__main__":
    unittest.main()
