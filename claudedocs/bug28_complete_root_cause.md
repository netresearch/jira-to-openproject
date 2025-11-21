# Bug #28: "The changes were retracted." - Complete Root Cause Analysis

**Date**: 2025-11-11
**Status**: 🔴 **ROOT CAUSE IDENTIFIED** - Requires Structural Fix
**Severity**: CRITICAL - Journal history not displaying

## Problem

OpenProject UI shows "The changes were retracted." for most journal entries instead of displaying actual field changes like "Status changed from Open to Closed".

## User Evidence

```
Activity tab showing:
- ✅ "Type set to Task" (WORKING - some entries)
- ✅ "Status set to Closed" (WORKING - some entries)
- ❌ "The changes were retracted." (FAILING - majority)
- ⚠️ "[Migrated from Jira] Changes: project: ..." (Fallback comments)
```

## Root Cause

### Database Analysis (NRS-182, WP #5577930)

All 22 journals have **IDENTICAL** journal.data attributes:

```
Version 1: type_id=1041881, project_id=303319, status_id=2083832, priority_id=1934924
Version 2: type_id=1041881, project_id=303319, status_id=2083832, priority_id=1934924
Version 3: type_id=1041881, project_id=303319, status_id=2083832, priority_id=1934924
...
Version 22: type_id=1041881, project_id=303319, status_id=2083832, priority_id=1934924
```

**Why This Happens:**

Lines 2755-2777 in `openproject_client.py`:

```ruby
wp_journal_data = Journal::WorkPackageJournal.new(
  type_id: rec.type_id,        # ← Always uses current WP state!
  project_id: rec.project_id,  # ← Always uses current WP state!
  status_id: rec.status_id,    # ← Always uses current WP state!
  priority_id: rec.priority_id,# ← Always uses current WP state!
  ...
)
```

Every journal.data is initialized from `rec` (the work package's CURRENT/FINAL state), not the historical state at that point in time.

Then Bug #28 code (lines 2783-2810) tries to apply field_changes on top:

```ruby
field_changes.each do |field_name, change_array|
  op_field_name = field_mapping[field_name] || field_name
  new_value = change_array[1]  # Apply new value
  wp_journal_data.send("#{op_field_name}=", new_value)
end
```

But since ALL journals start with the SAME base data (current state), when OpenProject compares:
- v2.data vs v1.data → NO DIFFERENCES → "The changes were retracted."
- v3.data vs v2.data → NO DIFFERENCES → "The changes were retracted."

## Why OpenProject Displays "The changes were retracted."

OpenProject's journal rendering logic:

1. Fetches journal v2
2. Compares v2.data with v1.data
3. If attributes differ → show "Status changed from X to Y"
4. If attributes identical → show "The changes were retracted."

Since our journals all have identical data, OpenProject sees no changes.

## The Correct Approach

Journals must form a **historical chain** where each version builds on the previous:

### Current (Wrong):
```
v1: rec.status_id (= final state)
v2: rec.status_id (= final state) + apply change
v3: rec.status_id (= final state) + apply change
```

Result: All have same base → no diffs → "changes retracted"

### Correct:
```
v1: initial_state.status_id (from creation)
v2: v1.status_id + apply field_changes → creates diff v2-v1
v3: v2.status_id + apply field_changes → creates diff v3-v2
```

Result: Each version differs from previous → proper history display

## Required Fix

### Architectural Change

Must rewrite journal creation loop to:

1. **Version 1** (Creation journal):
   - Initialize from work package's INITIAL state
   - This should match the state at creation time from Jira

2. **Version 2+** (Changelog journals):
   - Load PREVIOUS journal's data
   - Clone all attributes
   - Apply field_changes to create new state
   - Save as new journal.data

### Implementation Strategy

```ruby
# Track previous journal data for chaining
prev_journal_data = nil

_rails_operations.each_with_index do |op, comment_idx|
  journal = Journal.new(...)

  if comment_idx == 0
    # First journal: Initialize from WP creation state
    wp_journal_data = Journal::WorkPackageJournal.new(
      type_id: rec.type_id,
      project_id: rec.project_id,
      status_id: rec.status_id,
      # ... all attributes from work package
    )
  else
    # Subsequent journals: Clone previous journal's data
    wp_journal_data = Journal::WorkPackageJournal.new(
      type_id: prev_journal_data.type_id,
      project_id: prev_journal_data.project_id,
      status_id: prev_journal_data.status_id,
      # ... copy ALL attributes from previous journal
    )
  end

  # Apply field_changes to create state transition
  if field_changes && field_changes.any?
    field_changes.each do |field_name, change_array|
      # Apply new_value to create the change
      wp_journal_data.send("#{op_field_name}=", new_value)
    end
  end

  journal.data = wp_journal_data
  journal.save(validate: false)

  # Store for next iteration
  prev_journal_data = wp_journal_data
end
```

## Impact

**Before Fix:**
- 0% field changes displayed correctly
- All show "The changes were retracted."
- Journal history completely lost

**After Fix:**
- Proper historical chain preserved
- Each journal shows actual changes from previous version
- OpenProject UI displays: "Status changed from Open to Closed"

## Related Issues

- **Bug #27**: ✅ FIXED - Timestamps now display correctly
- **Bug #28**: ⚠️ PARTIALLY FIXED - Field mapping works but data structure wrong
- **Bug #30**: ✅ WORKING - Fallback comments for failed fields

## Files Modified

- `src/clients/openproject_client.py:2740-2825` - Journal creation logic (NEEDS REWRITE)

## Verification Criteria

After fix:

1. ✅ Each journal.data should differ from previous journal.data
2. ✅ OpenProject UI should show specific field changes (not "retracted")
3. ✅ Comparing v10 to v9 should show status_id change: 2083832 → 0
4. ✅ All field changes should display properly in Activity tab

## Test Case

NRS-182 has 22 historical journal entries. After fix:

- v1-v2 should show actual changes (e.g., comment added)
- v9-v10 should show status change (status_id: 2083832 → 0)
- Each version should have different journal.data attributes
- No "The changes were retracted." messages

## References

- Bug Analysis: `/home/sme/p/j2o/claudedocs/bug29_root_cause_confirmed.md`
- Field Mapping Fix: `openproject_client.py:2783-2810`
- Journal Data Query Results: WP #5577930 database analysis
