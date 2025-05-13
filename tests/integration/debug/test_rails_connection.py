#!/usr/bin/env python3
"""
Test script to verify Rails console connection.
"""

import sys
import pytest
from src.clients.rails_console_client import RailsConsoleClient
from src.clients.openproject_client import OpenProjectClient


def test_simple_tmux_command() -> None:
    """Test a simple command in the Rails console."""
    print("TESTING TMUX COMMANDS")
    print("====================")

    # Initialize Rails console client
    try:
        tmux_session = "rails_console"
        rails_client = RailsConsoleClient(
            tmux_session_name=tmux_session,
            command_timeout=30,
        )
        print("✅ Connected to Rails console")
    except Exception as e:
        print(f"❌ Failed to connect to Rails console: {str(e)}")
        pytest.fail(f"Could not connect to Rails console: {str(e)}")

    # Try to execute a simple command
    try:
        result = rails_client.execute("1 + 1")
        print(f"Rails console result: {result}")
        print("✅ Successfully executed command")
        assert "2" in result, "Expected result '2' not found in output"
    except Exception as e:
        print(f"❌ Failed to execute command: {str(e)}")
        pytest.fail(f"Failed to execute command: {str(e)}")


def test_openproject_client() -> None:
    """Test OpenProject client script execution."""
    print("\nTESTING OPENPROJECT CLIENT")
    print("=========================")

    # Initialize OpenProject client
    try:
        op_client = OpenProjectClient()
        is_connected = op_client.is_connected()
        print(f"✅ OpenProject client initialized, connected: {is_connected}")
        assert is_connected, "OpenProject client is not connected"
    except Exception as e:
        print(f"❌ Failed to initialize OpenProject client: {str(e)}")
        pytest.fail(f"Failed to initialize OpenProject client: {str(e)}")

    # Test script execution
    try:
        script = """
        # Simple test script
        puts "Script executed in #{Rails.env} environment"
        true
        """

        result = op_client.execute(script)
        print(f"Script execution result: {result}")

        # Check for successful execution by looking for expected output string
        assert result is not None, "Script execution returned None"

        # Convert result to string to handle different possible output formats
        result_str = str(result)
        assert "Script executed in" in result_str, "Expected execution confirmation not found in output"
        assert "environment" in result_str, "Expected environment message not found in output"

        print("✅ Successfully executed script via OpenProject client")
    except Exception as e:
        print(f"❌ Failed to execute script: {str(e)}")
        pytest.fail(f"Failed to execute script: {str(e)}")


if __name__ == "__main__":
    success = test_simple_tmux_command() and test_openproject_client()
    sys.exit(0 if success else 1)
