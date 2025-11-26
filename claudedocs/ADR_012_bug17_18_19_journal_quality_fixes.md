# ADR-012: Bug #17, #18, #19 - Journal Quality Fixes

## Status
Bug #17: IMPLEMENTED - Verified
Bug #18: IMPLEMENTED - Verified
Bug #19: IMPLEMENTED - Verified

## Context
During NRS-182 journal migration validation, three related issues were discovered affecting journal quality:
1. "Deleted user" appearing for timestamp-only operations
2. "The changes were retracted" messages for empty operations
3. "The changes were retracted" messages for no-change field mappings

## Problem Analysis

### Bug #17: "Deleted user" Attribution

**Root Cause**: Timestamp operations (`set_created_at`, `set_journal_created_at`) don't include a `user_id` field. The Ruby code converted `nil.to_i = 0` to DeletedUser (ID 2).

**Evidence**:
```ruby
# OLD CODE:
user_id = (op['user_id'] || op[:user_id]).to_i
user_id = 2 if user_id == 0  # Fallback to DeletedUser
```

### Bug #18: Empty Operations Creating Phantom Journals

**Root Cause**: Only `create_comment` operations were being skipped when empty. Other operation types (including timestamp-only operations) with no meaningful content were still creating journals.

**Evidence** from debug logs:
```
op_idx=16 type=set_journal_created_at notes=nil field_changes=nil
→ Created phantom journal with "The changes were retracted"
```

### Bug #19: No-Change Field Mappings

**Root Cause**: Python's `_process_changelog_item` was generating field_changes like `{"assigned_to_id": [None, None]}` - technically NOT empty, but representing no actual change. These bypassed Ruby's empty operation skip logic.

**Evidence** from debug logs:
```
op_idx=3 type=create_comment field_changes={"assigned_to_id": [None, None]}
→ Ruby sees non-empty field_changes, creates journal
→ OpenProject sees no actual change, displays "The changes were retracted"
```

## Solution

### Bug #17 Fix: Use WP Author as Fallback
**File**: `src/ruby/create_work_package_journals.rb` (lines 300-306)

```ruby
# BUG #13 FIX: Fallback to work package author if user_id is 0 or nil
# BUG #17 FIX: Use work package author_id as fallback instead of DeletedUser (ID 2)
raw_user_id = (op['user_id'] || op[:user_id]).to_i
fallback_user_id = rec.author_id && rec.author_id > 0 ? rec.author_id : 2
user_id = raw_user_id > 0 ? raw_user_id : fallback_user_id
```

### Bug #18 Fix: Skip Timestamp-Only and All Empty Operations
**File**: `src/ruby/create_work_package_journals.rb` (lines 276-302)

```ruby
# BUG #18 FIX: Skip timestamp-only operations that don't create meaningful journals
timestamp_only_ops = ['set_created_at', 'set_updated_at', 'set_closed_at', 'set_journal_created_at']
if timestamp_only_ops.include?(op_type) && op_idx != 0
  puts "J2O bulk item #{idx}: SKIP #{op_type} (Bug #18 fix - timestamp-only)" if verbose
  next
end

# BUG #15 + BUG #16 + BUG #18 FIX: Skip ALL operations with no meaningful content
notes_preview = op['notes'] || op[:notes]
field_changes_preview = op['field_changes'] || op[:field_changes]

is_empty_operation = (notes_preview.nil? || notes_preview.to_s.strip.empty?) &&
                     (field_changes_preview.nil? || field_changes_preview.empty?)
if is_empty_operation && op_idx != 0
  puts "J2O bulk item #{idx}: SKIP empty #{op_type} (Bug #18 fix - no content)" if verbose
  next
end
```

### Bug #19 Fix: Skip No-Change Mappings in Python
**File**: `src/migrations/work_package_migration.py` (multiple locations in `_process_changelog_item`)

Added `if from_value == to_value: return None` checks to all field mapping paths:

```python
# For user fields (assignee, reporter):
if from_id == to_id:
    return None

# For issuetype:
if from_op_id == to_op_id:
    return None

# For status:
if from_op_id == to_op_id:
    return None

# For priority:
if from_value == to_value:
    return None

# For time estimate fields:
if from_hours == to_hours:
    return None

# For generic fields:
if from_value == to_value:
    return None
return {op_field: [from_value, to_value]}
```

## Validation Results

### Before Fixes
- Journals: 26+
- "Deleted user" in v1, v2
- "The changes were retracted" messages: 3-4 journals

### After Fixes
- Journals: 23
- "Deleted user": **0**
- "The changes were retracted": **0**

### Test Output
```
WP #5584967: Rechner & Zugang TDNA Techniker
Total journals: 23

v1 2011-08-18 User: Michael Ablass (creation)
v2-v23: All have meaningful notes or field changes

RETRACTED: 0/23
Journals with 'Deleted user': 0
```

## Technical Impact

### Defense-in-Depth Strategy
The fixes implement a layered approach:
1. **Python Layer (Bug #19)**: Don't generate no-change field mappings
2. **Ruby Layer (Bug #18)**: Skip empty operations and timestamp-only ops
3. **Ruby Layer (Bug #17)**: Use author_id fallback for missing user_id

### Performance
- Fewer operations sent from Python to Ruby
- Fewer journal entries created
- No negative impact on migration speed

## Related ADRs
- ADR-005: Bug #9 Progressive State Building Fix
- ADR-010: Bug #14 & #15 Phantom Journal Elimination
- ADR-011: Bug #16 Unmapped Field Preservation

## Date
2025-11-26

## Bug Status Summary
- Bug #9: FIXED (progressive state building)
- Bug #10: FIXED (initial state initialization)
- Bug #11: FIXED (changelog ID extraction)
- Bug #12: FIXED (state snapshot ordering)
- Bug #13: FIXED (user_id=0 causing 500 error)
- Bug #14: FIXED (set_journal_user phantom journals)
- Bug #15: SUPERSEDED by Bug #16
- Bug #16: FIXED (unmapped field preservation)
- Bug #17: FIXED ("Deleted user" attribution) ← THIS ADR
- Bug #18: FIXED (empty operations creating phantom journals) ← THIS ADR
- Bug #19: FIXED (no-change field mappings) ← THIS ADR
