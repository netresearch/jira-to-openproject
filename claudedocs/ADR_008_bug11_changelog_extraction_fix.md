# ADR-008: Bug #11 - Changelog ID Extraction Fix

## Status
IMPLEMENTED - Partial Success

## Context
During NRS-182 journal migration validation, we discovered that changelog field changes were storing string display names instead of integer IDs.

## Problem Analysis

### Root Cause
In `src/utils/enhanced_audit_trail_migrator.py`, the `extract_changelog_from_issue()` method was incorrectly extracting changelog items:

```python
# BEFORE (Bug #11):
change_item = {
    "from": getattr(item, "fromString", None),  # BUG: Using fromString!
    "to": getattr(item, "toString", None),      # BUG: Using toString!
}

# AFTER (Fixed):
change_item = {
    "from": getattr(item, "from", None),        # Correct: Uses ID
    "to": getattr(item, "to", None),            # Correct: Uses ID
}
```

Jira API provides:
- `item.from` / `item.to` → Integer IDs (e.g., "1" for Open status)
- `item.fromString` / `item.toString` → Display names (e.g., "Open")

### Impact
The `_process_changelog_item()` function in `work_package_migration.py` expected integer IDs to map to OpenProject IDs, but received string names like "Open" instead of "1", causing mapping lookups to fail and return None.

## Solution

### File Modified
- `src/utils/enhanced_audit_trail_migrator.py` (lines 173-180)

### Change
```python
change_item = {
    "field": item.field,
    "fieldtype": getattr(item, "fieldtype", None),
    "fieldId": getattr(item, "fieldId", None),
    # BUG #11 FIX: Use "from"/"to" for IDs, not fromString/toString
    "from": getattr(item, "from", None),
    "fromString": getattr(item, "fromString", None),
    "to": getattr(item, "to", None),
    "toString": getattr(item, "toString", None),
}
```

## Validation Results

### Python State Snapshots (CORRECT)
Debug logs show progressive state building now produces correct values:
```
[BUG11-STATUS] status change: from_jira=1 -> from_op=2083828, to_jira=6 -> to_op=2083832
[BUG11-TYPE] issuetype change: from_jira=3 -> from_op=1041881, to_jira=10404 -> to_op=1042237
```

NRS-182 state snapshots:
- Op 1: type_id=1041881 (Task), status_id=2083828 (Open)
- Op 14: type_id=1041881 (Task), status_id=2083832 (Closed)
- Op 27: type_id=1042237 (Access), status_id=2083832 (Closed)

### OpenProject Journals (PARTIALLY CORRECT)
```
v1-v10:  type=Task, status=Open       ✓ Correct
v11:     type=Task, status=Closed     ✓ Correct (matches Jira 2011-09-09 status change)
v12-v13: type=Task, status=Closed     ✓ Correct
v14:     type=Task, status=Open       ✗ INCORRECT (should be Closed)
v15-v17: type=Task, status=Closed     ✓ Correct
v18:     type=Task, status=Open       ✗ INCORRECT (should be Closed)
v19-v26: type=Access, status=Closed   ✓ Correct (type changed on 2019-01-04)
v27:     type=Task, status=Open       ✗ INCORRECT (should be Access/Closed)
```

### Jira Ground Truth
- Created: 2011-08-18 as Task/Open
- Status change: 2011-09-09 Open → Closed (ONLY status change)
- Type change: 2019-01-04 Task → Access
- Final state: Access/Closed

## Remaining Issues

### Bug #12: Workflow Field Misinterpretation
The Jira "Workflow" field changes (workflow scheme changes, NOT status changes) appear to be incorrectly affecting the progressive state building. Example:
```
2012-10-26: Workflow change (scheme change, not status)
2018-11-20: Workflow change (scheme change, not status)
```

These should NOT affect status_id in the state snapshots, but the journal shows status flipping back to Open at these points.

### Bug #13: Last Journal Initial State
Journal v27 (the last journal) shows type=Task/status=Open, which is the INITIAL state, not the FINAL state (should be Access/Closed).

## Side-by-Side Comparison: NRS-182

### Jira Ground Truth
| Event | Date | Field | From | To |
|-------|------|-------|------|-----|
| Created | 2011-08-18 | - | - | Task/Open |
| Status change | 2011-09-09 | status | Open (1) | Closed (6) |
| Type change | 2019-01-04 | issuetype | Task (3) | Access (10404) |
| **Final State** | 2024-08-22 | - | - | **Access/Closed** |

### Python State Snapshots (CORRECT)
| Operation | type_id | status_id | Expected |
|-----------|---------|-----------|----------|
| Op 1 | 1041881 (Task) | 2083828 (Open) | Initial state |
| Op 14 | 1041881 (Task) | 2083832 (Closed) | After status change |
| Op 27 | 1042237 (Access) | 2083832 (Closed) | Final state |

### OpenProject Journals (PARTIAL ISSUES)
| Version | Date | Type | Status | Correct? |
|---------|------|------|--------|----------|
| v1-v10 | 2011-08-18 to 2011-09-07 | Task | Open | ✓ Yes |
| v11 | 2011-09-09 | Task | Closed | ✓ Yes |
| v12-v13 | 2011-09-13 to 2012-10-26 | Task | Closed | ✓ Yes |
| v14 | 2017-11-30 | Task | **Open** | ✗ Should be Closed |
| v15-v17 | 2018-05-24 to 2018-11-20 | Task | Closed | ✓ Yes |
| v18 | 2019-01-04 | Task | **Open** | ✗ Should be Closed |
| v19-v26 | 2019-01-04 to 2024-08-22 | Access | Closed | ✓ Yes |
| v27 | 2024-08-22 | **Task** | **Open** | ✗ Should be Access/Closed |

## Analysis

### Bug #11: FIXED
The changelog extraction in `enhanced_audit_trail_migrator.py` was incorrectly using `fromString`/`toString` instead of `from`/`to` for ID values. This has been fixed and Python now correctly provides integer IDs for status and type mappings.

### Bug #12: REMAINING - State Snapshot Application
The Python state_snapshot values are correct, but the OpenProject journals show incorrect values at specific versions (v14, v18, v27). This indicates an issue in how Ruby applies the state_snapshot values to journal.data.

Possible causes:
1. **Index mismatch**: Operation indices in Python may not align with journal version numbers
2. **Serialization issue**: state_snapshot values may not be correctly serialized in JSON
3. **Ruby processing order**: The iteration order in Ruby may not match Python's operation order

### Workflow Field: NOT THE CAUSE
The "Workflow" field is correctly excluded from field_mappings (not in the mapping dictionary), so it returns `None` from `_process_changelog_item()` and does not affect state building.

## What Was Fixed

1. **Bug #11 - Changelog ID Extraction**: `enhanced_audit_trail_migrator.py` lines 173-180
   - Changed `getattr(item, "fromString", None)` to `getattr(item, "from", None)`
   - Changed `getattr(item, "toString", None)` to `getattr(item, "to", None)`

2. **Bug #10 - Initial State Initialization**: `work_package_migration.py` lines 1862-1878
   - Changed from `work_package.get("type_id")` to local variable `type_id`
   - Changed from `work_package.get("status_id")` to local variable `status_op_id`

## What Remains

1. **Bug #12**: Investigate why Ruby's journal.data doesn't match Python's state_snapshot for certain operations
2. **Testing**: Need to add debug logging in Ruby to trace actual values being applied
3. **Validation**: Consider adding automated tests to compare Jira history with OpenProject journals

## Recommendations

1. **Add Ruby-side logging** showing actual state_snapshot values being read and applied
2. **Verify JSON serialization** of state_snapshot from Python to Ruby
3. **Check operation ordering** to ensure Python operations align with Ruby journal versions
4. **Consider adding validation step** that compares generated journals against Jira changelog

## Related ADRs
- ADR-005: Bug #9 Progressive State Building Fix
- ADR-006: Bug #9 Activities Page 500 Error
- ADR-007: Bug #9 User ID Zero Fix

## Date
2025-11-25

## Status Update
- Bug #10: FIXED
- Bug #11: FIXED
- Bug #12: IDENTIFIED (state_snapshot application in Ruby)
