#!/usr/bin/env python3
"""Summarize Custom Fields

This script reads the custom fields data from the JSON file
and provides a summary of the different field types and attributes.
"""

import json
import os
import sys
from collections import Counter
from typing import Any


def summarize_custom_fields(json_file: str) -> dict[str, Any]:
    """Summarize the custom fields data.

    Args:
        json_file: Path to the JSON file with custom fields data

    Returns:
        Dictionary with summary information

    """
    # Read the JSON file
    with open(json_file) as f:
        custom_fields = json.load(f)

    # Collect summary information
    field_count = len(custom_fields)
    field_formats: Counter[str] = Counter()
    field_types: Counter[str] = Counter()
    required_fields = 0
    all_project_fields = 0

    # Analyze each field
    for field in custom_fields:
        field_formats[field["field_format"]] += 1

        if "type" in field:
            field_types[field["type"]] += 1

        if field.get("is_required", False):
            required_fields += 1

        if field.get("is_for_all", False):
            all_project_fields += 1

    # Collect the most common field names for examples
    common_field_examples = {}
    for format_type in field_formats:
        examples = [field["name"] for field in custom_fields if field["field_format"] == format_type][:3]
        common_field_examples[format_type] = examples

    # Create the summary
    summary = {
        "total_fields": field_count,
        "field_formats": dict(field_formats),
        "field_types": dict(field_types),
        "required_fields": required_fields,
        "all_project_fields": all_project_fields,
        "field_examples": common_field_examples,
    }

    return summary


def print_summary(summary: dict[str, Any]) -> None:
    """Print the summary information in a readable format.

    Args:
        summary: Dictionary with summary information

    """
    print("Custom Fields Summary")
    print("===================\n")

    print(f"Total custom fields: {summary['total_fields']}")
    print(f"Required fields: {summary['required_fields']}")
    print(f"Fields available in all projects: {summary['all_project_fields']}\n")

    print("Field Formats:")
    for format_type, count in sorted(summary["field_formats"].items(), key=lambda x: x[1], reverse=True):
        percentage = count / summary["total_fields"] * 100
        print(f"  {format_type}: {count} ({percentage:.1f}%)")
        if format_type in summary["field_examples"]:
            examples = summary["field_examples"][format_type]
            print(f"    Examples: {', '.join(examples)}")

    if summary["field_types"]:
        print("\nField Types:")
        for field_type, count in sorted(summary["field_types"].items(), key=lambda x: x[1], reverse=True):
            percentage = count / summary["total_fields"] * 100
            print(f"  {field_type}: {count} ({percentage:.1f}%)")


def main() -> int:
    """Main entry point."""
    # Get the input file
    json_file = "var/data/openproject_custom_fields_rails.json"
    if len(sys.argv) > 1:
        json_file = sys.argv[1]

    # Check if file exists
    if not os.path.exists(json_file):
        print(f"Error: File not found: {json_file}")
        return 1

    # Summarize the data
    summary = summarize_custom_fields(json_file)

    # Print the summary
    print_summary(summary)

    return 0


if __name__ == "__main__":
    sys.exit(main())
