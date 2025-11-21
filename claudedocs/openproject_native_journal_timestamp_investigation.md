# OpenProject Native Journal Timestamp Behavior Investigation

## Investigation Status

**Date**: 2025-11-06
**Status**: BLOCKED - No tmux session available

## Objective

Investigate how OpenProject natively handles multiple journal entries with very close timestamps to understand the proper approach for fixing the validity_period bug during Jira-to-OpenProject migration.

## Background

### The Problem

**Jira Behavior**:
- Allows comments and changelog entries at identical timestamps (down to millisecond precision)
- Example: `2011-08-23 13:41:21.000` for both a comment and a status change

**OpenProject Behavior**:
- Uses PostgreSQL `tstzrange` type for `validity_period` column in journals table
- Has two constraints:
  1. `journals_validity_period_not_empty` - prevents empty ranges
  2. `non_overlapping_journals_validity_periods` - prevents overlapping ranges

**Migration Issue**:
- When Jira items with identical timestamps are migrated as separate journals, they create empty or overlapping validity_period ranges
- Affects ~70% of test issues

### Previous Fix Attempts

#### Fix Attempt #5: Timestamp Collision Detection with Millisecond Separation
- **Approach**: Detect timestamp collisions after sorting all journal entries, add 1 millisecond to later entry
- **Implementation**: Both UPDATE path (lines 588-611) and CREATE path (lines 1680+) in work_package_migration.py:680-747
- **Result**: FAILED
- **Root Cause**: Existing OpenProject journals are stored with **second-precision only**, no milliseconds. When we query `next_created` from existing journals, we get timestamps like `2011-08-23 13:41:21+00` (no milliseconds), making our millisecond separation ineffective.

#### Timestamp Formatting Bypass Bug (FIXED)
- **Bug**: Lines 711-712 and 727-730 checked if timestamps contained 'T' and used them AS-IS without formatting
- **Fix**: Removed bypass, ALWAYS format timestamps through `strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'`
- **Status**: Fixed in `/home/sme/p/j2o/src/migrations/work_package_migration.py:680-747`
- **Impact**: Insufficient - the real issue is database-level precision mismatch

## What We Need to Investigate

### Questions to Answer

1. **Database Schema**:
   - What is the datetime_precision of `journals.created_at` column?
   - Does the database support microsecond precision?
   - What data type is used for `validity_period`?

2. **OpenProject's Native Behavior**:
   - When OpenProject creates journal entries through normal operation (UI, API), how does it handle timestamps?
   - Does it use millisecond/microsecond precision?
   - How does it calculate validity_period ranges for consecutive entries?

3. **Timestamp Collision Scenarios**:
   - What happens if we try to create two journal entries with identical timestamps through OpenProject's API?
   - Does OpenProject automatically separate them?
   - Does it reject the second entry?

### Test Design

#### Test 1: Schema Investigation
```ruby
# Query PostgreSQL schema for journals table
result = ActiveRecord::Base.connection.execute(<<-SQL)
  SELECT
    column_name,
    data_type,
    datetime_precision,
    column_default
  FROM information_schema.columns
  WHERE table_name = 'journals'
    AND column_name IN ('created_at', 'updated_at')
  ORDER BY column_name;
SQL

# Expected findings:
# - data_type: "timestamp with time zone"
# - datetime_precision: 6 (microseconds) or 3 (milliseconds) or NULL (seconds)
```

#### Test 2: Native Journal Creation
```ruby
# Create test work package
wp = WorkPackage.new(
  project_id: 303319,
  type_id: Type.find_by(name: 'Task').id,
  status_id: Status.find_by(name: 'New').id,
  priority_id: IssuePriority.find_by(name: 'Normal').id,
  subject: 'TEST: Journal Timestamp Investigation'
)
wp.save

# Add multiple journals as quickly as possible
5.times do |i|
  wp.subject = "TEST: Journal #{i}"
  wp.save
end

# Query the resulting journals
wp.journals.order(:version).each do |j|
  puts "Version #{j.version}:"
  puts "  created_at: #{j.created_at.inspect}"
  puts "  validity_period: #{j.validity_period.inspect}"
end
```

#### Test 3: Explicit Timestamp Setting (if possible)
```ruby
# Try to create journals with manually set timestamps
# This tests whether OpenProject allows timestamp manipulation
```

## Expected Findings

### Hypothesis 1: OpenProject Uses Second-Precision
- Database supports microsecond precision, but OpenProject rounds to seconds
- validity_period is calculated based on second-precision timestamps
- This would explain why existing journals have `2011-08-23 13:41:21+00` format

### Hypothesis 2: OpenProject Automatically Separates Collisions
- When creating journals rapidly, OpenProject might automatically add small time differences
- This would be the safest approach for our migration

### Hypothesis 3: OpenProject Allows Millisecond Precision
- Database and OpenProject both support millisecond/microsecond precision
- The issue is specific to how we're creating journals during migration
- We need to adjust our approach to match OpenProject's native behavior

## Recommended Fix Approach

### Option A: Use Second-Precision with 1-Second Separation
If OpenProject uses second-precision:
```python
# Detect collisions at second-precision
if timestamp_collision_at_second_precision:
    # Add 1 second instead of 1 millisecond
    new_timestamp = base_timestamp + timedelta(seconds=1)
```

### Option B: Query Database for Actual Precision
```python
# Before migration, query the database schema
# Adjust our timestamp handling based on actual database precision
# Use appropriate separation (1 second for second-precision, 1 millisecond for millisecond-precision)
```

### Option C: Use OpenProject's Native Journal Creation
```python
# Instead of bulk-creating journals with custom timestamps,
# Use OpenProject's API to create each journal entry
# Let OpenProject handle timestamp management natively
```

## Blocker: No Tmux Session

### Current Status
```
$ tmux ls
no server running on /tmp/tmux-1000/default
```

**Cannot proceed with investigation** until:
1. Tmux session is restarted
2. Rails console is accessible
3. OpenProjectClient can execute Ruby commands

### Alternative Approaches

1. **Direct PostgreSQL Query**:
   - Connect directly to PostgreSQL database
   - Query schema and sample data
   - Limited: Cannot test native OpenProject behavior

2. **OpenProject API Testing**:
   - Use REST API to create work packages and journals
   - Monitor resulting database entries
   - Limited: May not reveal internal timestamp handling

3. **Code Analysis**:
   - Review OpenProject source code for journal creation logic
   - Analyze how timestamps are set and validity_period is calculated
   - Most thorough but requires source code access

## Next Steps

### Immediate Actions (Once Tmux Available)
1. Start tmux session: `tmux new -s rails_console`
2. Connect to OpenProject: `docker exec -it openproject-web-1 bash`
3. Launch Rails console: `bundle exec rails console`
4. Execute Test 1 (Schema Investigation)
5. Execute Test 2 (Native Journal Creation)
6. Document findings

### Implementation Plan
1. Based on investigation results, implement proper fix
2. Test with 10 issues
3. Validate all journals have correct validity_period
4. Run full NRS migration (3,817 issues)

## Reference Files

- Fix Attempt #5 Implementation: `src/migrations/work_package_migration.py:680-747`
- Test Scripts: `/tmp/test_journal_timestamps.py`, `/tmp/query_journal_schema.py`
- Previous Test Logs: `/tmp/nrs_TEST_FIX_ATTEMPT_5.log`, `/tmp/nrs_TEST_TIMESTAMP_FMT_FIX2.log`

## Key Insight

**The fundamental issue**: We cannot simply add milliseconds to our timestamps because we're comparing against existing OpenProject journals that only have second-precision. We need to understand OpenProject's native precision and adapt our approach accordingly.

**Critical Question**: Does the problem occur because:
1. OpenProject stores timestamps at second-precision? (Need 1-second separation)
2. OpenProject stores at millisecond-precision but we're not formatting correctly? (Need millisecond separation)
3. OpenProject uses a different mechanism entirely? (Need to understand and replicate)

Only direct investigation via Rails console can answer these questions definitively.
