# ADR 001: OpenProject Journal Creation for Migrated Work Packages

## Status
SUPERSEDED by ADR_003 - 2025-11-10
ORIGINALLY ACCEPTED - 2025-11-07

**NOTE**: This ADR documents the initial discovery of Bugs #17 and #18. For the complete journal migration journey including all bug fixes (Bugs #17-25), see **[ADR_003: Journal Migration Complete Journey](ADR_003_journal_migration_complete_journey.md)**.

## Context

During Jira to OpenProject migration, we need to create journal entries (comments and changelog) for work packages to preserve the complete history from Jira. OpenProject uses a complex journaling system with specific requirements.

## Investigation Summary

### OpenProject Journal Architecture

1. **Two-Table Design**:
   - `journals` table: Contains journal metadata (user_id, version, notes, validity_period)
   - `work_package_journals` table: Contains work package state snapshot (all WP attributes including author_id)

2. **Key Relationships**:
   ```ruby
   Journal.new(
     journable_id: wp.id,
     journable_type: 'WorkPackage',
     user_id: 1,              # Who created THIS journal entry
     notes: 'comment text',
     version: 2
   )

   Journal::WorkPackageJournal.new(
     type_id: wp.type_id,
     project_id: wp.project_id,
     author_id: wp.author_id,  # REQUIRED: Original WP author
     # ... all other WP attributes ...
   )
   ```

3. **Critical Constraints**:
   - `work_package_journals.author_id`: **NOT NULL** constraint
   - `journals.version`: **UNIQUE** per (journable_id, journable_type)
   - `validity_period`: PostgreSQL `tstzrange` type for temporal validity

## Bugs Discovered

### Bug #17: Missing author_id in WorkPackageJournal
**Symptoms**: All journal creation attempts fail silently with PostgreSQL NOT NULL violation

**Root Cause**: `openproject_client.py:2711-2737` creates WorkPackageJournal without setting `author_id`

**Evidence**:
```
PG::NotNullViolation: ERROR:  null value in column "author_id" of relation "work_package_journals" violates not-null constraint
```

**Impact**: **100% of comments fail to be created** despite:
- ✅ Journal data correctly prepared (22 create_comment operations for NRS-182)
- ✅ Data transmitted to bulk creation (confirmed in JSON files)
- ✅ Ruby code executes without exceptions (fails silently at DB level)
- ❌ All journals rejected by PostgreSQL before insertion

### Bug #18: No Error Logging from Ruby
**Symptoms**: Journal creation failures happen silently with no visible errors

**Root Cause**: Ruby bulk creation script catches all exceptions with bare `rescue` blocks

**Impact**: Debugging requires manual Rails console testing

## Decision

### Fix Bug #17: Add author_id to WorkPackageJournal

**Location**: `src/clients/openproject_client.py:2711-2737`

**Change Required**:
```ruby
wp_journal_data = Journal::WorkPackageJournal.new(
  type_id: rec.type_id,
  project_id: rec.project_id,
  subject: rec.subject,
  description: rec.description,
  # ... other fields ...
  author_id: rec.author_id,  # ADD THIS LINE
  # ... remaining fields ...
)
```

### Validity Period Requirements

Based on OpenProject migration (20230608151123):
- Use PostgreSQL `tstzrange` (timestamp with timezone range)
- Format: `start_timestamp..end_timestamp` (Ruby Range object)
- For migration: Start at journal timestamp, end at next journal timestamp or Time.now.utc
- Critical: Overlapping ranges violate exclusion constraint `non_overlapping_journals_validity_periods`

## Test Plan

1. **Manual Test** (✅ EXECUTED):
   - Created test journal in Rails console
   - Discovered author_id NOT NULL constraint
   - Confirmed adding author_id allows creation

2. **Integration Test** (PENDING):
   - Run 10-issue test with NRS-182 (has 22 journal entries)
   - Verify all 22 comments created successfully
   - Check validity_periods don't overlap

3. **Full Migration** (PENDING):
   - Run complete NRS migration (3,828 issues)
   - Validate journal counts match Jira

## Consequences

### Positive
- Complete journal history migrated from Jira
- Comments and changelog preserved
- Temporal validity tracking enabled

### Negative
- Additional complexity in bulk creation script
- Must handle validity_period overlaps carefully
- Requires all WP attributes in journal snapshot

### Risks Mitigated
- ✅ Silent failures now understood
- ✅ Root cause identified (missing author_id)
- ✅ Fix straightforward (add one field)

## References

- OpenProject Journal Model: https://github.com/opf/openproject/blob/dev/app/models/journal.rb
- Migration: `20230608151123_add_validity_period_to_journals.rb`
- Bug Reports: `/home/sme/p/j2o/claudedocs/bug10_missing_issues_root_cause_analysis.md`
- Implementation: `src/clients/openproject_client.py:2690-2749`

## Lessons Learned

1. **Always test journal creation manually first** before bulk operations
2. **PostgreSQL constraints are strict** - NOT NULL violations fail silently in rescue blocks
3. **Two-table design requires synchronization** - Journal + WorkPackageJournal must both succeed
4. **Version uniqueness** matters - must track max_version per work package
5. **Verbose logging environment variables** may not propagate to Rails runner context

## Next Actions

1. ✅ Document findings in this ADR
2. ✅ Fix author_id in openproject_client.py
3. ✅ Test with NRS-182 (22 journals expected)
4. ⏳ Run full 10-issue test (blocked by Bug #25)
5. ⏳ Run complete NRS migration
6. ⏳ Validate all ~85,000 journal entries created successfully

## See Also

- **[ADR_003: Journal Migration Complete Journey](ADR_003_journal_migration_complete_journey.md)** - Comprehensive documentation of all bugs (#17-25) and complete implementation
- [ADR_002: Journal Migration Three Bug Fixes](ADR_002_journal_migration_three_bug_fixes.md) - Bugs #22, #23, #24
