# Bug #9: Error Checking Investigation Status

## Date
2025-11-21 15:43

## Problem Summary
Only 21 of 27 expected journals are being created for NRS-182, despite implementing comprehensive error checking code.

## Investigation Timeline

### 1. Root Cause Identified (Previous Session)
- **Finding**: `journal.save(validate: false)` returns false but doesn't raise exception
- **Location**: `/home/sme/p/j2o/src/ruby/create_work_package_journals.rb:302`
- **Impact**: Operations 22-27 fail silently, no error logging
- **Evidence**: Database query showed only 1 journal for work package 5581104

### 2. Error Checking Implemented (Current Session)
- **Location**: Lines 302-327 in Ruby template
- **Features**:
  - Capture `save_result` variable
  - Check `!save_result || !journal.persisted?`
  - Log `journal.errors.full_messages`
  - Propagate errors to Python layer
  - Early exit with `next` keyword

### 3. Test Migration Run (Current Session)
- **Command**: `export J2O_TEST_ISSUES="NRS-182" J2O_BULK_RUBY_VERBOSE=1 && python3 src/main.py migrate --components work_packages --jira-project-filter NRS --force --no-confirm`
- **Log File**: `/tmp/bug9_error_checking_test.log`
- **Result**: Only 21 journals created again
- **Unexpected**: NO ERROR messages from new error checking code

### 4. Misleading Warning Message Discovered
- **Location**: Line 143 in Ruby template
- **Message**: "WARNING - Op 22 journal not persisted, cannot update validity_period!"
- **Context**: Inside `apply_timestamp_and_validity` lambda
- **Timing**: Runs BEFORE save() operation at line 302
- **Misleading Because**:
  - New journals are `persisted?=false` before save()
  - This WARNING is printed even for journals that will successfully save
  - The WARNING appears in logs, making it look like save() failed
  - But save() hasn't happened yet when WARNING is printed!

## Current Mystery

**Why isn't the error checking code (lines 305-327) being executed?**

### Evidence:
1. ✅ Error checking code is definitely in the Ruby template (verified via Read tool)
2. ✅ Migration was run with `J2O_BULK_RUBY_VERBOSE=1` flag
3. ✅ Log shows "Using state_snapshot for v22 (15 fields)"
4. ✅ Log shows "Op 22 using timestamp: 2024-08-22 08:40:45 UTC"
5. ✅ Log shows "Op 22 before update: persisted=false"
6. ✅ Log shows WARNING from line 143 (misleading, runs BEFORE save)
7. ❌ Log does NOT show "ERROR - Failed to save journal v22" (should be from line 309)
8. ❌ Log does NOT show "ERROR - Validation errors: ..." (should be from line 310)
9. ❌ Only 21 journals created instead of 27

### Possible Explanations:
1. **Code execution stops before line 302?** - No, log shows "Op 22 before update" from line 131
2. **save() succeeds but journal doesn't persist?** - Contradicts ActiveRecord behavior
3. **Error checking condition never triggers?** - Would mean save() succeeded
4. **Exception thrown before error checking?** - Would be caught by rescue block
5. **Different code path for bulk create?** - Need to verify which Ruby template is actually used
6. **Rails console client caching old template?** - MOST LIKELY CAUSE!

## Critical Discovery: Template Caching Issue

The Rails console client may be caching the old Ruby template code! Evidence:
- Log shows OLD WARNING message from line 143
- Log does NOT show NEW ERROR messages from lines 309-310
- This suggests the Rails console is executing an old cached version of the template

### Action Required:
1. Restart Rails console to clear template cache
2. Re-run migration test
3. Verify NEW error messages appear in logs

## Code Structure (For Reference)

### Lambda: `apply_timestamp_and_validity` (Lines 117-147)
```ruby
apply_timestamp_and_validity = lambda do |journal, op_idx, created_at_str|
  # ... set validity_period ...

  if journal.persisted?
    result = journal.update_columns(...)  # Line 136
  else
    puts "WARNING - Op #{op_idx+1} journal not persisted, cannot update validity_period!"  # Line 143
  end

  target_time
end
```

**Note**: This lambda runs BEFORE save(), so `persisted?=false` is NORMAL!

### Save Operation (Lines 299-335)
```ruby
# Line 299: Call lambda BEFORE save
target_time = apply_timestamp_and_validity.call(journal, op_idx, created_at_str)

# Line 302: Save journal
save_result = journal.save(validate: false)

# Lines 305-327: Error checking (NEW CODE - may not be executed due to caching!)
if !save_result || !journal.persisted?
  error_msg = "Failed to save journal v#{current_version}"
  error_details = journal.errors.full_messages.join(", ")

  puts "J2O bulk item #{idx}: ERROR - #{error_msg}" if verbose
  puts "J2O bulk item #{idx}: ERROR - Validation errors: #{error_details}" if verbose
  # ... propagate errors ...

  next  # Skip to next operation
end

# Line 330: Update timestamps (only if save succeeded)
journal.update_columns(created_at: target_time, updated_at: target_time)

# Line 335: Success message
puts "J2O bulk item #{idx}: Created journal v#{journal.version} (op #{op_idx+1}/#{ops.length})" if verbose
```

## Next Steps

1. **RESTART Rails console** to clear template cache
2. **Re-run migration** with verbose logging
3. **Check logs** for NEW error messages from lines 309-310
4. **If still no errors**: Add explicit debug logging before/after save() call
5. **If errors appear**: Analyze validation error messages to identify root cause

## Files Referenced
- Ruby Template: `/home/sme/p/j2o/src/ruby/create_work_package_journals.rb`
- Test Log: `/tmp/bug9_error_checking_test.log`
- Work Package Mapping: `/home/sme/p/j2o/var/data/work_package_mapping.json`
- Bulk Result: `/home/sme/p/j2o/var/data/bulk_result_NRS_20251121_143251.json`
