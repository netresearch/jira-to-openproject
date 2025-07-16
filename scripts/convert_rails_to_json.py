#!/usr/bin/env python3
"""Convert Rails output to JSON.

This script takes Ruby-formatted output and converts it to valid JSON.
"""

import json
import re
import sys
from pathlib import Path


def convert_ruby_to_json(input_file: str, output_file: str) -> bool:
    """Convert Ruby hash syntax to valid JSON.

    Args:
        input_file: Path to input file with Ruby hash syntax
        output_file: Path to output JSON file

    """
    # Read the input file
    with Path(input_file).open() as f:
        ruby_str = f.read()

    # Extract the array portion
    match = re.search(r"\[\{.*\}\]", ruby_str, re.DOTALL)
    if not match:
        print("Could not find array in input file")
        return False

    ruby_array = match.group()

    # Convert Ruby syntax to JSON

    # First replace Ruby hash keys (symbol: value) with JSON format ("symbol": value)
    json_str = re.sub(r"(\w+):", r'"\1":', ruby_array)

    # Replace nil with null
    json_str = json_str.replace("nil", "null")

    # Handle special cases for possible_values arrays
    # This specific pattern handles ["5"] format
    json_str = re.sub(r'\["(\d+)"\]', r'["\1"]', json_str)

    try:
        # Parse to validate and pretty-print
        data = json.loads(json_str)

        # Write to output file
        with Path(output_file).open("w") as f:
            json.dump(data, f, indent=2)

        print(f"Successfully converted {len(data)} records to JSON")
        print(f"Saved to {output_file}")
        return True
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}")
        return False


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python convert_rails_to_json.py <input_file> <output_file>")
        sys.exit(1)

    success = convert_ruby_to_json(sys.argv[1], sys.argv[2])
    sys.exit(0 if success else 1)
