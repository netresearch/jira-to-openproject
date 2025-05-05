#!/usr/bin/env python3
"""
Create Custom Fields JSON

This script parses a custom fields list captured from the Rails console
and creates a JSON file with the custom field data.
"""

import json
import re
import sys
from typing import Any


def parse_custom_fields_list(input_file: str) -> list[dict[str, Any]]:
    """
    Parse the custom fields list.

    Args:
        input_file: Path to the custom fields list file

    Returns:
        List of custom field dictionaries
    """
    custom_fields = []

    # Read the input file
    with open(input_file) as f:
        lines = f.readlines()

    # Find the start and end markers
    start_idx = -1
    end_idx = -1

    for i, line in enumerate(lines):
        if "FIELDS_START" in line:
            start_idx = i + 1  # Start after the marker
        elif "FIELDS_END" in line:
            end_idx = i
            break

    if start_idx == -1 or end_idx == -1 or start_idx >= end_idx:
        print("Could not find start and end markers in the file")
        return []

    # Parse the custom fields
    field_lines = lines[start_idx:end_idx]

    for line in field_lines:
        line = line.strip()
        if not line:
            continue

        # Parse the line with format: "id: name (field_format)"
        match = re.match(r"(\d+): (.*) \((.*)\)", line)
        if match:
            field_id, field_name, field_format = match.groups()

            custom_fields.append(
                {"id": int(field_id), "name": field_name, "field_format": field_format}
            )

    return custom_fields


def main() -> int:
    """Main entry point."""
    if len(sys.argv) < 3:
        print("Usage: python create_custom_fields_json.py <input_file> <output_file>")
        return 1

    input_file = sys.argv[1]
    output_file = sys.argv[2]

    # Parse the custom fields list
    custom_fields = parse_custom_fields_list(input_file)

    if not custom_fields:
        print("No custom fields found")
        return 1

    # Save to output file
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
