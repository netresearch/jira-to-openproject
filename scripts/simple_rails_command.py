#!/usr/bin/env python3
"""Simple Rails Command.

This script runs a simple command in the Rails console via tmux
and captures the output to verify that the command execution
and output capture are working correctly.
"""

import json
import re
import subprocess
import sys
import time


def run_command_in_tmux(session_name: str, command: str) -> bool:
    """Run a command in the specified tmux session."""
    print(f"Running command in tmux session '{session_name}':")
    print(f"Command: {command}")

    # Clear the screen first (Ctrl+L)
    subprocess.run(["tmux", "send-keys", "-t", session_name, "C-l"], check=True)
    time.sleep(0.5)

    # Send the command
    subprocess.run(["tmux", "send-keys", "-t", session_name, command], check=True)
    subprocess.run(["tmux", "send-keys", "-t", session_name, "Enter"], check=True)

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
        text=True,
    )

    output = result.stdout
    print(f"Captured {len(output)} characters")
    return output


def main() -> None:
    """Main entry point."""
    # Parse command line arguments
    output_file = None
    if len(sys.argv) > 1:
        output_file = sys.argv[1]

    session_name = "rails_console"

    # Run a simple command that outputs a marker we can easily find
    marker_start = "START_CUSTOM_FIELDS_OUTPUT"
    marker_end = "END_CUSTOM_FIELDS_OUTPUT"

    # Build a command that outputs the markers and a simple count
    command = f"""
    puts "{marker_start}"
    puts CustomField.count
    puts "{marker_end}"
    """

    # Send the command to tmux
    run_command_in_tmux(session_name, command)

    # Wait for the command to complete
    print("Waiting for command to complete...")
    time.sleep(3)

    # Capture the output
    output = capture_tmux_output(session_name)

    # Extract the content between the markers
    pattern = f"{marker_start}(.*?){marker_end}"
    match = re.search(pattern, output, re.DOTALL)

    if match:
        result = match.group(1).strip()
        count_lines = [line for line in result.split("\n") if line.strip().isdigit()]
        if count_lines:
            custom_field_count = int(count_lines[0].strip())
            print(f"Found {custom_field_count} custom fields")
        else:
            custom_field_count = 0
            print("Could not determine custom field count")

        # Now try to get the list of custom fields using proper JSON generation
        print("\nRetrieving all custom fields using direct JSON output...")

        # Use unique markers to clearly identify the output
        json_start = "CUSTOM_FIELDS_JSON_START_MARKER"
        json_end = "CUSTOM_FIELDS_JSON_END_MARKER"

        # Create a command that generates valid JSON in Ruby and outputs it
        # We're now using JSON.generate to ensure proper JSON formatting
        json_command = f"""
        require 'json'

        # Output start marker
        puts "{json_start}"

        # Generate field data
        fields = CustomField.all.map do |cf|
          {{
            id: cf.id,
            name: cf.name,
            field_format: cf.field_format,
            type: cf.type,
            is_required: cf.is_required,
            is_for_all: cf.is_for_all
          }}
        end

        # Generate valid JSON using Ruby's JSON library
        json_output = JSON.generate(fields)

        # Output JSON in a way that's easy to find
        puts "JSON_START"
        puts json_output
        puts "JSON_END"

        # Output end marker
        puts "{json_end}"
        """

        # Send the command to tmux
        run_command_in_tmux(session_name, json_command)

        # Wait for the command to complete - large output may need more time
        print("Waiting for JSON output...")
        time.sleep(10)

        # Capture the output
        json_output = capture_tmux_output(session_name, lines=5000)  # Capture more lines for large datasets

        # Save the raw output for debugging if needed
        with open("raw_tmux_output.txt", "w") as f:
            f.write(json_output)
        print("Saved raw tmux output to raw_tmux_output.txt for debugging")

        # Extract the content between the JSON markers
        json_section_pattern = f"{json_start}.*?JSON_START(.*?)JSON_END.*?{json_end}"
        json_section_match = re.search(json_section_pattern, json_output, re.DOTALL)

        if json_section_match:
            # Extract the JSON string
            json_str = json_section_match.group(1).strip()
            print(f"Found JSON string (first 100 chars): '{json_str[:100]}...'")

            try:
                # Parse the JSON
                custom_fields = json.loads(json_str)
                print(f"Successfully parsed JSON with {len(custom_fields)} custom fields")

                # Save to local file if path provided
                if output_file:
                    with open(output_file, "w") as f:
                        json.dump(custom_fields, f, indent=2)
                    print(f"Saved custom fields to {output_file}")

                # Print a summary
                print("\nCustom Fields:")
                for field in custom_fields[:5]:  # Show first 5 fields
                    print(f"- {field.get('name')} (ID: {field.get('id')}, Type: {field.get('field_format')})")
                if len(custom_fields) > 5:
                    print(f"... and {len(custom_fields) - 5} more fields")
            except json.JSONDecodeError as e:
                print(f"Error parsing JSON: {e}")
                print(f"Raw JSON string (first 500 chars):\n{json_str[:500]}")
        else:
            print("Could not find JSON_START/JSON_END markers in the output")

            # Try to find the JSON output directly
            raw_json_match = re.search(r'\[\{"id":\d+,.*\}\]', json_output, re.DOTALL)
            if raw_json_match:
                raw_json = raw_json_match.group(0)
                print(f"Found raw JSON (first 100 chars): '{raw_json[:100]}...'")

                try:
                    # Parse the JSON
                    custom_fields = json.loads(raw_json)
                    print(f"Successfully parsed raw JSON with {len(custom_fields)} custom fields")

                    # Save to local file if path provided
                    if output_file:
                        with open(output_file, "w") as f:
                            json.dump(custom_fields, f, indent=2)
                        print(f"Saved custom fields to {output_file}")

                    # Print a summary
                    print("\nCustom Fields:")
                    for field in custom_fields[:5]:  # Show first 5 fields
                        print(f"- {field.get('name')} (ID: {field.get('id')}, Type: {field.get('field_format')})")
                    if len(custom_fields) > 5:
                        print(f"... and {len(custom_fields) - 5} more fields")
                except json.JSONDecodeError as e:
                    print(f"Error parsing raw JSON: {e}")
            else:
                print("Could not find any JSON array in the output")
                print(f"Output between markers (first 200 chars): {json_output[:200]}")
    else:
        print("Could not find output between markers")
        print(f"Raw output (first 200 characters): {output[:200]}")


if __name__ == "__main__":
    main()
