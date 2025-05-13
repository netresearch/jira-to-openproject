#!/usr/bin/env python3
"""
Client Architecture Demo

This script demonstrates the layered client architecture with proper dependency injection:
1. SSHClient (Foundation Layer)
2. DockerClient (uses SSHClient)
3. RailsConsoleClient (uses tmux)
4. OpenProjectClient (orchestrates all clients)

Run this script with appropriate configuration to see how the components work together.
"""

import os
import sys
import traceback

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

# Import after path is set
from src import config
from src.clients.docker_client import DockerClient
from src.clients.openproject_client import OpenProjectClient
from src.clients.rails_console_client import RailsConsoleClient
from src.clients.ssh_client import SSHClient


def demo_each_client_independently():
    """
    Demonstrate each client layer independently, showing how they can be used directly.
    """
    print("=" * 80)
    print("DEMONSTRATING INDIVIDUAL CLIENT LAYERS")
    print("=" * 80)

    # Load configuration
    ssh_host = config.openproject_config.get("server")
    ssh_user = config.openproject_config.get("user")
    ssh_key_file = config.openproject_config.get("key_file")
    container_name = config.openproject_config.get("container")
    tmux_session = config.openproject_config.get("tmux_session_name", "rails_console")

    # 1. SSHClient (Foundation Layer)
    print("\n--- SSHClient Demo ---")
    ssh_client = SSHClient(host=ssh_host, user=ssh_user, key_file=ssh_key_file, retry_count=3)

    # Execute a simple command
    result = ssh_client.execute_command("echo 'Hello from SSH Client'")
    print(f"SSH Command Result: {result.get('stdout', '')}")

    # Check if a file exists on the remote server
    remote_file_exists = ssh_client.check_remote_file_exists("/etc/hosts")
    print(f"Remote file /etc/hosts exists: {remote_file_exists}")

    # 2. DockerClient (uses SSHClient)
    print("\n--- DockerClient Demo ---")
    # Create Docker client using the SSHClient we created above (dependency injection)
    docker_client = DockerClient(container_name=container_name, ssh_client=ssh_client)  # Inject the SSHClient

    # Execute a command in the container
    result = docker_client.execute_command("echo 'Hello from Docker Client'")
    print(f"Docker Command Result: {result.get('stdout', '')}")

    # Check if a file exists in the container
    container_file_exists = docker_client.check_file_exists_in_container("/app/config/database.yml")
    print(f"Container file database.yml exists: {container_file_exists}")

    # 3. RailsConsoleClient (uses tmux)
    print("\n--- RailsConsoleClient Demo ---")
    rails_client = RailsConsoleClient(tmux_session_name=tmux_session, command_timeout=30)

    # Execute a simple Ruby command
    result = rails_client.execute("puts 'Hello from Rails Console'; 'SUCCESS'")
    print(f"Rails Command Result: {result}")


def demo_orchestrated_workflow():
    """
    Demonstrate the complete orchestrated workflow using OpenProjectClient.
    """
    print("\n" + "=" * 80)
    print("DEMONSTRATING ORCHESTRATED WORKFLOW WITH OpenProjectClient")
    print("=" * 80)

    # Create the OpenProjectClient which initializes all other clients
    client = OpenProjectClient()

    # Check connection
    if client.is_connected():
        print("✅ Successfully connected to OpenProject")
    else:
        print("❌ Failed to connect to OpenProject")
        return

    # Execute a query to count projects
    result = client.execute_query("Project.count")
    print(f"Project count: {result.get('output')}")

    # Alternative using built-in method
    project_count = client.count_records("Project")
    print(f"Project count (using count_records): {project_count}")

    # Execute a more complex query
    result = client.execute_query(
        """
    # Get statistics about work packages
    stats = {
      total: WorkPackage.count,
      open: WorkPackage.where(status: Status.where(is_closed: false)).count,
      closed: WorkPackage.where(status: Status.where(is_closed: true)).count
    }
    stats  # Return the statistics
    """
    )

    if result.get("status") == "success":
        print(f"Work Package Statistics: {result.get('output')}")
    else:
        print(f"Query failed: {result.get('error')}")


def demo_file_transfer():
    """
    Demonstrate file transfer workflow.
    """
    print("\n" + "=" * 80)
    print("DEMONSTRATING FILE TRANSFER")
    print("=" * 80)

    # Create a test file locally
    with open("/tmp/test_transfer.txt", "w") as f:
        f.write("This is a test file for demonstrating the file transfer workflow.")

    # Create clients for file transfer
    ssh_host = config.openproject_config.get("server")
    ssh_user = config.openproject_config.get("user")
    ssh_key_file = config.openproject_config.get("key_file")
    container_name = config.openproject_config.get("container")

    # 1. Create the SSHClient (Foundation Layer)
    ssh_client = SSHClient(host=ssh_host, user=ssh_user, key_file=ssh_key_file)

    # 2. Create DockerClient with injected SSHClient
    docker_client = DockerClient(container_name=container_name, ssh_client=ssh_client)

    # 3. Transfer to remote host via SSHClient
    print("Transferring file to remote host...")
    result = ssh_client.copy_file_to_remote("/tmp/test_transfer.txt", "/tmp/test_transfer.txt")
    print(f"SSH transfer result: {result.get('status')}")

    # 4. Transfer from remote host to container via DockerClient
    print("Transferring file to container...")
    result = docker_client.copy_file_to_container("/tmp/test_transfer.txt", "/tmp/test_transfer.txt")
    print(f"Docker transfer result: {result.get('status')}")

    # 5. Verify file in container
    print("Verifying file in container...")
    exists = docker_client.check_file_exists_in_container("/tmp/test_transfer.txt")
    print(f"File exists in container: {exists}")

    # Clean up
    print("Cleaning up...")
    ssh_client.execute_command("rm /tmp/test_transfer.txt")
    docker_client.execute_command("rm /tmp/test_transfer.txt")
    os.remove("/tmp/test_transfer.txt")


def main():
    """Main function demonstrating client architecture."""
    print("CLIENT ARCHITECTURE DEMONSTRATION")
    print("-" * 40)

    config.log_level = "info"  # Reduce log noise for demo

    try:
        # Demo each client independently
        demo_each_client_independently()

        # Demo orchestrated workflow
        demo_orchestrated_workflow()

        # Demo file transfer
        demo_file_transfer()

        print("\n✅ Demo completed successfully!")

    except Exception as e:
        print(f"\n❌ Demo failed: {str(e)}")
        traceback.print_exc()


if __name__ == "__main__":
    main()
