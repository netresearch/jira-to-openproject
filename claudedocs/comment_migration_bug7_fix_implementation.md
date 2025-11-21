# Bug #7 Fix: Non-Overlapping Historical Validity Periods

**Date**: 2025-10-30
**Status**: ✅ Implemented - Testing in progress
**Location**: `/home/sme/p/j2o/src/migrations/work_package_migration.py` (lines 580-679)

---

## Problem Statement

**Bug #7**: PostgreSQL exclusion constraint violation when migrating comments
**Root Cause**: All comments were assigned open-ended validity_period ranges, causing overlaps
**Error**: `PG::ExclusionViolation: ERROR: conflicting key value violates exclusion constraint`
**Impact**: 90 errors in full NRS migration, 0 comments migrated

---

## User's Critical Requirement

**NEVER USE THE API for historical data migration**

The OpenProject API (`wp.add_journal()`) is ONLY for current operations. Historical migration must:
- Use direct database insertion (`Journal.new()`)
- Preserve original Jira timestamps in validity_period
- Calculate non-overlapping validity_period ranges based on historical timeline

---

## Solution Implemented

### Architecture Change

**Before** (WRONG):
- Process comments one-by-one in a loop
- Each comment gets open-ended validity_period: `["timestamp",)`
- Result: All ranges overlap → exclusion constraint violation

**After** (CORRECT):
- Collect ALL comments upfront for each work package
- Sort by original Jira timestamp
- Calculate non-overlapping validity_period ranges:
  - Comments 1 to N-1: Closed range `["timestamp1", "timestamp2")`
  - Comment N (last): Open-ended range `["timestamp",)`
- Use direct `Journal.new()` with calculated historical ranges

### Implementation Details

```python
# Step 1: Collect all new comments
new_comments = []
for comment in comments:
    if comment_body and comment_body not in existing_notes:
        new_comments.append({
            "body": comment_body,
            "author_id": comment_author_id,
            "created": comment_created
        })

# Step 2: Sort by timestamp
new_comments.sort(key=lambda c: c.get("created", ""))

# Step 3: Calculate non-overlapping validity_period ranges
for i, comment in enumerate(new_comments):
    is_last_comment = (i == len(new_comments) - 1)

    if is_last_comment:
        # Last comment: open-ended range
        validity_period = f'["{validity_start_iso}",)'
    else:
        # Other comments: closed range ending at next comment's start
        next_comment = new_comments[i + 1]
        validity_period = f'["{validity_start_iso}", "{validity_end_iso}")'

    # Step 4: Direct database insertion with historical timestamp
    journal = Journal.new(
        journable_type: 'WorkPackage',
        journable_id: wp_id,
        user_id: comment_author_id,
        version: max_version + 1,
        notes: comment_body,
        data_type: 'Journal',
        data_id: wp_id,
        validity_period: validity_period  # Historical, non-overlapping
    )
    journal.save(validate: false)
    journal.update_column(:created_at, comment_created)  # Historical timestamp
```

---

## Example: 3 Comments Timeline

**Jira Comments**:
- Comment 1: 2011-01-21T12:12:36Z
- Comment 2: 2012-03-12T22:03:04Z
- Comment 3: 2012-06-14T15:37:20Z

**Validity Periods** (Non-Overlapping):
```
Comment 1: ["2011-01-21T12:12:36Z", "2012-03-12T22:03:04Z")  ← Ends when Comment 2 begins
Comment 2: ["2012-03-12T22:03:04Z", "2012-06-14T15:37:20Z")  ← Ends when Comment 3 begins
Comment 3: ["2012-06-14T15:37:20Z",)                         ← Open-ended (last comment)
```

**Result**: No overlaps ✅ Exclusion constraint satisfied ✅

---

## Key Differences from API Approach

| Aspect | API Approach (WRONG) | Historical Migration (CORRECT) |
|--------|---------------------|--------------------------------|
| **Method** | `wp.add_journal()` | `Journal.new()` with `save(validate: false)` |
| **Timestamp** | Current time (2025-10-30) | Original Jira timestamp (2011-01-21) |
| **validity_period** | Auto-managed by API | Calculated from historical timeline |
| **Purpose** | Live user operations | Historical data preservation |

---

## Testing

**Test Script**: `/tmp/test_10_nrs_issues.py`
**Test Log**: `/tmp/test_CORRECTED_NON_OVERLAPPING.log`
**Test Size**: 10 NRS issues with multiple comments
**Expected**: No exclusion constraint errors, all comments migrated with correct historical validity_period ranges

---

## Next Steps

1. ✅ Implementation complete
2. ⏳ Testing in progress (10-issue test)
3. ⏳ If test passes: Run full NRS migration (~3,817 issues)
4. ⏳ Verify comments migrated with correct non-overlapping validity_period ranges

---

## Files Modified

- `/home/sme/p/j2o/src/migrations/work_package_migration.py` (lines 580-679)
  - Reverted API-based code
  - Implemented non-overlapping historical validity_period calculation
  - Added comment sorting by timestamp
  - Changed from one-by-one processing to batch collection + sorted processing

---

## ADR Update Required

**Rule to Add**: "NEVER USE THE API for historical data migration, except for testing OpenProject behavior"

**Rationale**:
- API uses current time, not historical time
- Historical migration must preserve original timestamps
- validity_period must reflect historical timeline, not current time
- Database integrity requires historical accuracy

---

## Lessons Learned

1. **API Purpose**: The API is for current operations, not historical migration
2. **Test Value**: API test revealed HOW validity_period works, not WHAT to implement
3. **Historical Preservation**: Migration must preserve original Jira timeline, not create new timeline
4. **Non-Overlapping Ranges**: Exclusion constraint requires careful calculation of validity_period end times
5. **User Feedback**: Strong user correction prevented wrong implementation from reaching production

---

## References

- Previous session report: `/home/sme/p/j2o/claudedocs/nrs_migration_final_status_report.md`
- Bug #7 analysis: Lines 34-60 in final status report
- API test results: `/tmp/test_add_comments_fixed.rb` execution results
