# OpenProject Custom Fields Overview

## Summary

This document provides an overview of the custom fields in our OpenProject instance, which we extracted from the Rails console. These custom fields are important for migrating data from Jira to OpenProject.

## Custom Fields Statistics

- **Total Custom Fields**: 125
- **Required Fields**: 0
- **Fields Available in All Projects**: 0

### Field Formats Distribution

1. **Text Fields** (79, 63.2%)
   - Examples: Account Details, UAT, External issue link

2. **Float Fields** (22, 17.6%)
   - Examples: Key Result: Current, Key Result: Start, Key Result: Target

3. **User Fields** (12, 9.6%)
   - Examples: Account Lead, Project Lead, Invoiced by

4. **Date Fields** (11, 8.8%)
   - Examples: Change start date, Target start, Target end

5. **Integer Fields** (1, 0.8%)
   - Example: Original story points

## Retrieval Process

The custom fields data was retrieved through the following process:

1. Connected to the OpenProject Rails console via tmux
2. Executed commands to list all custom fields with their properties
3. Parsed the output and converted it to JSON format
4. Saved the data to `var/data/openproject_custom_fields_rails.json`

## Technical Details

The custom fields were extracted using a combination of:

1. The `OpenProjectRailsClient` class to interact with the Rails console
2. Custom scripts to parse the output and extract the relevant data
3. JSON conversion to create a standardized format

## Scripts Created

During this process, we created several scripts to help retrieve and analyze the custom fields:

1. `scripts/test_rails_connection.py` - Tests the connection to the Rails console
2. `scripts/get_openproject_fields.py` - Retrieves custom fields from the Rails console
3. `scripts/simple_rails_command.py` - Runs simple commands in the Rails console
4. `scripts/extract_json.py` - Extracts JSON data from the Rails console output
5. `scripts/create_custom_fields_json.py` - Creates a JSON file with custom fields data
6. `scripts/summarize_custom_fields.py` - Summarizes the custom fields statistics

## How to Use the Data

The custom fields data can be used in various ways:

1. **Custom Field Mapping**: Map Jira custom fields to OpenProject custom fields
2. **Data Migration**: Ensure correct data types are preserved during migration
3. **Analysis**: Understand how custom fields are used in the system

## Next Steps

Based on the retrieved data, the next steps for the Jira to OpenProject migration could include:

1. Creating a mapping between Jira and OpenProject custom fields
2. Implementing data conversion for different field types
3. Testing the migration process with sample data
