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

### Issue Type (Work Package Types) Migration

**Status**: ✅ Implementation and Unit Tests Complete

**Unit Tests**:
- `test_extract_jira_issue_types`: Tests extraction of issue type data from Jira
- `test_extract_openproject_work_package_types`: Tests extraction of work package type data from OpenProject
- `test_create_issue_type_mapping`: Tests the mapping strategy between Jira and OpenProject
- `test_migrate_issue_types_via_rails`: Tests the direct migration of issue types via Rails console
- `test_migrate_issue_types`: Tests the full migration process for issue types
- `test_analyze_issue_type_mapping`: Tests the analysis functionality for issue type mapping
- `test_update_mapping_file`: Tests updating the mapping file after manual creation of work package types

**Manual Testing Steps**:
1. Verify issue type extraction from Jira:
   - Check that all Jira issue types are extracted correctly
   - Verify key attributes (name, description, subtask flag)

2. Verify work package type extraction from OpenProject:
   - Check that existing OpenProject work package types are identified
   - Verify key attributes (name, color, milestone flag)

3. Test issue type mapping creation:
   - Check that exact matches by name are correctly mapped
   - Verify default mappings are applied correctly
   - Verify the mapping template file is created with correct information

4. Test work package type creation via Rails:
   - Identify Jira issue types that have no match in OpenProject
   - Run the migration for these types using the Rails console
   - Verify work package types are created in OpenProject with correct attributes
   - Check both direct execution and script generation options

5. Test the complete migration process:
   - Run the migrate_issue_types method
   - Verify the mapping analysis is generated correctly
   - Check that the ID mapping file is created with correct mappings

6. Test work package type usage in work package migration:
   - Create test issues in Jira of different types
   - Run the work package migration
   - Verify the issues are created with correct work package types in OpenProject

7. Test the analysis functionality:
   - Run the analyze_issue_type_mapping method
   - Verify it correctly reports on matched vs. unmatched types
   - Check that it identifies types that need to be created

8. Test updating the mapping file:
   - After manually creating work package types in OpenProject
   - Run update_mapping_file method
   - Verify the mapping file is updated with correct IDs

9. Test edge cases:
   - Issue type with unusual name
   - Issue type that has no default mapping
   - Sub-task issue types
   - Milestone issue types

### Workflow Migration

**Status**: ✅ Implementation and Unit Tests Complete

**Unit Tests**:
- `test_extract_jira_statuses`: Tests extraction of status data from Jira
- `test_extract_openproject_statuses`: Tests extraction of status data from OpenProject
- `test_create_status_mapping`: Tests the mapping between Jira and OpenProject statuses
- `test_create_status_in_openproject`: Tests creation of statuses in OpenProject
- `test_migrate_statuses`: Tests the migration of statuses from Jira to OpenProject
- `test_create_workflow_configuration`: Tests the generation of workflow configuration documentation
- `test_analyze_status_mapping`: Tests the analysis of status mapping
- `test_extract_jira_workflows`: Tests extraction of workflow data from Jira

**Manual Testing Steps**:
1. Verify status extraction from Jira:
   - Check that all Jira statuses are extracted correctly
   - Verify key attributes (name, category, color)

2. Verify status extraction from OpenProject:
   - Check that existing OpenProject statuses are identified
   - Verify key attributes (name, color, closed flag)

3. Test status mapping creation:
   - Check that exact matches by name are correctly mapped
   - Verify the mapping file is created with correct information

4. Test status creation in OpenProject:
   - Identify Jira statuses that have no match in OpenProject
   - Run the migration for these statuses
   - Verify statuses are created in OpenProject with correct attributes

5. Test the complete status migration process:
   - Run the migrate_statuses method
   - Verify that unmatched statuses are created in OpenProject
   - Check that the mapping file is updated correctly

6. Verify workflow configuration in OpenProject:
   - Check that workflow configuration instructions are generated
   - Understand that OpenProject automatically makes all statuses available
     for all work package types by default
   - Verify that any custom workflow configurations are documented

7. Manual configuration of workflows:
   - Using the Admin interface in OpenProject, navigate to:
     Administration > Work packages > Types
   - For each work package type, verify the available statuses
   - Configure any specific workflow rules needed based on the mapping
   - Test transitions between statuses for each work package type

8. Test the workflow analysis functionality:
   - Run the analyze_status_mapping method
   - Verify it correctly reports on the status of mappings

9. Test workflow usage in work package migration:
   - Create test issues in Jira with different statuses
   - Run the work package migration
   - Verify the work packages are created with correct statuses in OpenProject
   - Test status transitions for migrated work packages

10. Verify status configuration in real projects:
    - Check status transitions in real project contexts
    - Verify that status workflows match the original Jira configuration
      as closely as possible

**Documentation**: See [workflow_configuration.md](workflow_configuration.md) for detailed steps on configuring workflows in OpenProject.

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
