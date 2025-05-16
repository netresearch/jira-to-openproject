#!/usr/bin/env python3
"""Extract Custom Fields from Tmux Output.

This script extracts the custom fields data directly from
the tmux session output and saves it to a JSON file.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from rich.console import Console

console = Console()


def capture_tmux_output(session_name: str = "rails_console", lines: int = 1000) -> str:
    """Capture output from the tmux session."""
    console.print(f"Capturing output from tmux session '{session_name}'...")

    try:
        # Capture pane content with history
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-S", f"-{lines}", "-t", session_name],
            check=True,
            capture_output=True,
            text=True,
        )

        output = result.stdout
        console.print(f"Captured {len(output)} characters")
    except subprocess.SubprocessError as e:
        msg = "Error capturing tmux output."
        raise RuntimeError(msg) from e

    return output


def parse_custom_fields(output: str) -> list[dict[str, Any]]:
    """Parse custom fields from the output."""
    # The output appears to be in a format like:
    # ID|Name|field_format|type|is_required|is_for_all
    fields: list[dict[str, Any]] = []

    # Process each line
    for line in output.split("\n"):
        line = line.strip()
        if not line:
            continue

        # Check if the line matches the expected format with pipe delimiters
        parts = line.split("|")
        if len(parts) >= 6:  # We need at least the essential fields
            try:
                # Try to convert the ID to an integer
                # to validate it's a real custom field line
                field_id = int(parts[0])

                # Create a field dictionary
                field = {
                    "id": field_id,
                    "name": parts[1],
                    "field_format": parts[2],
                    "type": parts[3],
                    "is_required": parts[4].lower() == "true",
                    "is_for_all": parts[5].lower() == "true",
                }

                fields.append(field)
            except (ValueError, IndexError):
                # Skip lines that don't match the expected format
                pass

    return fields


def main() -> int:
    """Main entry point."""
    # Get the output file path
    output_file = Path("var/data/openproject_custom_fields_rails.json")
    if len(sys.argv) > 1:
        output_file = Path(sys.argv[1])

    # Create the output directory if it doesn't exist
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Capture the tmux output
    output = capture_tmux_output(lines=10000)  # Capture a lot of history

    # Parse the custom fields
    fields: list[dict[str, Any]] = parse_custom_fields(output)

    if not fields:
        console.print("No custom fields found in the tmux output")
        return 1

    # Save the fields to the output file
    with output_file.open("w") as f:
        json.dump(fields, f, indent=2)

    console.print(f"Successfully saved {len(fields)} custom fields to {output_file}")

    # Print a sample of the fields
    console.print("\nCustom Fields Sample:")
    for field in fields[:5]:  # Show first 5 fields
        console.print(
            f"- {field['name']} (ID: {field['id']}, Type: {field['field_format']})",
        )
    if len(fields) > 5:
        console.print(f"... and {len(fields) - 5} more fields")

    return 0


if __name__ == "__main__":
    sys.exit(main())
