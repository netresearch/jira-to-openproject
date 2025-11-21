# Bug #12: Journal validity_period Fix - Attempt #2

## Problem Summary from Attempt #1
Bug #12 Fix Attempt #1 FAILED because validity_period was set AFTER journal.save() using update_column(). PostgreSQL CHECK constraint `journals_validity_period_not_empty` is enforced DURING save, so the save failed before update_column() could execute.

## Root Cause
PostgreSQL CHECK constraints are enforced during INSERT/UPDATE operations, before the transaction commits. Setting validity_period after save using update_column() never executes because the save fails first.

## Fix #2 Implementation

### Pattern Change
**WRONG (Attempt #1)**: Set validity_period AFTER save using update_column()
```ruby
journal.save(validate: false)
journal.update_column(:validity_period, validity_period)
```

**CORRECT (Attempt #2)**: Set validity_period BEFORE save as direct attribute
```ruby
journal.validity_period = validity_period
journal.save(validate: false)
```

### UPDATE Path: `/home/sme/p/j2o/src/migrations/work_package_migration.py`

**Line 726**: Added validity_period setting BEFORE save
```python
journal.data = data
journal.validity_period = '{validity_period}'

if journal.save(validate: false)
    journal.update_column(:created_at, '{comment_created}') if '{comment_created}' != ''
    puts journal.id
```

**Removed**: Line 729 `journal.update_column(:validity_period, '{validity_period}')`

### CREATE Path: `/home/sme/p/j2o/src/clients/openproject_client.py`

**Line 2744**: Added validity_period setting BEFORE save
```ruby
journal.data = data
journal.validity_period = validity_period
journal.save(validate: false)
journal.update_column(:created_at, created_at) if created_at
```

**Removed**: Line 2746 `journal.update_column(:validity_period, validity_period)`

## Pattern Applied
Both UPDATE and CREATE code paths now:
1. Calculate validity_period before journal creation
2. Create journal with Journal.new() (without validity_period, data_type, data_id per Bug #9 fix)
3. Populate Journal::WorkPackageJournal with work package snapshot (Bug #11 fix #7)
4. Set journal.data
5. **Set journal.validity_period BEFORE save** ← Bug #12 Fix Attempt #2
6. Save journal with `validate: false`
7. Set created_at if provided using update_column

## Testing Status

**Test Started**: 2025-11-03 (continuation session, Fix Attempt #2)
**Test Issues**: 10 previously failed issues (NRS-171, NRS-182, NRS-191, NRS-198, NRS-204, NRS-42, NRS-59, NRS-66, NRS-982, NRS-4003)
**Test Log**: `/tmp/nrs_TEST_BUG12_FIX2.log`

## Expected Outcome
If successful:
- All 10 test issues migrate without errors
- No validity_period constraint violations
- All bugs #9, #10, #11, #12 fixed
- Ready for full NRS migration (3,817 issues)

## Historical Context
- Bug #9: Malformed tstzrange - Fixed by removing fields from Journal.new()
- Bug #10: Date constraint violations - Fixed with None-safe validation
- Bug #11: type_id NULL - Fixed by populating 27 work package attributes (7 iterations)
- Bug #12 Attempt #1: Set validity_period AFTER save - FAILED (CHECK constraint enforced during save)
- Bug #12 Attempt #2: Set validity_period BEFORE save - TESTING NOW

## Next Steps
1. ⏳ Monitor test for completion (~2-5 minutes)
2. ⏳ Verify no constraint errors
3. ⏳ If successful, run full NRS migration
4. ⏳ If fails, investigate and iterate (Fix Attempt #3)
