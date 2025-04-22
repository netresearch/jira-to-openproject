#!/usr/bin/env python3
"""
Get Custom Fields (Simple Approach)

This script uses the most reliable approach to get custom fields from Rails console:
1. Directly runs a simple command with clear markers in the Rails console
2. Captures the output
3. Creates a JSON file with the custom fields
"""

import json
import os
import subprocess
import sys
import time


def run_command_in_tmux(session_name, command):
    """Run a command in the specified tmux session."""
    print(f"Running command in tmux session '{session_name}'...")

    # Clear the screen first (Ctrl+L)
    subprocess.run(["tmux", "send-keys", "-t", session_name, "C-l"], check=True)
    time.sleep(0.5)

    # Send the command
    subprocess.run(["tmux", "send-keys", "-t", session_name, command], check=True)
    subprocess.run(["tmux", "send-keys", "-t", session_name, "Enter"], check=True)

    print("Command sent successfully")
    return True


def capture_tmux_output(session_name, lines=1000):
    """Capture output from the tmux session."""
    print(f"Capturing output from tmux session '{session_name}'...")

    # Capture pane content with history
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-S", f"-{lines}", "-t", session_name],
        check=True,
        capture_output=True,
        text=True,
    )

    output = result.stdout
    print(f"Captured {len(output)} characters")
    return output


def main():
    """Main entry point."""
    # Parse command line arguments
    output_file = "var/data/openproject_custom_fields_rails.json"
    if len(sys.argv) > 1:
        output_file = sys.argv[1]

    session_name = "rails_console"

    # Use a very simple approach: get a list of each custom field with its basic attributes
    # We use unique markers to ensure we can find our output
    command = """
    puts 'FIELDS_START'
    CustomField.all.each do |cf|
      puts "#{cf.id}|#{cf.name}|#{cf.field_format}|#{cf.type}|#{cf.is_required}|#{cf.is_for_all}"
    end
    puts 'FIELDS_END'
    """

    # Run the command
    run_command_in_tmux(session_name, command)

    # Wait for the command to complete
    print("Waiting for command to complete...")
    time.sleep(5)

    # Capture the output
    output = capture_tmux_output(session_name, lines=2000)

    # Save the raw output for debugging if needed
    with open("raw_output_simple.txt", "w") as f:
        f.write(output)

    # Extract the data between markers
    start_marker = "FIELDS_START"
    end_marker = "FIELDS_END"

    start_idx = output.find(start_marker)
    end_idx = output.find(end_marker, start_idx)

    if start_idx == -1 or end_idx == -1 or start_idx >= end_idx:
        print("Could not find the markers in the output")
        return 1

    # Extract and process the lines between markers
    content = output[start_idx + len(start_marker) : end_idx].strip()
    custom_fields = []

    for line in content.split("\n"):
        line = line.strip()
        if not line or "=>" in line or line.startswith("irb"):
            continue

        # Parse the pipe-delimited line
        parts = line.split("|")
        if len(parts) >= 6:
            try:
                custom_field = {
                    "id": int(parts[0]),
                    "name": parts[1],
                    "field_format": parts[2],
                    "type": parts[3],
                    "is_required": parts[4].lower() == "true",
                    "is_for_all": parts[5].lower() == "true",
                }
                custom_fields.append(custom_field)
            except (ValueError, IndexError) as e:
                print(f"Error parsing line: {line}, Error: {e}")

    if not custom_fields:
        print("No custom fields found in the output")
        return 1

    # Save the custom fields to the output file
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w") as f:
        json.dump(custom_fields, f, indent=2)

    print(f"Successfully saved {len(custom_fields)} custom fields to {output_file}")

    # Print a summary
    print("\nCustom Fields:")
    for field in custom_fields[:5]:  # Show first 5 fields
        print(f"- {field['name']} (ID: {field['id']}, Type: {field['field_format']})")
    if len(custom_fields) > 5:
        print(f"... and {len(custom_fields) - 5} more fields")

    return 0


if __name__ == "__main__":
    sys.exit(main())
