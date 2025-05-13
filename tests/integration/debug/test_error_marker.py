#!/usr/bin/env python3
"""
Test script to verify Rails console error marker handling
"""

import sys

import pytest

from src.clients.rails_console_client import RailsConsoleClient


def test_error_marker_detection() -> None:
    """Test if Rails console client can distinguish between error markers in source code and actual errors"""
    print("TESTING ERROR MARKER DETECTION")
    print("==============================")

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

    print("\nTest 1: Command with error markers in the code (should succeed)")
    command1 = """
    # This command has the word ERROR_MARKER in it but doesn't actually error
    marker_string = "This is just a test ERROR_MARKER string"
    puts "Command output: #{marker_string}"
    "SUCCESS: Test completed"
    """

    try:
        result1 = rails_client.execute(command1)
        print(f"Command result: {result1}")
        # Instead of checking for "Ruby error:" which might be in the template,
        # check for "Ruby error:" followed by actual error details, indicating a real error
        assert "ERROR_MARKER" in result1, "Test string marker not found in output"
        assert "Ruby error: NameError:" not in result1, "Unexpected actual error detected in output"
        assert "Ruby error: SyntaxError:" not in result1, "Unexpected actual error detected in output"
        assert "Command output: This is just a test ERROR_MARKER string" in result1, "Expected output string not found"
        print("✅ TEST 1 PASSED: Command with error markers in code correctly detected as success")
    except Exception as e:
        print(f"❌ TEST 1 FAILED with exception: {str(e)}")
        pytest.fail(f"Test 1 failed with exception: {str(e)}")

    print("\nTest 2: Command that deliberately causes an error")
    command2 = """
    # This command will cause a NameError (undefined variable)
    undefined_variable + 1
    "Should never reach here"
    """

    # Test 2 should raise an exception
    with pytest.raises(Exception):
        result2 = rails_client.execute(command2)
        print("❌ TEST 2 FAILED: Expected an exception but got result")
        print(f"Result: {result2}")

    print("✅ TEST 2 PASSED: Correctly caught error")

    print("\nTest 3: Command with success marker in text")
    command3 = """
    # This command has the word SUCCESS in it to test if we detect success markers
    puts "This output contains SUCCESS message"
    "Completed successfully"
    """

    try:
        result3 = rails_client.execute(command3)
        print(f"Command result: {result3}")
        assert "SUCCESS" in result3, "Success marker not found in output"
        assert "Ruby error: NameError:" not in result3, "Unexpected error detected in output"
        print("✅ TEST 3 PASSED: Command with success marker correctly detected as success")
    except Exception as e:
        print(f"❌ TEST 3 FAILED with exception: {str(e)}")
        pytest.fail(f"Test 3 failed with exception: {str(e)}")


if __name__ == "__main__":
    success = test_error_marker_detection()
    sys.exit(0 if success else 1)
