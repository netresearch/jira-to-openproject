#!/usr/bin/env bash
#
# Test migration for 10 SPECIFIC NRS issues including known Bug #10 failures
# Uses J2O_TEST_ISSUES environment variable for precise issue targeting
#

set -e

echo "========================================================================"
echo "NRS MIGRATION TEST - 10 Specific Issues"
echo "Start: $(date --iso-8601=seconds)"
echo "========================================================================"
echo ""
echo "Test Issues (including known Bug #10 failures):"
echo "  1. NRS-171 (from successful tests)"
echo "  2. NRS-182 (from successful tests)"
echo "  3. NRS-191 (from successful tests)"
echo "  4. NRS-198 (from successful tests)"
echo "  5. NRS-204 (from successful tests)"
echo "  6. NRS-42  (Bug #10: due_date 3 days before start_date)"
echo "  7. NRS-59  (Bug #10: due_date 1 day before start_date)"
echo "  8. NRS-66  (Bug #10: due_date 9 days before start_date)"
echo "  9. NRS-982 (Bug #10: due_date 10 days before start_date)"
echo " 10. NRS-4003 (Bug #10: due_date 2 YEARS before start_date)"
echo ""

# STEP 1: Delete existing test work packages for clean test
echo "========================================================================"
echo "STEP 1: Cleaning up existing test work packages..."
echo "========================================================================"

cat <<'RUBY' | ssh sobol.nr 'docker exec -i openproject-web-1 bundle exec rails runner -'
cf = CustomField.find_by(type: 'WorkPackageCustomField', name: 'J2O Origin Key')
test_keys = %w[NRS-171 NRS-182 NRS-191 NRS-198 NRS-204 NRS-42 NRS-59 NRS-66 NRS-982 NRS-4003]
deleted_count = 0

puts "Deleting existing test work packages..."
test_keys.each do |key|
  wps = WorkPackage.joins(:custom_values)
                   .where(project_id: 303319)
                   .where(custom_values: {custom_field_id: cf.id, value: key})

  wps.each do |wp|
    wp.destroy
    deleted_count += 1
    puts "  Deleted WP #{wp.id} (#{key})"
  end
end

puts "\nTotal deleted: #{deleted_count}"
RUBY

echo ""

# STEP 2: Run migration with J2O_TEST_ISSUES environment variable
echo "========================================================================"
echo "STEP 2: RUNNING MIGRATION ON 10 ISSUES"
echo "========================================================================"
echo "Using J2O_TEST_ISSUES environment variable for precise targeting"
echo "Extended timeout: 600 seconds"
echo ""

# Set environment variables for test
export J2O_QUERY_TIMEOUT=600
export J2O_TEST_ISSUES="NRS-171,NRS-182,NRS-191,NRS-198,NRS-204,NRS-42,NRS-59,NRS-66,NRS-982,NRS-4003"

python -m src.main migrate \
  --components work_packages \
  --jira-project-filter NRS \
  --no-backup \
  --force \
  --no-confirm

MIGRATION_RESULT=$?

echo ""
echo "========================================================================"
echo "Migration completed with exit code: $MIGRATION_RESULT"
echo "========================================================================"

# STEP 3: Verify results
echo ""
echo "========================================================================"
echo "STEP 3: VERIFICATION"
echo "========================================================================"

cat <<'RUBY' | ssh sobol.nr 'docker exec -i openproject-web-1 bundle exec rails runner -'
cf = CustomField.find_by(type: 'WorkPackageCustomField', name: 'J2O Origin Key')
test_keys = %w[NRS-171 NRS-182 NRS-191 NRS-198 NRS-204 NRS-42 NRS-59 NRS-66 NRS-982 NRS-4003]

puts "Work Package Verification:"
puts "-" * 80

success_count = 0
failed_count = 0

test_keys.each do |key|
  wps = WorkPackage.joins(:custom_values)
                   .where(project_id: 303319)
                   .where(custom_values: {custom_field_id: cf.id, value: key})

  if wps.count > 0
    wp = wps.first
    journal_count = Journal.where(journable_id: wp.id, journable_type: 'WorkPackage').count
    comment_count = Journal.where(journable_id: wp.id, journable_type: 'WorkPackage')
                           .where.not(notes: [nil, '']).count

    success_count += 1
    puts "✓ #{key}: WP #{wp.id} | Journals: #{journal_count} | Comments: #{comment_count}"
    puts "  Dates: start=#{wp.start_date} due=#{wp.due_date}"
  else
    failed_count += 1
    puts "❌ #{key}: NOT FOUND"
  end
end

puts "-" * 80
puts "SUMMARY: #{success_count}/#{test_keys.length} successful"
puts "SUCCESS RATE: #{(success_count.to_f / test_keys.length * 100).round(1)}%"

if success_count == test_keys.length
  puts "\n✅ TEST PASSED - All issues migrated successfully!"
else
  puts "\n❌ TEST FAILED - #{failed_count} issues missing"
end
RUBY

# STEP 4: Check for errors in logs
echo ""
echo "========================================================================"
echo "Recent log errors (if any):"
echo "========================================================================"
tail -100 var/logs/migration_$(date +%Y-%m-%d)*.log 2>/dev/null | grep -E "ERROR|Exception|PG::" | tail -20 || echo "No errors found"

echo ""
echo "========================================================================"
echo "Test complete!"
echo "End: $(date --iso-8601=seconds)"
echo "========================================================================"

if [ $MIGRATION_RESULT -eq 0 ]; then
    echo "✅ Migration process succeeded"
else
    echo "❌ Migration process failed with exit code $MIGRATION_RESULT"
fi

exit $MIGRATION_RESULT
