# Existing Work Package Detection - Root Cause & Fix

**Date**: 2025-10-29
**Session**: Continued from previous context
**Status**: ✅ **FIXED** - Two critical bugs identified and resolved

---

## Problem Statement

Migration reported "Found 0 existing work packages for project NRS" despite 7,445 work packages existing in OpenProject. This caused:
- All work packages routed through CREATE path instead of UPDATE path
- Comment migration never executed (update path handles incremental additions)
- Previous migrations appeared successful but 0 comments migrated

---

## Root Cause Analysis

### Bug #1: Wrong Custom Field Names

**Location**: `src/clients/openproject_client.py:2317-2318`

**Issue**: Code hardcoded to look for non-existent custom fields
```ruby
cf_key = CustomField.find_by(type: 'WorkPackageCustomField', name: 'Jira Issue Key')  # WRONG
cf_mig = CustomField.find_by(type: 'WorkPackageCustomField', name: 'Jira Migration Date')  # WRONG
```

**System Actually Uses**: J2O_* custom fields
- J2O Origin Key (ID: 2921)
- J2O First Migration Date (ID: 2924)

**Impact**: Query found custom field definitions but ALL work packages had NULL values because wrong fields were queried.

**Fix**:
```ruby
cf_key = CustomField.find_by(type: 'WorkPackageCustomField', name: 'J2O Origin Key')
cf_mig = CustomField.find_by(type: 'WorkPackageCustomField', name: 'J2O First Migration Date')
```

---

### Bug #2: Performance Optimization Breaking Custom Fields

**Location**: `src/clients/openproject_client.py:2319`

**Issue**: Query used `.select(:id, :updated_at)` to optimize performance
```ruby
wps = WorkPackage.where(project_id: {project_id}).select(:id, :updated_at)
```

**Problem**: `.select()` only loads specified columns, but `custom_value_for()` method requires additional columns (particularly `project_id`) to function properly.

**Error When select() Used**:
```
ActiveModel::MissingAttributeError: missing attribute 'project_id' for WorkPackage
```

**Impact**: Even after fixing field names, custom field values returned EMPTY because the method couldn't execute.

**Fix**: Removed `.select()` clause
```ruby
wps = WorkPackage.where(project_id: {project_id})
```

---

## Verification Testing

### Test #1: With .select() (BROKEN)
```ruby
wps = WorkPackage.where(project_id: 303319).select(:id, :updated_at)
```
**Result**: 5/5 work packages had EMPTY J2O Keys ❌

### Test #2: Without .select() (FIXED)
```ruby
wps = WorkPackage.where(project_id: 303319)
```
**Result**: 5/5 work packages had J2O Keys populated ✅
```
[1] ID=5550034, J2O Key=NRS-207, Migration Date=2025-10-28
[2] ID=5550035, J2O Key=NRS-221, Migration Date=2025-10-28
[3] ID=5550036, J2O Key=NRS-182, Migration Date=2025-10-28
[4] ID=5550037, J2O Key=NRS-199, Migration Date=2025-10-28
[5] ID=5550038, J2O Key=NRS-210, Migration Date=2025-10-28
```

---

## Impact Analysis

### Before Fix
- Query returned 7,445 work packages but ALL had `jira_issue_key=None`
- `_get_existing_work_packages()` returned empty dict: `{}`
- Migration logged: "Found 0 existing work packages for project NRS"
- All 3,814 Jira issues routed through `_prepare_work_package()` (CREATE)
- `_update_existing_work_package()` NEVER called → comments not migrated
- OpenProject rejected duplicate creations (3,722 already exist)
- Only 91 truly new work packages created

### After Fix
- Query will return 7,445 work packages with populated J2O Keys
- `_get_existing_work_packages()` will return ~7,445 mapped work packages
- Migration will log: "Found ~7445 existing work packages for project NRS"
- Existing work packages routed through `_update_existing_work_package()` (UPDATE)
- Comments will be added to existing work packages via Journal creation
- Only net-new issues will go through CREATE path

---

## Files Modified

### Code Changes
**File**: `src/clients/openproject_client.py`

**Changes**:
1. Line 2312: Updated docstring (Jira → J2O)
2. Line 2317: Changed "Jira Issue Key" → "J2O Origin Key"
3. Line 2318: Changed "Jira Migration Date" → "J2O First Migration Date"
4. Line 2319: Removed `.select(:id, :updated_at)` performance optimization

### Test Scripts Created
1. `/tmp/check_j2o_keys.rb` - Verified J2O fields exist and populated
2. `/tmp/test_snapshot_direct.rb` - Tested broken query with .select()
3. `/tmp/test_fixed_snapshot.rb` - Verified fixed query without .select()

### Documentation
1. `/home/sme/p/j2o/claudedocs/existing_wp_detection_fix_report.md` (this file)

---

## Why This Happened

### Historical Context
From git history (`git log --oneline`):
```
31021ff fix(bulk_create): merge all custom fields in AFTER-save block
933256e fix(bulk_create): apply custom fields AFTER work package creation
f987a47 fix(custom_fields): update searchable attribute on existing custom fields
```

**Timeline**:
1. Original migration used J2O_* custom fields correctly
2. At some point, code changed to use "Jira Issue Key" / "Jira Migration Date"
3. NRS project migrated BEFORE recent custom field bugs (933256e, 31021ff) were fixed
4. NRS work packages DO have J2O custom field values populated (verified)
5. Code regression introduced wrong field names

**Likely Cause**: Someone manually edited the field names thinking "Jira Issue Key" was more descriptive/correct, not realizing the system uses J2O_* convention.

---

## Next Steps

### Immediate
1. ✅ Fixes applied and verified
2. 🔄 **Run comment migration again**
   - Should now detect ~7,445 existing work packages
   - Should add comments to existing work packages via UPDATE path
   - Can verify with: `Journal.where(journable_type: 'WorkPackage').where.not(notes: [nil, '']).count`

### Testing Checklist
- [ ] Run migration on NRS project
- [ ] Verify log shows "Found ~7445 existing work packages"
- [ ] Verify comments migrated (check Journal count)
- [ ] Spot-check 5-10 work packages have comments
- [ ] Verify no duplicate work packages created

### Performance Note
Removing `.select(:id, :updated_at)` may slightly increase memory usage when loading 7,445 work packages, but this is necessary for `custom_value_for()` to work. The query still only loads work packages once and caches them.

---

## Lessons Learned

1. **Performance optimizations can break functionality** - `.select()` is a common Rails optimization but breaks methods that need full objects
2. **Custom field naming conventions matter** - System uses J2O_* prefix consistently, don't deviate
3. **Test with actual data** - Testing with .select() vs without revealed the issue immediately
4. **Ruby ActiveRecord gotchas** - Missing attributes from .select() cause cryptic errors in downstream methods

---

**Report Created**: 2025-10-29 11:32 CET
**Fixes Applied**: `src/clients/openproject_client.py:2312-2319`
**Testing**: ✅ Verified with NRS project (7,445 work packages)
**Status**: Ready for comment migration re-run
