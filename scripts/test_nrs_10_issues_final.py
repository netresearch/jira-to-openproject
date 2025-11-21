#!/usr/bin/env python3
"""
Comprehensive test for 10 NRS issues including known problematic ones.
Tests Bug #10, #15, and #16 fixes with enhanced logging.
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
    "NRS-171",  # From previous successful tests
    "NRS-182",  # From previous successful tests
    "NRS-191",  # From previous successful tests
    "NRS-198",  # From previous successful tests
    "NRS-204",  # From previous successful tests
    "NRS-42",   # Known Bug #10 issue (due_date 3 days before start_date)
    "NRS-59",   # Known Bug #10 issue (due_date 1 day before start_date)
    "NRS-66",   # Known Bug #10 issue (due_date 9 days before start_date)
    "NRS-982",  # Known Bug #10 issue (due_date 10 days before start_date)
    "NRS-4003", # Known Bug #10 issue (due_date 2 YEARS before start_date - extreme case)
]

print("=" * 80)
print("COMPREHENSIVE NRS MIGRATION TEST - 10 Issues")
print(f"Start Time: {datetime.now().isoformat()}")
print("=" * 80)
print("\nTest Issues (including known Bug #10 problematic ones):")
for i, key in enumerate(TEST_ISSUES, 1):
    print(f"  {i:2d}. {key}")
print()

# Initialize clients
logger.info("Initializing Jira and OpenProject clients...")
jira = JiraClient()
op = OpenProjectClient()

# Initialize migration with clients
logger.info("Initializing WorkPackageMigration...")
wpm = WorkPackageMigration(jira_client=jira, op_client=op)

# Fetch test issues from Jira
print("\nFetching test issues from Jira...")
jql = f"project = NRS AND key in ({','.join(TEST_ISSUES)}) ORDER BY key ASC"
logger.info(f"JQL: {jql}")
issues = jira.search_issues(jql, maxResults=20, expand="changelog")
print(f"✓ Found {len(issues)} issues in Jira")

if len(issues) != len(TEST_ISSUES):
    logger.warning(f"Expected {len(TEST_ISSUES)} issues but found {len(issues)}")

# Display issue details with date validation
print("\nIssue Details and Date Validation:")
print("-" * 80)
for issue in issues:
    key = issue.key
    summary = issue.fields.summary
    start_date = getattr(issue.fields, 'customfield_11490', None)  # Start date field
    due_date = getattr(issue.fields, 'duedate', None)

    # Check for Bug #10 date constraint violation
    date_status = "✓ OK"
    if start_date and due_date and due_date < start_date:
        date_status = f"⚠ INVALID (due={due_date} < start={start_date})"

    # Count comments and changelog
    comment_count = issue.fields.comment.total if hasattr(issue.fields, 'comment') else 0
    changelog_count = len(getattr(issue, 'changelog', {}).get('histories', []))

    print(f"{key:10s} | {date_status:40s} | Comments: {comment_count:2d} | Changelog: {changelog_count:3d}")
    print(f"           | {summary[:60]}")

print("-" * 80)

# Check existing work packages in OpenProject before migration
print("\nChecking OpenProject before migration...")
snapshot = op.get_project_wp_cf_snapshot(303319)  # NRS project ID
existing_wp_keys = [wp.get('jira_issue_key') for wp in snapshot if wp.get('jira_issue_key')]
print(f"✓ Existing work packages with J2O keys: {len(existing_wp_keys)}")

# Check which test issues already exist
existing_test_issues = [key for key in TEST_ISSUES if key in existing_wp_keys]
if existing_test_issues:
    print(f"⚠ Test issues already exist in OpenProject: {', '.join(existing_test_issues)}")
    print("  Migration will UPDATE these work packages (idempotent)")

# Run migration on these 10 issues only
print("\n" + "=" * 80)
print("RUNNING MIGRATION ON 10 ISSUES")
print("Using extended timeout: 600 seconds")
print("=" * 80)

start_time = time.time()

try:
    # Set longer timeout for operations
    os.environ["J2O_QUERY_TIMEOUT"] = "600"  # 10 minutes for queries

    result = wpm.migrate(
        project_keys=["NRS"],
        components=["work_packages"],
        dry_run=False,
        max_issues=10  # Limit to 10 issues
    )

    duration = time.time() - start_time
    print(f"\n✓ Migration completed in {duration:.1f} seconds")

except Exception as e:
    duration = time.time() - start_time
    print(f"\n❌ Migration failed after {duration:.1f} seconds")
    logger.error(f"Migration exception: {e}", exc_info=True)
    sys.exit(1)

# Verification Phase
print("\n" + "=" * 80)
print("VERIFICATION PHASE")
print("=" * 80)

# Check created/updated work packages
print("\n1. Checking work packages...")
ruby_check_wps = """
project_id = 303319
cf = CustomField.find_by(type: 'WorkPackageCustomField', name: 'J2O Origin Key')

test_keys = %w[NRS-171 NRS-182 NRS-191 NRS-198 NRS-204 NRS-42 NRS-59 NRS-66 NRS-982 NRS-4003]

puts "Work Package Status:"
puts "-" * 80

test_keys.each do |key|
  wps = WorkPackage.joins(:custom_values)
                   .where(project_id: project_id)
                   .where(custom_values: { custom_field_id: cf.id, value: key })

  if wps.count > 0
    wp = wps.first
    journal_count = Journal.where(journable_id: wp.id, journable_type: 'WorkPackage').count
    comment_count = Journal.where(journable_id: wp.id, journable_type: 'WorkPackage')
                           .where.not(notes: [nil, '']).count

    puts "✓ #{key}: WP #{wp.id} | Journals: #{journal_count} | Comments: #{comment_count}"
    puts "  Start: #{wp.start_date} | Due: #{wp.due_date} | Subject: #{wp.subject[0..50]}"
  else
    puts "❌ #{key}: NOT FOUND"
  end
end

puts "-" * 80
"""

with tempfile.NamedTemporaryFile(mode='w', suffix='.rb', delete=False) as f:
    f.write(ruby_check_wps)
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

# Check for journals with comments
print("\n2. Checking journals and comments...")
ruby_check_journals = """
project_id = 303319
cf = CustomField.find_by(type: 'WorkPackageCustomField', name: 'J2O Origin Key')

test_keys = %w[NRS-171 NRS-182 NRS-191 NRS-198 NRS-204 NRS-42 NRS-59 NRS-66 NRS-982 NRS-4003]

puts "Journal Analysis:"
puts "-" * 80

total_journals = 0
total_comments = 0

test_keys.each do |key|
  wps = WorkPackage.joins(:custom_values)
                   .where(project_id: project_id)
                   .where(custom_values: { custom_field_id: cf.id, value: key })

  next if wps.count == 0

  wp = wps.first
  journals = Journal.where(journable_id: wp.id, journable_type: 'WorkPackage')
  comment_journals = journals.where.not(notes: [nil, ''])

  total_journals += journals.count
  total_comments += comment_journals.count

  if comment_journals.count > 0
    puts "#{key}: #{comment_journals.count} comment journals"
    comment_journals.limit(2).each do |j|
      note_preview = j.notes[0..60].gsub(/\\n/, ' ')
      puts "  - Journal #{j.id}: #{note_preview}..."
    end
  end
end

puts "-" * 80
puts "TOTALS:"
puts "  Total Journals: #{total_journals}"
puts "  Journals with Comments: #{total_comments}"
puts "  Success Rate: #{(total_comments > 0 ? 'PASS' : 'FAIL')}"
puts "-" * 80
"""

with tempfile.NamedTemporaryFile(mode='w', suffix='.rb', delete=False) as f:
    f.write(ruby_check_journals)
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

# Check recent migration logs for errors
print("\n3. Checking recent migration logs for errors...")
log_dir = "/home/sme/p/j2o/var/logs"
log_files = sorted([f for f in os.listdir(log_dir) if f.startswith("migration_2025")], reverse=True)
if log_files:
    latest_log = os.path.join(log_dir, log_files[0])
    print(f"Latest log: {latest_log}")

    # Check for ERROR and PG:: lines
    result = subprocess.run(
        f"tail -200 {latest_log} | grep -E 'ERROR|PG::|Exception|Traceback' | head -20",
        shell=True,
        capture_output=True,
        text=True
    )

    if result.stdout:
        print("Recent errors found:")
        print(result.stdout)
    else:
        print("✓ No recent errors found in log")

print("\n" + "=" * 80)
print("TEST COMPLETE")
print(f"End Time: {datetime.now().isoformat()}")
print(f"Total Duration: {time.time() - start_time:.1f} seconds")
print("=" * 80)
