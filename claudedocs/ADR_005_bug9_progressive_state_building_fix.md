# ADR 005: Bug #9 - Progressive State Building with Timestamp Collision Detection

**Date**: 2025-11-24
**Status**: Resolved
**Issue**: NRS-182 failing to create all 27 journals due to validity period overlaps

## Context

Bug #9 affects the Progressive State Building feature, which creates incremental journal snapshots for work package history migration from Jira to OpenProject. For issue NRS-182, 27 journal entries (v1-v27) needed to be created to represent the complete history, but the migration was failing due to PostgreSQL exclusion constraint violations on the `non_overlapping_journals_validity_periods` constraint.

### Initial Error Pattern
- **Work package created**: Successfully (ID varies per migration)
- **Journals created**: Only some succeeded before failures
- **Error count**: Initially 3 errors, reduced to 2, finally 0
- **Error type**: `PG::ExclusionViolation` - overlapping validity periods

Example conflict:
```
Key (journable_id, journable_type, validity_period)=(5581115, WorkPackage, ["2011-08-18 11:54:44+00","2011-08-23 13:41:21+00"))
conflicts with existing key
(journable_id, journable_type, validity_period)=(5581115, WorkPackage, ["2011-08-18 11:54:44+00","2011-08-18 11:54:44.000001+00"))
```

## Investigation Process

### Discovery 1: Ruby 3.4 Compatibility Issue
**File**: `src/ruby/create_work_package_journals.rb:163, 166-168`

**Problem**: ArgumentError when calling `strftime` on database timestamp objects
```ruby
target_time_str = target_time.to_s(:db)  # FAILED in Ruby 3.4
```

**Solution**: Explicit Time object conversion before strftime
```ruby
target_time_str = target_time.to_time.strftime('%Y-%m-%d %H:%M:%S.%6N%:z')

period_end_time = journal.validity_period.end.is_a?(Time) ?
  journal.validity_period.end :
  Time.parse(journal.validity_period.end.to_s)
period_end_str = period_end_time.strftime('%Y-%m-%d %H:%M:%S.%6N%:z')
```

**Result**: ArgumentError resolved (reduced from 3 to 2 errors)

### Discovery 2: Validity Period Tracking
**File**: `src/ruby/create_work_package_journals.rb:149-156`

**Problem**: `last_used_timestamp` was tracking journal START times, causing the next journal to potentially overlap

**Analysis**:
- Journal v1: `[2011-08-18 11:54:44, 2011-08-18 11:54:44.000001)`
- `last_used_timestamp` set to START: `2011-08-18 11:54:44`
- Journal v2 tries to start at `2011-08-18 11:54:44` → OVERLAP!

**Solution**: Track validity period END times
```ruby
# Update tracker for next operation - use END of validity period
if journal.validity_period.end
  last_used_timestamp = journal.validity_period.end
else
  # Endless range - next operation should use its own timestamp or fallback
  last_used_timestamp = target_time
end
```

**Result**: Better validity period chain, but collisions still occurred (still 2 errors)

### Discovery 3: Timestamp Collision Detection (THE ROOT CAUSE)
**File**: `src/ruby/create_work_package_journals.rb:114-121`

**Problem**: Multiple operations had IDENTICAL timestamps from Jira changelog, causing them to attempt creating journals with the same start time despite validity period END tracking.

**Analysis**:
- Operation 1 timestamp: `2011-08-18 11:54:44`
- Operation 2 timestamp: `2011-08-18 11:54:44` (SAME!)
- Even with END tracking, op 2's explicit timestamp caused overlap

**Log Evidence**:
```
J2O bulk item 0: Op 17 timestamp collision detected:
2018-11-20 16:11:15 UTC <= 2018-11-20 16:11:15 UTC,
adjusted to 2018-11-20 16:11:15.000001 UTC
```

**Solution**: Detect and adjust colliding timestamps
```ruby
if created_at_str && !created_at_str.empty?
  target_time = Time.parse(created_at_str).utc

  # BUG #9 FIX (CRITICAL): Ensure timestamp progression
  # If parsed timestamp is not after last used, bump it forward
  if last_used_timestamp && target_time <= last_used_timestamp
    original_time = target_time
    target_time = last_used_timestamp + SYNTHETIC_TIMESTAMP_INCREMENT_US
    puts "J2O bulk item #{idx}: Op #{op_idx+1} timestamp collision detected: #{original_time} <= #{last_used_timestamp}, adjusted to #{target_time}" if verbose
  else
    puts "J2O bulk item #{idx}: Op #{op_idx+1} using timestamp: #{target_time}" if verbose
  end
elsif last_used_timestamp
  # Synthetic timestamp case (unchanged)
  target_time = last_used_timestamp + SYNTHETIC_TIMESTAMP_INCREMENT_US
  puts "J2O bulk item #{idx}: Op #{op_idx+1} using synthetic timestamp: #{target_time}" if verbose
else
  # Fallback case (unchanged)
  target_time = (rec.created_at || Time.now).utc
  puts "J2O bulk item #{idx}: Op #{op_idx+1} using fallback timestamp: #{target_time}" if verbose
end
```

**Result**: ALL validity period conflicts resolved! Error count: 0

## Complete Solution

### Three-Part Fix Applied

1. **Ruby 3.4 Time Conversion** (lines 163, 166-168)
   - Ensures Time objects before strftime calls
   - Prevents ArgumentError in Ruby 3.4

2. **Validity Period END Tracking** (lines 149-156)
   - Updates `last_used_timestamp` to journal END time
   - Creates proper sequential validity period chains

3. **Timestamp Collision Detection** (lines 114-121)
   - Detects when operation timestamp <= `last_used_timestamp`
   - Bumps colliding timestamps forward by 1 microsecond
   - Maintains historical order while preventing overlaps

### Test Results

**Final Migration**:
- **Work package created**: ID 5581115
- **Created count**: 1
- **Error count**: 0
- **Errors**: [] (empty)
- **Status**: SUCCESS

**All 27 journal operations completed successfully!**

**Collision Detections Observed**:
- Operation 17: timestamp adjusted (2018-11-20 16:11:15)
- Operation 18: timestamp adjusted (2019-01-04 11:45:58)

## Consequences

### Positive
- All 27 journals for NRS-182 created successfully
- No validity period overlaps
- Maintains historical timestamp order
- Ruby 3.4 compatible
- Handles Jira changelog timestamp collisions gracefully

### Technical Debt Cleared
- Removed redundant timestamp checks at lines 106-109 (now handled by line 114-121)
- Consolidated timestamp extraction logic

## Files Modified

- `/home/sme/p/j2o/src/ruby/create_work_package_journals.rb`
  - Lines 114-121: Timestamp collision detection
  - Lines 149-156: Validity period END tracking
  - Lines 163, 166-168: Ruby 3.4 Time conversion

## Related Issues

- Bug #32: Validity period collision detection (similar but different context)
- Bug #6: Timezone consistency in timestamp parsing
- Bug #1: Synthetic timestamp increments

## Verification

### Bulk Result JSON
```json
{
  "result": {
    "status": "success",
    "created_count": 1,
    "error_count": 0,
    "errors": []
  }
}
```

### Migration Log
```
[09:23:33] SUCCESS Component 'work_packages' completed successfully (1/1 items migrated)
[09:23:33] SUCCESS Migration completed successfully in 60.98 seconds
```

## Lessons Learned

1. **Root Cause vs Symptoms**: The validity period overlap was a symptom; the root cause was timestamp collisions from Jira changelog data
2. **Multiple Interacting Fixes**: Required THREE fixes working together - Time conversion, END tracking, AND collision detection
3. **Historical Data Challenges**: Real-world Jira data has timestamp collisions that must be handled
4. **Database Constraints**: PostgreSQL exclusion constraints are strict - even 1 microsecond overlap is rejected

## References

- Test log: `/tmp/bug9_timestamp_collision_fix.log`
- Bulk result: `/home/sme/p/j2o/var/data/bulk_result_NRS_20251124_082333.json`
- Issue: NRS-182 (Jira ID: 23023)
