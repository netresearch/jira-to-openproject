# Migration Re-run Plan

## Overview
Full migration re-run with data pruning, error monitoring, and quality verification.

---

## Phase 1: Data Pruning

### 1.1 Prune OpenProject Data
Delete all OP data except admin user (#1):
```ruby
# In Rails console - ORDER MATTERS (dependencies)
WorkPackage.destroy_all
Journal.destroy_all
Attachment.destroy_all
Project.destroy_all
Group.destroy_all
User.where.not(id: 1).destroy_all
CustomField.destroy_all
Type.where.not(is_standard: true).destroy_all
Status.where.not(is_default: true).destroy_all
IssuePriority.destroy_all
Enumeration.destroy_all
```

### 1.2 Clear Local Caches/Mappings
```bash
rm -f var/data/*_mapping.json
rm -f var/data/jira_*.json
rm -f var/data/openproject_*.json
rm -f var/data/tempo_*.json
rm -f var/data/work_package_*.json
rm -f var/data/bulk_update_*.json
```

---

## Phase 2: Full Migration (Monitored)

### Migration Components (in order)
| # | Component | Description | Est. Items |
|---|-----------|-------------|------------|
| 1 | users | Jira users → OP users | ~450 |
| 2 | groups | Jira groups → OP groups | ~20 |
| 3 | custom_fields | Jira CFs → OP CFs | ~170 |
| 4 | priorities | Jira priorities → OP priorities | ~10 |
| 5 | link_types | Issue link types | ~11 |
| 6 | issue_types | Jira types → OP types | ~87 |
| 7 | status_types | Jira statuses → OP statuses | ~69 |
| 8 | resolutions | Jira resolutions | ~10 |
| 9 | companies | Tempo companies → OP projects | ~77 |
| 10 | accounts | Tempo accounts | ~182 |
| 11 | projects | Jira projects → OP subprojects | ~237 |
| 12 | workflows | Status workflows | varies |
| 13 | agile_boards | Jira boards → OP boards | varies |
| 14 | sprint_epic | Sprints and epics | varies |
| 15 | **work_packages_skeleton** | WP skeletons (Phase 1) | **~69,000** |
| 16 | attachments | File attachments | varies |
| 17 | attachment_provenance | Attachment metadata | varies |
| 18 | **work_packages_content** | WP content (Phase 2) | **~69,000** |
| 19 | versions | Jira versions | varies |
| 20 | components | Jira components | varies |
| 21 | labels | Jira labels | varies |
| 22 | native_tags | OP tags | varies |
| 23 | story_points | Story point CFs | varies |
| 24 | estimates | Time estimates | varies |
| 25 | security_levels | Security levels | varies |
| 26 | affects_versions | Affected versions | varies |
| 27 | customfields_generic | Generic CFs | varies |
| 28 | relations | Issue links → WP relations | varies |
| 29 | remote_links | External links | varies |
| 30 | inline_refs | Inline references | varies |
| 31 | watchers | Jira watchers → OP watchers | varies |
| 32 | votes_reactions | Votes | varies |
| 33 | time_entries | Tempo worklogs → OP time | varies |
| 34 | category_defaults | Category settings | varies |
| 35 | admin_schemes | Admin configurations | varies |
| 36 | reporting | Final reports | - |

### Run Command
```bash
uv run python -m src.main migrate --profile full --stop-on-error --no-confirm 2>&1 | tee var/logs/full_migration_$(date +%Y%m%d_%H%M%S).log
```

### Error Handling Protocol
On ANY error or warning:
1. STOP migration immediately
2. Analyze error in log
3. Fix root cause in code
4. Resume or restart component

---

## Phase 3: Log Review

### Review Categories
1. **UX Issues**: User-facing messages, progress indicators, clarity
2. **DX Issues**: Developer experience, debugging info, error messages
3. **Performance**: Slow operations, timeouts, batch sizes
4. **Reliability**: Retries, error recovery, data integrity

### Log Analysis Commands
```bash
# Errors
grep -i "error\|exception\|failed" var/logs/full_migration_*.log

# Warnings
grep -i "warning\|warn" var/logs/full_migration_*.log

# Timing
grep -i "took\|seconds\|duration" var/logs/full_migration_*.log

# Skipped items
grep -i "skipped\|skip" var/logs/full_migration_*.log
```

---

## Phase 4: Quality Verification

### 4.1 Count Verification
| Source | Expected | Actual |
|--------|----------|--------|
| Jira issues | 68,928 | ? |
| OP work packages | 68,928 | ? |
| Jira projects | 237 | ? |
| OP projects | 237 | ? |

### 4.2 Random Sample Comparison (100 issues)
For each of 100 randomly selected Jira issues, verify:
- [ ] Subject matches summary
- [ ] Type matches issue type
- [ ] Status matches status
- [ ] Priority matches priority
- [ ] Author matches reporter
- [ ] Assignee matches assignee
- [ ] Created date matches
- [ ] Updated date matches
- [ ] Description content matches
- [ ] Comments count matches
- [ ] Attachments count matches

### Comparison Script
```python
# Select 100 random Jira issue keys
# For each: fetch Jira data, fetch OP data, compare fields
# Report: matches, mismatches, missing
```

---

## Execution Checklist

- [ ] Phase 1.1: Prune OP data
- [ ] Phase 1.2: Clear local caches
- [ ] Phase 2: Run full migration (monitor for errors)
- [ ] Phase 3: Review logs for improvements
- [ ] Phase 4.1: Verify counts
- [ ] Phase 4.2: Compare 100 random issues
- [ ] Document findings and improvements

---

## Success Criteria

1. **Zero errors** in migration log
2. **Issue counts match** (Jira vs OP)
3. **100% field accuracy** in random sample
4. **All timestamps preserved** (created_at, updated_at)
5. **All metadata correct** (priority, author, assignee)
