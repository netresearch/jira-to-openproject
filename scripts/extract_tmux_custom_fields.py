#!/usr/bin/env python3
"""
Extract Custom Fields from Tmux Output

This script extracts the custom fields data directly from
the tmux session output and saves it to a JSON file.
"""

import subprocess
import re
import json
import sys
import os

def capture_tmux_output(session_name="rails_console", lines=1000):
    """Capture output from the tmux session."""
    print(f"Capturing output from tmux session '{session_name}'...")

    try:
        # Capture pane content with history
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-S", f"-{lines}", "-t", session_name],
            check=True,
            capture_output=True,
            text=True
        )

        output = result.stdout
        print(f"Captured {len(output)} characters")
        return output
    except subprocess.SubprocessError as e:
        print(f"Error capturing tmux output: {e}")
        return ""

def parse_custom_fields(output):
    """Parse custom fields from the output."""
    # The output appears to be in a format like:
    # ID|Name|field_format|type|is_required|is_for_all
    fields = []

    # Process each line
    for line in output.split('\n'):
        line = line.strip()
        if not line:
            continue

        # Check if the line matches the expected format with pipe delimiters
        parts = line.split('|')
        if len(parts) >= 6:  # We need at least the essential fields
            try:
                # Try to convert the ID to an integer to validate it's a real custom field line
                field_id = int(parts[0])

                # Create a field dictionary
                field = {
                    'id': field_id,
                    'name': parts[1],
                    'field_format': parts[2],
                    'type': parts[3],
                    'is_required': parts[4].lower() == 'true',
                    'is_for_all': parts[5].lower() == 'true'
                }

                fields.append(field)
            except (ValueError, IndexError):
                # Skip lines that don't match the expected format
                pass

    return fields

def main():
    """Main entry point."""
    # Get the output file path
    output_file = "var/data/openproject_custom_fields_rails.json"
    if len(sys.argv) > 1:
        output_file = sys.argv[1]

    # Create the output directory if it doesn't exist
    os.makedirs(os.path.dirname(output_file), exist_ok=True)

    # Capture the tmux output
    output = capture_tmux_output(lines=10000)  # Capture a lot of history

    # Parse the custom fields
    fields = parse_custom_fields(output)

    if not fields:
        print("No custom fields found in the tmux output")
        return 1

    # Save the fields to the output file
    with open(output_file, 'w') as f:
        json.dump(fields, f, indent=2)

    print(f"Successfully saved {len(fields)} custom fields to {output_file}")

    # Print a sample of the fields
    print("\nCustom Fields Sample:")
    for field in fields[:5]:  # Show first 5 fields
        print(f"- {field['name']} (ID: {field['id']}, Type: {field['field_format']})")
    if len(fields) > 5:
        print(f"... and {len(fields) - 5} more fields")

    return 0

if __name__ == "__main__":
    sys.exit(main())
