#!/usr/bin/env python3
"""
Validate Bug #9 - Progressive State Fix
"""
import sys
sys.path.insert(0, '/home/sme/p/j2o')

from src.clients.rails_console_client import RailsConsoleClient
import yaml

# Load config
with open('/home/sme/p/j2o/config/config.yaml') as f:
    config = yaml.safe_load(f)

# Initialize Rails console client
rails_client = RailsConsoleClient(config['openproject'])

# Get journal data
wp_id = 5581100

print(f"=== Bug #9 Progressive State Fix Validation ===\n")
print(f"Work Package ID: {wp_id}\n")

# Get journal count
cmd = f"wp = WorkPackage.find({wp_id}); puts wp.journals.count"
count = int(rails_client.execute_command(cmd).strip())
print(f"Journal Count: {count}")

# Get specific versions to check progressive state
print(f"\n=== Progressive State Validation ===")
for version in [1, 5, 10, 15, 20, 27]:
    cmd = f"""
wp = WorkPackage.find({wp_id});
j = wp.journals.find_by(version: {version});
if j && j.data
  puts "#{j.data.status_id}|#{j.data.assigned_to_id}"
else
  puts "MISSING"
end
""".strip()
    result = rails_client.execute_command(cmd).strip()

    if result == "MISSING":
        print(f"v{version}: MISSING DATA")
    else:
        status_id, assigned_to_id = result.split("|")
        print(f"v{version}: status_id={status_id}, assigned_to_id={assigned_to_id}")

print(f"\n=== Validation Complete ===")
print(f"Expected: Different status_id values across versions (NOT all identical)")
print(f"Expected: NO zero values for status_id (indicates empty string coercion)")
print(f"\n🔗 View in browser: http://openproject.sobol.nr/work_packages/{wp_id}/activity")
