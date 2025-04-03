#!/usr/bin/env python3
"""
Get Complete Custom Fields

This script runs commands in the Rails console to get detailed
information about custom fields and saves it to a JSON file.
"""

import subprocess
import time
import re
import json
import sys
import os
from typing import List, Dict, Any

def run_command_in_tmux(session_name: str, command: str) -> bool:
    """Run a command in the specified tmux session."""
    print(f"Running command in tmux session '{session_name}':")
    print(f"Command: {command}")

    # Clear the screen first (Ctrl+L)
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, "C-l"],
        check=True
    )
    time.sleep(0.5)

    # Send the command
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, command],
        check=True
    )
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, "Enter"],
        check=True
    )

    print("Command sent to tmux session")
    return True

def capture_tmux_output(session_name: str, lines: int = 1000) -> str:
    """Capture output from the tmux session."""
    print(f"Capturing output from tmux session '{session_name}'")

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

def get_complete_custom_fields(session_name: str = "rails_console", output_file: str = None) -> List[Dict[str, Any]]:
    """
    Get detailed information about custom fields from the Rails console.

    Args:
        session_name: tmux session name
        output_file: path to output JSON file

    Returns:
        List of custom field data
    """
    # Markers for our output
    start_marker = "CUSTOM_FIELDS_DETAIL_START"
    end_marker = "CUSTOM_FIELDS_DETAIL_END"

    # Command to get detailed custom field information
    command = f"""
    puts '{start_marker}'

    # Get all custom fields with detailed information
    result = CustomField.all.map do |cf|
      {{
        id: cf.id,
        name: cf.name,
        field_format: cf.field_format,
        type: cf.type,
        is_required: cf.is_required,
        is_for_all: cf.is_for_all,
        possible_values: cf.possible_values.nil? ? [] : cf.possible_values,
        default_value: cf.default_value,
        regexp: cf.regexp,
        min_length: cf.min_length,
        max_length: cf.max_length,
        editable: cf.editable,
        visible: cf.visible,
        created_at: cf.created_at,
        updated_at: cf.updated_at
      }}
    end

    # Output the data in a way that's easy to parse line by line
    puts "FIELD_COUNT: #{{result.size}}"
    result.each_with_index do |cf, i|
      puts "FIELD ##{{i+1}}: "
      puts "  ID: #{{cf[:id]}}"
      puts "  Name: #{{cf[:name]}}"
      puts "  Format: #{{cf[:field_format]}}"
      puts "  Type: #{{cf[:type]}}"
      puts "  Required: #{{cf[:is_required]}}"
      puts "  For All: #{{cf[:is_for_all]}}"
      puts "  Possible Values: #{{cf[:possible_values].inspect}}"
      puts "  Default Value: #{{cf[:default_value].inspect}}"
      puts "  Regexp: #{{cf[:regexp].inspect}}"
      puts "  Min Length: #{{cf[:min_length].inspect}}"
      puts "  Max Length: #{{cf[:max_length].inspect}}"
      puts "  Editable: #{{cf[:editable]}}"
      puts "  Visible: #{{cf[:visible]}}"
      puts "  Created At: #{{cf[:created_at]}}"
      puts "  Updated At: #{{cf[:updated_at]}}"
    end
    puts '{end_marker}'
    """

    # Run the command
    run_command_in_tmux(session_name, command)

    # Wait for the command to complete
    print("Waiting for command to complete...")
    time.sleep(10)

    # Capture the output
    output = capture_tmux_output(session_name, lines=10000)

    # Save raw output for debugging
    with open("raw_fields_output.txt", "w") as f:
        f.write(output)

    # Find the markers in the output
    start_idx = output.find(start_marker)
    end_idx = output.find(end_marker)

    if start_idx == -1 or end_idx == -1 or start_idx >= end_idx:
        print("Could not find start and end markers in the output")
        return []

    # Extract the content between the markers
    content = output[start_idx + len(start_marker):end_idx].strip()

    # Parse the custom fields
    custom_fields = []
    current_field = None
    field_count = 0

    for line in content.split('\n'):
        line = line.strip()
        if not line:
            continue

        if line.startswith('FIELD_COUNT:'):
            # Extract the field count
            try:
                field_count = int(line.split(':')[1].strip())
                print(f"Found {field_count} custom fields")
            except (ValueError, IndexError):
                print("Could not parse field count")

        elif line.startswith('FIELD #'):
            # Start a new field
            if current_field:
                custom_fields.append(current_field)
            current_field = {}

        elif line.startswith('  ID:') and current_field is not None:
            try:
                current_field['id'] = int(line.split(':', 1)[1].strip())
            except (ValueError, IndexError):
                pass

        elif line.startswith('  Name:') and current_field is not None:
            try:
                current_field['name'] = line.split(':', 1)[1].strip()
            except IndexError:
                pass

        elif line.startswith('  Format:') and current_field is not None:
            try:
                current_field['field_format'] = line.split(':', 1)[1].strip()
            except IndexError:
                pass

        elif line.startswith('  Type:') and current_field is not None:
            try:
                current_field['type'] = line.split(':', 1)[1].strip()
            except IndexError:
                pass

        elif line.startswith('  Required:') and current_field is not None:
            current_field['is_required'] = 'true' in line.lower()

        elif line.startswith('  For All:') and current_field is not None:
            current_field['is_for_all'] = 'true' in line.lower()

        elif line.startswith('  Possible Values:') and current_field is not None:
            try:
                values_str = line.split(':', 1)[1].strip()
                if values_str == '[]':
                    current_field['possible_values'] = []
                else:
                    # This is a simplified approach - proper parsing would need more logic
                    values = []
                    if values_str.startswith('[') and values_str.endswith(']'):
                        values_list = values_str[1:-1].split(',')
                        for v in values_list:
                            v = v.strip()
                            if v.startswith('"') and v.endswith('"'):
                                values.append(v[1:-1])
                            elif v.isdigit():
                                values.append(int(v))
                            elif v == 'nil':
                                values.append(None)
                            else:
                                values.append(v)
                    current_field['possible_values'] = values
            except IndexError:
                current_field['possible_values'] = []

    # Add the last field
    if current_field:
        custom_fields.append(current_field)

    print(f"Parsed {len(custom_fields)} custom fields")

    # Save to output file if provided
    if output_file:
        with open(output_file, 'w') as f:
            json.dump(custom_fields, f, indent=2)
        print(f"Saved custom fields to {output_file}")

    # Print a summary
    print("\nCustom Fields:")
    for field in custom_fields[:5]:  # Show first 5 fields
        print(f"- {field.get('name')} (ID: {field.get('id')}, Type: {field.get('field_format')})")
    if len(custom_fields) > 5:
        print(f"... and {len(custom_fields) - 5} more fields")

    return custom_fields

def main():
    """Main entry point."""
    # Parse command line arguments
    output_file = None
    if len(sys.argv) > 1:
        output_file = sys.argv[1]
    else:
        output_file = "var/data/openproject_custom_fields_complete.json"

    # Get the custom fields
    fields = get_complete_custom_fields(
        session_name="rails_console",
        output_file=output_file
    )

    return 0 if fields else 1

if __name__ == "__main__":
    sys.exit(main())
