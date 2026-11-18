#!/usr/bin/env python3
# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Get Complete Custom Fields.

This script runs commands in the Rails console to get detailed
information about custom fields and saves it to a JSON file.
"""

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

if __package__:
    from .export_json_file import write_json_export
else:
    from export_json_file import write_json_export


def run_command_in_tmux(session_name: str, command: str) -> bool:
    """Submit one literal Ruby command to the selected Rails console."""
    subprocess.run(["tmux", "send-keys", "-t", session_name, "-l", command], check=True)
    subprocess.run(["tmux", "send-keys", "-t", session_name, "Enter"], check=True)
    return True


def capture_tmux_output(session_name: str, lines: int = 10000) -> str:
    return subprocess.check_output(
        ["tmux", "capture-pane", "-J", "-p", "-S", f"-{lines}", "-t", session_name],
        text=True,
    )


def _field_query(token: str) -> str:
    # Only public model attributes are serialized. Rails versions can omit legacy columns.
    return (
        "require 'json'; count = 0; CustomField.order(:id).each do |field|; "
        "row = field.as_json(only: %i[id name field_format type is_required is_for_all default_value "
        "regexp min_length max_length editable visible created_at updated_at]); "
        "row['possible_values'] = field.possible_values if field.respond_to?(:possible_values); "
        f"puts '{token} ' + JSON.generate(row); count += 1; end; puts '{token} END ' + count.to_s; nil"
    )


def _read_fields(output: str, token: str) -> list[dict[str, Any]] | None:
    rows = []
    for line in output.splitlines():
        if not line.startswith(token + " "):
            continue
        value = line.removeprefix(token + " ")
        if value.startswith("END "):
            if int(value.removeprefix("END ")) != len(rows):
                msg = "Custom field output was truncated in the tmux history"
                raise ValueError(msg)
            return rows
        rows.append(json.loads(value))
    return None


def get_complete_custom_fields(
    session_name: str = "rails_console",
    output_file: str | None = None,
) -> list[dict[str, Any]]:
    """Return all available custom field metadata from the selected Rails console."""
    token = "J2O_FIELDS_" + uuid4().hex
    run_command_in_tmux(session_name, _field_query(token))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        fields = _read_fields(capture_tmux_output(session_name), token)
        if fields is not None:
            if output_file:
                write_json_export(Path(output_file), fields)
            print(f"Found {len(fields)} custom fields")
            for field in fields[:5]:
                print(f"- {field['name']} (ID: {field['id']}, Type: {field['field_format']})")
            return fields
        time.sleep(0.25)
    msg = "Timed out waiting for the custom field export in the Rails console"
    raise TimeoutError(msg)


def main() -> int:
    """Main entry point."""
    # Parse command line arguments
    output_file = sys.argv[1] if len(sys.argv) > 1 else "var/data/openproject_custom_fields_complete.json"

    # Get the custom fields
    get_complete_custom_fields(
        session_name="rails_console",
        output_file=output_file,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
