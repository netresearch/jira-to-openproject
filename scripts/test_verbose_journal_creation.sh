#!/usr/bin/env bash
# Quick test with ONE issue and verbose Ruby logging to debug journal creation

set -e

echo "Testing journal creation with verbose logging for NRS-182..."

# Delete existing
cat <<'RUBY' | ssh sobol.nr 'docker exec -i openproject-web-1 bundle exec rails runner -'
cf = CustomField.find_by(type: 'WorkPackageCustomField', name: 'J2O Origin Key')
wps = WorkPackage.joins(:custom_values).where(project_id: 303319).where(custom_values: {custom_field_id: cf.id, value: 'NRS-182'})
wps.each { |wp| wp.destroy }
puts "Cleaned up existing NRS-182"
RUBY

# Run migration with VERBOSE logging enabled
export J2O_BULK_RUBY_VERBOSE=1
export J2O_TEST_ISSUES="NRS-182"
export J2O_QUERY_TIMEOUT=600

echo "Running migration with verbose logging..."
python -m src.main migrate --components work_packages --jira-project-filter NRS --no-backup --force --no-confirm 2>&1 | tee /tmp/verbose_journal_test.log | tail -200

# Check result
cat <<'RUBY' | ssh sobol.nr 'docker exec -i openproject-web-1 bundle exec rails runner -'
cf = CustomField.find_by(type: 'WorkPackageCustomField', name: 'J2O Origin Key')
wp = WorkPackage.joins(:custom_values).where(project_id: 303319).where(custom_values: {custom_field_id: cf.id, value: 'NRS-182'}).first
if wp
  journals = Journal.where(journable_id: wp.id, journable_type: 'WorkPackage')
  puts "WP #{wp.id}: #{journals.count} journals total"
  journals.order(:version).each { |j| puts "  v#{j.version}: notes_length=#{j.notes&.length || 0}" }
else
  puts "NRS-182 not found!"
end
RUBY
