#!/usr/bin/env python3
"""Test comment migration with 10 NRS issues."""

import sys
sys.path.insert(0, "/home/sme/p/j2o")

from src.migrations.work_package_migration import WorkPackageMigration
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient

print("=" * 80)
print("COMMENT MIGRATION TEST - 10 NRS Issues")
print("=" * 80)

# Initialize clients
jira = JiraClient()
op = OpenProjectClient()

# Initialize migration with clients
wpm = WorkPackageMigration(jira_client=jira, op_client=op)

# Get 10 issues from NRS project
print("\nFetching 10 issues from NRS project...")
jql = "project = NRS ORDER BY key ASC"
issues = jira.search_issues(jql, maxResults=10)
print(f"✓ Found {len(issues)} issues")

# Get first issue details to verify comments exist in Jira
first_issue = jira.get_issue(issues[0].key, expand="changelog,renderedFields")
print(f"\nSample Issue: {first_issue.key}")
print(f"  Summary: {first_issue.fields.summary}")
if hasattr(first_issue.fields, 'comment') and first_issue.fields.comment:
    print(f"  Jira Comments: {first_issue.fields.comment.total}")
else:
    print(f"  Jira Comments: 0")

# Check existing work packages in OpenProject before migration
print("\nChecking OpenProject before migration...")
from src.clients.openproject_client import OpenProjectClient
op = OpenProjectClient()

# Use the fixed snapshot query
snapshot = op.get_project_wp_cf_snapshot(303319)
existing_count = len([wp for wp in snapshot if wp.get('jira_issue_key')])
print(f"✓ Existing work packages with J2O keys: {existing_count}")

# Run migration on these 10 issues only
print("\n" + "=" * 80)
print("RUNNING MIGRATION ON 10 ISSUES")
print("=" * 80)

result = wpm.migrate(
    project_keys=["NRS"],
    components=["work_packages"],
    dry_run=False,
    max_issues=10  # Limit to 10 issues
)

print("\n" + "=" * 80)
print("MIGRATION COMPLETE - VERIFYING RESULTS")
print("=" * 80)

# Check if comments were created
print("\nChecking for journals with comments...")
ruby_check = """
project_id = 303319
journals = Journal.where(journable_type: 'WorkPackage')
                  .joins('INNER JOIN work_packages ON work_packages.id = journals.journable_id')
                  .where('work_packages.project_id = ?', project_id)
                  .where.not(notes: [nil, ''])

puts "Total journals with notes: #{journals.count}"

if journals.count > 0
  puts "Sample journals:"
  journals.limit(5).each do |j|
    wp = j.journable
    cf_key = CustomField.find_by(type: 'WorkPackageCustomField', name: 'J2O Origin Key')
    jira_key = cf_key ? wp.custom_value_for(cf_key)&.value : nil
    puts "  Journal #{j.id}: WP=#{wp.id} (#{jira_key}), Notes length=#{j.notes&.length || 0}"
  end
end
"""

import tempfile
with tempfile.NamedTemporaryFile(mode='w', suffix='.rb', delete=False) as f:
    f.write(ruby_check)
    ruby_file = f.name

import subprocess
result = subprocess.run(
    f"cat {ruby_file} | ssh sobol.nr 'docker exec -i openproject-web-1 bundle exec rails runner -'",
    shell=True,
    capture_output=True,
    text=True
)

print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr)

# Cleanup
import os
os.unlink(ruby_file)

print("\n" + "=" * 80)
print("TEST COMPLETE")
print("=" * 80)
