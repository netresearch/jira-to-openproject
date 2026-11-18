#!/usr/bin/env python3
# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Get Custom Fields (Simple Approach).

This script uses the most reliable approach to get custom fields from Rails console:
1. Directly runs a simple command with clear markers in the Rails console
2. Captures the output
3. Creates a JSON file with the custom fields
"""

import sys
from pathlib import Path

if __package__:
    from .get_complete_custom_fields import get_complete_custom_fields
    from .operator_files import write_operator_json
else:
    from get_complete_custom_fields import get_complete_custom_fields
    from operator_files import write_operator_json

_FIELDS = ("id", "name", "field_format", "type", "is_required", "is_for_all")


def main() -> int:
    """Write the basic field attributes from the current completed Rails query."""
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("var/data/openproject_custom_fields_rails.json")
    fields = get_complete_custom_fields()
    if not fields:
        print("No custom fields found in the output")
        return 1

    rows = [{key: field.get(key) for key in _FIELDS} for field in fields]
    write_operator_json(output, rows)
    print(f"Saved {len(rows)} custom fields to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
