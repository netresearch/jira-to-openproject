#!/usr/bin/env python3
"""Check if the 10 test work packages exist using a simpler Rails console approach."""

import sys
sys.path.insert(0, '/home/sme/p/j2o/src')

from clients.openproject_client import OpenProjectClient

def check_work_packages():
    """Query OpenProject to check if test work packages exist."""
    
    op = OpenProjectClient()
    
    print("=" * 70)
    print("CHECKING WORK PACKAGE EXISTENCE")
    print("=" * 70)
    
    # Test issue keys from the integration test
    test_keys = ["NRS-171", "NRS-182", "NRS-191", "NRS-198", "NRS-204",
                 "NRS-42", "NRS-59", "NRS-66", "NRS-982", "NRS-4003"]
    
    # Simple Ruby script using basic string interpolation
    ruby_script = """
test_keys = ['NRS-171', 'NRS-182', 'NRS-191', 'NRS-198', 'NRS-204', 'NRS-42', 'NRS-59', 'NRS-66', 'NRS-982', 'NRS-4003']

# Find custom field ID
jira_cf = CustomField.find_by(name: 'Jira Issue Key')

if jira_cf.nil?
  puts 'ERROR: Jira Issue Key custom field not found'
  exit
end

puts "Found Jira Issue Key custom field: ID=#{jira_cf.id}"
puts ""

found = 0
not_found = []

test_keys.each do |key|
  wp = WorkPackage.joins(:custom_values)
    .where(project_id: 303319)
    .where('custom_values.custom_field_id = ? AND custom_values.value = ?', jira_cf.id, key)
    .first
  
  if wp
    found += 1
    journal_count = wp.journals.count
    puts "✅ #{key}: WP##{wp.id} - #{journal_count} journals"
  else
    not_found << key
    puts "❌ #{key}: NOT FOUND"
  end
end

puts ""
puts "=== SUMMARY ==="
puts "Found: #{found}/#{test_keys.length} work packages"

if not_found.any?
  puts "Missing: #{not_found.join(', ')}"
end
"""
    
    print("\nQuerying OpenProject...")
    print("-" * 70)
    
    result = op.execute_large_query_to_json_file(ruby_script)
    
    if result:
        print(result)
    else:
        print("ERROR: Failed to query OpenProject - no result returned")
        return None
    
    print("=" * 70)
    return result

if __name__ == "__main__":
    check_work_packages()
