# ADR 006: Bug #9 - Activities Page 500 Error Investigation

**Date**: 2025-11-24
**Status**: Under Investigation
**Issue**: NRS-182 activities page returns HTTP 500 error despite successful migration

## Context

After implementing the complete Bug #9 fix (timestamp collision detection), the migration reported complete success:
- Work package created: ID 5581115
- Created count: 1
- Error count: 0
- Errors: [] (empty)

However, when the user attempts to access the activities page at `https://openproject.sobol.nr/work_packages/5581115/activities`, the server returns HTTP 500 Internal Server Error.

## Current Status

### Migration Success Indicators
- ✅ Migration completed in 60.98 seconds
- ✅ Bulk result shows `error_count: 0`
- ✅ Work package ID 5581115 created
- ✅ No PostgreSQL constraint violations
- ✅ Timestamp collision detection working (ops 17, 18 adjusted)

### Failure Indicators
- ❌ Activities page returns HTTP 500 error
- ❌ Cannot access work package activities via UI
- ❌ Indicates journal data may have integrity issues not caught by migration

## Investigation Attempted

### 1. HTTP Status Check
```bash
curl -s -I "https://openproject.sobol.nr/work_packages/5581115/activity"
```
**Result**: HTTP 302 redirect to login page (authentication required)

### 2. Docker Container Status
```bash
docker ps --filter "name=openproject"
```
**Result**: Only `j2o-mock-openproject-1` running (Prism mock server), no production OpenProject container accessible locally

### 3. Rails Console Query Attempt
Attempted to query journal count via RailsConsoleClient but query hung without completing.

## Architecture Understanding

### Current Setup
1. **Local Environment**:
   - Migration runs locally via Python scripts
   - Posts to OpenProject API (either mock or real)
   - tmux Rails console session exists locally

2. **Production Environment**:
   - Real OpenProject instance at `https://openproject.sobol.nr`
   - Receives API calls from migration
   - Stores actual work package and journal data
   - UI access requires authentication

3. **Isolation**:
   - Cannot directly access production OpenProject logs
   - Cannot directly query production database
   - Must rely on API access or user reports for production issues

## Hypothesis

The journal records were created via API calls and passed OpenProject's API validation, but contain data that causes rendering failures when the UI attempts to display the activities page.

### Possible Root Causes

1. **Invalid Journal Data**:
   - `customizable_data` field contains malformed JSON or invalid structure
   - Journal notes contain special characters not properly escaped
   - Data fields violate OpenProject UI expectations but not API constraints

2. **Missing References**:
   - User IDs in journal.user_id don't exist in OpenProject
   - Referenced custom fields don't exist
   - Referenced status/type/priority IDs are invalid

3. **Validity Period Issues**:
   - Despite no PostgreSQL errors, validity periods may have logical issues
   - Endless ranges or edge cases not handled by UI renderer
   - Timestamp precision issues in display logic

4. **Journal Data Fields**:
   - Missing required fields that API doesn't enforce but UI requires
   - NULL values in fields that UI assumes are populated
   - Array vs scalar mismatches in field values

## Investigation Blocked

Cannot proceed with investigation because:

1. **No Production Log Access**: Cannot see actual 500 error stack trace from OpenProject application
2. **No Production Database Access**: Cannot query journal records directly to inspect their structure
3. **Authentication Required**: Cannot test activities page access without credentials
4. **Rails Console Unclear**: Local tmux Rails console connection unclear - may not be connected to production

## Required Next Steps

### For User to Provide

1. **OpenProject Application Logs**:
   ```bash
   # On production OpenProject server
   tail -200 /var/log/openproject/production.log | grep -A 20 "5581115"
   ```
   OR
   ```bash
   docker logs <openproject-container-name> --tail 200 | grep -A 20 "5581115"
   ```

2. **Database Journal Inspection**:
   ```ruby
   # In Rails console on production
   wp = WorkPackage.find(5581115)
   journals = Journal.where(journable_id: 5581115, journable_type: "WorkPackage").order(:version)

   puts "Journal count: #{journals.count}"

   # Check for NULL user IDs
   null_users = journals.select { |j| j.user_id.nil? }
   puts "Journals with NULL user_id: #{null_users.count}"

   # Check validity periods
   journals.each do |j|
     puts "v#{j.version}: #{j.validity_period.inspect} user=#{j.user_id}"
   end

   # Check customizable_data structure
   journals.first(3).each do |j|
     puts "v#{j.version} customizable_data keys: #{j.customizable_data&.keys&.join(', ')}"
   end
   ```

3. **Actual Error Message**:
   - Access the activities page while tailing logs
   - Capture the full stack trace
   - Note which specific journal or operation causes the error

## Potential Fixes (Once Root Cause Identified)

### If Missing User References
```ruby
# Assign journals to a system user
system_user = User.admin.first
Journal.where(journable_id: 5581115, user_id: nil).update_all(user_id: system_user.id)
```

### If Invalid customizable_data
```ruby
# Reset to empty hash
Journal.where(journable_id: 5581115).each do |j|
  if j.customizable_data&.is_a?(String)
    j.update(customizable_data: {})
  end
end
```

### If Validity Period Issues
```ruby
# Rebuild validity periods from timestamps
journals = Journal.where(journable_id: 5581115).order(:version)
journals.each_with_index do |j, idx|
  next_j = journals[idx + 1]
  if next_j
    j.update(validity_period: Range.new(j.created_at, next_j.created_at, true))
  else
    j.update(validity_period: Range.new(j.created_at, nil, true))
  end
end
```

### If Invalid Field Values
```ruby
# Check and fix data types
Journal.where(journable_id: 5581115).each do |j|
  j.data.each do |attr, values|
    # Ensure arrays are arrays, scalars are scalars
    if values.is_a?(Array) && values.one?
      j.data[attr] = values.first
    end
  end
  j.save
end
```

## Lessons Learned

1. **API Validation ≠ UI Compatibility**: Migration API calls reporting success doesn't guarantee UI will render the data correctly
2. **Need Integration Tests**: Should verify activities page rendering after migration, not just API success
3. **Need Production Access**: Investigation of production issues requires production log/database access
4. **Validation Gaps**: OpenProject API may accept data that UI cannot render

## Files Referenced

- Work Package ID: 5581115
- Bulk result: `/home/sme/p/j2o/var/data/bulk_result_NRS_20251124_082333.json`
- Migration log: `/tmp/bug9_timestamp_collision_fix.log`
- Previous ADR: `ADR_005_bug9_progressive_state_building_fix.md`

## Status Summary

**Migration**: ✅ SUCCESS (all 27 operations completed, 0 errors)
**Functionality**: ❌ BLOCKED (activities page returns 500 error)
**Investigation**: 🔍 PAUSED (requires production access for root cause analysis)

## Next Action

User must provide either:
1. Production OpenProject logs showing the 500 error stack trace
2. Production database access to inspect journal records
3. Authentication credentials to test activities page access
