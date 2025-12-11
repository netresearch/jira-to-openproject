#!/usr/bin/env python
"""
Generate description updates JSON for NRS work packages.
Uses existing J2O infrastructure to fetch Jira descriptions and map to OpenProject WP IDs.
"""
import json
import sys
import os

# Add project root to path
sys.path.insert(0, '/home/sme/p/j2o')

from src.clients.jira_client import JiraClient
from src.utils.markdown_converter import MarkdownConverter

def main():
    print("=" * 60)
    print("NRS Description Updates Generator")
    print("=" * 60)

    # Load existing work package mapping
    wp_mapping_file = '/home/sme/p/j2o/var/data/work_package_mapping.json'

    print(f"\nLoading work package mapping from {wp_mapping_file}...")
    if not os.path.exists(wp_mapping_file):
        print(f"ERROR: Work package mapping file not found: {wp_mapping_file}")
        return 1

    with open(wp_mapping_file, 'r') as f:
        wp_mapping = json.load(f)

    # Filter for NRS issues only - key is jira_id, but we need to filter by project_key
    # and create a lookup by jira_key for matching with fetched Jira issues
    nrs_mappings = {}
    for jira_id, data in wp_mapping.items():
        if data.get('project_key') == 'NRS':
            jira_key = data.get('jira_key')
            if jira_key:
                nrs_mappings[jira_key] = data
    print(f"Found {len(nrs_mappings)} NRS work packages in mapping")

    if not nrs_mappings:
        print("ERROR: No NRS mappings found")
        return 1

    # Initialize Jira client
    print("\nInitializing Jira client...")
    jira_client = JiraClient()

    # Initialize markdown converter
    markdown = MarkdownConverter()

    # Fetch all NRS issues with descriptions from Jira
    print("\nFetching NRS issues from Jira...")
    jql = 'project = NRS ORDER BY key ASC'

    # Fetch in batches
    all_issues = []
    start_at = 0
    batch_size = 100

    while True:
        try:
            issues = jira_client.jira.search_issues(
                jql,
                startAt=start_at,
                maxResults=batch_size,
                fields='description,key,summary'
            )
            if not issues:
                break
            all_issues.extend(issues)
            print(f"  Fetched {len(all_issues)} issues...")
            if len(issues) < batch_size:
                break
            start_at += batch_size
        except Exception as e:
            print(f"ERROR fetching issues: {e}")
            break

    print(f"Total Jira issues fetched: {len(all_issues)}")

    # Build description updates
    updates = []
    issues_with_desc = 0
    issues_without_desc = 0
    issues_not_in_mapping = 0

    for issue in all_issues:
        jira_key = issue.key
        description = getattr(issue.fields, 'description', '') or ''

        if not description:
            issues_without_desc += 1
            continue

        issues_with_desc += 1

        if jira_key not in nrs_mappings:
            issues_not_in_mapping += 1
            continue

        # Convert Jira markdown to OpenProject format
        try:
            converted_desc = markdown.convert(description)
        except Exception as e:
            print(f"  WARNING: Failed to convert description for {jira_key}: {e}")
            converted_desc = description

        if converted_desc:
            op_wp_id = nrs_mappings[jira_key].get('openproject_id')
            if op_wp_id:
                updates.append({
                    'jira_key': jira_key,
                    'wp_id': op_wp_id,
                    'description': converted_desc,
                    'description_length': len(converted_desc)
                })

    print(f"\nStatistics:")
    print(f"  Issues with description: {issues_with_desc}")
    print(f"  Issues without description: {issues_without_desc}")
    print(f"  Issues not in mapping: {issues_not_in_mapping}")
    print(f"  Updates to apply: {len(updates)}")

    # Save to JSON file
    output_file = '/home/sme/p/j2o/var/data/nrs_description_updates.json'
    with open(output_file, 'w') as f:
        json.dump(updates, f, indent=2)
    print(f"\nSaved updates to {output_file}")

    # Also save to /tmp for the Ruby script
    tmp_file = '/tmp/nrs_description_updates.json'
    with open(tmp_file, 'w') as f:
        json.dump(updates, f, indent=2)
    print(f"Copied to {tmp_file}")

    return 0

if __name__ == '__main__':
    sys.exit(main())
