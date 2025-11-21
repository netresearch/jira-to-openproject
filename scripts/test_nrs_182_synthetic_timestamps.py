#!/usr/bin/env python3
"""
Test synthetic timestamp fix for Bug #32 - NRS-182 missing journals
Verify that operations 23-27 no longer fail with EXCLUSION constraint violations
"""

import sys
import os
import json
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.migrations.work_package_migration import WorkPackageMigration
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.config import logger

def test_nrs_182_synthetic_timestamps():
    """Test NRS-182 migration with synthetic timestamp fix"""
    print("=" * 80)
    print("Testing Bug #32 Synthetic Timestamp Fix on NRS-182")
    print(f"Start Time: {datetime.now().isoformat()}")
    print("=" * 80)

    issue_key = "NRS-182"
    print(f"\n🧪 Target Issue: {issue_key}")
    print(f"Previous Result: 22/23 journals (EXCLUSION constraint violations)")
    print(f"Fix Applied: Synthetic timestamp generation with microsecond increments")
    print(f"Expected Result: 23/23 journals")
    print()

    try:
        # Initialize clients
        logger.info("Initializing Jira and OpenProject clients...")
        jira = JiraClient()
        op = OpenProjectClient()
        wpm = WorkPackageMigration(jira_client=jira, op_client=op)

        # Fetch NRS-182 from Jira
        print(f"📥 Fetching {issue_key} from Jira...")
        jql = f"key = {issue_key}"
        issues = jira.search_issues(jql, maxResults=1, expand="changelog")

        if not issues or len(issues) == 0:
            print(f"❌ ERROR: {issue_key} not found in Jira")
            return False

        issue = issues[0]
        changelog_count = len(getattr(issue, 'changelog', {}).get('histories', []))
        print(f"✅ Found {issue_key}: {issue.fields.summary}")
        print(f"   Changelog entries: {changelog_count}")

        # Get work package mapping
        print(f"\n📊 Checking existing work package mapping...")
        mapping_file = "/home/sme/p/j2o/var/data/work_package_mapping.json"
        with open(mapping_file, 'r') as f:
            wp_mapping = json.load(f)

        if issue_key not in wp_mapping:
            print(f"❌ ERROR: {issue_key} not found in work package mapping")
            print("   Run full migration first to create work package")
            return False

        wp_id = wp_mapping[issue_key]
        print(f"✅ Found existing work package: {wp_id}")

        # Check current journal count BEFORE deletion
        print(f"\n📊 Checking current journal count...")
        journals_before = op.get_work_package_journals(wp_id)
        journal_count_before = len(journals_before)
        print(f"   Current journals: {journal_count_before}/23")

        # Delete existing work package to force clean re-migration
        print(f"\n🗑️  Deleting existing work package {wp_id} to force clean re-migration...")
        try:
            op.delete_work_package(wp_id)
            print(f"✅ Successfully deleted work package {wp_id}")
        except Exception as e:
            print(f"⚠️  Warning: Could not delete work package: {e}")

        # Remove from mapping file to allow re-creation
        print(f"   Removing {issue_key} from work package mapping...")
        del wp_mapping[issue_key]
        with open(mapping_file, 'w') as f:
            json.dump(wp_mapping, f, indent=2)
        print(f"✅ Mapping updated")

        # Run migration to create fresh work package with synthetic timestamps
        print(f"\n🔄 Running fresh migration for {issue_key} (verbose mode)...")
        print(f"   This will create a new work package with synthetic timestamp fix")
        print()

        # Process just this one issue
        result = wpm._migrate_work_packages_batch([issue], dry_run=False, verbose=True)

        # Get new work package ID from mapping
        print(f"\n📊 Verifying journal count after migration...")
        with open(mapping_file, 'r') as f:
            wp_mapping_new = json.load(f)

        if issue_key not in wp_mapping_new:
            print(f"❌ ERROR: {issue_key} not found in mapping after migration")
            print("   Migration may have failed")
            return False

        new_wp_id = wp_mapping_new[issue_key]
        print(f"✅ New work package created: {new_wp_id}")

        journals_after = op.get_work_package_journals(new_wp_id)
        journal_count_after = len(journals_after)

        print(f"\n{'='*80}")
        print(f"RESULTS:")
        print(f"  Old WP {wp_id}: {journal_count_before}/23 journals (deleted)")
        print(f"  New WP {new_wp_id}: {journal_count_after}/23 journals")
        print(f"  Improvement: {journal_count_after - journal_count_before:+d} journals")
        print(f"{'='*80}")

        if journal_count_after == 23:
            print("\n✅ SUCCESS: All 23 journals created!")
            print("✅ Synthetic timestamp fix resolved EXCLUSION constraint violations!")
            print("✅ Operations 23-27 now have unique timestamps with microsecond increments")
            return True
        elif journal_count_after > journal_count_before:
            print(f"\n⚠️  PARTIAL SUCCESS: {journal_count_after}/23 journals created")
            print(f"   Improvement: +{journal_count_after - journal_count_before} journals")
            print(f"   Missing: {23 - journal_count_after} journals")
            return False
        else:
            print(f"\n❌ NO IMPROVEMENT: Still {journal_count_after}/23 journals")
            print("   Check migration logs for details")
            return False

    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_nrs_182_synthetic_timestamps()
    sys.exit(0 if success else 1)
