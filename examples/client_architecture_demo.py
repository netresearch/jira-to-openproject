#!/usr/bin/env python3
# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Client Architecture Demo.

This script demonstrates the layered client architecture with proper dependency injection:
1. SSHClient (Foundation Layer)
2. DockerClient (uses SSHClient)
3. RailsConsoleClient (uses tmux)
4. OpenProjectClient (orchestrates all clients)

Run this script with appropriate configuration to see how the components work together.
"""

import shlex
import tempfile
import traceback
from contextlib import ExitStack
from pathlib import Path

# Import after path is set
from src import config
from src.infrastructure.openproject.docker_client import DockerClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.infrastructure.openproject.rails_console_client import RailsConsoleClient
from src.infrastructure.openproject.ssh_client import SSHClient


def demo_each_client_independently() -> None:
    """Demonstrate each client layer independently, showing how they can be used directly."""
    print("=" * 80)
    print("DEMONSTRATING INDIVIDUAL CLIENT LAYERS")
    print("=" * 80)

    # Load configuration
    ssh_host = config.openproject_config.get("server")
    ssh_user = config.openproject_config.get("user")
    container_name = config.openproject_config.get("container")
    tmux_session = config.openproject_config.get("tmux_session_name", "rails_console")

    # 1. SSHClient (Foundation Layer)
    print("\n--- SSHClient Demo ---")
    ssh_client = SSHClient(host=ssh_host, user=ssh_user, retry_count=3)

    # Execute a simple command
    stdout, _, _ = ssh_client.execute_command("echo 'Hello from SSH Client'")
    print(f"SSH Command Result: {stdout.strip()}")

    # Check if a file exists on the remote server
    remote_file_exists = ssh_client.check_remote_file_exists("/etc/hosts")
    print(f"Remote file /etc/hosts exists: {remote_file_exists}")

    # 2. DockerClient (uses SSHClient)
    print("\n--- DockerClient Demo ---")
    # Create Docker client using the SSHClient we created above (dependency injection)
    docker_client = DockerClient(
        container_name=container_name,
        ssh_client=ssh_client,
    )  # Inject the SSHClient

    # Execute a command in the container
    stdout, _, _ = docker_client.execute_command("echo 'Hello from Docker Client'")
    print(f"Docker Command Result: {stdout.strip()}")

    # Check if a file exists in the container
    container_file_exists = docker_client.check_file_exists_in_container(
        "/app/config/database.yml",
    )
    print(f"Container file database.yml exists: {container_file_exists}")

    # 3. RailsConsoleClient (uses tmux)
    print("\n--- RailsConsoleClient Demo ---")
    rails_client = RailsConsoleClient(
        tmux_session_name=tmux_session,
        command_timeout=30,
    )

    # Execute a simple Ruby command
    result = rails_client.execute("puts 'Hello from Rails Console'; 'SUCCESS'")
    print(f"Rails Command Result: {result}")


def demo_orchestrated_workflow() -> None:
    """Demonstrate the complete orchestrated workflow using OpenProjectClient."""
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
    print(f"Project count: {result}")

    # Alternative using built-in method
    project_count = client.count_records("Project")
    print(f"Project count (using count_records): {project_count}")

    # Execute a more complex query
    result = client.execute_json_query(
        """
    # Get statistics about work packages
    stats = {
      total: WorkPackage.count,
      open: WorkPackage.where(status: Status.where(is_closed: false)).count,
      closed: WorkPackage.where(status: Status.where(is_closed: true)).count
    }
    stats  # Return the statistics
    """,
    )

    print(f"Work Package Statistics: {result}")


def _temporary_transfer_file(execute, stack: ExitStack) -> Path:
    directory, stderr, status = execute("mktemp -d")
    root = Path(directory.strip())
    if status != 0 or not root.is_absolute() or root == Path("/"):
        raise RuntimeError(f"Could not allocate temporary directory: {stderr}")
    target = root / "transfer.txt"
    stack.callback(execute, f"rm -f -- {shlex.quote(str(target))} && rmdir -- {shlex.quote(str(root))}")
    return target


def demo_file_transfer() -> None:
    """Demonstrate file transfer workflow."""
    print("\n" + "=" * 80)
    print("DEMONSTRATING FILE TRANSFER")
    print("=" * 80)

    ssh_client = SSHClient(
        host=config.openproject_config.get("server"), user=config.openproject_config.get("user"), retry_count=3
    )
    docker_client = DockerClient(container_name=config.openproject_config.get("container"), ssh_client=ssh_client)
    with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
        local_file = Path(directory) / "transfer.txt"
        local_file.write_text("File transfer demonstration.", encoding="utf-8")
        remote_file = _temporary_transfer_file(ssh_client.execute_command, cleanup)
        container_file = _temporary_transfer_file(docker_client.execute_command, cleanup)
        ssh_client.copy_file_to_remote(local_file, remote_file)
        docker_client.copy_file_to_container(remote_file, container_file)
        print(f"File exists in container: {docker_client.check_file_exists_in_container(container_file)}")


def main() -> None:
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
        print(f"\n❌ Demo failed: {e!s}")
        traceback.print_exc()


if __name__ == "__main__":
    main()
