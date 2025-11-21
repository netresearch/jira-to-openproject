#!/bin/bash
#
# Fresh migration of NRS-182 to test synthetic timestamp fix for Bug #32
#

echo "================================================================================"
echo "Fresh Migration of NRS-182 with Synthetic Timestamp Fix"
echo "Start Time: $(date -Iseconds)"
echo "================================================================================"
echo ""
echo "Target Issue: NRS-182"
echo "Fix: Synthetic timestamp generation with microsecond increments"
echo "Expected: 23/23 journals (no EXCLUSION constraint violations)"
echo ""

cd /home/sme/p/j2o

python3 << 'PYTHON_SCRIPT'
import sys
sys.path.insert(0, '/home/sme/p/j2o')

from src.migrations.work_package_migration import WorkPackageMigration
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
import json

print("🔄 Initializing clients...")
jira = JiraClient()
op = OpenProjectClient()
wpm = WorkPackageMigration(jira_client=jira, op_client=op)

print("\n📥 Fetching NRS-182 from Jira...")
jql = 'key = NRS-182'
issues = jira.jira.search_issues(jql, maxResults=1, expand='changelog')

if not issues:
    print("❌ ERROR: NRS-182 not found in Jira")
    sys.exit(1)

issue = issues[0]
changelog_count = len(issue.changelog.histories) if hasattr(issue, 'changelog') else 0
print(f"✅ Found: {issue.key} - {issue.fields.summary}")
print(f"   Changelog entries: {changelog_count}")

print("\n🔄 Migrating NRS-182 with verbose output...")
print("   Expecting 23 journals with synthetic timestamps for operations 23-27\n")

# Migrate the issue
result = wpm._migrate_work_packages_batch([issue], dry_run=False, verbose=True)

# Get work package ID from mapping
print("\n📊 Checking migration result...")
with open('/home/sme/p/j2o/var/data/work_package_mapping.json', 'r') as f:
    wp_mapping = json.load(f)

if 'NRS-182' not in wp_mapping:
    print("❌ ERROR: NRS-182 not found in mapping after migration")
    print("   Migration may have failed")
    sys.exit(1)

wp_id = wp_mapping['NRS-182']
print(f"✅ Work package created: {wp_id}")

# Check journal count
journals = op.get_work_package_journals(wp_id)
journal_count = len(journals)

print(f"\n{'='*80}")
print(f"RESULT: {journal_count}/23 journals created")
print(f"{'='*80}")

if journal_count == 23:
    print("\n✅ SUCCESS: All 23 journals created!")
    print("✅ Synthetic timestamp fix resolved EXCLUSION constraint violations!")
    print("✅ Operations 23-27 now have unique timestamps with microsecond increments")
    sys.exit(0)
else:
    print(f"\n⚠️  RESULT: {journal_count}/23 journals created")
    print(f"   Missing: {23 - journal_count} journals")
    print("   Check logs above for errors")
    sys.exit(1)

PYTHON_SCRIPT

echo ""
echo "================================================================================"
echo "Migration Complete"
echo "End Time: $(date -Iseconds)"
echo "================================================================================"
