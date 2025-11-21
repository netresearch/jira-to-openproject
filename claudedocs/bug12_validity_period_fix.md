# Bug #12: Journal validity_period Fix

## Problem Summary
After fixing Bug #11 (type_id NULL), discovered Bug #12: journals table CHECK constraint violation for empty validity_period.

## Error
```
PG::CheckViolation: ERROR: new row for relation "journals" violates check constraint "journals_validity_period_not_empty"
```

## Root Cause
Bug #9 fix removed validity_period from Journal.new() to fix malformed tstzrange. However, validity_period was still being calculated but never set on the journal after save. The journals table has a CHECK constraint requiring non-empty validity_period.

## Fix Implementation

### UPDATE Path: `/home/sme/p/j2o/src/migrations/work_package_migration.py`

**Line 680**: validity_period already calculated
```python
validity_period = f'["{validity_start_iso}", "{validity_end_iso}")'
```

**Line 729**: Added validity_period setting after journal save
```python
journal.update_column(:validity_period, '{validity_period}')
```

### CREATE Path: `/home/sme/p/j2o/src/clients/openproject_client.py`

**Lines 2702-2705**: Added validity_period calculation
```ruby
validity_start = created_at || Time.now.utc.iso8601
validity_end = Time.now.utc.iso8601
validity_period = "[\"#{validity_start}\", \"#{validity_end}\")"
```

**Line 2746**: Added validity_period setting after journal save
```ruby
journal.update_column(:validity_period, validity_period)
```

## Pattern Applied
Both UPDATE and CREATE code paths now:
1. Calculate validity_period before journal creation
2. Create journal with Journal.new() (without validity_period, data_type, data_id per Bug #9 fix)
3. Save journal with `validate: false`
4. Set created_at if provided
5. Set validity_period using update_column

## Testing Status

**Test Started**: 2025-11-03 (continuation session)
**Test Issues**: 10 previously failed issues (NRS-171, NRS-182, NRS-191, NRS-198, NRS-204, NRS-42, NRS-59, NRS-66, NRS-982, NRS-4003)
**Test Log**: `/tmp/nrs_TEST_BUG12_FIXED.log`

## Expected Outcome
If successful:
- All 10 test issues migrate without errors
- No validity_period constraint violations
- All bugs #9, #10, #11, #12 fixed
- Ready for full NRS migration (3,817 issues)

## Historical Context
This is the 12th bug fix in the NRS migration journey:
- Bug #9: Malformed tstzrange - Fixed by removing fields from Journal.new()
- Bug #10: Date constraint violations - Fixed with None-safe validation
- Bug #11: type_id NULL - Fixed by populating 27 work package attributes (7 iterations)
- Bug #12: validity_period empty - Fixed by setting validity_period after save

## Next Steps
1. ⏳ Monitor test for completion (~2-5 minutes)
2. ⏳ Verify no constraint errors
3. ⏳ If successful, run full NRS migration
4. ⏳ Achieve 100% migration success rate
