# ADR-010: Bug #14 & #15 - Phantom Journal Elimination

## Status
Bug #14: IMPLEMENTED - Verified
Bug #15: SUPERSEDED by Bug #16 fix (see ADR-011)

**IMPORTANT**: The Bug #15 analysis in this ADR was INCORRECT. See ADR-011 for the correct understanding. Those "empty" operations were NOT "view events" - they were REAL changelog entries for Jira fields we don't map to OpenProject. Bug #16 fix preserves them as notes instead of skipping them.

## Context
During NRS-182 journal migration validation, users reported that OpenProject showed journal entries attributed to wrong users and dates. Example complaint: "Michael Ablass 08/22/2024 08:40 AM The changes were retracted" - but Michael Ablass never made any change on that date.

## Problem Analysis

### Bug #14: `set_journal_user` Creates Phantom Journals

**Root Cause**: The `set_journal_user` operation (meant to set the user on journal v1) was being processed as a regular operation, creating a new journal entry.

The flow was:
1. Python creates `set_journal_user` operation with `user_id` but NO timestamp
2. Bug #12 fix sorts operations by timestamp
3. `set_journal_user` (no timestamp) gets fallback timestamp "9999-12-31" → sorted to END
4. Ruby processes it as operation #27, creating a phantom journal entry
5. Result: "Michael Ablass 08/22/2024" appears (attributed to wrong user/date)

### Bug #15: Empty `create_comment` Operations Create "Retracted" Entries

**Root Cause**: Our Python code creates `create_comment` operations for ALL Jira changelog entries, but we only track certain fields (type_id, status_id, assigned_to_id, etc.). When a changelog entry modifies fields we don't track (watchers, attachments, labels, custom fields, etc.), the operation ends up with:
- Empty `notes` field (because it's a field change, not a comment)
- Empty `field_changes` (because we don't map those fields)

These operations were being migrated as journals with no visible changes, causing OpenProject to display "The changes were retracted." message.

**Evidence** from migrated data:
```json
{"type": "create_comment", "user_id": 148895, "notes": "", "created_at": "2024-08-22T08:40:45.119+0000", "field_changes": None}
```
This is a Jira changelog entry for a field we don't track.

## Solution

### File Modified
- `src/ruby/create_work_package_journals.rb` (lines 267-288)

### Bug #14 Fix
Skip `set_journal_user` operations entirely - they should NOT create new journals:

```ruby
# BUG #14 FIX (CRITICAL): Skip set_journal_user - it should NOT create new journals
# This operation is meant to set the user on v1, but with Bug #12 sorting it ends up
# at the END of operations (no timestamp → sorted last) and creates phantom journals.
# The first operation already sets v1's user_id correctly, so we skip this entirely.
if op_type == 'set_journal_user'
  puts "J2O bulk item #{idx}: SKIP set_journal_user (Bug #14 fix - no phantom journal)" if verbose
  next
end
```

### Bug #15 Fix
Skip empty `create_comment` operations that have no notes and no field_changes:

```ruby
# BUG #15 FIX: Skip empty create_comment operations (no notes AND no field_changes)
# These are Jira "view" events - someone opened the issue but made no changes.
# They create phantom journals that show "The changes were retracted." in OpenProject.
# Exception: First operation (op_idx=0) must always be processed to create v1.
notes_preview = op['notes'] || op[:notes]
field_changes_preview = op['field_changes'] || op[:field_changes]
is_empty_comment = op_type == 'create_comment' &&
                   (notes_preview.nil? || notes_preview.to_s.strip.empty?) &&
                   (field_changes_preview.nil? || field_changes_preview.empty?)
if is_empty_comment && op_idx != 0
  puts "J2O bulk item #{idx}: SKIP empty create_comment at #{op['created_at']} (Bug #15 fix - no phantom journal)" if verbose
  next
end
```

## Validation Results

### Before Fix (27 journals)
```
v25 (2024-08-22 08:39) User: Björn Marten (ID: 148895)
v26 (2024-08-22 08:40) User: Björn Marten (ID: 148895)  ← Empty view event
v27 (2024-08-22 08:40) User: Michael Ablass (ID: 149073) ← Phantom from set_journal_user
```

### After Fix (14 journals)
```
v14 (2019-01-04) User: Sebastian Mendel (ID: 149135)  ← Last actual change
```

### Journals Eliminated
- 13 phantom/empty journals removed
- All remaining journals have meaningful changes
- State progression preserved correctly

### State Progression (Preserved)
| Version | Date | Type | Status |
|---------|------|------|--------|
| v1-v9 | 2011-08-18 to 2011-09-07 | Task | Open |
| v10 | 2011-09-09 | Task | Closed |
| v11-v13 | 2017-11-30 to 2019-01-04 | Task | Closed |
| v14 | 2019-01-04 | Access | Closed |

This matches the Jira ground truth:
- Created 2011-08-18: Task/Open
- Status changed 2011-09-09: Task/Closed
- Type changed 2019-01-04: Access/Closed

## Technical Analysis

### Why These Fixes Work

**Bug #14**: The first operation (usually `set_created_at`) already sets journal v1 with the correct user. The `set_journal_user` operation is redundant and now correctly skipped.

**Bug #15**: Empty changelog entries in Jira are just "view" events - they don't represent actual changes and should not create journal entries in OpenProject.

### Impact
- Reduced journal count from 27 to 14 (48% reduction for NRS-182)
- Eliminated all phantom "The changes were retracted" entries
- Eliminated incorrect user/date attributions
- Preserved all meaningful state changes

### Performance Impact
- Slight improvement: fewer journal entries to create
- No negative impact on migration process

## Related ADRs
- ADR-005: Bug #9 Progressive State Building Fix
- ADR-006: Bug #9 Activities Page 500 Error
- ADR-007: Bug #9 User ID Zero Fix (Bug #13)
- ADR-008: Bug #11 Changelog ID Extraction Fix
- ADR-009: Bug #12 State Snapshot Ordering Fix
- ADR-011: Bug #16 Unmapped Field Preservation (supersedes Bug #15)

## Date
2025-11-25

## Bug Status Summary
- Bug #9: FIXED (progressive state building)
- Bug #10: FIXED (initial state initialization)
- Bug #11: FIXED (changelog ID extraction)
- Bug #12: FIXED (state snapshot ordering)
- Bug #13: FIXED (user_id=0 causing 500 error)
- Bug #14: FIXED (set_journal_user phantom journals) ← THIS ADR
- Bug #15: SUPERSEDED by Bug #16 (see ADR-011) ← Incorrect analysis
- Bug #16: FIXED (unmapped field preservation - see ADR-011)
