#!/usr/bin/env python3
"""Check journal count for NRS-182 work package"""

import sys
from pathlib import Path

# Imports use the ``src.infrastructure...`` package path, so the project
# root (parent of ``src/``) must be on sys.path, not ``src/`` itself.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.infrastructure.openproject.rails_console_client import RailsConsoleClient


def main():
    client = RailsConsoleClient()

    # Query journal count
    count_result = client.execute(
        """
Journal.where(journable_id: 5581104, journable_type: 'WorkPackage').count
    """.strip(),
    )

    print(f"Journal count result: {count_result}")

    # Query version numbers
    versions_result = client.execute(
        """
Journal.where(journable_id: 5581104, journable_type: 'WorkPackage').pluck(:version).sort
    """.strip(),
    )

    print(f"Versions result: {versions_result}")


if __name__ == "__main__":
    main()
