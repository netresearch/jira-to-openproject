# OpenProject Workflow Configuration Guide

This document outlines the steps required to configure workflows in OpenProject based on the Jira workflow mapping.

## Overview

OpenProject's approach to workflows differs from Jira's:

1. In OpenProject, all statuses are available to all work package types by default
2. Custom workflow configurations need to be done manually through the Admin interface
3. The migration tool helps by:
   - Creating missing statuses in OpenProject
   - Providing a mapping between Jira and OpenProject statuses
   - Analyzing workflow transitions from Jira
   - Generating documentation for manual configuration

## Automated Steps

The following steps are automated by the migration tool:

### 1. Status Migration

Run the status migration component to ensure all required statuses exist in OpenProject:

```bash
python src/main.py migrate --components workflow
```

This will:
- Extract statuses from Jira
- Extract existing statuses from OpenProject
- Create a mapping between Jira and OpenProject statuses
- Create any missing statuses in OpenProject
- Generate a status mapping file (`var/data/status_mapping.json`)
- Produce an analysis of the status mapping (`var/data/status_mapping_analysis.json`)

### 2. Workflow Analysis

The migration tool analyzes Jira workflows and creates a workflow mapping:

```bash
# This is run as part of the 'workflow' component
python src/main.py migrate --components workflow
```

This will:
- Extract workflow definitions from Jira
- Analyze workflow transitions for each Jira issue type
- Generate a workflow mapping file (`var/data/workflow_mapping.json`)
- Create a configuration guide (`var/data/workflow_configuration.json`)

## Manual Configuration Steps

After the automated steps, manual configuration is required to set up the workflows properly in OpenProject.

### 1. Verify Status Mapping

1. Open the `var/data/status_mapping.json` file
2. Verify that all Jira statuses have corresponding OpenProject statuses
3. Check for any unmatched statuses in the analysis report

### 2. Configure Workflows in OpenProject Admin Interface

#### Access the Admin Area

1. Log in to OpenProject as an administrator
2. Navigate to: **Administration > Work Packages > Status**
3. Verify all required statuses exist and have correct colors/properties

#### Configure Status Transitions per Type

1. Navigate to: **Administration > Work Packages > Types**
2. For each work package type:
   - Select the type (e.g., "Bug", "Task", etc.)
   - Click on the **Workflow** tab
   - Configure status transitions based on the Jira workflow mapping

### 3. Workflow Configuration Example

For each work package type, refer to the mapping and configure according to this template:

#### Example: Bug Type Workflow

Reference the following section in `workflow_mapping.json`:

```json
"Bug": {
  "transitions": [
    {"from": "Open", "to": "In Progress"},
    {"from": "In Progress", "to": "Testing"},
    {"from": "Testing", "to": "Done"},
    {"from": "Done", "to": "Open"}
  ]
}
```

In the OpenProject Admin interface:
1. Select the "Bug" type
2. In the Workflow tab, configure the transitions:
   - From "Open" status, allow transition to "In Progress"
   - From "In Progress" status, allow transition to "Testing"
   - From "Testing" status, allow transition to "Done"
   - From "Done" status, allow transition to "Open"

### 4. Default Configurations

If not explicitly configuring transitions, OpenProject allows any transition between statuses. To restrict workflows to match Jira:

1. For each work package type, go to the Workflow tab
2. Set the "Allow status changes from" dropdown to "Selected status" for each status
3. Select only the valid target statuses based on the workflow mapping

## Testing Workflow Configurations

After configuration, test the workflows to ensure they match the expected behavior:

1. Create a test project in OpenProject
2. Create work packages of different types
3. Attempt to transition between statuses
4. Verify that only allowed transitions are permitted

## Considerations

- OpenProject has a different workflow model compared to Jira
- Some complex Jira workflows may need to be simplified for OpenProject
- Consider documenting any differences for end-user training

## Advanced Configuration

For complex workflow requirements, consider:

1. Using OpenProject's REST API to programmatically configure workflows
2. Implementing custom workflow scripts/plugins
3. Creating transition buttons for common actions

## Troubleshooting

If workflows don't behave as expected:

1. Verify the status mapping is correct
2. Check the workflow configuration in the Admin interface
3. Ensure the user has appropriate permissions
4. Look for any status transition restrictions in project settings
