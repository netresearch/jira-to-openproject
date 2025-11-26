# ADR-009: Bug #12 - State Snapshot Ordering Fix

## Status
IMPLEMENTED - Verified

## Context
During NRS-182 journal migration validation, we discovered that certain journal versions (v14, v18, v27) were showing incorrect type_id and status_id values, despite Python generating correct state snapshots.

## Problem Analysis

### Root Cause
**Python assigns state_snapshots based on array index, but Ruby re-sorts operations by timestamp before iterating.**

The flow was:
1. Python builds `_rails_operations` array:
   - [0-4]: Timestamp operations (set_created_at, set_journal_created_at, set_journal_user, set_updated_at)
   - [5-26]: Journal entries (comments/changelogs)

2. Python assigns state_snapshots in REVERSE order (26→0):
   - ops[26] gets FINAL state (Access/Closed)
   - ops[3] (set_updated_at, timestamp=2019-01-04) gets NEAR-INITIAL state
   - ops[2] (set_journal_user, no timestamp) gets NEAR-INITIAL state
   - ops[0] gets INITIAL state (Task/Open)

3. Ruby SORTS all operations by timestamp:
   ```ruby
   ops = rails_ops.sort_by do |op|
     created_at_str = op['created_at'] || op[:created_at] || op['timestamp'] || op[:timestamp]
     created_at_str ? Time.parse(created_at_str).utc : Time.now.utc
   end
   ```
   - set_updated_at (2019-01-04) moves to position ~18
   - set_journal_user (no timestamp → Time.now) moves to END (position 26)

4. Result: state_snapshots misaligned with journal versions
   - v18: Received set_updated_at's state_snapshot (near-initial Task/Open) ❌
   - v27: Received set_journal_user's state_snapshot (near-initial Task/Open) ❌

### Evidence
Work Package #5581124 journals BEFORE fix:
```
v14 (2017-11-30): type=Task, status=Open  ❌ (should be Closed)
v18 (2019-01-04): type=Task, status=Open  ❌ (should be Closed)
v27 (2024-08-22): type=Task, status=Open  ❌ (should be Access/Closed)
```

## Solution

### File Modified
- `src/migrations/work_package_migration.py` (lines 1859-1867)

### Change
Added sorting of operations by timestamp BEFORE assigning state_snapshots:

```python
# BUG #12 FIX (CRITICAL): Sort operations by timestamp BEFORE assigning state_snapshots
# Python assigns state_snapshots by array index, but Ruby re-sorts by timestamp.
# Without this sort, timestamp operations (set_updated_at, set_journal_user) end up
# at wrong positions after Ruby's sort, causing state_snapshot misalignment.
# This ensures Python's assignment order matches Ruby's processing order.
work_package["_rails_operations"].sort(
    key=lambda op: op.get("created_at") or op.get("timestamp") or "9999-12-31T23:59:59"
)
```

### Sorting Key Logic
- Check `created_at` first (standard operation timestamp)
- Fall back to `timestamp` (alternative field)
- Default to `"9999-12-31T23:59:59"` for operations without timestamps (moves to end, matching Ruby's `Time.now.utc` fallback)

## Validation Results

### Work Package #5584958 journals AFTER fix:
```
v1-v10:  Task/Open     ✅ (initial state)
v11:     Task/Closed   ✅ (status change 2011-09-09)
v12-v18: Task/Closed   ✅ (maintained correctly!)
v19:     Access/Closed ✅ (type change 2019-01-04)
v20-v27: Access/Closed ✅ (final state correct!)
```

All 27 journals now correctly show:
- Initial state: Task/Open
- After status change (2011-09-09): Task/Closed
- After type change (2019-01-04): Access/Closed
- Final state: Access/Closed

### Comparison with Jira Ground Truth
| Event | Date | Expected State | OpenProject Result |
|-------|------|----------------|-------------------|
| Created | 2011-08-18 | Task/Open | ✅ v1-v10: Task/Open |
| Status change | 2011-09-09 | Task/Closed | ✅ v11-v18: Task/Closed |
| Type change | 2019-01-04 | Access/Closed | ✅ v19-v27: Access/Closed |

## Technical Analysis

### Why This Fix Works
1. **Aligns producer with consumer**: Python now sorts operations the same way Ruby will process them
2. **No Ruby changes needed**: The fix is entirely in Python's data preparation stage
3. **Deterministic behavior**: The fallback string `"9999-12-31T23:59:59"` ensures consistent ordering for operations without timestamps

### Consensus Validation
Multi-model consensus was achieved:
- **gemini-2.5-pro (FOR stance)**: 10/10 confidence
- **gemini-2.5-flash (AGAINST stance)**: 9/10 confidence

Both models validated:
- Root cause analysis is correct
- Fix is minimally invasive (single line)
- Sorting key mirrors Ruby's logic correctly
- Critical for data integrity
- Industry best practice (align producer with consumer)

### Performance Impact
- Sorting ~27 operations is O(n log n) ≈ negligible
- Single operation per work package
- No memory overhead

## Related ADRs
- ADR-005: Bug #9 Progressive State Building Fix
- ADR-006: Bug #9 Activities Page 500 Error
- ADR-007: Bug #9 User ID Zero Fix (Bug #13)
- ADR-008: Bug #11 Changelog ID Extraction Fix

## Date
2025-11-25

## Bug Status Summary
- Bug #9: FIXED (progressive state building)
- Bug #10: FIXED (initial state initialization)
- Bug #11: FIXED (changelog ID extraction)
- Bug #12: FIXED (state snapshot ordering) ← THIS ADR
- Bug #13: FIXED (user_id=0 causing 500 error)
