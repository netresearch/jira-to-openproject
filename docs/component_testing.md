# Component Testing Status

This document summarizes the testing status of key migration components and provides guidelines for manual testing.

## Completed Components

### Project Migration

**Status**: ✅ Implementation and Unit Tests Complete

**Unit Tests**:
- `test_extract_jira_projects`: Tests extraction of project data from Jira
- `test_extract_openproject_projects`: Tests extraction of project data from OpenProject
- `test_create_project_in_openproject`: Tests creation of a single project in OpenProject
- `test_migrate_projects`: Tests the full migration process for projects
- `test_analyze_project_mapping`: Tests the analysis functionality for project mapping

**Manual Testing Steps**:
1. Verify project extraction from Jira:
   - Check that all expected Jira projects are extracted
   - Verify key project attributes (key, name, description)

2. Verify project extraction from OpenProject:
   - Check that existing OpenProject projects are correctly identified
   - Verify key project attributes

3. Test project creation:
   - Create a new test Jira project
   - Run the migration for just this project
   - Verify the project is created in OpenProject with correct attributes
   - Check that the project identifier follows naming conventions

4. Test project mapping:
   - Verify projects with the same name are correctly mapped
   - Verify the mapping file contains correct information
   - Check that account associations are correctly maintained

5. Test project hierarchy (if applicable):
   - Create test Jira projects with parent-child relationships
   - Run the migration
   - Verify the hierarchy is preserved in OpenProject

6. Test project with custom fields:
   - Verify custom fields like 'Tempo Account' are correctly set on projects
   - Test projects with and without account associations

7. Test the analysis functionality:
   - Run the analyze_project_mapping method
   - Verify it correctly reports on new vs. existing projects
   - Check it accurately reports on account associations

8. Test idempotency:
   - Run the migration twice
   - Verify no duplicate projects are created
   - Check that the mapping is correctly updated

9. Test edge cases:
   - Project with very long name/identifier
   - Project with special characters in name
   - Project with no description

### Link Type (Relations) Migration

**Status**: ✅ Implementation and Unit Tests Complete

**Unit Tests**:
- `test_extract_jira_link_types`: Tests extraction of link type data from Jira
- `test_extract_openproject_relation_types`: Tests extraction of relation type data from OpenProject
- `test_create_link_type_mapping`: Tests the mapping strategy between Jira and OpenProject
- `test_create_relation_type_in_openproject`: Tests creation of a relation type in OpenProject
- `test_migrate_link_types`: Tests the full migration process for link types
- `test_analyze_link_type_mapping`: Tests the analysis functionality for link type mapping

**Manual Testing Steps**:
1. Verify link type extraction from Jira:
   - Check that all Jira link types are extracted correctly
   - Verify key attributes (name, inward, outward)

2. Verify relation type extraction from OpenProject:
   - Check that existing OpenProject relation types are identified
   - Verify key attributes

3. Test link type mapping creation:
   - Check that obvious matches by name are correctly mapped
   - Verify similar matches are identified correctly
   - Verify the mapping file is created with correct information

4. Test relation type creation in OpenProject:
   - Identify a Jira link type that has no match
   - Run the migration for this type
   - Verify the relation type is created in OpenProject with correct attributes

5. Test the complete migration process:
   - Run the migrate_link_types method
   - Verify that unmatched types are created in OpenProject
   - Check that the mapping file is updated correctly

6. Test relation usage in work package migration:
   - Create test issues in Jira with links between them
   - Run the work package migration
   - Verify the links are correctly preserved in OpenProject
   - Check that the correct relation types are used

7. Test the analysis functionality:
   - Run the analyze_link_type_mapping method
   - Verify it correctly reports on the mapping status
   - Check statistics on match types

8. Test edge cases:
   - Link type with unusual characters
   - Link type with very similar name but different function
   - Link type that might match multiple OpenProject types

9. Test relation creation error handling:
   - Simulate API errors during relation type creation
   - Verify the error is handled gracefully
   - Check that the migration continues with other types

## Running the Tests

### Automated Tests

Run the unit tests for these components with:

```bash
# Run all tests
python -m unittest discover tests

# Run specific component tests
python -m unittest tests.test_project_migration
python -m unittest tests.test_link_type_migration
```

### Manual Tests

For manual testing, use the following command:

```bash
# For project migration
python src/main.py migrate --components projects

# For link type migration
python src/main.py migrate --components link_types

# To run with dry-run mode (no changes made to OpenProject)
python src/main.py migrate --components projects --dry-run
python src/main.py migrate --components link_types --dry-run
```

## Next Steps

After successfully testing these components, proceed to testing the next components, particularly focusing on:

1. **Issue Types (Work Package Types)**
2. **Workflows**
3. **Work Packages (Issues)**

Each of these components builds on the foundation of the projects and link types.
