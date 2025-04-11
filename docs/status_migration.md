# OpenProject Status Migration Guide

This document outlines the process for migrating Jira statuses to OpenProject, including the steps for creating and configuring statuses in OpenProject based on the extracted Jira statuses.

## Overview

Status migration involves:
1. Extracting statuses from Jira
2. Extracting statuses from OpenProject
3. Creating a mapping between Jira and OpenProject statuses
4. Creating any missing statuses in OpenProject
5. Configuring the statuses in OpenProject workflows

## Migration Process

### 1. Extract Jira Statuses

First, extract all statuses and status categories from Jira:

```bash
python run_migration.py migrate --components status --force
```

This will:
- Extract all statuses from Jira
- Extract status categories from Jira
- Store the data in `var/data/jira_statuses.json` and `var/data/jira_status_categories.json`

### 2. Extract OpenProject Statuses

The same command will also extract existing statuses from OpenProject:

```bash
python run_migration.py migrate --components status --force
```

This will:
- Extract all statuses from OpenProject
- Store the data in `var/data/op_statuses.json`

### 3. Create Status Mapping

The migration tool will automatically create a mapping between Jira and OpenProject statuses based on name similarity and status category:

```bash
python run_migration.py migrate --components status
```

This will:
- Create a mapping between Jira and OpenProject statuses
- Store the mapping in `var/data/status_mapping.json`

### 4. Create Missing Statuses in OpenProject

To create statuses that exist in Jira but not in OpenProject, you have two options:

#### Option 1: Automated Creation via Rails Console (Recommended)

```bash
python run_migration.py migrate --components status --direct-migration
```

This will:
- Connect to the OpenProject Rails console
- Create statuses that exist in Jira but not in OpenProject
- Update the status mapping with the newly created statuses

#### Option 2: Manual Configuration in OpenProject

1. Login to OpenProject as an administrator
2. Navigate to Administration → Work packages → Status
3. Create each missing status:
   - Click "New status"
   - Enter the name (from the Jira status)
   - Set whether it's a closed status (usually based on Jira status category)
   - Select an appropriate color
   - Click "Create"
4. After creating all statuses, update the status mapping:
   ```bash
   python run_migration.py migrate --components status --update-mapping
   ```

### 5. Configure Workflow Status Transitions

Status transitions are handled as part of workflow configuration. See the [Workflow Migration Guide](workflow_migration.md) for details on configuring workflows.

## Testing the Status Migration

### 1. Validation Tests

Run the automated validation test:

```bash
python -m tests.test_status_migration
```

This will:
- Verify the status count matches between Jira and OpenProject
- Verify that all Jira statuses have a corresponding OpenProject status
- Check the mapping file integrity

### 2. Manual Testing

1. Login to OpenProject
2. Navigate to Administration → Work packages → Status
3. Verify that all expected statuses exist
4. Create a test work package
5. Verify that you can transition the work package through all the statuses according to the workflow
6. Compare the status transitions with the original Jira workflow

### 3. Status Mapping Analysis

Run the status mapping analysis tool:

```bash
python run_migration.py analyze --component status
```

This will produce a report showing:
- Status mapping completeness
- Potential issues or conflicts
- Suggestions for improving the mapping

## Troubleshooting

### Issue: Status Not Created

If a status fails to be created:

1. Check the logs for error messages
2. Verify that the Rails console is accessible
3. Try creating the status manually in OpenProject
4. Update the status mapping file manually if needed

### Issue: Status Transitions Not Working

If status transitions don't match the expected workflow:

1. Verify that the workflow configuration has been completed
2. Check the role-based permissions for status transitions
3. Refer to the workflow migration documentation for fixing workflow issues

## Reference

- Jira Status API: https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-status/#api-group-status
- OpenProject Status API: https://www.openproject.org/docs/api/endpoints/status/
