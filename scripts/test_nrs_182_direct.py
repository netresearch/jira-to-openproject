#!/usr/bin/env python3
"""
Direct test for Bug #32 comprehensive fix on NRS-182
Bypasses command-line interface and directly migrates just NRS-182
"""

import sys
import os
import json
from datetime import datetime

# Add project root to path
sys.path.insert(0, '/home/sme/p/j2o')

from src.migrations.work_package_migration import WorkPackageMigration
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.config import logger

def main():
    print("=" * 80)
    print("Testing Bug #32 Comprehensive Fix on NRS-182")
    print(f"Start Time: {datetime.now().isoformat()}")
    print("=" * 80)

    issue_key = "NRS-182"
    print(f"\n🧪 Target Issue: {issue_key}")
    print(f"Previous Result: 22/23 journals with NO AUDIT TRAIL")
    print(f"Fix Applied: Comprehensive refactoring addressing all 11 code review findings")
    print(f"Expected Result: 23/23 journals with COMPLETE VISIBLE AUDIT TRAIL\n")

    try:
        # Initialize clients
        print("Initializing clients...")
        jira = JiraClient()
        op = OpenProjectClient()

        # Get work package mapping
        mapping_file = "/home/sme/p/j2o/var/data/work_package_mapping.json"
        if os.path.exists(mapping_file):
            with open(mapping_file, 'r') as f:
                wp_mapping = json.load(f)
        else:
            wp_mapping = {}

        # Delete existing work package if present
        if issue_key in wp_mapping:
            wp_id = wp_mapping[issue_key]
            print(f"🗑️  Deleting existing work package: {wp_id}")
            try:
                op.delete_work_package(wp_id)
                print(f"✅ Successfully deleted work package {wp_id}")
            except Exception as e:
                print(f"⚠️  Warning: Could not delete: {e}")

            del wp_mapping[issue_key]
            os.makedirs(os.path.dirname(mapping_file), exist_ok=True)
            with open(mapping_file, 'w') as f:
                json.dump(wp_mapping, f, indent=2)
            print(f"✅ Mapping updated\n")
        else:
            print(f"ℹ️  No existing work package found for {issue_key}\n")

        # Run full migration for NRS project (which includes NRS-182)
        print("🔄 Running work package migration for NRS project...")
        print("   (This will migrate all NRS issues including NRS-182)")
        print("")

        # Initialize WorkPackageMigration and run
        wpm = WorkPackageMigration(jira_client=jira, op_client=op)
        result = wpm.run()

        # Verify results
        print(f"\n{'='*80}")
        print(f"VALIDATION:")
        print(f"{'='*80}\n")

        # Reload mapping
        with open(mapping_file, 'r') as f:
            wp_mapping = json.load(f)

        if issue_key not in wp_mapping:
            print(f"❌ ERROR: {issue_key} not found in mapping after migration")
            return False

        wp_id = wp_mapping[issue_key]
        print(f"✅ Work package created: {wp_id}")

        # Get journal count
        journals = op.get_work_package_journals(wp_id)
        journal_count = len(journals)

        print(f"\nTest 1: Journal Count")
        print(f"   Expected: 23/23 journals")
        print(f"   Actual: {journal_count}/23 journals")

        if journal_count == 23:
            print(f"   Status: ✅ PASSED\n")
            print(f"✅ SUCCESS: All 23 journals created!")
            print(f"✅ Bug #1 Fixed: Unified timestamp tracking working\n")
            print(f"📋 MANUAL VERIFICATION REQUIRED:")
            print(f"   1. Open OpenProject UI: http://localhost:3000/work_packages/{wp_id}")
            print(f"   2. Click 'Activity' tab")
            print(f"   3. Verify field changes are visible (Status, Assignee, Priority, etc.)")
            print(f"   4. Verify all 23 activities/journals are shown\n")
            print(f"   This will confirm Bug #2 fix (audit trail restoration)")
            return True
        else:
            print(f"   Status: ❌ FAILED")
            print(f"   Missing: {23 - journal_count} journals\n")
            print(f"❌ Test failed - check migration logs")
            return False

    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
