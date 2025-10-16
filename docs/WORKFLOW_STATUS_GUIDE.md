# OpenProject Workflow and Status Configuration

## Overview

This guide covers migrating Jira statuses and configuring OpenProject workflows. OpenProject handles workflows differently than Jira:

- All statuses are available to all work package types by default
- The migration tool now automates status creation and populates workflow transitions for configured roles via the `workflows` component
- Manual validation in the OpenProject admin UI is still recommended to confirm role-based permissions and edge-case transitions

## Automated Migration Process

### 1. Run Status Migration

```bash
python src/main.py migrate --components status_types workflows --force
```

This single command:

**Status Operations:**
- Extracts all statuses and status categories from Jira
- Extracts existing statuses from OpenProject
- Creates mapping between Jira and OpenProject statuses
- Creates missing statuses in OpenProject
- Stores data in `var/data/jira_statuses.json` and `var/data/op_statuses.json`

**Workflow Operations:**
- Extracts workflow definitions from Jira
- Analyzes workflow transitions for each issue type
- Generates workflow mapping file (`var/data/workflow_mapping.json`)
- Creates OpenProject workflow transition records for mapped roles
- Produces workflow configuration documentation for audit purposes

### 2. Review Generated Files

After migration, review these generated files:

- **`var/data/status_mapping.json`** - Complete status mapping between systems
- **`var/data/status_mapping_analysis.json`** - Analysis of mapping decisions
- **`var/data/workflow_mapping.json`** - Workflow transition analysis
- **`var/data/workflow_configuration_guide.txt`** - Manual configuration steps

## Status Mapping Logic

The migration tool uses this priority order for mapping Jira statuses to OpenProject:

1. **Exact name match** (case-insensitive)
2. **Fuzzy name matching** (handles variations like "In Progress" vs "In-Progress")
3. **Status category mapping** (uses Jira's status categories)
4. **Default fallback** (creates new status if no match found)

### Status Categories

| Jira Category | OpenProject Equivalent | Description |
|---------------|------------------------|-------------|
| To Do | New | Work not yet started |
| In Progress | In progress | Work currently being done |
| Done | Closed | Completed work |
| Custom | Custom | Project-specific statuses |

## Manual Workflow Configuration

After running the migration, review the generated transitions in OpenProject and adjust only if project-specific rules require it:

### 1. Access Workflow Configuration

1. Log in to OpenProject as Administrator
2. Navigate to **Administration** → **Work packages** → **Workflow**
3. Select the work package type to configure

### 2. Configure Allowed Transitions

For each work package type:

1. Select **Role** (e.g., Project admin, Developer, etc.)
2. Confirm the automatically-created transitions match expectations
3. Adjust permissions or add guard conditions for edge cases as needed
4. Save configuration changes

### 3. Validation Steps

After configuration:

```bash
# Verify status assignments work correctly
python scripts/test_status_assignment.py

# Test workflow transitions
python scripts/test_workflow_transitions.py
```

## Troubleshooting

### Common Issues

**Missing Status in OpenProject:**
- Check `var/data/status_mapping_analysis.json` for creation failures
- Verify OpenProject admin permissions
- Re-run migration with `--force` flag

**Incorrect Status Mapping:**
- Edit `var/data/status_mapping.json` manually
- Re-run migration to apply corrections
- Update workflow configuration accordingly

**Workflow Transition Issues:**
- Review `var/data/workflow_mapping.json` for transition analysis
- Check OpenProject role permissions
- Verify status assignments in Admin interface

### Manual Status Creation

If automatic creation fails, create statuses manually:

1. **Administration** → **Work packages** → **Status**
2. Click **+ Status**
3. Configure:
   - **Name**: Use exact name from Jira
   - **Color**: Choose appropriate color
   - **Default done ratio**: Set percentage (0% for new, 100% for closed)
   - **Closed**: Check if this represents completed work

## Best Practices

### Status Naming

- Use consistent naming conventions across systems
- Avoid special characters or excessive spaces
- Keep names concise but descriptive

### Workflow Design

- Minimize the number of statuses (5-8 per workflow is optimal)
- Ensure logical flow between statuses
- Consider role-based access when designing transitions
- Test workflows with real work packages before go-live

### Migration Validation

Always validate the migration results:

```bash
# Check status creation
python scripts/validate_status_migration.py

# Verify workflow configuration
python scripts/validate_workflow_setup.py

# Test end-to-end work package creation
python scripts/test_workpackage_lifecycle.py
```

This guide consolidates the previous separate workflow configuration and status migration documentation into a single, actionable reference.
