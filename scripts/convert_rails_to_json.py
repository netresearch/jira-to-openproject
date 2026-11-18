#!/usr/bin/env python3
# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Convert Rails output to JSON.

This script takes Ruby-formatted output and converts it to valid JSON.
"""

import json
import re
import sys
from pathlib import Path

if __package__:
    from .operator_files import read_operator_text, write_operator_json
else:
    from operator_files import read_operator_text, write_operator_json


_WORD = re.compile(r"\w+")
_SYMBOL_ROCKET = re.compile(r":(\w++)\s*+=>")


def _json_tokens(source: str) -> str:
    """Translate Ruby hash delimiters and nil while preserving quoted JSON strings."""
    output = []
    decoder = json.JSONDecoder()
    index = 0
    while index < len(source):
        char = source[index]
        if char == '"':
            _, end = decoder.raw_decode(source, index)
            output.append(source[index:end])
        elif symbol := _SYMBOL_ROCKET.match(source, index):
            output.append(json.dumps(symbol[1]) + ":")
            end = symbol.end()
        elif word := _WORD.match(source, index):
            value = word[0]
            end = word.end()
            if source.startswith(":", end):
                output.append(json.dumps(value))
            elif value == "nil":
                output.append("null")
            else:
                output.append(value)
        elif source.startswith("=>", index):
            output.append(":")
            end = index + 2
        else:
            output.append(char)
            end = index + 1
        index = end
    return "".join(output)


def convert_ruby_to_json(input_file: str, output_file: str) -> bool:
    """Convert Ruby hash syntax to valid JSON.

    Args:
        input_file: Path to input file with Ruby hash syntax
        output_file: Path to output JSON file

    """
    # Read the input file
    ruby_str = read_operator_text(Path(input_file))

    # Extract the array portion
    start = ruby_str.find("[{")
    end = ruby_str.rfind("}]")
    if start < 0 or end < start:
        print("Could not find array in input file")
        return False

    try:
        data = json.loads(_json_tokens(ruby_str[start : end + 2]))

        # Write to output file
        write_operator_json(Path(output_file), data)

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
