#!/usr/bin/env python3
"""
Test script to run the migration with all components to check for parameter handling.
"""

import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__))))

from run_migration import run_migration

# Run a migration with all components in dry-run mode
result = run_migration(
    dry_run=True,
    components=[
        "users",
        "custom_fields",
        "companies",
        "accounts",
        "projects",
        "link_types",
        "issue_types",
        "work_packages"
    ],
    no_backup=True,
    force=True
)

# Print the result
print('\nMigration completed with the following components:')
for component, result_data in result["components"].items():
    status = result_data.get("status", "unknown")
    message = result_data.get("message", "")
    print(f"  {component}: {status} {message}")

print('\nOverall:', result["overall"]["status"])
