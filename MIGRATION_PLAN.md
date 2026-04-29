# Comprehensive Migration Plan

## Phase 1: Environment Preparation
- [ ] 1.1 Kill any running migration processes
- [ ] 1.2 Prune ALL OpenProject data (keep only admin user #1)
- [ ] 1.3 Clear ALL local caches, mappings, and snapshots
- [ ] 1.4 Verify clean state (OP: 1 user, 0 work packages, 0 projects)

## Phase 2: Full Migration Execution
- [ ] 2.1 Recreate tmux session for Rails console
- [ ] 2.2 Run FULL migration with ALL components:
  - users, groups, custom_fields, priorities, link_types
  - issue_types, status_types, resolutions, companies, accounts
  - projects, workflows, agile_boards, sprint_epic
  - work_packages_skeleton, attachments, attachment_provenance
  - work_packages_content, versions, components, labels
  - native_tags, story_points, estimates, security_levels
  - affects_versions, customfields_generic, relations
  - remote_links, inline_refs, watchers, votes_reactions
  - time_entries, category_defaults, admin_schemes, reporting
- [ ] 2.3 Monitor migration progress every 5 minutes
- [ ] 2.4 On error: Stop, diagnose, fix code, re-run from failed component

## Phase 3: Post-Migration Verification
- [ ] 3.1 Verify counts match:
  - Users: Jira vs OP
  - Projects: Jira vs OP
  - Work Packages: Jira issues vs OP work packages
  - Attachments: Jira vs OP
- [ ] 3.2 Check for orphan records
- [ ] 3.3 Verify mapping files are complete

## Phase 4: Data Integrity - 100 Random Issue Comparison
- [ ] 4.1 Select 100 random Jira issues from mapping
- [ ] 4.2 For each issue, compare:
  - Subject/Summary
  - Description content
  - Type, Status, Priority
  - Author, Assignee
  - Created/Updated dates
  - Custom fields
  - Attachments count
  - Comments/Journal entries
- [ ] 4.3 Document any discrepancies

## Phase 5: Log Review & Recommendations
- [ ] 5.1 Review migration logs for warnings/errors
- [ ] 5.2 Identify UX improvements
- [ ] 5.3 Identify DX improvements
- [ ] 5.4 Identify performance bottlenecks
- [ ] 5.5 Identify reliability concerns
- [ ] 5.6 Document recommendations

## Success Criteria
- All components complete without critical errors
- 100% of Jira issues mapped to OP work packages
- 100 random issues pass data integrity check
- No orphan records
- Clear documentation of any known issues
