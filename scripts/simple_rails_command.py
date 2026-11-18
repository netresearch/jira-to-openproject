#!/usr/bin/env python3
# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Simple Rails Command.

This script runs a simple command in the Rails console via tmux
and captures the output to verify that the command execution
and output capture are working correctly.
"""

import sys

if __package__:
    from .get_complete_custom_fields import get_complete_custom_fields
else:
    from get_complete_custom_fields import get_complete_custom_fields


def main() -> None:
    """Check the existing Rails console and optionally write its custom field definitions."""
    output_file = sys.argv[1] if len(sys.argv) > 1 else None
    get_complete_custom_fields("rails_console", output_file)


if __name__ == "__main__":
    main()
