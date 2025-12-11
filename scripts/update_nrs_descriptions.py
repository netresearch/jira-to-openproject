#!/usr/bin/env python
"""
Script to update descriptions for NRS work packages that were migrated without descriptions.
This fixes the bug where jira_id/jira_key in attrs caused assign_attributes to fail silently.
"""
import json
import sys
import os
sys.path.insert(0, '/home/sme/p/j2o')

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.utils.markdown_converter import MarkdownConverter

def main():
    # Initialize clients
    print("Initializing clients...")
    jira_client = JiraClient()
    op_client = OpenProjectClient()
    markdown = MarkdownConverter()
    
    # Custom field ID for J2O Origin Key
    J2O_ORIGIN_KEY_CF_ID = 2912
    
    # Get NRS project ID from OpenProject
    print("Getting NRS project...")
    nrs_project = op_client.get_project_by_identifier("nrs")
    if not nrs_project:
        print("ERROR: NRS project not found in OpenProject")
        return 1
    nrs_project_id = nrs_project['id']
    print(f"NRS project ID: {nrs_project_id}")
    
    # Get all work packages in NRS project
    print("Fetching NRS work packages from OpenProject...")
    all_wps = []
    offset = 0
    page_size = 100
    while True:
        wps = op_client.get_work_packages(
            project_id=nrs_project_id, 
            page_size=page_size,
            offset=offset
        )
        if not wps:
            break
        all_wps.extend(wps)
        print(f"  Fetched {len(all_wps)} work packages...")
        if len(wps) < page_size:
            break
        offset += page_size
    
    print(f"Total work packages: {len(all_wps)}")
    
    # Build map of jira_key -> wp_id (using J2O Origin Key custom field)
    jira_to_wp = {}
    wp_with_no_key = 0
    for wp in all_wps:
        jira_key = None
        cf_values = wp.get('customField' + str(J2O_ORIGIN_KEY_CF_ID)) or wp.get('_embedded', {}).get('customFields', {})
        # Try different ways to get the custom field value
        if cf_values:
            if isinstance(cf_values, str):
                jira_key = cf_values
            elif isinstance(cf_values, dict):
                jira_key = cf_values.get(str(J2O_ORIGIN_KEY_CF_ID)) or cf_values.get(f'customField{J2O_ORIGIN_KEY_CF_ID}')
        
        if jira_key:
            jira_to_wp[jira_key] = wp['id']
        else:
            wp_with_no_key += 1
    
    print(f"Work packages with Jira key: {len(jira_to_wp)}")
    print(f"Work packages without Jira key: {wp_with_no_key}")
    
    # Fetch Jira issues for NRS project
    print("Fetching Jira issues for NRS project...")
    jql = 'project = NRS ORDER BY key ASC'
    jira_issues = jira_client.search_issues(jql, max_results=5000, fields=['description', 'key'])
    print(f"Total Jira issues: {len(jira_issues)}")
    
    # Build description updates
    updates = []
    for issue in jira_issues:
        jira_key = issue.key
        description = getattr(issue.fields, 'description', '') or ''
        
        if jira_key in jira_to_wp and description:
            # Convert Jira markdown to OpenProject format
            converted_desc = markdown.convert(description)
            if converted_desc:
                updates.append({
                    'jira_key': jira_key,
                    'wp_id': jira_to_wp[jira_key],
                    'description': converted_desc
                })
    
    print(f"Updates to apply: {len(updates)}")
    
    # Save to JSON file
    output_file = '/home/sme/p/j2o/var/data/nrs_description_updates.json'
    with open(output_file, 'w') as f:
        json.dump(updates, f, indent=2)
    print(f"Saved updates to {output_file}")
    
    return 0

if __name__ == '__main__':
    sys.exit(main())
