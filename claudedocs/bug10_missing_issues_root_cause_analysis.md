# Bug #10: Root Cause Analysis - 91 Missing Issues

**Date**: 2025-11-03
**Investigator**: Claude
**Status**: Root cause identified

---

## Executive Summary

**Problem**: Migration reported 3,809 issues should be created, but only 3,718 were successfully migrated, leaving 91 issues missing (2.4% failure rate).

**Root Cause**: PostgreSQL check constraint `work_packages_due_larger_start_date` violation. All 91 failed issues have **due dates that occur BEFORE their start dates**, which violates OpenProject's database constraint that enforces `due_date >= start_date`.

**Impact**: Silent failures - these issues are rejected at the database level but not logged as errors in the simplified bulk creation workflow.

---

## Evidence

### 1. Total Count Analysis

```
Total Jira issues:      3,817
Found existing:             8
Should CREATE:          3,809
Actually created:       3,718
Missing:                   91  (2.4% failure rate)
```

### 2. Error Pattern Discovery

Analyzed recent bulk result files across multiple migration batches:

```
Batch Analysis (Top 10 recent):
  bulk_result_NRS_20251103_062945.json: Created: 766, Errors: 43
  bulk_result_NRS_20251103_062441.json: Created: 986, Errors: 14
  bulk_result_NRS_20251103_061842.json: Created: 984, Errors: 16
  bulk_result_NRS_20251103_061214.json: Created: 982, Errors: 18
  ...

Total unique failed keys: 91
```

### 3. Constraint Violation Details

**Database Error**:
```sql
PG::CheckViolation: ERROR: new row for relation "work_packages"
violates check constraint "work_packages_due_larger_start_date"
```

**Constraint Definition**:
```sql
CHECK (due_date >= start_date)
```

**100% of failures** are due to this single constraint violation.

---

## Complete List of 91 Failed Issues

### Pattern Distribution

| Pattern             | Count | Description                        |
|---------------------|-------|------------------------------------|
| Off by 1 day        |    20 | Start date is 1 day after due date |
| Off by week         |    25 | Start date 2-7 days after due      |
| Off by months       |    20 | Start date 8-60 days after due     |
| Off by year+        |    26 | Start date >60 days after due      |

### Issue Type Distribution

| Issue Type          | Count | % of Total |
|---------------------|-------|------------|
| Account deletion    |    22 | 24.2%      |
| Task                |    21 | 23.1%      |
| Epic                |    18 | 19.8%      |
| Purchase            |     8 | 8.8%       |
| Service Request     |     8 | 8.8%       |
| Account Request     |     6 | 6.6%       |
| Access              |     4 | 4.4%       |
| Sub-Task            |     3 | 3.3%       |
| Problem             |     1 | 1.1%       |

### All 91 Failed Jira Keys

```
NRS-42, NRS-57, NRS-59, NRS-65, NRS-66, NRS-67, NRS-69, NRS-72,
NRS-122, NRS-178, NRS-186, NRS-208, NRS-256, NRS-454, NRS-982,
NRS-992, NRS-1030, NRS-1050, NRS-1068, NRS-1150, NRS-1292, NRS-1382,
NRS-1386, NRS-1408, NRS-1421, NRS-1490, NRS-1498, NRS-1520, NRS-1588,
NRS-1667, NRS-1746, NRS-1786, NRS-2043, NRS-2077, NRS-2375, NRS-2528,
NRS-2590, NRS-2620, NRS-2763, NRS-2775, NRS-2840, NRS-2902, NRS-2903,
NRS-2941, NRS-3030, NRS-3039, NRS-3096, NRS-3185, NRS-3250, NRS-3262,
NRS-3275, NRS-3298, NRS-3323, NRS-3329, NRS-3332, NRS-3349, NRS-3365,
NRS-3403, NRS-3446, NRS-3459, NRS-3472, NRS-3476, NRS-3487, NRS-3517,
NRS-3563, NRS-3564, NRS-3580, NRS-3586, NRS-3612, NRS-3650, NRS-3765,
NRS-3794, NRS-3827, NRS-3864, NRS-3905, NRS-3906, NRS-3936, NRS-3937,
NRS-3958, NRS-3959, NRS-3960, NRS-3966, NRS-3983, NRS-4003, NRS-4040,
NRS-4041, NRS-4073, NRS-4076, NRS-4077, NRS-4102, NRS-4116
```

---

## Sample Failed Issues

### Example 1: NRS-42 (Off by 3 days)
```
Jira Key:    NRS-42
Type:        Account Request
Status:      Closed
Start Date:  2018-03-19
Due Date:    2018-03-16
Problem:     Due date is 3 days BEFORE start date
```

### Example 2: NRS-3250 (Off by 16 days)
```
Jira Key:    NRS-3250
Type:        Account Request
Status:      Closed
Start Date:  2024-04-03
Due Date:    2024-03-18
Problem:     Due date is 16 days BEFORE start date
```

### Example 3: NRS-4003 (Off by 669 days - extreme case)
```
Jira Key:    NRS-4003
Type:        Task
Status:      Closed
Start Date:  2025-08-12
Due Date:    2023-10-13
Problem:     Due date is 1.8 YEARS BEFORE start date
```

---

## Root Cause Analysis

### Why These Issues Have Invalid Date Ranges

**Hypothesis 1: Jira Data Quality Issues** ✓ **MOST LIKELY**
- Jira allows setting due dates before start dates (no constraint)
- These issues may have had dates updated manually over time
- Account deletion issues often have reversed dates (22 of 91 failures)
- Issues were closed but dates never corrected

**Hypothesis 2: Migration Date Transformation Error** ❌ **UNLIKELY**
- Would affect all issues, not just 91
- Would show pattern across issue types (it doesn't - specific types affected)
- Date format conversion appears correct in successful migrations

**Hypothesis 3: Timezone Conversion Issues** ❌ **UNLIKELY**
- Off-by-1-day cases are only 20/91 (22%)
- Most failures are off by weeks or months
- Timezone issues would only affect timestamps, not date portions

### Why Failures Are Silent

**Current Bulk Creation Flow**:
```python
# Simplified bulk creation in rails_console_client.py
def _bulk_create_work_packages_simplified():
    # Batch INSERT via Ruby script
    # Returns only created IDs, not individual errors
    # Database constraint violations caught but not logged per-issue
```

**Problem**: The simplified bulk creation mode optimizes for speed but loses granular error tracking. When PostgreSQL rejects a row due to constraint violation, the error is counted but the specific issue details are not logged.

---

## Impact Assessment

### Data Integrity Impact
- **Medium**: 2.4% of issues not migrated
- All failed issues have STATUS = "Closed"
- Most are older issues (50% from before 2024)
- No active work affected

### User Experience Impact
- **Low-Medium**: Users searching for historical issues won't find them
- Account deletion requests missing from audit trail
- Historical task/epic references broken

### Migration Completeness
- **91 of 3,817 issues (2.4%)** not migrated
- Concentrated in specific issue types
- Affects audit trail and historical reporting

---

## Recommended Solutions

### Option A: Auto-Correction During Migration ✓ **RECOMMENDED**

**Approach**: Detect and fix date inconsistencies before insertion

```python
# In work_package_migration.py
def _prepare_work_package_data(issue_data):
    start_date = issue_data.get('start_date')
    due_date = issue_data.get('duedate')

    # Auto-correct: if due < start, swap or adjust
    if start_date and due_date:
        if due_date < start_date:
            # Strategy 1: Set due = start (conservative)
            issue_data['duedate'] = start_date
            logger.warning(f"Auto-corrected dates for {issue_data['jira_key']}: "
                         f"due_date was {due_date}, adjusted to {start_date}")

            # OR Strategy 2: Clear both dates (safest)
            # issue_data['start_date'] = None
            # issue_data['duedate'] = None
```

**Pros**:
- Migrates all issues
- Maintains data completeness
- Logs corrections for audit

**Cons**:
- Changes original Jira data semantics
- May mask underlying data quality issues

### Option B: Skip with Detailed Logging

**Approach**: Skip invalid issues but log comprehensive details

```python
def validate_dates(issue_data):
    if issue_data.get('duedate') < issue_data.get('start_date'):
        logger.error(f"SKIP {issue_data['jira_key']}: Invalid date range - "
                    f"due={issue_data['duedate']} < start={issue_data['start_date']}")
        return False
    return True
```

**Pros**:
- Preserves data integrity
- Clear audit trail of skipped issues
- No data modification

**Cons**:
- 91 issues remain unmigrated
- Breaks historical references

### Option C: Migrate Without Dates ✓ **CONSERVATIVE**

**Approach**: For invalid date ranges, clear both dates and migrate issue

```python
def sanitize_dates(issue_data):
    if issue_data.get('duedate') and issue_data.get('start_date'):
        if issue_data['duedate'] < issue_data['start_date']:
            logger.warning(f"Clearing invalid dates for {issue_data['jira_key']}")
            issue_data['duedate'] = None
            issue_data['start_date'] = None
    return issue_data
```

**Pros**:
- All issues migrated
- No data corruption (dates simply removed)
- Preserves all other issue data

**Cons**:
- Loses date information
- May impact reporting

---

## Implementation Priority

**Priority**: HIGH (2.4% data loss, but all closed issues)

**Recommended Action**: Implement **Option A (Auto-Correction)** OR **Option C (Clear Dates)**

**Reasoning**:
1. All 91 failed issues are CLOSED - not active work
2. Historical completeness important for audit trail
3. Auto-correction or clearing dates better than skipping
4. Can be implemented quickly in existing migration code

---

## Verification Plan

1. **Pre-Migration Validation**:
   ```sql
   SELECT jira_key, start_date, duedate
   FROM jira_issues
   WHERE duedate < start_date;
   ```

2. **Post-Correction Validation**:
   ```python
   # Verify all issues processed
   assert len(migrated_issues) + len(skipped_issues) == total_issues
   ```

3. **Spot Check Samples**:
   - Verify NRS-42, NRS-3250, NRS-4003 migrated successfully
   - Confirm dates corrected/cleared as expected

---

## Files Generated

- `/home/sme/p/j2o/var/data/failed_issues_analysis.json` - Complete details of all 91 failed issues

---

## Next Steps

1. ✓ Root cause identified: Database constraint violation
2. ✓ All 91 failed issues cataloged
3. ⏳ Implement chosen solution (Option A or C recommended)
4. ⏳ Add pre-migration date validation
5. ⏳ Re-run migration for 91 failed issues
6. ⏳ Verify 100% migration completeness

---

## Appendix: Technical Details

### Database Constraint
```sql
-- OpenProject work_packages table constraint
ALTER TABLE work_packages
ADD CONSTRAINT work_packages_due_larger_start_date
CHECK (due_date IS NULL OR start_date IS NULL OR due_date >= start_date);
```

### Bulk Result File Structure
```json
{
  "result": {
    "status": "success",
    "created": [{"index": 1, "id": 5575943}, ...],
    "errors": [
      {
        "index": 0,
        "errors": ["PG::CheckViolation: ..."]
      }
    ],
    "created_count": 766,
    "error_count": 43,
    "total": 809
  },
  "meta": [
    {
      "jira_id": "129940",
      "jira_key": "NRS-3250",
      "start_date": "2024-04-03",
      "duedate": "2024-03-18",
      ...
    }
  ]
}
```

### Pattern Analysis Query
```python
# Categorize by date difference severity
diff_days = (start_date - due_date).days
if diff_days == 1: category = "off_by_1_day"
elif diff_days <= 7: category = "off_by_week"
elif diff_days <= 60: category = "off_by_months"
else: category = "off_by_year_plus"
```

---

**End of Report**
