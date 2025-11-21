#!/bin/bash
# Final test for NRS-182 ONLY using environment variable filtering
# This is the CORRECT approach: use J2O_TEST_ISSUES to filter

cd /home/sme/p/j2o

echo "================================================================================"
echo "NRS-182 ONLY - Bug #32 Regression Fix Validation"
echo "================================================================================"
echo ""
echo "🧪 Test: Migrate ONLY NRS-182"
echo "📝 Expected: 23/23 journals with complete audit trail"
echo "🔧 Fix: Comprehensive refactoring + 2 regression bug fixes"
echo ""

# Delete existing work package mapping for NRS-182 to force re-migration
echo "🗑️  Cleaning NRS-182 from mapping..."
python3 << 'PYEOF'
import json
import sys
sys.path.insert(0, '/home/sme/p/j2o')

mapping_file = "/home/sme/p/j2o/var/data/work_package_mapping.json"

try:
    with open(mapping_file, 'r') as f:
        data = json.load(f)

    if 'NRS-182' in data:
        wp_id = data['NRS-182']
        print(f"   Found NRS-182 → WP #{wp_id}")

        # Delete from OpenProject
        from src.clients.openproject_client import OpenProjectClient
        op = OpenProjectClient()
        try:
            op.delete_work_package(wp_id)
            print(f"   ✅ Deleted WP #{wp_id} from OpenProject")
        except Exception as e:
            print(f"   ⚠️  Could not delete WP #{wp_id}: {e}")

        # Remove from mapping
        del data['NRS-182']
        with open(mapping_file, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"   ✅ Removed NRS-182 from mapping")
    else:
        print("   ℹ️  NRS-182 not in mapping (clean slate)")
except FileNotFoundError:
    print("   ℹ️  Mapping file not found (clean slate)")
except Exception as e:
    print(f"   ⚠️  Error during cleanup: {e}")
PYEOF

echo ""
echo "🔄 Migrating NRS-182..."
echo ""

# Run migration with J2O_TEST_ISSUES environment variable
export J2O_TEST_ISSUES="NRS-182"
python3 src/main.py migrate \
    --components work_packages \
    --jira-project-filter NRS \
    --force \
    --no-confirm 2>&1 | tee /tmp/nrs_182_final.log

echo ""
echo "================================================================================"
echo "VALIDATION"
echo "================================================================================"
echo ""

# Check results
python3 << 'PYEOF'
import json
import sys
sys.path.insert(0, '/home/sme/p/j2o')

from src.clients.openproject_client import OpenProjectClient

mapping_file = "/home/sme/p/j2o/var/data/work_package_mapping.json"

try:
    with open(mapping_file, 'r') as f:
        data = json.load(f)

    if 'NRS-182' in data:
        wp_id = data['NRS-182']
        print(f"✅ NRS-182 FOUND in mapping")
        print(f"   Work Package ID: {wp_id}")

        # Get journal count
        op = OpenProjectClient()
        journals = op.get_work_package_journals(wp_id)
        count = len(journals)

        print(f"\n📊 JOURNAL COUNT:")
        print(f"   Expected: 23")
        print(f"   Actual:   {count}")

        if count == 23:
            print(f"\n✅ SUCCESS - ALL 23 JOURNALS CREATED!")
        else:
            print(f"\n❌ FAIL - Got {count}/23 journals")
            sys.exit(1)

        print(f"\n🔗 DIRECT LINKS:")
        print(f"   Overview: http://openproject.sobol.nr/work_packages/{wp_id}")
        print(f"   Activity: http://openproject.sobol.nr/work_packages/{wp_id}/activity")
        print(f"\n✨ Click the Activity tab to verify complete audit trail!")

    else:
        print("❌ FAIL - NRS-182 not found in mapping after migration")
        sys.exit(1)

except FileNotFoundError:
    print("❌ FAIL - Mapping file not found")
    sys.exit(1)
except Exception as e:
    print(f"❌ ERROR: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
PYEOF

echo ""
echo "================================================================================"
echo "Test completed"
echo "================================================================================"
