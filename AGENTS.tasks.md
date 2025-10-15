# AGENTS.tasks.md - Task Management with bd

**CRITICAL**: This project uses `bd` (beads) as the **single source of truth** for ALL task tracking, issue management, bug tracking, and work planning. No exceptions.

## Core Principle

**ALL work MUST be tracked in bd.** We do not use:
- ❌ TODO/FIXME/XXX/HACK comments in code (without bd reference)
- ❌ Markdown files for plans, tracking, roadmaps, or backlogs
- ❌ Markdown checklists (`- [ ]`) for task tracking
- ❌ Separate planning documents for refactoring, implementation, features, reviews, evaluations
- ❌ Comments like "we need to..." or "future work:" without creating bd issue

## bd Workflow

### 1. Finding Work

**When asked "what's next?" or "what should I work on?":**
```bash
# Check open issues by priority
bd list --status open --priority 1

# Check all open issues
bd list --status open

# Find ready work (no blockers)
bd ready --json | jq '.[0]'
```

**ALWAYS use bd to determine next tasks.** Never guess or suggest work without checking bd first.

### 2. Creating Issues

**MANDATORY: Create bd issue for ANY discovered work:**

```bash
# Create a bug
bd create "Description of bug" -t bug -p 1

# Create a task
bd create "Description of task" -t task -p 1

# Create a feature
bd create "Description of feature" -t feature -p 2

# With dependencies
bd create "Fix authentication" -t bug -p 1
bd dep add j2o-123 j2o-100 --type blocks
```

**Issue Types:**
- `task` - Work items, improvements, refactoring
- `bug` - Defects, errors, broken functionality
- `feature` - New capabilities, enhancements

**Priority Levels:**
- `1` (P1) - Critical, blocking, must-do
- `2` (P2) - Important, should-do
- `3` (P3) - Nice-to-have, could-do

### 3. Updating Issues During Work

**Track progress by updating issues throughout development:**

```bash
# Mark as in progress (when you start)
bd update j2o-123 --status in_progress

# Add planning notes
bd comment j2o-123 "Planning: Will implement ETL pattern with 3 phases"

# Add implementation details
bd comment j2o-123 "Implementation: Added _extract(), _map(), _load() methods"

# Add findings/results
bd comment j2o-123 "Found 5 related files that need updates"

# Link discovered work
bd create "Related bug found during implementation" -t bug -p 2
bd dep add j2o-124 j2o-123 --type discovered-from
```

### 4. Completing Work

**Close issues with detailed completion information:**

```bash
# Close with reason
bd close j2o-123 -r "✅ Implemented ETL pattern. Added tests. Commit: abc1234"

# For bugs - include fix details
bd close j2o-124 -r "✅ Fixed by validating input. Added regression test. Root cause: missing validation"

# For features - include outcomes
bd close j2o-125 -r "✅ Added caching layer. Performance improved 3x. Coverage: 95%"
```

## Automatic Issue Creation

**AI agents MUST automatically create bd issues when:**

1. **Discovering bugs** - Any defect, error, or broken functionality
2. **Finding TODOs** - Any TODO/FIXME/HACK/XXX marker in code
3. **Identifying improvements** - Performance issues, code quality problems
4. **Planning refactoring** - Architecture changes, code cleanup
5. **Detecting missing features** - Gaps in functionality
6. **Noting technical debt** - Shortcuts, workarounds, temporary solutions

**Process:**
```bash
# 1. Create issue immediately
bd create "Found TODO in user_migration.py line 45" -t task -p 2

# 2. Update code with reference
# Before: # TODO: implement validation
# After:  # TODO(j2o-126): implement validation

# 3. Link to current work
bd dep add j2o-126 j2o-123 --type discovered-from
```

## TODO Comments Policy

**Forbidden without bd reference:**
```python
# ❌ WRONG - No bd reference
# TODO: implement this feature

# ❌ WRONG - Vague TODO
# FIXME: this is broken

# ✅ CORRECT - With bd reference
# TODO(j2o-126): implement validation (see bd issue for details)

# ✅ CORRECT - With context
# FIXME(j2o-127): race condition in concurrent updates (documented in bd)
```

**When you find unmarked TODOs:**
1. Create bd issue immediately
2. Update code comment with reference
3. Add details to bd issue

## Checking Open Work

**Before starting any work session:**
```bash
# Review all open issues
bd list --status open

# Check your current work
bd list --status in_progress

# Find blockers
bd list --status blocked

# Check dependencies
bd show j2o-123  # Shows depends-on and blocks relationships
```

## Planning in bd

**Use bd for ALL planning activities:**

### Architecture Planning
```bash
bd create "Design caching layer for API calls" -t feature -p 1
bd comment j2o-130 "Architecture:
- Cache layer: Redis with TTL
- Cache keys: entity_type:entity_id
- Invalidation: on write operations
- Fallback: direct API call on cache miss"
```

### Implementation Planning
```bash
bd create "Implement user migration caching" -t task -p 1
bd comment j2o-131 "Implementation plan:
1. Add CacheManager class
2. Update UserMigration to use cache
3. Add cache invalidation hooks
4. Add tests for cache behavior"
```

### Refactoring Planning
```bash
bd create "Refactor client architecture" -t task -p 1
bd comment j2o-132 "Refactoring plan:
- Extract SSHClient from OpenProjectClient
- Create DockerClient for container ops
- Update RailsConsoleClient dependencies
Affected files: 12 files in src/clients/"
```

## Review and Evaluation in bd

**Track reviews and evaluations as tasks:**

```bash
# Code review
bd create "Review work package migration implementation" -t task -p 2
bd comment j2o-133 "Review findings:
- Performance: Good, uses batching
- Tests: Need 3 more edge cases
- Security: Input validation present
Action items: Create separate issues for test gaps"

# Performance evaluation
bd create "Evaluate migration performance" -t task -p 2
bd comment j2o-134 "Performance evaluation:
- 1000 issues: 5 minutes
- 10000 issues: 52 minutes
- Bottleneck: API rate limiting
Recommendation: Implement caching (j2o-130)"
```

## Migration to bd

**When migrating existing planning docs to bd:**

```bash
# 1. Read the planning document
# 2. Create issues for each work item
bd create "Item 1 from plan.md" -t task -p 1
bd create "Item 2 from plan.md" -t task -p 1

# 3. Link related issues
bd dep add j2o-136 j2o-135 --type blocks

# 4. Delete the planning document
rm docs/plans/implementation-plan.md

# 5. Update .gitignore to prevent future planning docs
echo "docs/plans/*.md" >> .gitignore
```

## bd Commands Quick Reference

### Viewing Issues
```bash
bd list                          # All issues
bd list --status open            # Open issues
bd list --status open -p 1       # Open P1 issues
bd list --type bug               # All bugs
bd show j2o-123                  # Show issue details
bd ready                         # Ready to work (no blockers)
```

### Creating Issues
```bash
bd create "Title" -t task -p 1         # Create task
bd create "Title" -t bug -p 1          # Create bug
bd create "Title" -t feature -p 2      # Create feature
```

### Updating Issues
```bash
bd update j2o-123 --status in_progress   # Mark in progress
bd update j2o-123 --status blocked       # Mark blocked
bd update j2o-123 --priority 1           # Change priority
bd comment j2o-123 "Additional info"     # Add comment
```

### Dependencies
```bash
bd dep add j2o-124 j2o-123 --type blocks          # 124 blocks 123
bd dep add j2o-125 j2o-123 --type depends-on      # 125 depends on 123
bd dep add j2o-126 j2o-123 --type discovered-from # 126 found during 123
bd dep list j2o-123                                # Show dependencies
```

### Completing Work
```bash
bd close j2o-123 -r "Completion details"   # Close issue
bd reopen j2o-123 -r "Reopening because"   # Reopen if needed
```

## Integration with Development Workflow

### Before Starting Work
```bash
# 1. Check what's next
bd list --status open -p 1

# 2. Pick an issue
bd show j2o-123

# 3. Mark as in progress
bd update j2o-123 --status in_progress

# 4. Create feature branch
git checkout -b feature/j2o-123-description
```

### During Work
```bash
# Add planning notes
bd comment j2o-123 "Planning: Implementing ETL pattern"

# Create discovered issues
bd create "Found edge case" -t bug -p 2
bd dep add j2o-150 j2o-123 --type discovered-from

# Update with progress
bd comment j2o-123 "Progress: Completed _extract() and _map()"
```

### Completing Work
```bash
# 1. Commit changes
git commit -m "feat(scope): implement feature (j2o-123)"

# 2. Close issue with details
bd close j2o-123 -r "✅ Implemented. Tests added. Commit: abc1234"

# 3. Merge to main
git checkout main && git merge feature/j2o-123-description
```

## Quality Gates

**Before closing any issue:**
1. ✅ All tests pass
2. ✅ Linting/type checks pass
3. ✅ Code committed with issue reference
4. ✅ Related issues created for discovered work
5. ✅ Completion details added to bd

## Examples

### Example 1: Bug Discovery
```bash
# During code review, found a bug
bd create "User migration fails on null email" -t bug -p 1
bd comment j2o-140 "Bug: user_migration.py line 45 crashes on users with email=None
Stacktrace: [paste stacktrace]
Affects: ~5% of users based on data analysis
Fix: Add null check before email validation"

# Update code with TODO
# TODO(j2o-140): Add null email handling

# Close when fixed
bd close j2o-140 -r "✅ Added null check. Added regression test. Verified on test data"
```

### Example 2: Feature Planning
```bash
# Plan new feature
bd create "Add caching layer for API calls" -t feature -p 1
bd comment j2o-141 "Architecture:
- Use Redis for caching
- TTL: 5 minutes for entity lookups
- Cache keys: f'{entity_type}:{entity_id}'
Implementation phases:
1. Add CacheManager class
2. Update clients to use cache
3. Add cache invalidation
4. Add metrics/monitoring"

# Break into tasks
bd create "Implement CacheManager class" -t task -p 1
bd dep add j2o-142 j2o-141 --type blocks

bd create "Update JiraClient for caching" -t task -p 1
bd dep add j2o-143 j2o-142 --type depends-on

bd create "Add cache metrics" -t task -p 2
bd dep add j2o-144 j2o-141 --type depends-on
```

### Example 3: Refactoring
```bash
# Plan refactoring
bd create "Refactor client architecture" -t task -p 1
bd comment j2o-145 "Current issues:
- OpenProjectClient has too many responsibilities
- SSH/Docker logic mixed with business logic
- Hard to test in isolation

Refactoring plan:
1. Extract SSHClient (base SSH operations)
2. Extract DockerClient (container operations)
3. Update RailsConsoleClient dependencies
4. Update OpenProjectClient to orchestrate

Affected files:
- src/clients/openproject_client.py (major)
- src/clients/rails_console_client.py (medium)
+ src/clients/ssh_client.py (new)
+ src/clients/docker_client.py (new)

Tests: Need 15 new unit tests for extracted clients"

# Track progress
bd comment j2o-145 "Progress: Completed SSHClient extraction"
bd comment j2o-145 "Progress: Completed DockerClient extraction"
bd close j2o-145 -r "✅ Refactored client architecture. All tests pass. 15 new tests added"
```

## Summary

**Remember:**
- ✅ ALL work tracked in bd
- ✅ Create issues immediately when discovering work
- ✅ Update issues during development
- ✅ Close issues with detailed completion info
- ✅ Use bd to check what's next
- ❌ No TODO comments without bd reference
- ❌ No markdown files for planning/tracking
- ❌ No markdown checklists for tasks

**bd is the single source of truth for all task management in this project.**
