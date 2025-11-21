#!/usr/bin/env bash
#
# Simple NRS migration test - first 10 issues
#

set -e

echo "========================================================================================================"
echo "NRS MIGRATION TEST - First 10 Issues"
echo "Start: $(date --iso-8601=seconds)"
echo "========================================================================================================"

# Set extended timeout
export J2O_QUERY_TIMEOUT=600

# Run migration
echo ""
echo "Running migration with --no-backup --force..."
echo ""

python -m src.main migrate \
  --components work_packages \
  --jira-project-filter NRS \
  --no-backup \
  --force \
  --no-confirm

RESULT=$?

echo ""
echo "========================================================================================================"
echo "Migration completed with exit code: $RESULT"
echo "End: $(date --iso-8601=seconds)"
echo "========================================================================================================"

# Show recent logs
echo ""
echo "Recent log errors (if any):"
tail -100 var/logs/migration_$(date +%Y-%m-%d)*.log | grep -E "ERROR|Exception|PG::" | tail -20 || echo "No errors found"

# Check work packages created
echo ""
echo "Checking created work packages..."

cat <<'RUBY' | ssh sobol.nr 'docker exec -i openproject-web-1 bundle exec rails runner -'
cf = CustomField.find_by(type: 'WorkPackageCustomField', name: 'J2O Origin Key')
wps = WorkPackage.joins(:custom_values)
                 .where(project_id: 303319)
                 .where("custom_values.custom_field_id = ?", cf.id)
                 .order("custom_values.value")
                 .limit(15)

puts "\nRecent NRS Work Packages:"
puts "-" * 80

wps.each do |wp|
  jira_key = wp.custom_value_for(cf)&.value || "UNKNOWN"
  journal_count = Journal.where(journable_id: wp.id, journable_type: 'WorkPackage').count
  comment_count = Journal.where(journable_id: wp.id, journable_type: 'WorkPackage')
                         .where.not(notes: [nil, '']).count

  puts "#{jira_key}: WP #{wp.id} | Journals: #{journal_count} | Comments: #{comment_count}"
  puts "  Dates: start=#{wp.start_date} due=#{wp.due_date}"
  puts "  Subject: #{wp.subject[0..60]}"
end

puts "-" * 80
puts "Total NRS WPs: #{wps.count}"
RUBY

echo ""
echo "Test complete!"
exit $RESULT
