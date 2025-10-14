# Task Completion Checklist

## Before Marking a Task Complete

### 1. Code Quality Checks
```bash
# Format code
make format                      # or: uvx ruff format src tests

# Run linting
make lint                        # or: uvx ruff check --fix src tests && uvx mypy src

# Type checking (currently scoped to config_loader.py)
make type-check                  # or: uvx mypy src
```

### 2. Testing Requirements

#### Fast Feedback Loop
```bash
# Run fast unit tests locally (recommended for rapid iteration)
make dev-test-fast               # Runs unit tests excluding slow/integration

# Or run all local tests
make dev-test                    # Full local test suite with pytest-xdist
```

#### Container-Based Validation
```bash
# Run unit tests in Docker (ensures native dependencies like libffi)
make container-test

# For integration tests (when applicable)
make container-test-integration
```

### 3. Component-Specific Checks

#### Work Package or Project Metadata Changes
```bash
# Run specific test suites
uv run --active --no-cache pytest -q \
  tests/unit/test_enhanced_timestamp_migrator.py \
  tests/unit/test_work_package_start_date.py \
  tests/functional/test_project_migration.py::TestProjectMigration::test_assign_project_lead_happy_path
```

#### JSON Payload Sanitization (Work Package Flows)
```bash
# Verify _links removal and ID flattening
uv run python -m pytest tests/unit/test_wp_json_clean.py -q
```

#### Checkpoint/Fast-Forward Logic Changes
```bash
# Verify checkpoint integrity
make container-test TEST_OPTS="-k work_package_checkpoint"

# Full rehearsal validation
python scripts/run_rehearsal.py --use-container --collect --stop
```

### 4. Documentation Updates

#### When to Update Docs
- ✅ New environment variables added
- ✅ Workflow or command changes
- ✅ Migration component behavior changes
- ✅ Security or validation rule changes

#### Files to Check
```bash
# Core documentation
README.md                        # If user-facing changes
docs/DEVELOPER_GUIDE.md          # If dev workflow changes
src/AGENTS.md                    # If src/ conventions change
tests/AGENTS.md                  # If test patterns change

# Configuration
.env.example                     # If new env vars added
config/config.yaml               # If config schema changes
```

### 5. Security Validation

#### For User Input Processing
```bash
# Run security-focused tests
pytest tests/unit/test_security_validation.py -k {component}
```

#### Input Validation Checklist
- ✅ All Jira keys validated (length, charset, format)
- ✅ No direct string interpolation in Ruby scripts
- ✅ Use Ruby `.inspect` method for safe escaping
- ✅ JSON payloads sanitized (remove `_links`, flatten IDs)

### 6. Rails Console Script Changes

#### Validation Requirements
- ✅ Minimal Ruby: only load JSON, instantiate models, assign attributes, save
- ✅ JSON fully compliant before invoking Rails
- ✅ Sanitize in Python (remove `_links`, flatten IDs, ensure required AR attributes)
- ✅ Single file-based result flow (Ruby writes JSON, Python copies to var/data)
- ✅ Tests assert generated JSON contains no `_links`

### 7. Git Workflow

#### Before Committing
```bash
# 1. Check current branch (should be feature branch, not main)
git status
git branch

# 2. Review changes
git diff

# 3. Stage changes
git add <files>

# 4. Commit with conventional commit format
git commit -m "feat(component): description"
# or: fix(component): description
# or: refactor(component): description
```

#### Commit Message Format
Use Conventional Commits:
- `feat(scope): description` - New feature
- `fix(scope): description` - Bug fix
- `refactor(scope): description` - Code refactoring
- `test(scope): description` - Test changes
- `docs(scope): description` - Documentation changes

Keep PRs < 300 net LOC when possible.

### 8. Work Tracking

#### Update Task Status
**Use `bd` (not markdown) for work tracking:**
```bash
# Update task status
bd update <issue-id> --status in_progress

# Close completed task
bd close <issue-id> --reason "Implemented and tested"

# Create discovered issues during work
bd create "Bug found: description" -t bug -p 0

# Link discovered work to parent
bd dep add <new-id> <parent-id> --type discovered-from
```

## Quality Gate Summary

### Minimal Gates (Every Task)
1. ✅ `make format` - Code formatted
2. ✅ `make lint` - Linting passes
3. ✅ `make dev-test-fast` - Fast tests pass
4. ✅ Git commit with proper message

### Standard Gates (Most Tasks)
1. ✅ Minimal gates (above)
2. ✅ `make container-test` - Full unit tests pass
3. ✅ Component-specific tests pass
4. ✅ Documentation updated if needed

### Comprehensive Gates (Critical Changes)
1. ✅ Standard gates (above)
2. ✅ `make container-test-integration` - Integration tests pass
3. ✅ Security validation for user input
4. ✅ Rehearsal run validates changes

## Quick Quality Check Commands
```bash
# All-in-one CI check
make ci                          # format + lint + test

# Full validation for critical changes
make format && make lint && make container-test && make container-test-integration
```

## When Stuck
1. Check `make status` for service health
2. Review logs with `make logs` or `make logs-app`
3. Consult `docs/DEVELOPER_GUIDE.md` for architecture guidance
4. Run data QA: `uv run --active --no-cache python scripts/data_qa.py --projects <KEY>`
5. For rehearsals: `python scripts/run_rehearsal.py --use-container --collect --stop`
