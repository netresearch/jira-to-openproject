#!/usr/bin/env python3
"""Test script to validate comment migration with limited issues."""

import os
import sys

# Disable fast-forward to test all issues
os.environ["J2O_FAST_FORWARD"] = "0"

# Limit to 10 issues for testing
os.environ["J2O_TEST_LIMIT"] = "10"

sys.path.insert(0, "/home/sme/p/j2o")

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.migrations.work_package_migration import WorkPackageMigration

# Initialize clients
print("=" * 80)
print("COMMENT MIGRATION TEST - Limited to 10 issues")
print("=" * 80)
print("\nInitializing API clients...")
jira_client = JiraClient()
op_client = OpenProjectClient()

# Initialize migration
print("Initializing migration...")
migration = WorkPackageMigration(jira_client, op_client)

# Monkey-patch to limit issues for testing
original_iter = migration._iter_all_project_issues

def limited_iter(project_key: str):
    """Wrapper to limit to first 10 issues."""
    count = 0
    for issue in original_iter(project_key):
        if count >= 10:
            print(f"\n[TEST] Stopped after {count} issues (test limit reached)")
            break
        count += 1
        yield issue

migration._iter_all_project_issues = limited_iter

# Run migration
print("\n" + "=" * 80)
print("Starting test migration (first 10 issues only)...")
print("=" * 80 + "\n")

result = migration._migrate_work_packages()

print("\n" + "=" * 80)
print("TEST MIGRATION COMPLETE")
print("=" * 80)
print(f"\nResults: {result}")
print("\nNext steps:")
print("1. Check logs for 'Found X comment(s)' messages")
print("2. Inspect var/data/bulk_result_*.json for create_comment operations")
print("3. If successful, run full migration with ./scripts/migrate_no_ff.sh")
