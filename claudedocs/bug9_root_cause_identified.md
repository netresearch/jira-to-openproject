# Bug #9 - Progressive State Building Root Cause Analysis

## Executive Summary

❌ **CRITICAL BUG CONFIRMED**: Progressive state algorithm is fundamentally broken. Database investigation proves all journals have identical state.

## Problem Statement

Despite implementing a `apply_field_changes_to_state` lambda and progressive state logic, journals show:
- v1-v5: ALL have status_id=2083832, type_id=1041881 (IDENTICAL - no progression!)
- v10-v27: status_id=0, type_id=0 (empty string coercion corruption)

User report confirms: Activity page shows extensive "The changes were retracted." messages.

## Root Cause Investigation

### Database Evidence

```ruby
wp = WorkPackage.find(5581100)
[1,2,3,5,10,15,20,25,27].each {|v|
  j = wp.journals.find_by(version: v)
  puts "v#{v}: status=#{j.data&.status_id}, type=#{j.data&.type_id}"
}

# Output:
v1: status=2083832, type=1041881
v2: status=2083832, type=1041881  ❌ SAME AS V1!
v3: status=2083832, type=1041881  ❌ SAME AS V1!
v5: status=2083832, type=1041881  ❌ SAME AS V1!
v10: status=0, type=1041881        ❌ CORRUPTED!
v15: status=0, type=0              ❌ CORRUPTED!
v20: status=0, type=0              ❌ CORRUPTED!
v25: status=0, type=0              ❌ CORRUPTED!
v27: status=0, type=0              ❌ CORRUPTED!
```

### Migration Logs Evidence

```bash
grep -E "field_changes keys" /tmp/bug9_final_migration.log

# Output:
field_changes keys:                    # EMPTY!
field_changes keys:                    # EMPTY!
field_changes keys:                    # EMPTY!
field_changes keys:                    # EMPTY!
```

Most operations have **EMPTY field_changes**, meaning no changes are applied!

## The Fatal Algorithm Flaw

### Current (BROKEN) Implementation

**File**: `/home/sme/p/j2o/src/ruby/create_work_package_journals.rb` (lines 151-169, 199, 249)

```ruby
# Initialize with FINAL state from work package
current_state = {
  type_id: rec.type_id,          # ← FINAL value
  status_id: rec.status_id,      # ← FINAL value
  subject: rec.subject,          # ← FINAL value
  # ... all FINAL values
}

# For each operation:
current_state = apply_field_changes_to_state.call(current_state, field_changes)
```

**What Happens**:
1. `current_state` starts with ALL FINAL values (status_id=2083832, etc.)
2. Operation 1: `field_changes` is EMPTY → No changes applied → keeps FINAL state
3. Operation 2: `field_changes` is EMPTY → No changes applied → keeps FINAL state
4. Operation 3: `field_changes` has "status":[null,""] → Empty string applied → status_id becomes 0!
5. Result: All journals either have FINAL state OR corrupted state (0)

### Why It's Wrong

**Progressive State Building** means:
- Start from BEGINNING (earliest state)
- Apply changes FORWARD in time
- Each journal reflects state AT THAT POINT IN TIME

**Current code** does:
- Start from END (final state)
- Try to apply changes backward
- But field_changes is mostly EMPTY
- So everything stays at FINAL state

## Technical Analysis

### Why field_changes Is Empty

The Python code (`work_package_migration.py`) creates operations with:
```python
"field_changes": {
    "status": [old_status, new_status],  # Only if status CHANGED
    "assignee": [old_user, new_user],     # Only if assignee CHANGED
    # etc.
}
```

**Key Point**: `field_changes` only contains FIELDS THAT CHANGED in that specific operation. It doesn't contain the COMPLETE state.

### Why Algorithm Fails

Given:
- Final state: {status_id: 2083832, type_id: 1041881, ...}
- Op1 field_changes: {} (empty - creation event)
- Op2 field_changes: {assigned_to_id: [null, 123]} (only assignee changed)
- Op3 field_changes: {status_id: [2083832, 2083833]} (only status changed)

Current algorithm:
```
v1: Start with {status_id: 2083832, ...} → Apply {} → Result: {status_id: 2083832, ...}
v2: Start with {status_id: 2083832, ...} → Apply {assigned_to_id: 123} → Result: {status_id: 2083832, assigned_to_id: 123}
v3: Start with {status_id: 2083832, ...} → Apply {status_id: 2083833} → Result: {status_id: 2083833, ...}
```

**Problem**: v1 and v2 both have the SAME status_id (2083832) because:
1. We started with the FINAL status
2. Op1 and Op2 don't change status
3. So they keep the FINAL status value

But in REALITY:
- v1 might have had status_id=2083830 (at creation)
- v2 might have had status_id=2083831 (after first status change)
- v27 has status_id=2083832 (final state)

## The Correct Solution

### Approach 1: Initialize from FIRST Operation

```ruby
# Extract INITIAL state from first operation's field_changes
# For creation events, field_changes might contain the initial state
# OR we need to reconstruct it from the work package and field_changes

if first_op["operation_type"] == "create"
  # Initialize with values from first operation
  current_state = {
    status_id: first_op_field_changes["status"]&.[](1) || rec.status_id,
    type_id: first_op_field_changes["type"]&.[](1) || rec.type_id,
    # etc.
  }
else
  # Fallback: start with final state but mark fields as unknown
  current_state = { ... }
end
```

### Approach 2: Store Complete State in Each Operation (Python Side)

Modify `work_package_migration.py` to include COMPLETE state snapshot in each operation:

```python
operation = {
    "field_changes": {
        "status": [old, new]  # Changes only
    },
    "complete_state": {
        "status_id": new_status_id,
        "type_id": current_type_id,
        "subject": current_subject,
        # ALL fields at this point in time
    }
}
```

Then Ruby side simply uses `complete_state` for each journal.

### Approach 3: Build State from OLD Values

```ruby
# Start with EMPTY state
current_state = {}

# For FIRST operation, use NEW values from field_changes
if op_idx == 0
  field_changes.each do |k, v|
    new_value = v.is_a?(Array) ? v[1] : v
    current_state[k.to_sym] = new_value
  end
else
  # For subsequent operations, use OLD value from field_changes
  # to understand state BEFORE this operation
  field_changes.each do |k, v|
    old_value = v.is_a?(Array) ? v[0] : nil
    current_state[k.to_sym] = old_value if old_value
  end
end
```

## Recommended Fix

**Use Approach 2** (Complete State Snapshots):

### Python Side (`work_package_migration.py`)

For each changelog entry, track COMPLETE accumulated state:

```python
current_state = {
    "status_id": initial_status,
    "type_id": initial_type,
    "assigned_to_id": None,
    # ... all fields
}

for changelog_entry in issue.changelog:
    # Apply changes to current_state
    for field, (old, new) in changelog_entry.items():
        current_state[field] = new

    # Store operation with complete state snapshot
    operations.append({
        "field_changes": {field: [old, new] for ...},
        "state_snapshot": current_state.copy()  # ← ADD THIS
    })
```

### Ruby Side (`create_work_package_journals.rb`)

Use the complete state snapshot:

```ruby
if op.has_key?("state_snapshot") && !op["state_snapshot"].nil?
  # Use complete state snapshot (preferred)
  journal_state = op["state_snapshot"]
else
  # Fallback to progressive state building (current broken method)
  current_state = apply_field_changes_to_state.call(current_state, field_changes)
  journal_state = current_state
end

journal.data = Journal::WorkPackageJournal.new(journal_state)
```

## Impact

### Severity

🔴 **CRITICAL** - Migration produces completely invalid journal history

### Affected Features

- Activity page unusable ("The changes were retracted." everywhere)
- Journal timeline incorrect
- Historical state cannot be reconstructed
- Auditing and compliance broken

### Data Integrity

- All migrated work packages have WRONG journal history
- Historical decisions cannot be traced
- Cannot roll back to previous states

## Next Steps

1. ✅ Root cause confirmed via database investigation
2. ⏳ Implement complete state snapshot approach in Python
3. ⏳ Update Ruby template to use state snapshots
4. ⏳ Test with NRS-182 to verify progressive state works
5. ⏳ Full NRS project migration with corrected algorithm

## Files Requiring Changes

1. `/home/sme/p/j2o/src/migrations/work_package_migration.py`
   - Add state snapshot tracking in changelog processing
   - Include `state_snapshot` in each operation dict

2. `/home/sme/p/j2o/src/ruby/create_work_package_journals.rb`
   - Use `state_snapshot` if available
   - Remove broken progressive state algorithm

---

**Report Generated**: 2025-11-21
**Investigation Method**: Direct database query + migration log analysis
**Status**: Root cause identified, fix approach determined
**Next**: Implement complete state snapshot solution
