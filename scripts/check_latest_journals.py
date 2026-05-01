#!/usr/bin/env python3
"""Check journal count for latest NRS-182 migration"""

import sys
from pathlib import Path

# Imports use the ``src.infrastructure...`` package path, so the project
# root (parent of ``src/``) must be on sys.path, not ``src/`` itself.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.infrastructure.openproject.rails_console_client import RailsConsoleClient


def main():
    client = RailsConsoleClient()

    # Get latest NRS-182 work package
    wp_result = client.execute(
        """
WorkPackage.where(project_id: Project.find_by(identifier: 'nrs').id).order(id: :desc).limit(1).pluck(:id).first
    """.strip(),
    )

    print(f"Latest work package ID: {wp_result}")

    if wp_result:
        # Extract ID from result
        wp_id = wp_result.strip() if isinstance(wp_result, str) else wp_result

        # Query journal count
        count_result = client.execute(
            f"""
Journal.where(journable_id: {wp_id}, journable_type: 'WorkPackage').count
        """.strip(),
        )

        print(f"Journal count: {count_result}")

        # Query versions
        versions_result = client.execute(
            f"""
Journal.where(journable_id: {wp_id}, journable_type: 'WorkPackage').pluck(:version).sort
        """.strip(),
        )

        print(f"Journal versions: {versions_result}")


if __name__ == "__main__":
    main()
