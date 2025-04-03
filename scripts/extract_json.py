#!/usr/bin/env python3
"""
Extract JSON from raw tmux output.

This script extracts JSON data from the raw tmux output file
by looking for specific markers in the output.
"""

import sys
import re
import json

def extract_json(input_file, output_file):
    """
    Extract JSON from raw tmux output.

    Args:
        input_file: Path to input file with raw tmux output
        output_file: Path to output JSON file

    Returns:
        True if extraction was successful, False otherwise
    """
    # Read the raw output file
    with open(input_file, 'r') as f:
        content = f.read()

    # First find the line numbers where json_output is printed
    lines = content.split('\n')

    # Find where "puts json_output" is located
    json_output_line = None
    for i, line in enumerate(lines):
        if "puts json_output" in line:
            json_output_line = i
            break

    if json_output_line is None:
        print("Could not find 'puts json_output' in the file")
        return False

    # The actual JSON should be in the next few lines
    # It might be split across multiple lines, so we need to reassemble it
    json_lines = []
    for i in range(json_output_line + 1, min(json_output_line + 50, len(lines))):
        line = lines[i]
        # Stop if we reach JSON_END or another command
        if "JSON_END" in line or line.startswith("irb(main):") and "puts" in line:
            break
        json_lines.append(line)

    # Join the lines and look for the JSON array
    json_text = ''.join(json_lines)

    # Find the JSON array in the text
    array_start = json_text.find('[{')
    array_end = json_text.rfind('}]') + 2

    if array_start >= 0 and array_end > array_start:
        json_array = json_text[array_start:array_end]
        print(f"Found JSON array (first 100 chars): '{json_array[:100]}...'")

        try:
            # Parse the JSON to verify it's valid
            data = json.loads(json_array)
            print(f"Successfully parsed JSON with {len(data)} records")

            # Save to output file
            with open(output_file, 'w') as f:
                json.dump(data, f, indent=2)

            print(f"Saved JSON data to {output_file}")

            # Print a summary of the first few fields
            print("\nCustom Fields:")
            for field in data[:5]:  # Show first 5 fields
                print(f"- {field.get('name')} (ID: {field.get('id')}, Type: {field.get('field_format')})")
            if len(data) > 5:
                print(f"... and {len(data) - 5} more fields")

            return True
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON: {e}")
            print(f"JSON array content (first 200 chars): {json_array[:200]}")
    else:
        print("Could not find JSON array in the output")

    return False

def main():
    """Main entry point."""
    # Check command line arguments
    if len(sys.argv) < 3:
        print("Usage: python extract_json.py <input_file> <output_file>")
        return 1

    input_file = sys.argv[1]
    output_file = sys.argv[2]

    success = extract_json(input_file, output_file)
    return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main())
