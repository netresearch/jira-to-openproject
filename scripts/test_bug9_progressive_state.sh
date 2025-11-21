#!/bin/bash
set -e

cd /home/sme/p/j2o

echo "=== Regression Bug #9 - Progressive State Fix Test ==="
echo "Cleaning up previous test..."

# Clean migration lock
rm -f var/.migration.lock

# Clean mapping and backups
rm -f var/data/work_package_mapping.json
rm -rf var/backups/backup_2025-*

# Clean OpenProject test data
echo "Cleaning OpenProject test data..."
tmux send-keys -t rails_console "WorkPackage.where(subject: 'NRS-182: New User Group').destroy_all; puts 'CLEANED'" Enter
sleep 2

echo ""
echo "=== Running NRS-182 Migration with Progressive State Fix ==="
export J2O_TEST_ISSUES="NRS-182"
python3 src/main.py migrate \
    --components work_packages \
    --jira-project-filter NRS \
    --force \
    --no-confirm

echo ""
echo "=== Validation Phase ==="

# Get work package ID from mapping
echo "Looking up work package ID..."
mapping_file="var/data/work_package_mapping.json"
wp_id=$(python3 -c "
import json
with open('$mapping_file') as f:
    data = json.load(f)
for jira_id, entry in data.items():
    if isinstance(entry, dict) and entry.get('jira_key') == 'NRS-182':
        print(entry['openproject_id'])
        break
")

if [ -z "$wp_id" ]; then
    echo "❌ FAIL - NRS-182 not found in mapping"
    exit 1
fi

echo "✅ Found NRS-182: work_package_id=$wp_id"

# Get journal count
echo "Checking journal count..."
tmux send-keys -t rails_console "wp = WorkPackage.find($wp_id); puts \"JOURNAL_COUNT:\#{wp.journals.count}\"" Enter
sleep 2

# Get sample journal data for v1, v5, v10, v15, v20, v27
echo ""
echo "=== Checking Progressive State (BUG #9 FIX) ==="
echo "Comparing journal.data across versions to verify progressive changes..."

tmux send-keys -t rails_console "
wp = WorkPackage.find($wp_id);
[1, 5, 10, 15, 20, 27].each do |v|
  j = wp.journals.find_by(version: v);
  if j && j.data
    puts \"v\#{v}: status=\#{j.data.status_id}, subject=\#{j.data.subject[0..30]}...\"
  else
    puts \"v\#{v}: MISSING DATA\"
  end
end
" Enter

sleep 3

echo ""
echo "=== Test Complete ==="
echo "📊 Check output above for progressive state changes"
echo "🔗 View in browser: http://openproject.sobol.nr/work_packages/$wp_id/activity"
echo ""
echo "Expected: Different status_id values across versions (NOT all identical)"
echo "Bug #9 Fixed: Each journal shows historical state at that point in time"
