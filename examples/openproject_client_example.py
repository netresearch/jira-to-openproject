#!/usr/bin/env python3
"""Example usage of the OpenProjectClient.

This example demonstrates how to use the new OpenProjectClient
to interact with OpenProject through the Rails console.
"""

from pprint import pprint

from src.clients.openproject_client import OpenProjectClient


def main() -> None:
    """Example usage of OpenProjectClient."""
    print("Initializing OpenProjectClient...")

    # Create client directly
    client = OpenProjectClient(
        container_name="openproject_web_1",
        ssh_host="example.com",
        ssh_user="username",
        # ssh_key_file parameter is no longer needed - using system SSH configuration
        tmux_session_name="rails_console",
    )

    print("Connected to OpenProject. Running examples...")

    # Example 1: Basic query execution
    print("\nExample 1: Count Users")
    count = client.count_records("User")
    print(f"Number of users: {count}")

    # Example 2: Find a record
    print("\nExample 2: Find a Project")
    project = client.find_record("Project", {"name": "Example Project"})
    if project:
        print(f"Found project: ID={project.get('id')}, Name={project.get('name')}")
    else:
        print("Project not found")

    # Example 3: Execute a custom query
    print("\nExample 3: Custom Query")
    result = client.execute_query("Project.last(3).pluck(:id, :name)")
    print("Last 3 projects:")
    pprint(result.get("output"))

    # Example 4: Execute a Ruby script
    print("\nExample 4: Ruby Script")
    script = """
    results = []
    Project.first(5).each do |project|
      results << {
        id: project.id,
        name: project.name,
        created_at: project.created_at,
        updated_at: project.updated_at
      }
    end
    results
    """

    script_result = client.execute_script(script)
    if script_result["status"] == "success":
        print("First 5 projects details:")
        pprint(script_result.get("output"))
    else:
        print(f"Error executing script: {script_result.get('error')}")

    # Example 5: Creating a record
    print("\nExample 5: Creating a Record (not actually executed)")
    # attributes = {
    #     "name": "Test Project",
    #     "identifier": "test-project-123",
    #     "description": "A test project created via the API",
    #     "status": "on_track"
    # }

    # Uncomment to actually create a project
    # result = client.create_record("Project", attributes)
    # if result["status"] == "success":
    #     print(f"Created project with ID: {result['record'].get('id')}")
    # else:
    #     print(f"Error creating project: {result.get('errors')}")

    print("\nAll examples completed.")


if __name__ == "__main__":
    main()
