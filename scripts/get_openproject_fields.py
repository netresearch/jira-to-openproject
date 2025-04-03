#!/usr/bin/env python3
"""
Get OpenProject Custom Fields

This script retrieves custom fields from OpenProject by:
1. Running a command in the Rails console via tmux
2. Capturing the output directly from tmux
3. Processing and saving the output as JSON
"""

import os
import sys
import json
import argparse
import subprocess
import re
from typing import List, Dict, Any

# Add parent directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Import necessary modules
from src import config

def capture_tmux_output(session_name: str = "rails_console") -> str:
    """
    Directly capture output from a tmux session.

    Args:
        session_name: tmux session name

    Returns:
        String with captured content
    """
    try:
        # Capture more content from the tmux session including scrollback
        # -S -1000 means start 1000 lines back, to get a lot more history
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-S", "-1000", "-t", session_name],
            check=True,
            capture_output=True,
            text=True
        )
        return result.stdout
    except subprocess.SubprocessError as e:
        print(f"Error capturing tmux output: {e}")
        return ""

def run_command_in_tmux(session_name: str, command: str) -> bool:
    """
    Run a command in the specified tmux session.

    Args:
        session_name: tmux session name
        command: command to run

    Returns:
        True if command was sent, False otherwise
    """
    try:
        # Clear the screen first
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "C-l"],
            check=True
        )

        # Send the command - Make sure to use quotes to avoid interpretation issues
        safe_command = f'"{command}"'  # Wrap in quotes
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, safe_command, "Enter"],
            check=True
        )
        return True
    except subprocess.SubprocessError as e:
        print(f"Error sending command to tmux: {e}")
        return False

def parse_ruby_output(output: str) -> List[Dict[str, Any]]:
    """
    Parse Ruby array output into Python data.

    Args:
        output: String containing Ruby array

    Returns:
        List of dictionaries with custom field data
    """
    print(f"Raw output (first 200 characters): {output[:200]}...")

    # First try regex pattern approach for Ruby array
    array_pattern = r'\[\{.*?id: \d+.*?\}\]'
    match = re.search(array_pattern, output, re.DOTALL)

    if not match:
        print("Could not find Ruby array with regex. Trying alternative pattern...")
        # Try a more general pattern
        array_pattern = r'\[\{.*?\}(?:,\s*\{.*?\})*\]'
        match = re.search(array_pattern, output, re.DOTALL)

    if match:
        ruby_array = match.group()
        print(f"Found Ruby array (first 100 characters): {ruby_array[:100]}...")

        # Instead of trying to parse as JSON, manually extract fields
        try:
            # Convert Ruby-style array to proper JSON
            # This could be more reliable than JSON parsing if format is consistent
            clean_json = convert_ruby_to_json(ruby_array)
            fields = json.loads(clean_json)
            return fields
        except (json.JSONDecodeError, Exception) as e:
            print(f"Error converting Ruby array to JSON: {str(e)}")
            print("Falling back to manual field extraction...")

    # Manual extraction as fallback
    try:
        fields = []
        lines = output.split('\n')
        current_field = None

        for line in lines:
            line = line.strip()

            # Start of a new field
            if line.startswith('{id:'):
                # Save the previous field if it exists
                if current_field is not None and 'id' in current_field:
                    fields.append(current_field)
                current_field = {}

            # Only process lines if we have a current field
            if current_field is not None:
                # Field properties
                if line.startswith('id:'):
                    try:
                        current_field['id'] = int(line.split(':', 1)[1].strip().rstrip(','))
                    except (ValueError, IndexError):
                        pass
                elif line.startswith('name:'):
                    try:
                        value = line.split(':', 1)[1].strip().rstrip(',')
                        if value.startswith('"') and value.endswith('"'):
                            value = value[1:-1]  # Remove quotes
                        current_field['name'] = value
                    except IndexError:
                        pass
                elif line.startswith('field_format:'):
                    try:
                        value = line.split(':', 1)[1].strip().rstrip(',')
                        if value.startswith('"') and value.endswith('"'):
                            value = value[1:-1]  # Remove quotes
                        current_field['field_format'] = value
                    except IndexError:
                        pass
                elif line.startswith('type:'):
                    try:
                        value = line.split(':', 1)[1].strip().rstrip(',')
                        if value.startswith('"') and value.endswith('"'):
                            value = value[1:-1]  # Remove quotes
                        current_field['type'] = value
                    except IndexError:
                        pass
                elif line.startswith('is_required:'):
                    current_field['is_required'] = 'true' in line.lower()
                elif line.startswith('is_for_all:'):
                    current_field['is_for_all'] = 'true' in line.lower()
                elif line.startswith('possible_values:'):
                    if 'nil' in line:
                        current_field['possible_values'] = None

            # End of a field entry
            if line.endswith('},') and current_field is not None and 'id' in current_field:
                fields.append(current_field)
                current_field = None

        # Add the last field if it exists
        if current_field is not None and 'id' in current_field:
            fields.append(current_field)

        print(f"Manually extracted {len(fields)} fields")
        return fields
    except Exception as e:
        print(f"Error in manual extraction: {str(e)}")
        return []

def convert_ruby_to_json(ruby_str: str) -> str:
    """
    Convert Ruby hash syntax to valid JSON.

    Args:
        ruby_str: String with Ruby hash syntax

    Returns:
        String with valid JSON syntax
    """
    # Replace Ruby hash rocket with colon+space for keys
    result = re.sub(r'(\w+):', r'"\1":', ruby_str)

    # Replace nil with null
    result = result.replace('nil', 'null')

    # Add quotes around string values that don't have them
    # This is a simplified approach and might not handle all cases
    result = re.sub(r':\s*([^"\d\[\{][^,\}\]]+)', r': "\1"', result)

    return result

def get_custom_fields(session_name: str = "rails_console", output_path: str = None) -> List[Dict[str, Any]]:
    """
    Retrieve custom fields from OpenProject using Rails console via tmux.

    Args:
        session_name: tmux session name
        output_path: path to save JSON output

    Returns:
        List of custom field data
    """
    print(f"Getting custom fields from Rails console in tmux session: {session_name}")

    import time

    # Generate the Ruby script path
    script_path = os.path.join(os.path.dirname(__file__), "get_custom_fields.rb")
    temp_output_path = "/tmp/openproject_custom_fields.json"

    # Check if the script exists
    if not os.path.exists(script_path):
        print(f"Error: Ruby script not found at {script_path}")
        return []

    # Load and run the Ruby script
    print(f"Loading Ruby script from {script_path}...")
    with open(script_path, 'r') as f:
        ruby_script = f.read()

    # Run the script in the Rails console
    if not run_command_in_tmux(session_name, f"load '{script_path}'"):
        print("Failed to load Ruby script in Rails console")
        return []

    # Wait for script to complete
    print("Waiting for script to complete...")
    time.sleep(5)

    # Capture output to see if the script ran successfully
    console_output = capture_tmux_output(session_name)
    print(f"Captured {len(console_output)} characters from tmux")

    # Check if the output file was created by running docker command
    print(f"Checking for output file: {temp_output_path}")

    # Try to read the output file from the Docker container
    op_container = config.openproject_config.get("container", "openproject-web-1")

    try:
        # Read the output file using docker
        result = subprocess.run(
            ["docker", "exec", op_container, "cat", temp_output_path],
            check=True,
            capture_output=True,
            text=True
        )
        json_data = result.stdout

        # Parse the JSON
        if json_data:
            try:
                fields = json.loads(json_data)
                print(f"Successfully loaded {len(fields)} custom fields from file")

                # Save to our own output file if path provided
                if output_path:
                    try:
                        with open(output_path, 'w') as f:
                            json.dump(fields, f, indent=2)
                        print(f"Saved custom fields to {output_path}")
                    except IOError as e:
                        print(f"Error saving to file: {e}")

                return fields
            except json.JSONDecodeError as e:
                print(f"Error parsing JSON from file: {e}")
                print(f"File content sample: {json_data[:200]}...")
        else:
            print("Output file was empty or not found")
    except subprocess.SubprocessError as e:
        print(f"Error reading output file: {e}")

    # If we get here, try to parse the output directly from tmux
    print("Attempting to parse custom fields directly from console output...")

    # Look for JSON output - anything between [ and ]
    json_pattern = r'\[.*\]'
    match = re.search(json_pattern, console_output, re.DOTALL)

    if match:
        json_str = match.group()
        print(f"Found JSON string (first 100 characters): {json_str[:100]}...")

        try:
            # Parse the JSON
            fields = json.loads(json_str)
            print(f"Successfully parsed JSON with {len(fields)} custom fields")

            # Save to file if path provided
            if output_path:
                try:
                    with open(output_path, 'w') as f:
                        json.dump(fields, f, indent=2)
                    print(f"Saved custom fields to {output_path}")
                except IOError as e:
                    print(f"Error saving to file: {e}")

            return fields
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON: {e}")
            print(f"JSON string sample: {json_str[:200]}...")
    else:
        print("Could not find JSON output in the tmux capture")
        print(f"Output sample: {console_output[:200]}...")

    # If all else fails, try manual extraction
    fields = parse_ruby_output(console_output)

    if fields:
        print(f"Manually extracted {len(fields)} custom fields")

        # Save to file if path provided
        if output_path:
            try:
                with open(output_path, 'w') as f:
                    json.dump(fields, f, indent=2)
                print(f"Saved custom fields to {output_path}")
            except IOError as e:
                print(f"Error saving to file: {e}")

        return fields

    # If we get here, we couldn't retrieve any custom fields
    print("No custom fields were retrieved using any method")
    return []

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Get custom fields from OpenProject via Rails console",
        epilog="""
Requirements:
  - tmux installed on your system
  - An existing tmux session with a running Rails console

Example:
  python scripts/get_openproject_fields.py --session rails_console --output var/data/custom_fields.json
        """
    )

    parser.add_argument(
        "--session",
        help="tmux session name (default: rails_console)",
        default="rails_console"
    )
    parser.add_argument(
        "--output",
        help="Output file path (JSON format)",
        default=None
    )

    return parser.parse_args()

def main():
    """Main entry point."""
    # Parse command line arguments
    args = parse_args()

    # Get output path using config paths
    output_path = args.output
    if not output_path:
        output_path = os.path.join(config.get_path("data"), "openproject_custom_fields_rails.json")

    # Get and save custom fields
    fields = get_custom_fields(
        session_name=args.session,
        output_path=output_path
    )

    # Print summary
    if fields:
        print("\nCustom Fields:")
        for field in fields:
            print(f"- {field.get('name')} (ID: {field.get('id')}, Type: {field.get('field_format')})")

    return 0 if fields else 1

if __name__ == "__main__":
    sys.exit(main())
