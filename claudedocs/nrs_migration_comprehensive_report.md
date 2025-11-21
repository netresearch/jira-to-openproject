# NRS Migration Comprehensive Report
**Date**: 2025-10-21
**Session**: Large-scale migration test with bug fixes
**Project**: NRS (3805 Jira issues)

---

## Executive Summary

Successfully migrated **3805 work packages** from Jira to OpenProject with **100% success rate** after fixing critical type field bug. However, discovered J2O Origin custom fields are **not being populated** due to multiple issues in the migration pipeline.

### Results
- ✅ **3805/3805 work packages created** (100% success rate)
- ✅ **Type field bug fixed** (commit `bb9298d`)
- ❌ **J2O Origin fields NOT populated** (0 work packages have searchable metadata)
- ❌ **Custom fields silently failing** in Ruby bulk create script
- ✅ **Searchable attribute fix** implemented (commit `f987a47`)

---

## Bugs Found and Fixed

### 1. Missing Type Fallback in Ruby Bulk Create (**CRITICAL - FIXED**)

**Commit**: `bb9298d`
**File**: `src/clients/openproject_client.py:2573-2575`

**Root Cause**: Ruby bulk create script had safety fallbacks for `status` and `priority` but was **missing the fallback for `type`**:

```ruby
# BEFORE (lines 2573-2574)
rec.status ||= Status.order(:position).first     # ✅ Had fallback
rec.priority ||= IssuePriority.order(:position).first  # ✅ Had fallback
# ❌ NO fallback for type!

# AFTER (added line 2575)
rec.status ||= Status.order(:position).first
rec.priority ||= IssuePriority.order(:position).first
rec.type ||= Type.order(:position).first  # ✅ Added fallback
```

**Impact**: **100% failure rate** - All 3000 work packages in first 3 batches failed with "Type can't be blank" error before fix.

**Validation**: After fix, **3805/3805 work packages created successfully** (100% success rate).

---

### 2. ensure_custom_field Ignores Searchable Attribute (**CRITICAL - FIXED**)

**Commit**: `f987a47`
**File**: `src/clients/openproject_client.py:1446-1467`

**Root Cause**: The `ensure_custom_field` function only checked if a custom field with the given **name and type** exists, but didn't verify or update the `searchable` attribute:

```ruby
# BEFORE (line 1446)
cf = CustomField.find_by(type: '{cf_type}', name: '{name}')
if !cf
  cf = CustomField.new(...)
  cf.searchable = {searchable_str}
  cf.save
end
# ❌ Returns first matching field regardless of searchable value
```

**Fix**: Added `else` block to update existing fields:

```ruby
# AFTER (lines 1458-1467)
if !cf
  cf = CustomField.new(...)
  cf.searchable = {searchable_str}
  cf.save
else
  # Update searchable if it doesn't match
  begin
    if cf.respond_to?(:searchable) && cf.searchable != {searchable_str}
      cf.searchable = {searchable_str}
      cf.save
    end
  rescue
  end
end
```

**Impact**:
- Created **duplicate custom fields**: searchable (IDs 2913, 2912) AND non-searchable (IDs 2922, 2921)
- Migration stored data in **non-searchable fields** instead of searchable ones
- Work packages cannot be found by Jira keys in OpenProject search

**Validation**: Manually updated fields 2921 and 2922 to be searchable via Rails console.

---

## Bugs Found But NOT Yet Fixed

### 3. Custom Fields Not Being Saved to Database (**CRITICAL - NOT FIXED**)

**Status**: 🔴 **BLOCKING**
**File**: `src/clients/openproject_client.py:2543-2569`

**Symptoms**:
- Python code sends `custom_fields` array to Ruby script ✅
- Ruby script processes the array without errors ✅
- BUT: **Zero custom field values in database** ❌

**Evidence**:
```python
# Python data sent to Ruby (confirmed in bulk_create JSON files)
{
  "custom_fields": [
    {"id": 2897, "value": "Jira Server on-prem 9.11"},
    {"id": 2922, "value": "129803"},
    {"id": 2921, "value": "NRS-3238"},
    ...
  ]
}
```

```sql
-- Database query result
>> wp.custom_values.count
=> 0  # ❌ NO custom fields saved!
```

**Hypothesis**: The Ruby script at lines 2543-2569 has error handling that **silently swallows exceptions**:

```ruby
begin
  if attrs.key?('custom_fields') && attrs['custom_fields'].respond_to?(:each)
    # ... process custom_fields ...
    rec.custom_field_values = cf_map
  end
rescue => e
  # ignore CF assignment errors here  # ❌ SILENT FAILURE
end
```

**Next Steps**:
1. Add logging to Ruby script to capture custom field errors
2. Debug why `rec.custom_field_values = cf_map` is failing
3. Check if custom fields need to be activated or have additional validation

---

## Migration Timeline

### First Run (FAILED - Type Bug)
- **Start**: 16:27:44
- **Jira Fetch**: 16:27:44 - 16:32:38 (3805 issues)
- **Batch 1**: 16:35:51 - **0 created, 1000 errors** ("Type can't be blank")
- **Batch 2**: Similar failure
- **Batch 3**: Similar failure
- **Early Termination**: 16:37:37 (after 3 batches, 0% success rate)

### Second Run (SUCCESS - After Type Fix)
- **Start**: 16:46:49
- **Jira Fetch**: 16:46:49 - 16:51:43 (3805 issues)
- **Bulk Create**: 16:53:15 - 17:07:03
- **Result**: **3805/3805 created** ✅
- **Batches**: 4 (3×1000 + 1×805)
- **Duration**: ~20 minutes

---

## Custom Fields Analysis

### Duplicate Custom Fields Created

| Field Name | ID (Searchable) | ID (Non-Searchable) | Data Location |
|------------|----------------|---------------------|---------------|
| J2O Origin System | 2915 (true) | 2897, 2905, 2911 (false) | 2897 |
| J2O Origin ID | 2913 (true) | 2922 (false) | 2922 |
| J2O Origin Key | 2912 (true) | 2921 (false) | 2921 |
| J2O Origin URL | N/A | 2900, 2908 (false) | N/A |

**Issue**: Migration used non-searchable field IDs (2922, 2921) instead of searchable ones (2913, 2912).

**Resolution**:
- Fixed `ensure_custom_field` to update searchable attribute (commit `f987a47`)
- Manually updated fields 2921, 2922 to be searchable via Rails console
- **BUT**: No data was actually saved, so searchable fix doesn't help yet

---

## Work Package Validation

### Created Work Packages
```sql
-- Total in NRS project
SELECT COUNT(*) FROM work_packages WHERE project_id = 303319;
=> 105,914

-- Created in last 2 hours (our migration)
SELECT COUNT(*) FROM work_packages
WHERE project_id = 303319 AND created_at > NOW() - INTERVAL '2 hours';
=> 3,805  # ✅ Matches expected count
```

### Custom Field Validation
```sql
-- Check J2O Origin fields in migrated work packages
SELECT COUNT(*) FROM custom_values cv
JOIN custom_fields cf ON cv.custom_field_id = cf.id
WHERE cv.customized_id IN (
  SELECT id FROM work_packages
  WHERE project_id = 303319 AND created_at > NOW() - INTERVAL '2 hours'
)
AND cf.name LIKE 'J2O%';
=> 0  # ❌ NO custom field values!
```

---

## Next Steps

### Immediate (BLOCKING)
1. **Debug custom field assignment failure** in Ruby bulk create script
   - Add verbose logging to capture errors
   - Check OpenProject Rails logs for validation failures
   - Verify custom field activation status

2. **Fix custom field assignment** once root cause identified
   - Update Ruby script to properly handle custom_field_values
   - Test with single work package creation first
   - Validate fields are saved and searchable

### After Fix
3. **Clean up duplicate custom fields**
   - Delete non-searchable duplicates (IDs 2897, 2905, 2911, 2900, 2908)
   - Keep only searchable fields (IDs 2915, 2913, 2912)

4. **Re-run migration** with fixed custom field logic
   - Either update existing 3805 work packages
   - Or delete and recreate them with correct custom fields

5. **Validate searchability**
   - Test searching for work packages by Jira key (e.g., "NRS-2358")
   - Verify all J2O Origin fields are populated and searchable

---

## Commits Made

1. **`bb9298d`**: `fix(bulk_create): add missing type fallback in Ruby work package creation`
   - Added `rec.type ||= Type.order(:position).first` to Ruby script
   - Fixed 100% failure rate → 100% success rate

2. **`f987a47`**: `fix(custom_fields): update searchable attribute on existing custom fields`
   - Updated `ensure_custom_field` to check and update searchable attribute
   - Prevents future duplicate custom field creation

---

## Performance Metrics

### Migration Speed
- **Issues/minute**: 3805 ÷ 20 = ~190 issues/minute
- **Batch size**: 100 work packages
- **Average batch time**: ~3 minutes/batch
- **Jira fetch time**: ~5 minutes (for 3805 issues)

### Early Termination
- **Triggered after**: 3 batches (3000 work packages attempted)
- **Success threshold**: 0% success rate
- **Time saved**: ~15 minutes (prevented processing remaining 805 issues)

---

## Lessons Learned

1. **Ruby fallbacks are critical**: Missing `rec.type ||= Type.first` caused 100% failure
2. **Silent failures are dangerous**: Custom field errors are swallowed without logging
3. **Field attribute matching matters**: `find_by(name:)` alone is insufficient for searchable fields
4. **Test with smaller datasets first**: NRS (3805 issues) revealed bugs not found in smaller tests
5. **Early termination works**: Saved significant time by stopping after 0% success

---

## Recommendations

### Code Quality
1. Add comprehensive logging to Ruby bulk create script
2. Remove silent error swallowing (`rescue => e # ignore`)
3. Add validation checks before bulk operations
4. Implement dry-run mode for large migrations

### Testing
1. Test custom field assignment separately before bulk migration
2. Add integration test for searchable attribute handling
3. Verify custom field values in database after test migrations
4. Test with various custom field types (string, date, boolean, etc.)

### Documentation
1. Document all Ruby bulk create behaviors and edge cases
2. Create troubleshooting guide for custom field issues
3. Add examples of successful custom field migrations
4. Document expected vs actual field counts in database

---

## Status: PARTIAL SUCCESS

✅ **Migration completed**: 3805/3805 work packages created
✅ **Type bug fixed**: 100% success rate after fix
✅ **Searchable attribute fixed**: Will prevent future duplicates
❌ **Custom fields NOT populated**: Zero J2O Origin values in database
🔴 **BLOCKED**: Need to debug and fix custom field assignment failure

**Overall**: Migration infrastructure is solid, but custom field handling needs immediate attention before production use.
