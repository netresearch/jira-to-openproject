# ADR 004: Bug #32 - Journal Validity Period Constraint Fix

**Status**: Resolved
**Date**: 2025-11-13
**Issue**: NRS-182 journals not being created during migration

## Problem Statement

During work package migration, only the creation journal was being saved to the database, while all historical journals (comments, field changes) were silently failing. For NRS-182, which should have 23 journals (1 creation + 22 history), only 1 journal was persisted.

## Investigation Process

### Data Flow Verification

1. **Python Layer** ✅
   - Confirmed `_rails_operations` data exists (22 operations for NRS-182)
   - Verified JSON payload contains journal data (107 type matches)

2. **Ruby Layer** ✅
   - Confirmed Ruby receives all operations (27 operations including creation)
   - Verified code enters journal creation block (`[RUBY] INSIDE JOURNAL` logged)
   - Work package creation succeeds

3. **Database Layer** ❌
   - Journals fail to save with no visible error in truncated logs
   - Initial logging limit of 500 chars masked the actual error

### Root Cause Discovery

**Step 1**: Increased logging limit from 500 to 10,000 characters (line 2901)

**Step 2**: Found error in full logs:
```
PG::CheckViolation: ERROR: new row for relation "journals" violates check constraint "journals_validity_period_not_empty"
DETAIL: Failing row contains (..., ["2011-08-18 11:54:44.828308+00","2025-11-13 13:50:45.295003+00"], ...)
```

**Step 3**: Examined PostgreSQL constraint:
```sql
CHECK (((NOT isempty(validity_period)) AND (validity_period IS NOT NULL)))
```

**Step 4**: Analyzed successful creation journal:
```ruby
validity_period.begin: 2025-11-13 13:50:42 UTC
validity_period.end: (empty)  # Endless range!
```

**Step 5**: Identified problematic code (line 2734):
```ruby
journal.validity_period = period_start..Time.now.utc  # Bounded range - WRONG!
```

## Root Cause

### Primary Issue: Auto-Created Journal v1

**Critical Discovery**: OpenProject automatically creates journal v1 via a `before_create` callback when a WorkPackage is instantiated. This was confirmed by examining the work package creation:

```ruby
# When WorkPackage.new is called, OpenProject runs:
# app/models/work_package.rb: before_create :add_journal

# Investigation revealed:
wp = WorkPackage.find(5577987)
journals = Journal.where(journable_id: wp.id, journable_type: 'WorkPackage')
puts "Journals: #{journals.count}"  # Result: 1 (auto-created v1)
```

**The Real Problem**: The migration code was attempting to CREATE journal v1 when it already existed, causing all subsequent journal operations to fail silently.

### Secondary Issue: Validity Period Constraint

OpenProject's journal system requires **endless ranges** for `validity_period` to represent "valid from this point forward". The constraint `journals_validity_period_not_empty` rejects bounded ranges with both start and end values.

The migration code was creating bounded ranges (`start..end`), which violate the constraint and cause silent save failures in the bulk operation context.

## Solution

### Step 1: Fix Journal v1 UPDATE vs CREATE

Modified journal creation logic to handle the auto-created journal v1:

**Key Changes**:
- **First operation (op_idx == 0)**: UPDATE existing journal v1 instead of creating
- **Subsequent operations (op_idx > 0)**: CREATE new journals v2, v3, etc.

```ruby
if op_idx == 0
  # FIRST OPERATION: Update existing auto-created journal v1
  journal = Journal.where(
    journable_id: rec.id,
    journable_type: 'WorkPackage',
    version: 1
  ).first

  if journal
    journal.user_id = user_id
    journal.notes = notes
    # ... update journal data and timestamps
  end
else
  # SUBSEQUENT OPERATIONS: Create new journals v2, v3, etc.
  max_version = Journal.where(journable_id: rec.id, journable_type: 'WorkPackage').maximum(:version) || 0
  journal = Journal.new(
    journable_id: rec.id,
    journable_type: 'WorkPackage',
    user_id: user_id,
    notes: notes,
    version: max_version + 1
  )
  # ... create journal data
end
```

### Step 2: Fix Validity Period Constraint

Changed validity_period from bounded to endless ranges:

**Before**:
```ruby
journal.validity_period = period_start..Time.now.utc  # Bounded - WRONG!
```

**After**:
```ruby
# BUG #32 FIX: Use endless range (no end value) to match OpenProject's native journal pattern
journal.validity_period = (period_start..)  # Endless - CORRECT!
```

Ruby endless range syntax `(start..)` creates a range with no end value, matching OpenProject's native journal creation behavior and satisfying the `journals_validity_period_not_empty` constraint.

### Step 3: Architectural Refactoring - Extract Ruby to External Files

**Problem**: Maintaining complex Ruby code embedded in Python strings created issues with:
- String escaping complexity (quotes, newlines, interpolation)
- Difficulty editing and debugging
- Code duplication across different journal operations

**Solution**: Extracted Ruby journal logic to `/home/sme/p/j2o/src/ruby/create_work_package_journals.rb`

**Changes Made**:

1. **Created `/home/sme/p/j2o/src/ruby/create_work_package_journals.rb`** (215 lines)
   - Contains complete journal creation logic
   - Includes Bug #32 fix (UPDATE v1, CREATE v2-v23)
   - Handles field_changes for proper journal.data population
   - Uses endless ranges for validity_period

2. **Modified `/home/sme/p/j2o/src/clients/openproject_client.py`**:
   - **Line 2449-2460**: Added file transfer logic to copy .rb file to container
   ```python
   if model == "WorkPackage":
       local_journal_rb = Path(__file__).parent.parent / "ruby" / "create_work_package_journals.rb"
       container_journal_rb = Path("/tmp") / "create_work_package_journals.rb"
       if local_journal_rb.exists():
           self.transfer_file_to_container(local_journal_rb, container_journal_rb)
   ```

   - **Line 2641-2754 → 2641-2642**: Replaced ~113 lines of embedded Ruby with single load statement
   ```ruby
   # BUG #32 FIX: Load journal creation logic from external .rb file
   load '/tmp/create_work_package_journals.rb'
   ```

**Benefits**:
- Eliminated string escaping issues
- Improved code maintainability and readability
- Enabled easier debugging and testing of Ruby logic
- Separated concerns (Python for orchestration, Ruby for OpenProject API)

## Test Results

**Test Case**: NRS-182 migration with Bug #32 fix

**Expected**: 23 journals (1 creation + 22 history)

**Result**: ✅ **23 journals created successfully**

```sql
SELECT COUNT(*) FROM journals WHERE journable_id = 5577988 AND journable_type = 'WorkPackage';
-- Result: 23
```

**Verification Query**:
```sql
SELECT id, version, created_at, validity_period
FROM journals
WHERE journable_id = 5577988
ORDER BY version;
```

All journals show endless range pattern:
```
validity_period: ["2011-08-18 11:54:44.828308+00",)
                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^  ← Start only, no end
```

## Impact

- **Bug #32**: RESOLVED - Journals now persist correctly
- **Data Integrity**: Historical data preserved with proper timestamps
- **OpenProject Compliance**: Follows native journal validity_period pattern
- **Silent Failures**: Eliminated by matching constraint requirements

## Related Issues

- Bug #28: Journal timestamp conversion (previously fixed)
- Bug #27: Journal field changes (previously fixed)
- All three journal bugs now resolved for complete history migration

## Technical Notes

**PostgreSQL Range Types**: OpenProject uses `tstzrange` (timestamp with timezone range) for `validity_period`. Endless ranges are represented as `[start,)` notation.

**Ruby Range Syntax**:
- Bounded: `start..end` → Both boundaries defined
- Endless: `(start..)` → Open-ended from start point
- Note: Parentheses required for endless range in Ruby 2.6+

**Bulk Operation Context**: Journal save failures in `_rails_operations` are silent unless explicitly logged. The bulk create operation continues even when individual journal saves fail, requiring careful error handling.
