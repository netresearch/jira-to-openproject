#!/usr/bin/env python3
"""
Validate NRS-182 journal migration using actual mapping structure (by Jira ID)
"""
import json
import sys

sys.path.insert(0, '/home/sme/p/j2o')

from src.clients.openproject_client import OpenProjectClient
import yaml

mapping_file = "/home/sme/p/j2o/var/data/work_package_mapping.json"

try:
    with open(mapping_file, 'r') as f:
        data = json.load(f)

    # Find NRS-182 by scanning all entries (mapping uses Jira ID as key)
    nrs_182_entry = None
    for jira_id, entry in data.items():
        if isinstance(entry, dict) and entry.get('jira_key') == 'NRS-182':
            nrs_182_entry = entry
            break

    if not nrs_182_entry:
        print("❌ FAIL - NRS-182 not found in mapping after migration")
        sys.exit(1)

    wp_id = nrs_182_entry['openproject_id']
    print(f"✅ NRS-182 FOUND in mapping")
    print(f"   Jira ID:         {nrs_182_entry['jira_id']}")
    print(f"   Work Package ID: {wp_id}")

    # Get journal count using OpenProject API
    with open('config/config.yaml') as f:
        config = yaml.safe_load(f)

    # Initialize OpenProject client using the actual server config
    from src.clients.rails_console_client import RailsConsoleClient
    rails_client = RailsConsoleClient(config['openproject'])
    
    # Use Rails to get journal count directly
    cmd = f"wp = WorkPackage.find({wp_id}); puts wp.journals.count"
    result = rails_client.execute_command(cmd)
    count = int(result.strip())



    print(f"\n📊 JOURNAL COUNT:")
    print(f"   Expected: 23")
    print(f"   Actual:   {count}")

    if count >= 23:
        print(f"\n✅ SUCCESS - {count} JOURNALS CREATED (target: 23)!")
    else:
        print(f"\n❌ FAIL - Got {count}/23 journals")
        sys.exit(1)

    print(f"\n🔗 DIRECT LINKS:")
    print(f"   Overview: http://openproject.sobol.nr/work_packages/{wp_id}")
    print(f"   Activity: http://openproject.sobol.nr/work_packages/{wp_id}/activity")
    print(f"\n✨ Click the Activity tab to verify complete audit trail!")

except FileNotFoundError:
    print("❌ FAIL - Mapping file not found")
    sys.exit(1)
except Exception as e:
    print(f"❌ ERROR: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
