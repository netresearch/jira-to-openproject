#!/usr/bin/env python3
"""
Simple test for NRS migration with 10 specific issues.
Tests all bug fixes with comprehensive validation.
"""

import sys
import time
import subprocess
import tempfile
import os
from datetime import datetime

sys.path.insert(0, "/home/sme/p/j2o")

from src.migrations.work_package_migration import WorkPackageMigration
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.config import logger

# Test issues - including known problematic ones from Bug #10
TEST_ISSUES = [
    "NRS-171", "NRS-182", "NRS-191", "NRS-198", "NRS-204",
    "NRS-42", "NRS-59", "NRS-66", "NRS-982", "NRS-4003"
]

print("=" * 80)
print("NRS MIGRATION TEST - 10 Issues")
print(f"Start: {datetime.now().isoformat()}")
print("=" * 80)
print("\nTest Issues:")
for i, key in enumerate(TEST_ISSUES, 1):
    print(f"  {i:2d}. {key}")
print()

# Initialize clients
logger.info("Initializing clients...")
jira = JiraClient()
op = OpenProjectClient()
wpm = WorkPackageMigration(jira_client=jira, op_client=op)

# Step 1: DELETE existing test work packages for clean test
print("\nStep 1: Cleaning up existing test work packages...")
test_keys_str = ' '.join(TEST_ISSUES)
ruby_cleanup = f"""
cf = CustomField.find_by(type: 'WorkPackageCustomField', name: 'J2O Origin Key')
test_keys = %w[{test_keys_str}]
deleted_count = 0

test_keys.each do |key|
  wps = WorkPackage.joins(:custom_values)
                   .where(project_id: 303319)
                   .where(custom_values: {{{{custom_field_id: cf.id, value: key}}}})

  wps.each do |wp|
    wp.destroy
    deleted_count += 1
    puts "Deleted WP #{{wp.id}} (#{{key}})"
  end
end

puts "Total deleted: #{{deleted_count}}"
"""

with tempfile.NamedTemporaryFile(mode='w', suffix='.rb', delete=False) as f:
    f.write(ruby_cleanup)
    ruby_file = f.name

result = subprocess.run(
    f"cat {ruby_file} | ssh sobol.nr 'docker exec -i openproject-web-1 bundle exec rails runner -'",
    shell=True,
    capture_output=True,
    text=True,
    timeout=60
)

print(result.stdout)
os.unlink(ruby_file)

# Step 2: Fetch test issues from Jira using batch_get_issues
print("\nStep 2: Fetching test issues from Jira...")
issues_dict = jira.batch_get_issues(TEST_ISSUES)
print(f"✓ Fetched {len(issues_dict)} issues from Jira")

# Display issue details
print("\nIssue Details:")
print("-" * 80)
for key, issue in issues_dict.items():
    summary = issue.fields.summary[:50]
    start_date = getattr(issue.fields, 'customfield_11490', None)
    due_date = getattr(issue.fields, 'duedate', None)

    date_status = "OK"
    if start_date and due_date and due_date < start_date:
        date_status = f"⚠ INVALID (due < start)"

    print(f"{key:10s} | {date_status:20s} | {summary}")

print("-" * 80)

# Step 3: Run migration for first 10 issues
print("\n" + "=" * 80)
print("Step 3: RUNNING MIGRATION")
print("=" * 80)

start_time = time.time()

try:
    # Set longer timeouts
    os.environ["J2O_QUERY_TIMEOUT"] = "600"

    result = wpm.migrate(
        project_keys=["NRS"],
        components=["work_packages"],
        dry_run=False,
        max_issues=10  # First 10 issues from NRS project
    )

    duration = time.time() - start_time
    print(f"\n✓ Migration completed in {duration:.1f}s")

except Exception as e:
    duration = time.time() - start_time
    print(f"\n❌ Migration failed after {duration:.1f}s")
    logger.error(f"Exception: {e}", exc_info=True)
    sys.exit(1)

# Step 4: Verify results
print("\n" + "=" * 80)
print("Step 4: VERIFICATION")
print("=" * 80)

ruby_verify = f"""
cf = CustomField.find_by(type: 'WorkPackageCustomField', name: 'J2O Origin Key')
test_keys = %w[{test_keys_str}]

puts "Work Package Verification:"
puts "-" * 80

success_count = 0
failed_count = 0

test_keys.each do |key|
  wps = WorkPackage.joins(:custom_values)
                   .where(project_id: 303319)
                   .where(custom_values: {{{{custom_field_id: cf.id, value: key}}}})

  if wps.count > 0
    wp = wps.first
    journal_count = Journal.where(journable_id: wp.id, journable_type: 'WorkPackage').count
    comment_count = Journal.where(journable_id: wp.id, journable_type: 'WorkPackage')
                           .where.not(notes: [nil, '']).count

    success_count += 1
    puts "✓ #{{key}}: WP #{{wp.id}} | Journals: #{{journal_count}} | Comments: #{{comment_count}}"
    puts "  Dates: start=#{{wp.start_date}} due=#{{wp.due_date}}"
  else
    failed_count += 1
    puts "❌ #{{key}}: NOT FOUND"
  end
end

puts "-" * 80
puts "SUMMARY: #{{success_count}}/#{{test_keys.length}} successful"
puts "SUCCESS RATE: #{{(success_count.to_f / test_keys.length * 100).round(1)}}%"
"""

with tempfile.NamedTemporaryFile(mode='w', suffix='.rb', delete=False) as f:
    f.write(ruby_verify)
    ruby_file = f.name

result = subprocess.run(
    f"cat {ruby_file} | ssh sobol.nr 'docker exec -i openproject-web-1 bundle exec rails runner -'",
    shell=True,
    capture_output=True,
    text=True,
    timeout=60
)

print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr)

os.unlink(ruby_file)

print("\n" + "=" * 80)
print("TEST COMPLETE")
print(f"End: {datetime.now().isoformat()}")
print(f"Duration: {time.time() - start_time:.1f}s")
print("=" * 80)
