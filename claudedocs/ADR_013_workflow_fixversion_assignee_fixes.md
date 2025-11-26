# ADR-013: Workflow Custom Field, fixVersion Mapping, and Assignee Lookup Fixes

## Status
All features: IMPLEMENTED - Verified

## Context
Three related improvements were needed for the migration tool:
1. Bug #20: Assignee lookup was using display name instead of username
2. fixVersion field mapping was not implemented
3. Jira Workflow scheme changes needed to be captured as a custom field

## Problem Analysis

### Bug #20: Assignee Lookup Using Wrong Field

**Root Cause**: The Python code was looking up users by display name (e.g., "Michael Ablass") instead of username (e.g., "michael.ablass").

**Impact**: Assignee values were not being set correctly on work packages because user lookups failed.

### fixVersion Not Mapped

**Root Cause**: The `fixVersions` field from Jira was not being mapped to OpenProject's `version` field, and versions that didn't exist needed to be created on-the-fly.

### Workflow Scheme Not Captured

**Root Cause**: Jira's "Workflow" field in changelog represents workflow scheme changes (e.g., "No approval - dual QA"), not status changes. This metadata was being captured in journal notes (via Bug #16 fix) but was not searchable as a custom field.

## Solution

### Bug #20 Fix: Use Username for User Lookup
**File**: `src/migrations/work_package_migration.py`

Changed user lookup from display name to username:
```python
# Before (broken):
user = self._lookup_user_by_name(display_name)

# After (working):
user = self._lookup_user_by_login(username)
```

### fixVersion Mapping with On-The-Fly Version Creation
**File**: `src/migrations/work_package_migration.py`

Added version mapping logic:
1. Extract `fixVersions` from Jira issue
2. Look up or create version in OpenProject project
3. Map to `version_id` field

```python
# Extract fixVersions and map to OpenProject version
fix_versions = jira_issue.fields.fixVersions
if fix_versions:
    version_name = fix_versions[0].name  # Use first fixVersion
    version_id = self._get_or_create_version(project_id, version_name)
    if version_id:
        attrs["version_id"] = version_id
```

### J2O Jira Workflow Custom Field
**Files**:
- `src/migrations/work_package_migration.py` (extraction logic)
- `src/clients/openproject_client.py` (custom field creation)

#### Custom Field Definition
Added "J2O Jira Workflow" to the custom field specifications:
```python
cf_specs = (
    ("J2O Origin System", "string", False),
    ("J2O Origin ID", "string", True),
    ("J2O Origin Key", "string", True),
    ("J2O Origin URL", "string", False),
    ("J2O First Migration Date", "date", False),
    ("J2O Last Update Date", "date", False),
    ("J2O Jira Workflow", "string", True),  # Searchable: Current/final Jira workflow scheme
)
```

#### Workflow Extraction Method
```python
def _extract_final_workflow(self, jira_issue: Any) -> str | None:
    """Extract the final/current workflow scheme name from Jira changelog."""
    try:
        changelog = getattr(jira_issue, "changelog", None)
        if not changelog:
            return None
        histories = getattr(changelog, "histories", None)
        if not histories:
            return None
        workflow_changes = []
        for history in histories:
            items = getattr(history, "items", [])
            for item in items:
                field = getattr(item, "field", None) or (item.get("field") if isinstance(item, dict) else None)
                if field == "Workflow":
                    to_string = getattr(item, "toString", None) or (item.get("toString") if isinstance(item, dict) else None)
                    if to_string:
                        created = getattr(history, "created", "")
                        workflow_changes.append((created, to_string))
        if workflow_changes:
            workflow_changes.sort(key=lambda x: x[0])
            final_workflow = workflow_changes[-1][1]
            return str(final_workflow)
    except Exception as e:
        pass
    return None
```

### Critical Fix: Custom Field Type Enablement
**File**: `src/clients/openproject_client.py`

**Root Cause Discovery**: Custom fields with `is_for_all=true` were NOT automatically enabled for all Types. The `type_ids` array was empty, preventing values from being saved.

**Solution**: Updated `ensure_custom_field()` to explicitly enable WorkPackageCustomFields for all types:

```ruby
# For WorkPackageCustomField, explicitly enable for all types
if cf.type == 'WorkPackageCustomField' && cf.type_ids.empty?
  begin
    cf.type_ids = Type.all.pluck(:id)
    cf.save
  rescue
  end
end
```

## Validation Results

### Before Fixes
- Assignee: Not set (user lookup failed)
- fixVersion: Not mapped
- J2O Jira Workflow: nil (custom field not enabled for types)

### After Fixes
```
WP #5584977: NRS-182
- Assigned to: Michael Ablass (correct user)
- Version: "1.0" (mapped from fixVersions)
- J2O Jira Workflow: "No approval - dual QA" (final workflow scheme)
```

### Workflow History in Journal Notes
The historical workflow changes are preserved in journal notes (via Bug #16 fix):
```
v12: Jira Workflow: Standard Bug Workflow → No approval - Workflow
v15: Jira Workflow: No approval - Workflow → No approval - dual QA
... (11 workflow changes total)
```

## Technical Impact

### Data Model
- New searchable custom field: "J2O Jira Workflow"
- Version mapping: Jira fixVersions → OpenProject version_id
- Assignee mapping: Uses username (login) not display name

### Defense-in-Depth
- Custom fields now auto-enable for all types on creation
- Existing custom fields are checked and enabled if needed

### Performance
- No negative impact on migration speed
- On-the-fly version creation adds minimal overhead

## Related ADRs
- ADR-011: Bug #16 - Unmapped Field Preservation (workflow history in notes)
- ADR-012: Bug #17, #18, #19 - Journal Quality Fixes

## Date
2025-11-26

## Bug/Feature Status Summary
- Bug #20: FIXED (assignee lookup uses username)
- fixVersion mapping: IMPLEMENTED (with on-the-fly version creation)
- J2O Jira Workflow: IMPLEMENTED (searchable custom field with final workflow scheme)
- Custom field type enablement: FIXED (auto-enables for all types)
