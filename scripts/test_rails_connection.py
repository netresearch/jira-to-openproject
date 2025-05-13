#!/usr/bin/env python3
"""Test Rails Console Connection

This script tests the connection to the OpenProject Rails console.
It's useful for verifying connectivity before running migrations
that require Rails console access.
"""

import argparse
import json
import os
import sys
from typing import Any

from dotenv import load_dotenv

# Add parent directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Import the OpenProject client
try:
    from src.clients.openproject_client import OpenProjectClient
except ImportError:
    print("Error: Could not import OpenProjectClient.")
    sys.exit(1)


def test_rails_connection(session_name: str = "rails_console", debug: bool = False) -> bool:
    """Test connection to the Rails console.

    Args:
        session_name: tmux session name containing Rails console (default: rails_console)
        debug: Enable debug mode

    Returns:
        bool: True if connection was successful, False otherwise

    """
    print(f"Testing Rails console connection to tmux session: {session_name}")

    try:
        # Create an OpenProject client
        client = OpenProjectClient(tmux_session_name=session_name)

        # Run a simple command to test execution
        print("Running test command...")
        result = client.execute_query("Rails.version")

        if result["status"] == "success":
            print(f"Rails version: {result['output']}")
        else:
            print(f"Error executing command: {result.get('error', 'Unknown error')}")
            return False

        # Run another command to test OpenProject environment
        print("Testing OpenProject environment...")
        result = client.execute_query("User.count")

        if result["status"] == "success":
            print(f"OpenProject user count: {result['output']}")
        else:
            print(f"Error executing command: {result.get('error', 'Unknown error')}")
            return False

        print("\nRails console connection test PASSED ✅")
        return True

    except Exception as e:
        print(f"\nError testing Rails console connection: {e}")
        print("Rails console connection test FAILED ❌")
        if debug:
            import traceback

            traceback.print_exc()
        return False


def get_custom_fields(session_name: str = "rails_console", debug: bool = False) -> list[dict[str, Any]]:
    """Retrieve all custom fields from OpenProject via Rails console.

    Args:
        session_name: tmux session name containing Rails console (default: rails_console)
        debug: Enable debug mode

    Returns:
        List of custom fields with their attributes

    """
    print(f"Retrieving custom fields from OpenProject via Rails console in tmux session: {session_name}")

    try:
        # Create an OpenProject client
        client = OpenProjectClient(tmux_session_name=session_name)

        # Execute command to get all custom fields with their attributes
        command = (
            "CustomField.all.map { |cf| { "
            "id: cf.id, "
            "name: cf.name, "
            "field_format: cf.field_format, "
            "type: cf.type, "
            "is_required: cf.is_required, "
            "is_for_all: cf.is_for_all, "
            "possible_values: cf.possible_values } }"
        )

        print("Executing custom fields query...")
        result = client.execute_query(command)

        if result["status"] == "error":
            print(f"Error retrieving custom fields: {result.get('error', 'Unknown error')}")
            return []

        # Extract and parse the custom fields from the output
        output = result.get("output", "")

        try:
            # Handle Ruby array of hashes output format
            if isinstance(output, str) and "[{" in output and "}]" in output:
                start = output.find("[{")
                end = output.find("}]", start) + 2
                ruby_array = output[start:end]

                # Convert Ruby syntax to Python syntax
                python_array = ruby_array.replace("=>", ":").replace("nil", "null")

                # Parse the JSON
                fields = json.loads(python_array)
                print(f"Successfully retrieved {len(fields)} custom fields")
                # Explicitly convert to the expected return type
                return [dict(field) for field in fields]
            if isinstance(output, list):
                # Output may already be parsed by the client
                print(f"Successfully retrieved {len(output)} custom fields")
                # Ensure each item is a dict[str, Any]
                return [dict(item) for item in output]
            print("Could not parse custom fields output as JSON")
            print(f"Raw output: {output}")
            return []
        except Exception as e:
            print(f"Error parsing custom fields output: {e!s}")
            if debug:
                import traceback

                traceback.print_exc()
            return []

    except Exception as e:
        print(f"\nError retrieving custom fields: {e}")
        if debug:
            import traceback

            traceback.print_exc()
        return []


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        Parsed command line arguments

    """
    parser = argparse.ArgumentParser(
        description="Test Rails console connection",
        epilog="""
Requirements:
  - tmux installed on your system
  - An existing tmux session with a running Rails console

Example:
  python scripts/test_rails_connection.py --session rails_console
        """,
    )

    parser.add_argument(
        "--session",
        help="tmux session name (default: rails_console)",
        default="rails_console",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    parser.add_argument("--get-fields", action="store_true", help="Retrieve and display custom fields")
    parser.add_argument("--output", help="Output file for custom fields (JSON format)", default=None)

    return parser.parse_args()


def main() -> None:
    """Main entry point.

    Returns:
        None

    """
    # Load environment variables
    load_dotenv()

    # Parse command line arguments
    args = parse_args()

    # Test Rails connection
    success = test_rails_connection(session_name=args.session, debug=args.debug)

    # Get custom fields if requested
    if success and args.get_fields:
        fields = get_custom_fields(session_name=args.session, debug=args.debug)

        if fields:
            # Print fields to console
            print("\nCustom Fields:")
            for field in fields:
                print(f"- {field.get('name')} (ID: {field.get('id')}, Type: {field.get('field_format')})")

            # Save to file if output path provided
            if args.output:
                with open(args.output, "w") as f:
                    json.dump(fields, f, indent=2)
                print(f"\nSaved {len(fields)} custom fields to {args.output}")

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
