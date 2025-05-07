#!/usr/bin/env python3
"""
Test module for RailsConsoleClient.

This module contains test cases for validating Rails console interactions.
"""

import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import subprocess

from src.clients.rails_console_client import RailsConsoleClient


class TestRailsConsoleClient(unittest.TestCase):
    """Test cases for the RailsConsoleClient class."""

    def setUp(self) -> None:
        """Set up the test environment."""
        # Create a temp directory for file operations
        self.temp_dir = tempfile.mkdtemp()

        # Create patchers
        self.subprocess_patcher = patch('src.clients.rails_console_client.subprocess')
        self.mock_subprocess = self.subprocess_patcher.start()

        # Mock successful tmux session check
        self.mock_subprocess.run.return_value.returncode = 0
        self.mock_subprocess.run.return_value.stdout = (
            "Test tmux output\n"
            "STARTtest_unique_id\n"
            "Command output\n"
            "ENDtest_unique_id"
        )

        # Make subprocess.SubprocessError available to the code
        self.mock_subprocess.SubprocessError = subprocess.SubprocessError
        self.mock_subprocess.CalledProcessError = subprocess.CalledProcessError

        self.logger_patcher = patch('src.clients.rails_console_client.logger')
        self.mock_logger = self.logger_patcher.start()

        self.time_patcher = patch('src.clients.rails_console_client.time')
        self.mock_time = self.time_patcher.start()
        # Make time.time() return incrementing values
        self.mock_time.time.side_effect = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.mock_time.sleep = MagicMock()  # Mock sleep to do nothing

        # Mock file operations
        self.os_patcher = patch('src.clients.rails_console_client.os')
        self.mock_os = self.os_patcher.start()
        self.mock_os.path.join = os.path.join  # Use real path join

        # Mock os.path.exists to return True
        self.mock_os.path.exists.return_value = True

        # Mock open file operations
        self.mock_file = MagicMock()
        self.mock_open_patcher = patch('builtins.open', return_value=self.mock_file)
        self.mock_open = self.mock_open_patcher.start()

        # File manager mock
        self.file_manager_patcher = patch('src.clients.rails_console_client.FileManager')
        self.mock_file_manager_class = self.file_manager_patcher.start()
        self.mock_file_manager = MagicMock()
        self.mock_file_manager.generate_unique_id.return_value = "test_unique_id"
        self.mock_file_manager.create_debug_session.return_value = "/path/to/debug/session"
        self.mock_file_manager_class.return_value = self.mock_file_manager

        # Mock _send_command_to_tmux method
        self.send_command_patcher = patch.object(RailsConsoleClient, '_send_command_to_tmux')
        self.mock_send_command = self.send_command_patcher.start()
        self.mock_send_command.return_value = (
            "Console output\n"
            "STARTtest_unique_id\n"
            "Command result\n"
            "ENDtest_unique_id\n"
        )

        # Initialize RailsConsoleClient after all mocks are set up
        self.rails_client = RailsConsoleClient(
            tmux_session_name="test_session"
        )

    def tearDown(self) -> None:
        """Clean up after each test."""
        # Stop all patchers
        self.subprocess_patcher.stop()
        self.logger_patcher.stop()
        self.time_patcher.stop()
        self.os_patcher.stop()
        self.file_manager_patcher.stop()
        self.mock_open_patcher.stop()
        self.send_command_patcher.stop()

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

        # Verify session existence was checked using tmux directly
        self.mock_subprocess.run.assert_any_call(
            ["tmux", "has-session", "-t", "test_session"],
            capture_output=True,
            text=True
        )

        # Verify success message was logged
        self.mock_logger.success.assert_called_once()

    def test_execute_script(self) -> None:
        """Test executing a Ruby command."""
        # Configure mock_send_command for a successful execution
        self.mock_send_command.return_value = (
            "Console output\n"
            "STARTtest_unique_id\n"
            "Command result\n"
            "ENDtest_unique_id\n"
        )

        # Execute a Ruby script
        test_script = "puts 'Hello from Ruby'"
        result = self.rails_client.execute(test_script)

        # Verify the result is correct
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["output"], "Command result")

        # Verify _send_command_to_tmux was called with the right parameters
        self.mock_send_command.assert_called_once()
        # The first argument should be the wrapped command
        self.assertIn(test_script, self.mock_send_command.call_args[0][0])
        # The second argument should be the timeout
        self.assertEqual(self.mock_send_command.call_args[0][1], 180)

    def test_execute_with_error(self) -> None:
        """Test executing a command that causes an error."""
        # Configure mock for an error scenario
        self.mock_send_command.return_value = (
            "Console output\n"
            "STARTtest_unique_id\n"
            "ERRORtest_unique_id\n"
            "Ruby error: NameError: undefined local variable\n"
            "ENDtest_unique_id\n"
        )

        # Execute the command that causes an error
        result = self.rails_client.execute("undefined_variable + 1")

        # Verify the error was detected
        self.assertEqual(result["status"], "error")
        self.assertIn("Ruby error", result.get("error", ""))

    def test_execute_with_success_keyword(self) -> None:
        """Test executing a command with SUCCESS keyword."""
        # Configure mock for a SUCCESS scenario
        self.mock_send_command.return_value = (
            "Console output\n"
            "STARTtest_unique_id\n"
            "SUCCESS\n"
            "ENDtest_unique_id\n"
        )

        # Execute the command
        result = self.rails_client.execute("puts 'SUCCESS'")

        # Verify the success was detected
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["output"]["success"])


if __name__ == "__main__":
    unittest.main()
