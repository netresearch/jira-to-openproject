#!/usr/bin/env python3
"""Test script to verify file access permissions in Rails console.

This script:
1. Creates a simple Ruby script locally
2. Transfers it to the container
3. Tries to load it in the Rails console

Usage:
    python test_direct_file.py
"""

import tempfile
from pathlib import Path

from src import config
from src.clients.docker_client import DockerClient
from src.clients.openproject_client import OpenProjectClient
from src.clients.rails_console_client import RailsConsoleClient
from src.clients.ssh_client import SSHClient


def main() -> None:
    """Run the file permission test."""
    print("Testing file permissions for Rails console...")

    # Get config
    op_config = config.openproject_config
    print(f"Server: {op_config.get('server')}")
    print(f"Container: {op_config.get('container')}")

    # Initialize clients
    ssh_client = SSHClient(
        host=op_config.get("server"),
        user=op_config.get("user"),
        key_file=op_config.get("key_file"),
        connect_timeout=10,
        operation_timeout=30,
    )

    docker_client = DockerClient(
        container_name=op_config.get("container"),
        ssh_client=ssh_client,
        command_timeout=30,
    )

    rails_client = RailsConsoleClient(
        tmux_session_name=op_config.get("tmux_session_name", "rails_console"),
        command_timeout=30,
    )

    op_client = OpenProjectClient(
        container_name=op_config.get("container"),
        ssh_host=op_config.get("server"),
        ssh_user=op_config.get("user"),
        tmux_session_name=op_config.get("tmux_session_name", "rails_console"),
        command_timeout=30,
    )

    # Create a test script
    ruby_script = """
    # Simple test script
    puts "This script is executing in the Rails console"
    puts "Current directory: #{Dir.pwd}"
    puts "Ruby version: #{RUBY_VERSION}"

    # Return something to verify it worked
    {
      success: true,
      message: "Script executed successfully",
      timestamp: Time.now.to_s,
      ruby_version: RUBY_VERSION,
      rails_version: Rails.version
    }
    """

    # Create a temp directory
    temp_dir = Path(tempfile.mkdtemp())
    local_path = temp_dir / "test_script.rb"

    # Write script to file
    with Path(local_path).open("w") as f:
        f.write(ruby_script)

    # Transfer to container using our modified method
    container_path = None
    try:
        print("Transferring script to container...")
        container_path = op_client._transfer_rails_script(str(local_path))
        print(f"Script transferred to container at: {container_path}")

        # Verify file exists in container
        exists = docker_client.check_file_exists_in_container(container_path)
        print(f"Container file exists check: {exists}")

        # Verify permissions
        stdout, stderr, rc = docker_client.execute_command(f"ls -la {container_path}")
        print(f"File permissions: {stdout}")

        # Try to load the script in Rails console
        print("\nExecuting script in Rails console...")
        result = rails_client.execute(f'load "{container_path}"')
        print("\nScript execution result:")
        print(result)

        print("\nTest completed successfully!")

    except Exception as e:
        print(f"Error during test: {e!s}")
    finally:
        # Clean up
        try:
            if container_path:
                docker_client.execute_command(f"rm -f {container_path}")
            if temp_dir.exists():
                for f in temp_dir.glob("*"):
                    f.unlink()
                temp_dir.rmdir()
        except Exception as e:
            print(f"Cleanup error: {e!s}")


if __name__ == "__main__":
    main()
