#!/usr/bin/env python3
"""Direct migration script that bypasses change detection framework."""

import os
import sys

# Disable checkpoints BEFORE importing anything
os.environ["J2O_FAST_FORWARD"] = "0"

sys.path.insert(0, "/home/sme/p/j2o")

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.migrations.work_package_migration import WorkPackageMigration

# Initialize clients (they read from config automatically)
print("Initializing API clients...")
jira_client = JiraClient()
op_client = OpenProjectClient()

# Initialize migration
print("Initializing migration...")
migration = WorkPackageMigration(jira_client, op_client)

# Call _migrate_work_packages() directly, bypassing change detection
print("Starting direct migration (bypassing change detection)...")
result = migration._migrate_work_packages()

print(f"\nMigration complete!")
print(f"Results: {result}")
