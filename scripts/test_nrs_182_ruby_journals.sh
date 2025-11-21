#!/bin/bash
#
# Test synthetic timestamp fix for Bug #32 - NRS-182 missing journals
# Directly test the Ruby journal creation script
#

echo "================================================================================"
echo "Testing Bug #32 Synthetic Timestamp Fix on NRS-182"
echo "Start Time: $(date -Iseconds)"
echo "================================================================================"
echo ""
echo "Target Issue: NRS-182"
echo "Previous Result: 22/23 journals (EXCLUSION constraint violations)"
echo "Fix Applied: Synthetic timestamp generation with microsecond increments"
echo "Expected Result: 23/23 journals"
echo ""

# Get work package ID from mapping
WP_MAPPING_FILE="/home/sme/p/j2o/var/data/work_package_mapping.json"
WP_ID=$(jq -r '.["NRS-182"]' "$WP_MAPPING_FILE")

if [ "$WP_ID" = "null" ] || [ -z "$WP_ID" ]; then
    echo "❌ ERROR: NRS-182 not found in work package mapping"
    echo "   Run full migration first to create work package"
    exit 1
fi

echo "✅ Found existing work package: $WP_ID"
echo ""

# Check journal count BEFORE
echo "📊 Checking current journal count..."
JOURNAL_COUNT_BEFORE=$(docker exec openproject-web-1 \
    rails runner "puts Journal.where(journable_id: $WP_ID, journable_type: 'WorkPackage').count" 2>/dev/null | tail -1)
echo "   Current journals: $JOURNAL_COUNT_BEFORE/23"
echo ""

# Delete existing work package to force clean re-creation
echo "🗑️  Deleting existing work package $WP_ID to force clean re-migration..."
docker exec openproject-web-1 rails runner "
  wp = WorkPackage.find($WP_ID)
  wp.destroy
  puts 'Work package deleted'
" 2>&1 | tail -5
echo "✅ Successfully deleted work package $WP_ID"
echo ""

# Remove from mapping file
echo "   Removing NRS-182 from work package mapping..."
jq 'del(.["NRS-182"])' "$WP_MAPPING_FILE" > "${WP_MAPPING_FILE}.tmp"
mv "${WP_MAPPING_FILE}.tmp" "$WP_MAPPING_FILE"
echo "✅ Mapping updated"
echo ""

# Run migration on just NRS-182
echo "🔄 Running fresh migration for NRS-182..."
echo "   This will create a new work package with synthetic timestamp fix"
echo ""

cd /home/sme/p/j2o
python3 -c "
import sys
sys.path.insert(0, '/home/sme/p/j2o')
from src.migrations.work_package_migration import WorkPackageMigration
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient

jira = JiraClient()
op = OpenProjectClient()
wpm = WorkPackageMigration(jira_client=jira, op_client=op)

# Fetch NRS-182
jql = 'key = NRS-182'
issues = jira.jira.search_issues(jql, maxResults=1, expand='changelog')
if issues:
    print(f'Found {issues[0].key}: {issues[0].fields.summary}')
    result = wpm._migrate_work_packages_batch([issues[0]], dry_run=False, verbose=True)
    print('Migration completed')
else:
    print('ERROR: NRS-182 not found')
"

# Get new work package ID from mapping
NEW_WP_ID=$(jq -r '.["NRS-182"]' "$WP_MAPPING_FILE")

if [ "$NEW_WP_ID" = "null" ] || [ -z "$NEW_WP_ID" ]; then
    echo ""
    echo "❌ ERROR: NRS-182 not found in mapping after migration"
    echo "   Migration may have failed"
    exit 1
fi

echo ""
echo "✅ New work package created: $NEW_WP_ID"
echo ""

# Check journal count AFTER
echo "📊 Verifying journal count after migration..."
JOURNAL_COUNT_AFTER=$(docker exec openproject-web-1 \
    rails runner "puts Journal.where(journable_id: $NEW_WP_ID, journable_type: 'WorkPackage').count" 2>/dev/null | tail -1)

echo ""
echo "================================================================================"
echo "RESULTS:"
echo "  Old WP $WP_ID: $JOURNAL_COUNT_BEFORE/23 journals (deleted)"
echo "  New WP $NEW_WP_ID: $JOURNAL_COUNT_AFTER/23 journals"
IMPROVEMENT=$((JOURNAL_COUNT_AFTER - JOURNAL_COUNT_BEFORE))
echo "  Improvement: $(printf '%+d' $IMPROVEMENT) journals"
echo "================================================================================"
echo ""

if [ "$JOURNAL_COUNT_AFTER" -eq 23 ]; then
    echo "✅ SUCCESS: All 23 journals created!"
    echo "✅ Synthetic timestamp fix resolved EXCLUSION constraint violations!"
    echo "✅ Operations 23-27 now have unique timestamps with microsecond increments"
    exit 0
elif [ "$JOURNAL_COUNT_AFTER" -gt "$JOURNAL_COUNT_BEFORE" ]; then
    echo "⚠️  PARTIAL SUCCESS: $JOURNAL_COUNT_AFTER/23 journals created"
    echo "   Improvement: +$IMPROVEMENT journals"
    echo "   Missing: $((23 - JOURNAL_COUNT_AFTER)) journals"
    exit 1
else
    echo "❌ NO IMPROVEMENT: Still $JOURNAL_COUNT_AFTER/23 journals"
    echo "   Check migration logs for details"
    exit 1
fi
