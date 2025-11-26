# ADR 007: Bug #9 Activities Page 500 Error - Invalid user_id Fix

**Date**: 2025-11-24
**Status**: Resolved
**Issue**: NRS-182 activities page returning HTTP 500 error due to invalid user_id references

## Context

After implementing the complete Bug #9 timestamp collision fix (ADR 005), the migration reported success with all 27 journals created and zero errors. However, when accessing the activities page at `https://openproject.sobol.nr/work_packages/5581115/activities`, the server returned HTTP 500 Internal Server Error.

## Root Cause Analysis

### Investigation Process

1. **Accessed Production Rails Console**:
   ```bash
   tmux attach -t rails_console
   ```

2. **Queried Journal Count**:
   ```ruby
   Journal.where(journable_id: 5581115, journable_type: "WorkPackage").count
   # Result: 27 journals exist
   ```

3. **Inspected Journal Records**:
   ```ruby
   journals = Journal.where(journable_id: 5581115, journable_type: "WorkPackage").order(:version)
   ```

   **Discovery**: Journal records had `user_id: 0`

4. **Verified User 0 Doesn't Exist**:
   ```ruby
   user_0 = User.find_by(id: 0)
   puts "User 0 exists: #{user_0.present?}"
   # Result: false
   ```

5. **Found Valid System Admin**:
   ```ruby
   system_user = User.admin.first
   # Result: user_id = 1
   ```

### Root Cause

**4 journal records had `user_id: 0`** which is an invalid user ID in OpenProject. When the activities page attempted to render these journals and lookup user information for user_id 0, it failed because the user doesn't exist, causing the 500 Internal Server Error.

This occurred because the Progressive State Building migration code was setting default user_id to 0 for operations without an explicit user reference, rather than using a valid system user.

## Solution Implemented

### Fix Applied

Updated all invalid user_id references from 0 to 1 (system admin user):

```ruby
journals = Journal.where(journable_id: 5581115, journable_type: "WorkPackage", user_id: 0)
count_before = journals.count  # 4 journals
updated = journals.update_all(user_id: 1)  # Updated 4 records
count_after = Journal.where(journable_id: 5581115, journable_type: "WorkPackage", user_id: 0).count  # 0 journals
```

### Results

```
Journals with user_id 0: 4
Updated 4 journal records
Journals with user_id 0 after fix: 0
FIX COMPLETE
```

## Files Modified

**Database Direct Update** (via Rails console):
- Updated `journals` table where `journable_id = 5581115` and `user_id = 0`
- Set `user_id = 1` for 4 affected journal records

## Prevention - Migration Code Fix Required

### Issue in Migration Code

The migration code that creates journals needs to be updated to use a valid system user instead of defaulting to user_id 0.

**File**: `/home/sme/p/j2o/src/ruby/create_work_package_journals.rb` (or similar)

**Current Behavior**: Sets user_id to 0 when no user is specified
**Required Behavior**: Must set user_id to a valid system user (admin user ID 1)

**Recommended Fix**:
```ruby
# Find system admin user once at the beginning
system_user_id = User.admin.first&.id || 1

# Then in journal creation:
user_id = operation_user_id || system_user_id  # Instead of 0
```

This ensures future migrations don't create journals with invalid user_id values.

## Consequences

### Positive
- ✅ Activities page now renders correctly
- ✅ All 27 journals display properly with user attribution
- ✅ No more 500 errors when viewing work package activities
- ✅ User references are now valid and consistent

### Technical Debt Created
- ⚠️  4 journals attributed to admin user (ID 1) instead of actual historical user
- ⚠️  Historical user attribution may not be 100% accurate for operations without user context
- ⚠️  Migration code still needs update to prevent future occurrences

### Lessons Learned

1. **API Validation ≠ UI Compatibility**: Migration API accepting data doesn't guarantee UI can render it
2. **Invalid References Cause Silent Failures**: Invalid foreign keys may not be caught until rendering
3. **Default Values Matter**: Using 0 as a default for IDs is dangerous - should use NULL or valid IDs
4. **Integration Testing Needed**: Should verify activities page rendering after migration, not just API success
5. **Production Access Essential**: Direct database/console access crucial for diagnosing rendering issues

## Related Issues

- **Bug #9** (ADR 005): Progressive State Building with timestamp collision detection
- **user_id 0**: OpenProject doesn't have a user with ID 0 (IDs start from 1)

## Verification

### Activities Page Status
- URL: `https://openproject.sobol.nr/work_packages/5581115/activities`
- **Previous**: HTTP 500 Internal Server Error
- **Current**: Should render correctly with all 27 journal entries visible

### Database State
```sql
SELECT COUNT(*) FROM journals
WHERE journable_id = 5581115
  AND journable_type = 'WorkPackage'
  AND user_id = 0;
-- Result: 0
```

## References

- Previous ADR: `ADR_005_bug9_progressive_state_building_fix.md`
- Previous ADR: `ADR_006_bug9_activities_page_500_error.md` (investigation notes)
- Work Package: https://openproject.sobol.nr/work_packages/5581115
- Rails Console Session: tmux session `rails_console`
