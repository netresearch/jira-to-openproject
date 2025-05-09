#!/usr/bin/env python3
"""
Test module for RailsConsoleClient.

This module contains test cases for validating Rails console interactions.
"""

import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock, Mock
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
            "--EXEC_START--test_unique_id\n"
            "Command output\n"
            "--EXEC_END--test_unique_id"
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
            "--EXEC_START--test_unique_id\n"
            "Command result\n"
            "--EXEC_END--test_unique_id\n"
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

    def test_execute_script(self):
        """Test executing a script in the Rails console."""
        # Define some sample output
        marker_id = "abcd1234"
        output = f"""
irb(main):001:0> load '/tmp/test_script.rb'
--EXEC_START--{marker_id}
42
--EXEC_END--{marker_id}
=> nil
irb(main):002:0>
"""
        # Mock generate_unique_id to return a fixed ID
        with patch.object(self.rails_client.file_manager, 'generate_unique_id', return_value=marker_id):
            # Mock _send_command_to_tmux to return sample output
            with patch.object(self.rails_client, '_send_command_to_tmux', return_value=output):
                # Execute the test
                result = self.rails_client.execute("load '/tmp/test_script.rb'")

                # Verify result is the actual output value from between load and EXEC_END
                self.assertEqual(result, "42")

    def test_execute_with_error(self):
        """Test executing a script that causes an error."""
        # Define sample output with error
        marker_id = "abcd1234"
        output = f"""
irb(main):001:0> undefined_variable + 1
--EXEC_START--{marker_id}
--EXEC_ERROR--{marker_id}
Ruby error: NameError: undefined local variable
/path/to/error.rb:123:in `<main>'
--EXEC_END--{marker_id}
=> nil
irb(main):002:0>
"""
        # Mock generate_unique_id to return a fixed ID
        with patch.object(self.rails_client.file_manager, 'generate_unique_id', return_value=marker_id):
            # Mock _send_command_to_tmux to return sample output
            with patch.object(self.rails_client, '_send_command_to_tmux', return_value=output):
                # Execute the test with expected exception
                with self.assertRaises(Exception) as context:
                    self.rails_client.execute("undefined_variable + 1")

                # Verify error message
                self.assertIn("NameError: undefined local variable", str(context.exception))

    def test_execute_with_success_keyword(self):
        """Test executing a script with a success keyword in the output."""
        # Define some sample output
        marker_id = "abcd1234"
        output = f"""
irb(main):001:0> puts 'success marker found'
--EXEC_START--{marker_id}
success marker found
--EXEC_END--{marker_id}
=> nil
irb(main):002:0>
"""
        # Mock generate_unique_id to return a fixed ID
        with patch.object(self.rails_client.file_manager, 'generate_unique_id', return_value=marker_id):
            # Mock _send_command_to_tmux to return sample output
            with patch.object(self.rails_client, '_send_command_to_tmux', return_value=output):
                # Execute the test
                result = self.rails_client.execute("puts 'success marker found'")

                # Verify result is the actual output text
                self.assertEqual(result, "success marker found")
                self.assertIn("success marker found", result)


if __name__ == "__main__":
    unittest.main()
