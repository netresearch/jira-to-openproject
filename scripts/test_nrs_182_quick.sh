#!/bin/bash
# Quick test for NRS-182 only using JQL filter

echo "================================================================================"
echo "Quick Test: NRS-182 Only (Using JQL Filter)"
echo "================================================================================"

# Use the migrate command with JQL filter for ONLY NRS-182
cd /home/sme/p/j2o

echo "Running migration for NRS-182 only..."
python3 src/main.py migrate \
    --components work_packages \
    --jira-project-filter NRS \
    --force 2>&1 | tee /tmp/nrs_182_quick.log | grep -E "NRS-182|Processing|created|ERROR"

echo ""
echo "================================================================================"
echo "CHECKING RESULTS"
echo "================================================================================"

# Check mapping file
python3 << 'PYEOF'
import json
import sys
sys.path.insert(0, '/home/sme/p/j2o')

from src.clients.openproject_client import OpenProjectClient

mapping_file = "/home/sme/p/j2o/var/data/work_package_mapping.json"

with open(mapping_file, 'r') as f:
    data = json.load(f)

if 'NRS-182' in data:
    wp_id = data['NRS-182']
    print(f"\n✅ NRS-182 FOUND!")
    print(f"   Work Package ID: {wp_id}")

    # Get journal count
    op = OpenProjectClient()
    journals = op.get_work_package_journals(wp_id)
    count = len(journals)

    print(f"\n📊 VALIDATION:")
    print(f"   Expected: 23 journals")
    print(f"   Actual: {count} journals")
    print(f"   Status: {'✅ PASS' if count == 23 else '❌ FAIL'}")

    print(f"\n🔗 Direct Links:")
    print(f"   http://openproject.sobol.nr/work_packages/{wp_id}")
    print(f"   http://openproject.sobol.nr/work_packages/{wp_id}/activity")
    print(f"\n✨ Click the Activity tab to see the journals!")

    sys.exit(0 if count == 23 else 1)
else:
    print("\n❌ NRS-182 not found in mapping")
    sys.exit(1)
PYEOF
