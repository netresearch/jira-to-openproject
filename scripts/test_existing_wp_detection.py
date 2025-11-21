#!/usr/bin/env python3
"""Test script to debug why existing work package detection returns 0."""

import sys
sys.path.insert(0, "/home/sme/p/j2o")

from src.clients.openproject_client import OpenProjectClient

# Initialize client
print("=" * 80)
print("TEST: Existing Work Package Detection")
print("=" * 80)

op_client = OpenProjectClient()

# NRS project ID from previous migrations
project_id = 303319

print(f"\nTesting snapshot query for project_id: {project_id}")
print("-" * 80)

try:
    snapshot = op_client.get_project_wp_cf_snapshot(project_id)
    print(f"\n✓ Snapshot query succeeded")
    print(f"  Returned {len(snapshot)} work packages")

    if len(snapshot) > 0:
        print(f"\n  First 3 results:")
        for i, wp in enumerate(snapshot[:3]):
            print(f"    [{i+1}] ID={wp.get('id')}, Jira Key={wp.get('jira_issue_key')}")
    else:
        print("\n  ⚠️  Empty result - investigating why...")

        # Test 1: Check if custom fields exist
        print("\n  Test 1: Checking if custom fields exist...")
        ruby_check_cfs = """
          cf_key = CustomField.find_by(type: 'WorkPackageCustomField', name: 'Jira Issue Key')
          cf_mig = CustomField.find_by(type: 'WorkPackageCustomField', name: 'Jira Migration Date')
          {
            jira_key_cf_exists: !cf_key.nil?,
            jira_key_cf_id: cf_key&.id,
            jira_mig_cf_exists: !cf_mig.nil?,
            jira_mig_cf_id: cf_mig&.id
          }
        """
        cf_result = op_client.execute_large_query_to_json_file(ruby_check_cfs)
        print(f"    Jira Key CF exists: {cf_result.get('jira_key_cf_exists')} (ID: {cf_result.get('jira_key_cf_id')})")
        print(f"    Jira Mig CF exists: {cf_result.get('jira_mig_cf_exists')} (ID: {cf_result.get('jira_mig_cf_id')})")

        # Test 2: Check if work packages exist in the project
        print("\n  Test 2: Checking if work packages exist in project...")
        ruby_check_wps = f"""
          {{
            wp_count: WorkPackage.where(project_id: {project_id}).count,
            sample_ids: WorkPackage.where(project_id: {project_id}).limit(5).pluck(:id)
          }}
        """
        wp_result = op_client.execute_large_query_to_json_file(ruby_check_wps)
        print(f"    Work package count: {wp_result.get('wp_count')}")
        print(f"    Sample IDs: {wp_result.get('sample_ids')}")

        # Test 3: Check custom values for sample work packages
        if wp_result.get('wp_count', 0) > 0 and wp_result.get('sample_ids'):
            print("\n  Test 3: Checking custom field values on sample work packages...")
            sample_id = wp_result['sample_ids'][0]
            ruby_check_cv = f"""
              wp = WorkPackage.find_by(id: {sample_id})
              cf_key = CustomField.find_by(type: 'WorkPackageCustomField', name: 'Jira Issue Key')
              {{
                wp_id: wp&.id,
                wp_subject: wp&.subject,
                custom_values_count: wp&.custom_values&.count,
                jira_key_value: (cf_key ? wp&.custom_value_for(cf_key)&.value : nil)
              }}
            """
            cv_result = op_client.execute_large_query_to_json_file(ruby_check_cv)
            print(f"    WP ID: {cv_result.get('wp_id')}")
            print(f"    Subject: {cv_result.get('wp_subject')}")
            print(f"    Custom values count: {cv_result.get('custom_values_count')}")
            print(f"    Jira Key value: {cv_result.get('jira_key_value')}")

except Exception as e:
    print(f"\n✗ Snapshot query failed: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 80)
print("TEST COMPLETE")
print("=" * 80)
