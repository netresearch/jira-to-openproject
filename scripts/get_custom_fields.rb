#!/usr/bin/env ruby
# Script to extract custom fields from OpenProject and save as JSON

require 'json'

# Get all custom fields
fields = CustomField.all.map do |cf|
  {
    id: cf.id,
    name: cf.name,
    field_format: cf.field_format,
    type: cf.type,
    is_required: cf.is_required,
    is_for_all: cf.is_for_all,
    possible_values: cf.possible_values
  }
end

# Convert to JSON
json_data = JSON.pretty_generate(fields)

# Output file path (default to tmp directory)
output_path = "/tmp/openproject_custom_fields.json"

# Write to file
File.write(output_path, json_data)

# Output confirmation
puts "Retrieved #{fields.size} custom fields"
puts "Saved custom fields to #{output_path}"

# Return fields count for verification
fields.size
