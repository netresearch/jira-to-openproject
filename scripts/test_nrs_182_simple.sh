#!/bin/bash
# Simple test script for Bug #32 comprehensive fix validation on NRS-182

echo "================================================================================"
echo "Testing Bug #32 Comprehensive Fix on NRS-182"
echo "Start Time: $(date -Iseconds)"
echo "================================================================================"
echo ""
echo "🧪 Target Issue: NRS-182"
echo "Previous Result: 22/23 journals with NO AUDIT TRAIL"
echo "Fix Applied: Comprehensive refactoring addressing all 11 code review findings"
echo "Expected Result: 23/23 journals with COMPLETE VISIBLE AUDIT TRAIL"
echo ""

# Delete existing NRS-182 work package if it exists
echo "🗑️  Deleting existing NRS-182 work package (if exists)..."
python3 -c "
import sys
import json
import os
sys.path.insert(0, '/home/sme/p/j2o')
from src.clients.openproject_client import OpenProjectClient

try:
    op = OpenProjectClient()
    mapping_file = '/home/sme/p/j2o/var/data/work_package_mapping.json'

    if os.path.exists(mapping_file):
        with open(mapping_file, 'r') as f:
            wp_mapping = json.load(f)

        if 'NRS-182' in wp_mapping:
            wp_id = wp_mapping['NRS-182']
            print(f'   Found existing work package: {wp_id}')
            try:
                op.delete_work_package(wp_id)
                print(f'✅ Successfully deleted work package {wp_id}')
            except Exception as e:
                print(f'⚠️  Warning: Could not delete: {e}')

            # Remove from mapping
            del wp_mapping['NRS-182']
            with open(mapping_file, 'w') as f:
                json.dump(wp_mapping, f, indent=2)
            print('✅ Mapping updated')
        else:
            print('ℹ️  No existing work package found for NRS-182')
    else:
        print('ℹ️  Mapping file does not exist yet')
except Exception as e:
    print(f'⚠️  Warning: Deletion script failed: {e}')
"

echo ""
echo "🔄 Running migration for NRS-182..."
echo ""

# Run migration with verbose logging for NRS-182 only
python3 /home/sme/p/j2o/src/main.py \
    --project NRS \
    --work-packages \
    --jql "key = NRS-182" \
    --verbose \
    2>&1 | tee /tmp/bug32_comprehensive_fix_migration.log

echo ""
echo "================================================================================"
echo "VALIDATION:"
echo "================================================================================"
echo ""

# Verify journal count
python3 -c "
import sys
import json
sys.path.insert(0, '/home/sme/p/j2o')
from src.clients.openproject_client import OpenProjectClient

try:
    op = OpenProjectClient()
    mapping_file = '/home/sme/p/j2o/var/data/work_package_mapping.json'

    with open(mapping_file, 'r') as f:
        wp_mapping = json.load(f)

    if 'NRS-182' not in wp_mapping:
        print('❌ ERROR: NRS-182 not found in mapping after migration')
        sys.exit(1)

    wp_id = wp_mapping['NRS-182']
    print(f'✅ Work package created: {wp_id}')

    # Get journal count
    journals = op.get_work_package_journals(wp_id)
    journal_count = len(journals)

    print(f'')
    print(f'Test 1: Journal Count')
    print(f'   Expected: 23/23 journals')
    print(f'   Actual: {journal_count}/23 journals')

    if journal_count == 23:
        print(f'   Status: ✅ PASSED')
        print(f'')
        print(f'✅ SUCCESS: All 23 journals created!')
        print(f'✅ Bug #1 Fixed: Unified timestamp tracking working')
        print(f'')
        print(f'📋 MANUAL VERIFICATION REQUIRED:')
        print(f'   1. Open OpenProject UI: http://localhost:3000/work_packages/{wp_id}')
        print(f'   2. Click \"Activity\" tab')
        print(f'   3. Verify field changes are visible (Status, Assignee, Priority, etc.)')
        print(f'   4. Verify all 23 activities/journals are shown')
        print(f'')
        print(f'   This will confirm Bug #2 fix (audit trail restoration)')
        sys.exit(0)
    else:
        print(f'   Status: ❌ FAILED')
        print(f'   Missing: {23 - journal_count} journals')
        print(f'')
        print(f'❌ Test failed - check migration logs for details')
        sys.exit(1)

except Exception as e:
    print(f'')
    print(f'❌ ERROR: {e}')
    import traceback
    traceback.print_exc()
    sys.exit(1)
"

exit_code=$?
echo ""
echo "================================================================================"
echo "Test completed at: $(date -Iseconds)"
echo "================================================================================"

exit $exit_code
