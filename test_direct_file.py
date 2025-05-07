#!/usr/bin/env python3
"""
Simple test script to directly test file creation via Rails console
"""

import os
import subprocess
import time

from src import config
from src.clients.ssh_client import SSHClient
from src.clients.docker_client import DockerClient

# Set up logger
logger = config.logger
config.log_level = "debug"


def test_direct_file_creation():
    """Test direct file creation in Rails console"""
    print("Testing direct file creation in Rails console...")

    try:
        # Create SSH client
        ssh_host = config.openproject_config.get("server")
        ssh_user = config.openproject_config.get("user")
        ssh_key_file = config.openproject_config.get("key_file")

        ssh_client = SSHClient(
            host=ssh_host,
            user=ssh_user,
            key_file=ssh_key_file
        )
        print(f"✅ SSH client initialized for host {ssh_host}")

        # Create Docker client
        container_name = config.openproject_config.get("container")
        docker_client = DockerClient(
            container_name=container_name,
            ssh_host=ssh_host,
            ssh_user=ssh_user,
            ssh_key_file=ssh_key_file,
            ssh_client=ssh_client  # Pass our SSH client
        )
        print(f"✅ Docker client initialized for container {container_name}")

        # Execute a simple command in Docker to create a file
        print("Creating a test file directly in Docker container...")
        result = docker_client.execute_command(
            'echo "Test content" > /tmp/docker_test_file.txt'
        )

        if result.get("status") == "success":
            print("✅ Docker command executed successfully")
        else:
            print(f"❌ Docker command failed: {result.get('error', result.get('stderr', 'Unknown error'))}")

        # Check if the file was created
        check_result = docker_client.execute_command(
            'ls -la /tmp/docker_test_file.txt'
        )

        if check_result.get("status") == "success" and "docker_test_file.txt" in check_result.get("stdout", ""):
            print("✅ Test file exists in Docker container")
            print(f"File details: {check_result.get('stdout')}")
        else:
            print("❌ Test file not found in Docker container")

        # Now use tmux to send a command to the Rails console
        print("\nTesting file creation via tmux Rails console...")

        # First, create a local test file
        test_content = """
        # Test file creation
        test_file = '/tmp/rails_test_file.txt'
        begin
          File.open(test_file, 'w') do |f|
            f.write('Test content from Rails console')
          end
          if File.exist?(test_file)
            puts "✅ SUCCESS: Created test file #{test_file} (#{File.size(test_file)} bytes)"
            true
          else
            puts "❌ ERROR: Failed to create test file #{test_file}"
            false
          end
        rescue => e
          puts "❌ ERROR: Exception during file creation: #{e.class.name}: #{e.message}"
          puts e.backtrace.join("\\n")
          false
        end
        """

        with open("/tmp/test_rails.rb", "w") as f:
            f.write(test_content)

        # Copy to container
        print("Copying test script to container...")
        copy_result = docker_client.copy_file_to_container(
            "/tmp/test_rails.rb",
            "/tmp/test_rails.rb"
        )

        if copy_result.get("status") == "success":
            print("✅ Test script copied to container")
        else:
            print(f"❌ Failed to copy test script to container: {copy_result.get('error', 'Unknown error')}")
            return

        # Send command to tmux to load the file
        tmux_session = "rails_console"
        tmux_command = "load '/tmp/test_rails.rb'"

        print(f"Sending command to tmux session {tmux_session}: {tmux_command}")
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", f"{tmux_session}:0.0", tmux_command, "Enter"],
                capture_output=True,
                text=True,
                check=True
            )
            print("✅ Command sent to tmux session")
        except Exception as e:
            print(f"❌ Failed to send command to tmux: {str(e)}")
            return

        # Wait for execution
        print("Waiting for command execution...")
        time.sleep(5)

        # Capture tmux pane output
        try:
            capture = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", f"{tmux_session}:0.0"],
                capture_output=True,
                text=True,
                check=True
            )
            output = capture.stdout
            print("\nTmux output (last 20 lines):")
            lines = output.splitlines()
            for line in lines[-20:]:
                print(f"  {line}")
        except Exception as e:
            print(f"❌ Failed to capture tmux output: {str(e)}")

        # Check if the file was created in the container
        print("\nChecking if file was created...")
        check_result = docker_client.execute_command(
            'ls -la /tmp/rails_test_file.txt'
        )

        if check_result.get("status") == "success" and "rails_test_file.txt" in check_result.get("stdout", ""):
            print("✅ Rails test file exists in Docker container")
            print(f"File details: {check_result.get('stdout')}")

            # Try to copy the file back
            print("\nCopying file back from container...")
            copy_result = docker_client.copy_file_from_container(
                "/tmp/rails_test_file.txt",
                "/tmp/rails_test_file_copy.txt"
            )

            if copy_result.get("status") == "success":
                print("✅ File copied back successfully")
                if os.path.exists("/tmp/rails_test_file_copy.txt"):
                    with open("/tmp/rails_test_file_copy.txt") as f:
                        content = f.read()
                    print(f"File content: {content}")
                else:
                    print("❌ Local copy not found")
            else:
                print(f"❌ Failed to copy file back: {copy_result.get('error', 'Unknown error')}")
        else:
            print("❌ Rails test file not found in Docker container")

    except Exception as e:
        print(f"❌ Error during test: {str(e)}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    test_direct_file_creation()
    print("\nTest complete.")
