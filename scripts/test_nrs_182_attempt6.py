#!/usr/bin/env python3
"""Test migration of NRS-182 with Bug #23 fix (Ruby output enabled)"""

import os
import sys

# Disable checkpoints
os.environ["J2O_FAST_FORWARD"] = "0"

sys.path.insert(0, "/home/sme/p/j2o")

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.migrations.work_package_migration import WorkPackageMigration

# Initialize clients
print("Initializing API clients...")
jira_client = JiraClient()
op_client = OpenProjectClient()

print("Initializing migration...")
migration = WorkPackageMigration({})

# Test with NRS-182 (expects 23 journals: 1 creation + 22 from journal entries)
test_issues = ["NRS-182"]
print(f"Testing specific issues: {test_issues}")

print("Starting direct migration (bypassing change detection)...")
result = migration.migrate_work_packages(
    specific_issues=test_issues,
    bypass_change_detection=True
)

print("\n" + "="*80)
print("Migration complete!")
print(f"Results: {result}")
print("="*80)
