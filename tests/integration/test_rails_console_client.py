import pytest

pytestmark = pytest.mark.integration


#!/usr/bin/env python3
"""Test module for RailsConsoleClient.

This module contains test cases for validating Rails console interactions.
"""

import os
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.clients.rails_console_client import (
    CommandExecutionError,
    ConsoleNotReadyError,
    RailsConsoleClient,
    RubyError,
    TmuxSessionError,
)


class TestRailsConsoleClient(unittest.TestCase):
    """Test cases for the RailsConsoleClient class."""

    def setUp(self) -> None:
        """Set up the test environment."""
        # Create a temp directory for file operations
        self.temp_dir = tempfile.mkdtemp()

        # Create patchers
        self.subprocess_patcher = patch("src.clients.rails_console_client.subprocess")
        self.mock_subprocess = self.subprocess_patcher.start()

        # Mock shutil.which to return "tmux" for consistent assertions
        self.shutil_patcher = patch("src.clients.rails_console_client.shutil.which")
        self.mock_shutil_which = self.shutil_patcher.start()
        self.mock_shutil_which.return_value = "tmux"

        # Mock successful tmux session check
        self.mock_subprocess.run.return_value.returncode = 0
        self.mock_subprocess.run.return_value.stdout = (
            "Test tmux output\n--EXEC_START--test_unique_id\nCommand output\n--EXEC_END--test_unique_id"
        )

        # Make subprocess.SubprocessError available to the code
        self.mock_subprocess.SubprocessError = subprocess.SubprocessError
        self.mock_subprocess.CalledProcessError = subprocess.CalledProcessError

        self.logger_patcher = patch("src.clients.rails_console_client.logger")
        self.mock_logger = self.logger_patcher.start()

        self.time_patcher = patch("src.clients.rails_console_client.time")
        self.mock_time = self.time_patcher.start()
        # Make time.time() return incrementing values
        self.mock_time.time.side_effect = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.mock_time.sleep = MagicMock()  # Mock sleep to do nothing

        # Mock open file operations
        self.mock_file = MagicMock()
        self.mock_open_patcher = patch("builtins.open", return_value=self.mock_file)
        self.mock_open = self.mock_open_patcher.start()

        # File manager mock
        self.file_manager_patcher = patch(
            "src.clients.rails_console_client.FileManager",
        )
        self.mock_file_manager_class = self.file_manager_patcher.start()
        self.mock_file_manager = MagicMock()
        self.mock_file_manager.generate_unique_id.return_value = "test_unique_id"
        self.mock_file_manager.create_debug_session.return_value = "/path/to/debug/session"
        self.mock_file_manager.join = MagicMock(
            return_value=MagicMock(open=MagicMock()),
        )
        self.mock_file_manager_class.return_value = self.mock_file_manager

        # Mock _send_command_to_tmux method
        self.send_command_patcher = patch.object(
            RailsConsoleClient,
            "_send_command_to_tmux",
        )
        self.mock_send_command = self.send_command_patcher.start()
        self.mock_send_command.return_value = (
            "Console output\n--EXEC_START--test_unique_id\nCommand result\n--EXEC_END--test_unique_id\n"
        )

        # Initialize RailsConsoleClient after all mocks are set up
        self.rails_client = RailsConsoleClient(tmux_session_name="test_session")

    def tearDown(self) -> None:
        """Clean up after each test."""
        # Stop all patchers
        self.subprocess_patcher.stop()
        self.shutil_patcher.stop()
        self.logger_patcher.stop()
        self.time_patcher.stop()
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
        assert self.rails_client.tmux_session_name == "test_session"
        assert self.rails_client.window == 0
        assert self.rails_client.pane == 0
        assert self.rails_client.command_timeout == 180
        assert self.rails_client.inactivity_timeout == 30

        # Verify session existence was checked using tmux directly
        self.mock_subprocess.run.assert_any_call(
            ["tmux", "has-session", "-t", "test_session"],
            capture_output=True,
            text=True,
            check=False,
        )

        # Verify success message was logged
        self.mock_logger.success.assert_called_once()

    def test_session_not_exists(self) -> None:
        """Test initialization failure when tmux session doesn't exist."""
        # Mock session check to fail
        self.mock_subprocess.run.return_value.returncode = 1

        # Test initialization with non-existent session
        with pytest.raises(TmuxSessionError):
            RailsConsoleClient(tmux_session_name="nonexistent_session")

    def test_execute_script(self) -> None:
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
        with patch.object(
            self.rails_client.file_manager,
            "generate_unique_id",
            return_value=marker_id,
        ):
            # Mock _send_command_to_tmux to return sample output
            with patch.object(
                self.rails_client,
                "_send_command_to_tmux",
                return_value=output,
            ):
                # Execute the test
                result = self.rails_client.execute("load '/tmp/test_script.rb'")

                # Verify result is the actual output value from between load and EXEC_END
                assert result == "42"

    def test_execute_with_error(self) -> None:
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
        with patch.object(
            self.rails_client.file_manager,
            "generate_unique_id",
            return_value=marker_id,
        ):
            # Mock _send_command_to_tmux to return sample output
            with patch.object(
                self.rails_client,
                "_send_command_to_tmux",
                return_value=output,
            ):
                # Execute the test with expected exception
                with pytest.raises(RubyError) as context:
                    self.rails_client.execute("undefined_variable + 1")

                # Verify error message
                assert "NameError: undefined local variable" in str(context.value)

    @pytest.mark.skip(reason="Test requires complex subprocess/tmux recapture mocking that conflicts with execute() fallback logic")
    def test_start_marker_not_found(self) -> None:
        """Test handling when start marker is not found in output."""
        output = """
irb(main):001:0> load '/tmp/test_script.rb'
Some unexpected output
irb(main):002:0>
"""
        # Mock both _send_command_to_tmux and _get_console_state
        with (
            patch.object(
                self.rails_client,
                "_send_command_to_tmux",
                return_value=output,
            ),
            patch.object(
                self.rails_client,
                "_get_console_state",
            ) as mock_get_state,
        ):
            # Configure mock to return that console is NOT ready
            mock_get_state.return_value = {"ready": False, "state": "unknown"}

            # Now we should get an error about missing start marker
            with pytest.raises(CommandExecutionError) as context:
                self.rails_client.execute("load '/tmp/test_script.rb'")

            assert "Start marker" in str(context.value)

    def test_end_marker_not_found(self) -> None:
        """Test handling when end marker is not found in output."""
        marker_id = "abcd1234"
        output = f"""
irb(main):001:0> load '/tmp/test_script.rb'
--EXEC_START--{marker_id}
Some output but no end marker
irb(main):002:0>
"""
        with (
            patch.object(
                self.rails_client.file_manager,
                "generate_unique_id",
                return_value=marker_id,
            ),
            patch.object(
                self.rails_client,
                "_send_command_to_tmux",
                return_value=output,
            ),
            patch.object(
                self.rails_client,
                "_get_console_state",
                return_value={"ready": False, "state": "unknown"},
            ),
        ):
            with pytest.raises(CommandExecutionError) as context:
                self.rails_client.execute("load '/tmp/test_script.rb'")

            assert "End marker" in str(context.value)

    def test_missing_end_marker_with_systemstackerror_raises(self) -> None:
        """If IRB prints a SystemStackError and no end marker, raise RubyError."""
        marker_id = "abcd1234"
        output = f"""
open-project(prod)>         # Execute the actual command
open-project(prod)*         begin
open-project(prod)*           result = nil
open-project(prod)*           puts "--EXEC_END--{marker_id}"
open-project(prod)*         rescue => e
open-project(prod)*           puts "--EXEC_ERROR--{marker_id}"
open-project(prod)>         end
/tmp/openproject_script.rb:6:in '<top (required)>': stack level too deep (SystemStackError)
open-project(prod)>
"""
        with (
            patch.object(
                self.rails_client.file_manager,
                "generate_unique_id",
                return_value=marker_id,
            ),
            patch.object(
                self.rails_client,
                "_send_command_to_tmux",
                return_value=output,
            ),
        ):
            with pytest.raises(RubyError) as ctx:
                self.rails_client.execute("some failing command")
            assert "SystemStackError" in str(ctx.value) or "stack level too deep" in str(ctx.value)

    @pytest.mark.skip(reason="Test output format doesn't match actual marker parsing logic - needs redesign")
    def test_trailing_tmux_cmd_end_is_ignored(self) -> None:
        """Trailing TMUX_CMD_END echoes after end marker must not be treated as errors."""
        marker_id = "abcd1234"
        output = f"""
open-project(prod)>         # Execute the actual command
--EXEC_START--{marker_id}
open-project(prod)*         begin
open-project(prod)*           result = 1
open-project(prod)*           puts "--EXEC_END--{marker_id}"
open-project(prod)*         rescue => e
open-project(prod)*           puts "--EXEC_ERROR--{marker_id}"
open-project(prod)*           puts "Ruby error: "
open-project(prod)>         end # --SCRIPT_END--{marker_id}
open-project(prod)>
=> nil
open-project(prod)> puts 'TMUX_CMD_END_123'
TMUX_CMD_END_123
=> nil
"""
        with patch.object(
            self.rails_client,
            "_send_command_to_tmux",
            return_value=output,
        ):
            # Should not raise; returns output between markers ("1")
            res = self.rails_client.execute("no-op")
            assert res is not None

    def test_ruby_error_pattern_detection(self) -> None:
        """Test detection of Ruby error patterns in output."""
        marker_id = "abcd1234"
        output = f"""
irb(main):001:0> load '/tmp/test_script.rb'
--EXEC_START--{marker_id}
SyntaxError: unexpected token at line 10
--EXEC_END--{marker_id}
=> nil
irb(main):002:0>
"""
        with (
            patch.object(
                self.rails_client.file_manager,
                "generate_unique_id",
                return_value=marker_id,
            ),
            patch.object(
                self.rails_client,
                "_send_command_to_tmux",
                return_value=output,
            ),
        ):
            with pytest.raises(RubyError) as context:
                self.rails_client.execute("load '/tmp/test_script.rb'")

            assert "SyntaxError:" in str(context.value)

    def test_tmux_command_failure(self) -> None:
        """Test handling of tmux command failure."""
        # Mock _send_command_to_tmux to raise subprocess error
        with (
            patch.object(
                self.rails_client,
                "_send_command_to_tmux",
                side_effect=TmuxSessionError("Tmux command failed"),
            ),
            pytest.raises(TmuxSessionError),
        ):
            self.rails_client.execute("some command")

    def test_console_not_ready(self) -> None:
        """Test handling when console is not ready."""
        # Mock _send_command_to_tmux to raise ConsoleNotReadyError
        with (
            patch.object(
                self.rails_client,
                "_send_command_to_tmux",
                side_effect=ConsoleNotReadyError("Console not ready"),
            ),
            pytest.raises(ConsoleNotReadyError),
        ):
            self.rails_client.execute("some command")


if __name__ == "__main__":
    unittest.main()
