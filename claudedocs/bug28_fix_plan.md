# Bug #28 Structural Fix - Implementation Plan

**Date**: 2025-11-11
**Status**: 📋 READY FOR IMPLEMENTATION

## Summary of Investigation

✅ **Bug #27 FIXED**: Journal timestamps display correctly in UI
⚠️ **Bug #28 PARTIALLY FIXED**: Field mapping works but data structure creates "The changes were retracted."

## Root Cause Confirmed

Database analysis of NRS-182 (WP #5577930) shows **ALL 22 journals have identical journal.data attributes**.

```
Every journal.data initialized from rec (current WP state):
v1-v22: ALL have status_id=2083832, priority_id=1934924, etc.
```

When OpenProject compares consecutive versions, it sees NO changes → displays "The changes were retracted."

## Required Implementation

### Change: Build Historical Chain

Current code (`openproject_client.py:2755-2777`):
```ruby
# WRONG: Every journal gets same base data from current WP
wp_journal_data = Journal::WorkPackageJournal.new(
  status_id: rec.status_id,  # Always uses CURRENT state
  ...
)
```

**New approach:**
```ruby
# Track previous journal data
previous_journal_data = nil

_rails_operations.each_with_index do |op, comment_idx|
  if comment_idx == 0
    # First journal: Use current WP state (correct as-is)
    wp_journal_data = Journal::WorkPackageJournal.new(
      type_id: rec.type_id,
      status_id: rec.status_id,
      ...
    )
  else
    # Subsequent journals: Clone PREVIOUS journal's data
    wp_journal_data = Journal::WorkPackageJournal.new(
      type_id: previous_journal_data.type_id,
      status_id: previous_journal_data.status_id,
      ...
    )
  end

  # Apply field_changes to create state transition
  if field_changes && field_changes.any?
    field_changes.each do |field_name, change_array|
      op_field_name = field_mapping[field_name] || field_name
      new_value = change_array[1]
      wp_journal_data.send("#{op_field_name}=", new_value)
    end
  end

  journal.data = wp_journal_data
  journal.save(validate: false)

  # Store for next iteration
  previous_journal_data = wp_journal_data
end
```

## Implementation Steps

1. **Lines 2740-2783**: Add `previous_journal_data = nil` before loop
2. **Lines 2755-2777**: Replace with conditional:
   - If `comment_idx == 0`: Initialize from `rec` (current logic)
   - If `comment_idx > 0`: Clone from `previous_journal_data`
3. **After line 2810**: Store `previous_journal_data = wp_journal_data`

## Expected Result

**Before:**
```
v1.status_id = 2083832 (current)
v2.status_id = 2083832 (current)  → No diff → "changes retracted"
v3.status_id = 2083832 (current)  → No diff → "changes retracted"
```

**After:**
```
v1.status_id = 2083832 (initial)
v2.status_id = 2083833 (v1 + changes)  → DIFF v2-v1 → "Status changed"
v3.status_id = 2083834 (v2 + changes)  → DIFF v3-v2 → "Status changed"
```

## Testing Plan

1. Clean up existing 10 test work packages
2. Run migration with structural fix
3. Verify in database: `journal.data` attributes differ between consecutive versions
4. Verify in UI: Actual field changes display instead of "The changes were retracted."

## Success Criteria

- ✅ Each journal.data differs from previous version
- ✅ OpenProject UI shows specific field changes
- ✅ Zero "The changes were retracted." messages
- ✅ All 22 NRS-182 journals display correctly

## Files to Modify

- `src/clients/openproject_client.py:2740-2825`

## Risk Assessment

**LOW RISK**
- Only affects journal.data initialization logic
- Field_changes application logic unchanged
- Fallback comment system (Bug #30) still works
- Defensive error handling (Bug #29) still in place

## Next Actions

1. Implement structural fix in `openproject_client.py`
2. Test with 10 NRS issues
3. Verify UI displays correctly
4. Proceed with full NRS migration (3,828 issues)
