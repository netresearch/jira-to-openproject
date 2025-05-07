#!/usr/bin/env python3
"""
Test script to verify Rails console error marker handling
"""

import sys

from src.clients.rails_console_client import RailsConsoleClient


def test_error_marker_detection():
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
        return False

    print("\nTest 1: Command with error markers in the code (should succeed)")
    command1 = """
    # This command has the word ERROR_MARKER in it but doesn't actually error
    marker_string = "This is just a test ERROR_MARKER string"
    puts "Command output: #{marker_string}"
    "SUCCESS: Test completed"
    """

    result1 = rails_client.execute(command1)
    print(f"Command result: {result1}")

    if result1.get("status") == "success":
        print("✅ TEST 1 PASSED: Command with error markers in code correctly detected as success")
    else:
        print("❌ TEST 1 FAILED: Command with error markers in code incorrectly detected as error")

    print("\nTest 2: Command that deliberately causes an error")
    command2 = """
    # This command will cause a NameError (undefined variable)
    undefined_variable + 1
    "Should never reach here"
    """

    result2 = rails_client.execute(command2)
    print(f"Command result: {result2}")

    if result2.get("status") == "error":
        print("✅ TEST 2 PASSED: Actual error correctly detected")
    else:
        print("❌ TEST 2 FAILED: Actual error incorrectly detected as success")

    print("\nTest 3: Command with success marker in text")
    command3 = """
    # This command has the word SUCCESS in it to test if we detect success markers
    puts "This output contains SUCCESS message"
    "Completed successfully"
    """

    result3 = rails_client.execute(command3)
    print(f"Command result: {result3}")

    if result3.get("status") == "success":
        print("✅ TEST 3 PASSED: Command with success marker correctly detected as success")
    else:
        print("❌ TEST 3 FAILED: Command with success marker incorrectly detected as error")

    # Summary
    all_passed = (
        result1.get("status") == "success" and
        result2.get("status") == "error" and
        result3.get("status") == "success"
    )

    if all_passed:
        print("\n✅ ALL TESTS PASSED!")
        return True
    else:
        print("\n❌ SOME TESTS FAILED!")
        return False


if __name__ == "__main__":
    success = test_error_marker_detection()
    sys.exit(0 if success else 1)
