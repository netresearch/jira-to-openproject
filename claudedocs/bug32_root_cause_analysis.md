# Bug #32: Root Cause Analysis - Historical Journal Chain Not Working

## Problem Statement

After implementing Bug #27 (timestamp fix), Bug #28 (field mapping), and Bug #31 (created_journal_count fix), the historical journal chain is still NOT working correctly.

### Evidence
- NRS-182 has 22 journals created
- Journals v1-v9: ALL have IDENTICAL data (status_id=2083832, same as current WP state)
- Journals v10-v22: Have NULL values (status_id=0)

## Root Cause Discovered

### The Core Issue

The Bug #28 structural fix initializes EVERY journal from `rec` (the CURRENT work package state), then applies field_changes. This creates a fundamental flaw:

```ruby
# Lines 2762-2785: FIRST journal
if created_journal_count == 0
  wp_journal_data = Journal::WorkPackageJournal.new(
    status_id: rec.status_id,  # ← Current WP state (e.g., "Resolved")
    # ... all other fields from rec
  )
end

# Lines 2787-2825: SUBSEQUENT journals
else
  wp_journal_data = Journal::WorkPackageJournal.new(
    status_id: previous_journal_data.status_id,  # ← Previous journal (also from rec!)
    # ... cloned from previous
  )
end

# Lines 2842-2861: Apply field_changes
field_changes.each do |field_name, change_array|
  new_value = change_array[1]  # NEW value from changelog
  wp_journal_data.send("#{op_field_name}=", new_value)
end
```

### Why This Fails

1. **First journal (v1)**:
   - Initializes from `rec` (current state: status="Resolved")
   - Applies field_changes from first changelog entry
   - BUT changelog might say "status changed from Open → In Progress"
   - Result: Journal data gets overwritten with "In Progress" (not current state)
   - Except... evidence shows v1-v9 all have SAME current state! So field_changes aren't being applied!

2. **Subsequent journals (v2-v9)**:
   - Clone from previous_journal_data
   - Apply their own field_changes
   - All end up with identical data from current state

### Missing Piece

The evidence (all journals v1-v9 have identical current state) suggests that **field_changes are NOT being applied** or **field_changes data is missing/empty** for most journal entries.

## Verification Needed

Need to check NRS-182's journal entries in Python to see if field_changes data exists:

```python
issue_data = jira_client.get_issue('NRS-182')
all_journal_entries = issue_data.get('_rails_operations', [])

for entry in all_journal_entries:
    field_changes = entry.get('field_changes', {})
    print(f"Entry {entry.get('created_at')}: {len(field_changes)} field changes")
    if field_changes:
        print(f"  Fields changed: {list(field_changes.keys())}")
```

## Hypothesis

**Most likely scenario**: The first 9 journal entries are COMMENTS ONLY (no field_changes), which explains:
- Why v1-v9 all have identical data (initialized from `rec`, no changes applied)
- Why v10-v22 have NULL values (Bug #31 fix broke when created_journal_count finally reached 0 for the first CHANGELOG entry)

## The Correct Solution

### Option A: Build Historical Chain BACKWARD

Start from the OLDEST state and build forward:

1. Calculate initial state by reverse-applying all field_changes from current state
2. Create first journal with this calculated initial state
3. For each subsequent journal, clone previous and apply field_changes[1] (new value)

### Option B: Use field_changes[0] (OLD value) for Initialization

Instead of initializing from `rec`, initialize each journal from the OLD value of field_changes:

```ruby
if created_journal_count == 0
  # Initialize from OLD values of first field_changes
  wp_journal_data = create_from_old_values(field_changes)
else
  # Clone from previous and apply NEW values
  wp_journal_data = clone_and_apply_new_values(previous_journal_data, field_changes)
end
```

### Option C: Separate Comments from Changelogs

Comments (no field_changes) should clone from `rec` or previous journal.
Changelogs (with field_changes) should build historical chain.

## Next Steps

1. Verify field_changes data exists in NRS-182 journal entries
2. Implement Option A or B based on findings
3. Test with NRS-182 to verify historical chain works
4. Run full migration

## Files Affected

- `/home/sme/p/j2o/src/clients/openproject_client.py` lines 2740-2900
