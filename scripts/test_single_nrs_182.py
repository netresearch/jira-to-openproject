#!/usr/bin/env python3
"""Simplest possible test: migrate ONLY NRS-182"""
import sys
sys.path.insert(0, '/home/sme/p/j2o')

from src.migrations.work_package_migration import WorkPackageMigration
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient

print("=" * 80)
print("SIMPLE TEST: NRS-182 Only")
print("=" * 80)

# Initialize
jira = JiraClient()
op = OpenProjectClient()
wpm = WorkPackageMigration(jira_client=jira, op_client=op)

# Fetch ONLY NRS-182
print("\nFetching NRS-182...")
issues = jira.jira.search_issues("key = NRS-182", maxResults=1, expand="changelog")
print(f"✅ Found: {issues[0].key}\n")

# Migrate it
print("Migrating NRS-182...")
wpm._migrate_issues_batch(issues, batch_size=1)

# Check result
import json
with open("/home/sme/p/j2o/var/data/work_package_mapping.json", 'r') as f:
    mapping = json.load(f)

if 'NRS-182' in mapping:
    wp_id = mapping['NRS-182']
    journals = op.get_work_package_journals(wp_id)
    count = len(journals)

    print(f"\n✅ MIGRATED: WP #{wp_id}")
    print(f"📊 Journals: {count}/23")
    print(f"{'✅ PASS' if count == 23 else '❌ FAIL'}")
    print(f"\n🔗 http://openproject.sobol.nr/work_packages/{wp_id}/activity")
else:
    print("\n❌ Migration failed")
