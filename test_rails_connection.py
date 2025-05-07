#!/usr/bin/env python3
"""
Simple test script to verify Rails console connection via local tmux
"""

import sys
import subprocess
import time

from src import config
from src.clients.openproject_client import OpenProjectClient

# Set up logger
logger = config.logger


def test_simple_tmux_command():
    """Test sending a simple command to tmux directly"""
    print("Testing direct tmux command...")

    # Check if tmux session exists
    tmux_session = "rails_console"
    cmd = ["tmux", "has-session", "-t", tmux_session]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ Tmux session '{tmux_session}' exists")
        else:
            print(f"❌ Tmux session '{tmux_session}' does not exist")
            return False
    except Exception as e:
        print(f"❌ Error checking tmux session: {str(e)}")
        return False

    # Try to send a command to the tmux session
    try:
        target = f"{tmux_session}:0.0"
        send_cmd = ["tmux", "send-keys", "-t", target, 'puts "HELLO_FROM_PYTHON"', "Enter"]
        subprocess.run(send_cmd, capture_output=True, text=True, check=True)
        print("✅ Command sent to tmux session")

        # Wait for output
        time.sleep(2)

        # Capture the output
        capture_cmd = ["tmux", "capture-pane", "-p", "-t", target]
        result = subprocess.run(capture_cmd, capture_output=True, text=True, check=True)
        output = result.stdout

        if "HELLO_FROM_PYTHON" in output:
            print("✅ Successfully found command output")
            print(f"Output excerpt: {output[-200:]}")
            return True
        else:
            print("❌ Could not find expected output")
            print(f"Output excerpt: {output[-200:]}")
            return False
    except Exception as e:
        print(f"❌ Error with tmux: {str(e)}")
        return False


def test_openproject_client():
    """Test OpenProjectClient connection"""
    print("\nTesting OpenProjectClient connection...")

    try:
        config.log_level = "debug"  # Enable debug logging

        # Initialize the client with default configuration
        client = OpenProjectClient()
        print("✅ Client initialized")

        # Test a direct, simple file creation command
        print("Testing file creation in container...")
        test_command = """
        # Try to create a simple test file in /tmp
        test_file = '/tmp/python_test_file.txt'
        content = 'Test content from Python'
        File.write(test_file, content)
        if File.exist?(test_file)
          "✅ SUCCESS: Created test file #{test_file} with content '#{content}'"
        else
          "❌ ERROR: Failed to create test file #{test_file}"
        end
        """

        # Execute the test command
        result = client.rails_client.execute(test_command)
        print(f"Command result: {result}")

        # Check for success in the raw output
        output_str = str(result)
        if "SUCCESS" in output_str:
            print("✅ File creation test successful (found SUCCESS marker in output)")
            return True
        elif result.get("status") == "success":
            print("✅ File creation test successful (status is success)")
            return True
        else:
            print(f"❌ File creation test failed: {result.get('error', 'Unknown error')}")
            return False

        # Now test the is_connected method
        if client.is_connected():
            print("✅ is_connected() reports successful connection")
        else:
            print("❌ Client reports connection failure")

    except Exception as e:
        print(f"❌ Error during client test: {str(e)}")
        import traceback
        traceback.print_exc()


def main():
    """Main test function"""
    print("Rails Console Connection Test\n")

    # First test direct tmux access
    tmux_ok = test_simple_tmux_command()

    # Then test the OpenProject client if tmux is ok
    if tmux_ok:
        client_ok = test_openproject_client()
    else:
        print("Skipping OpenProject client test due to tmux issues")
        client_ok = False

    # Report overall result
    if tmux_ok and client_ok:
        print("\n✅ All tests PASSED!")
        return 0
    else:
        print("\n❌ Some tests FAILED!")
        return 1


if __name__ == "__main__":
    sys.exit(main())
