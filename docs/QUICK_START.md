# j2o Quick Start Guide

**Target Audience**: Developers new to the j2o migration tool
**Time**: 30-45 minutes
**Prerequisites**: Python 3.13+, Docker, SSH access to OpenProject

---

## Table of Contents

1. [Environment Setup](#environment-setup)
2. [Your First Migration](#your-first-migration)
3. [Understanding the Codebase](#understanding-the-codebase)
4. [Common Development Workflows](#common-development-workflows)
5. [Troubleshooting](#troubleshooting)
6. [Next Steps](#next-steps)

---

## Environment Setup

### Step 1: Clone and Install Dependencies

```bash
# Clone repository
git clone <repository-url>
cd jira-to-openproject-migration

# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv sync --frozen

# Verify installation
uv run python --version  # Should show Python 3.13+
```

### Step 2: Configure Environment

```bash
# Copy environment template
cp .env.example .env

# Edit configuration
nano .env  # or your preferred editor
```

**Minimum Required Configuration**:
```bash
# Jira Configuration
J2O_JIRA_URL=https://your-jira-server.com
J2O_JIRA_USERNAME=your-username
J2O_JIRA_API_TOKEN=your-api-token

# OpenProject Configuration
J2O_OPENPROJECT_URL=https://your-openproject.com
J2O_OPENPROJECT_API_KEY=your-api-key

# SSH Configuration (for remote OpenProject)
J2O_OPENPROJECT_SERVER=openproject-server.com
J2O_OPENPROJECT_USER=admin
J2O_OPENPROJECT_CONTAINER=openproject-web-1

# PostgreSQL (for Docker services)
POSTGRES_PASSWORD=secure_password_here
```

### Step 3: Start Development Services

```bash
# Start Docker services (PostgreSQL, Redis)
make up

# Verify services are running
make status
```

### Step 4: Setup Rails Console Access

The j2o tool uses a tmux-backed Rails console for direct OpenProject database operations.

```bash
# Install .irbrc for console stability
make install-irbrc

# Start Rails console session
make start-rails ATTACH=true

# Test in the console (you should see IRB prompt)
# Type: User.count
# Press Ctrl+B, then D to detach (leave running)
```

**Troubleshooting Rails Console**:
- If connection fails: Verify `J2O_OPENPROJECT_SERVER`, `J2O_OPENPROJECT_USER`, `J2O_OPENPROJECT_CONTAINER`
- If tmux not found: Install with `apt-get install tmux` or `brew install tmux`
- To reattach: `make attach-rails`

### Step 5: Run Fast Tests

```bash
# Run fast unit tests to verify setup
make dev-test-fast

# If tests pass, you're ready to develop!
```

---

## Your First Migration

### Understanding Migration Flow

All migrations follow this pattern:
```
Extract (from Jira) → Map (transform) → Load (to OpenProject)
```

### Dry Run: User Migration

Let's do a safe dry-run of user migration:

```bash
# Dry run - no actual changes
uv run python -m src.main migrate --dry-run --components users --limit 10 --no-confirm
```

**What this does**:
1. Extracts first 10 users from Jira
2. Transforms to OpenProject format
3. Shows what *would* be created (without actually creating)
4. Saves mapping file to `var/data/users_mapping.json`

**Expected Output**:
```
[INFO] Starting migration: users
[INFO] Extracting users from Jira...
[INFO] Found 10 users
[INFO] Mapping users...
[INFO] Dry run: Would create 10 users
[INFO] Saved mapping: var/data/users_mapping.json
```

### Inspect the Results

```bash
# View extracted Jira data
cat var/data/jira_users.json | jq '.[0]'

# View transformed OpenProject data
cat var/data/op_users.json | jq '.[0]'

# View mapping
cat var/data/users_mapping.json | jq '.'
```

### Real Migration: Create Users

Once you're confident with the dry run:

```bash
# Real migration (creates users in OpenProject)
uv run python -m src.main migrate --components users --limit 5 --no-confirm
```

**What this does**:
1. Extracts 5 users from Jira
2. Transforms to OpenProject format
3. **Actually creates users** via Rails console
4. Updates mapping file with Jira ID → OpenProject ID

**Verify in OpenProject**:
- Log into OpenProject web UI
- Navigate to Administration → Users
- You should see the newly created users

### View Logs

```bash
# View migration logs
ls var/logs/migration_*.log

# Tail recent log
tail -f var/logs/migration_$(date +%Y%m%d).log

# View Rails console logs
ls var/logs/rails_console_*.log
```

---

## Understanding the Codebase

### Project Structure

```
j2o/
├── src/                    # Main application code
│   ├── clients/           # Client layer (SSH, Docker, Rails, OpenProject)
│   ├── migrations/        # 40+ migration modules
│   ├── mappings/          # Data transformation logic
│   ├── models/            # Pydantic data models
│   ├── utils/             # Utility functions
│   ├── config/            # Configuration utilities
│   └── main.py            # CLI entry point
├── tests/                  # Test suite
│   ├── unit/              # Fast unit tests
│   ├── integration/       # Integration tests
│   └── end_to_end/        # E2E tests
├── docs/                   # Documentation
├── config/                 # Configuration files
├── scripts/                # Operational scripts
├── var/                    # Runtime data (logs, results)
├── Makefile                # Development commands
├── pyproject.toml          # Dependencies
└── AGENTS.md               # Development rules
```

### Key Files to Know

**Entry Points**:
- `src/main.py` - CLI entry point
- `src/migration.py` - Migration orchestration

**Client Layer** (see [CLIENT_API.md](CLIENT_API.md)):
- `src/clients/ssh_client.py` - SSH foundation
- `src/clients/docker_client.py` - Container operations
- `src/clients/rails_console_client.py` - Rails console interaction
- `src/clients/openproject_client.py` - High-level orchestration

**Base Classes**:
- `src/migrations/base_migration.py` - Abstract migration base class

**Example Migrations** (see [MIGRATION_COMPONENTS.md](MIGRATION_COMPONENTS.md)):
- `src/migrations/user_migration.py` - User migration (good reference)
- `src/migrations/project_migration.py` - Project migration
- `src/migrations/work_package_migration.py` - Complex migration example

### Code Style Principles

**Exception-Based Error Handling** (NOT return codes):
```python
# ✅ CORRECT
def process_data(data: dict) -> dict:
    try:
        return perform_operation(data)
    except ValueError as e:
        raise RuntimeError(f"Processing failed: {e}") from e

# ❌ WRONG
def process_data(data: dict) -> dict:
    result = {"status": "error", "data": None}
    return result
```

**Optimistic Execution** (validate in handlers):
```python
# ✅ CORRECT
def copy_file(source: str, dest: str) -> None:
    try:
        shutil.copy2(source, dest)
    except Exception as e:
        # Collect diagnostics ONLY on failure
        diagnostics = {"source_exists": os.path.exists(source)}
        raise FileError(f"Copy failed: {e}", diagnostics) from e

# ❌ WRONG
def copy_file(source: str, dest: str) -> None:
    if not os.path.exists(source):
        raise FileNotFoundError(...)
    # Finally do the work...
```

**Modern Python Typing**:
```python
# ✅ CORRECT
def process(items: list[str], config: dict[str, int]) -> User | None:
    pass

# ❌ WRONG
from typing import List, Dict, Optional
def process(items: List[str], config: Dict[str, int]) -> Optional[User]:
    pass
```

**See Also**:
- [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) - Complete development standards
- [code_style_conventions.md](code_style_conventions.md) - Code style reference

---

## Common Development Workflows

### Daily Development Loop

```bash
# 1. Check git status
git status
git branch  # Should be on feature branch, NOT main

# 2. Start services if needed
make up

# 3. Run fast tests
make dev-test-fast

# 4. Make code changes...

# 5. Format and lint
make format
make lint

# 6. Run tests again
make dev-test-fast

# 7. Commit changes
git add .
git commit -m "feat(users): add timezone mapping"
```

### Work Tracking with bd

**j2o uses `bd` (not markdown) for work tracking:**

```bash
# See available work
bd ready --json | jq '.[0]'

# Start working on task
bd update <issue-id> --status in_progress

# Create discovered issues during work
bd create "Bug: User locale mapping fails for de_DE" -t bug -p 0

# Link discovered work to parent
bd dep add <new-id> <parent-id> --type discovered-from

# Complete task
bd close <issue-id> --reason "Implemented timezone mapping"
```

### Running Specific Tests

```bash
# Run specific test file
pytest tests/unit/test_user_migration.py -v

# Run specific test class
pytest tests/unit/test_user_migration.py::TestUserMigration -v

# Run specific test method
pytest tests/unit/test_user_migration.py::TestUserMigration::test_extract -v

# Run tests matching pattern
pytest -k "user" -v

# Run with markers
pytest -m unit  # Unit tests only
pytest -m "not slow"  # Exclude slow tests
```

### Debugging Migrations

```bash
# Enable debug logging
export J2O_LOG_LEVEL=DEBUG

# Run migration with limit
uv run python -m src.main migrate --debug --components users --limit 5 --no-confirm

# Check logs
tail -f var/logs/migration_*.log

# Inspect Rails console logs
cat var/logs/rails_console_*.log

# Check mapping files
cat var/data/users_mapping.json | jq '.'
```

### Checkpoint Management

Work package migrations use checkpoints for resumability:

```bash
# View checkpoint database
sqlite3 .migration_checkpoints.db "SELECT * FROM checkpoints;"

# Reset checkpoints (after snapshot restore or corruption)
uv run python -m src.main migrate --reset-wp-checkpoints --components work_packages
```

---

## Troubleshooting

### "SSH Connection Failed"

**Problem**: Cannot connect to OpenProject server

**Solution**:
```bash
# 1. Verify SSH access manually
ssh -i ~/.ssh/your-key user@openproject-server "echo test"

# 2. Check key permissions
chmod 600 ~/.ssh/your-key

# 3. Verify environment variables
echo $J2O_OPENPROJECT_SERVER
echo $J2O_OPENPROJECT_USER

# 4. Test Docker access
ssh user@openproject-server "docker ps"
```

### "Rails Console Not Ready"

**Problem**: Cannot execute commands in Rails console

**Solution**:
```bash
# 1. Install .irbrc (if not done)
make install-irbrc

# 2. Restart Rails console
# Kill existing session
tmux kill-session -t rails_console

# Start new session
make start-rails ATTACH=true

# 3. Verify console is responsive
# Type: puts "test"
# Should see: test
```

### "Test

s Failing"

**Problem**: Tests fail after setup

**Solution**:
```bash
# 1. Ensure services are running
make status

# 2. Check PostgreSQL
docker compose logs postgres

# 3. Run tests in container (full deps)
make container-test

# 4. Check specific failing test
pytest tests/unit/test_failing.py -v --tb=short
```

### "Missing Mapping File"

**Problem**: `FileNotFoundError: users_mapping.json`

**Solution**:
- Mapping files are created during migrations
- Run migration to generate: `uv run python -m src.main migrate --components users`
- Check `var/data/` directory for existing mappings

### "Work Package Start Date Missing"

**Problem**: Work packages have no start date

**Solution**:
- Check custom field configuration in Jira
- Verify custom field IDs: `customfield_18690`, `customfield_12590`, etc.
- Fallback uses first "In Progress" status transition
- See: [docs/decisions/2025-10-04-start-date-from-history.md](decisions/2025-10-04-start-date-from-history.md)

---

## Next Steps

### Learn More

**Essential Reading**:
1. **[KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md)** - Complete documentation index
2. **[DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md)** - Development standards
3. **[CLIENT_API.md](CLIENT_API.md)** - Client layer API reference
4. **[MIGRATION_COMPONENTS.md](MIGRATION_COMPONENTS.md)** - Migration catalog
5. **[WORKFLOW_STATUS_GUIDE.md](WORKFLOW_STATUS_GUIDE.md)** - Workflow setup

**Architecture**:
- [ARCHITECTURE.md](ARCHITECTURE.md) - System architecture
- [SECURITY.md](SECURITY.md) - Security guidelines

### Practice Migrations

Try these migrations in order:

1. **Users** (simplest):
   ```bash
   uv run python -m src.main migrate --components users --limit 10 --no-confirm
   ```

2. **Groups**:
   ```bash
   uv run python -m src.main migrate --components groups --no-confirm
   ```

3. **Projects**:
   ```bash
   uv run python -m src.main migrate --components projects --limit 5 --no-confirm
   ```

4. **Work Packages** (most complex):
   ```bash
   uv run python -m src.main migrate --components work_packages --limit 10 --no-confirm
   ```

### Develop Your First Migration

Want to add a new migration component? Follow this guide:

1. **Create migration file**: `src/migrations/my_migration.py`
2. **Inherit from BaseMigration**
3. **Implement abstract methods**: `extract()`, `map()`, `load()`
4. **Add tests**: `tests/unit/test_my_migration.py`
5. **Update documentation**: Add to [MIGRATION_COMPONENTS.md](MIGRATION_COMPONENTS.md)

**Template**:
```python
from src.migrations.base_migration import BaseMigration

class MyMigration(BaseMigration):
    def extract(self) -> list[dict]:
        """Extract data from Jira."""
        return self.jira_client.get_my_data()

    def map(self, jira_data: list[dict]) -> list[dict]:
        """Transform to OpenProject format."""
        return [self._map_item(item) for item in jira_data]

    def load(self, openproject_data: list[dict]) -> None:
        """Load into OpenProject."""
        self.op_client.create_batch(openproject_data)

    def _map_item(self, jira_item: dict) -> dict:
        """Transform single item."""
        return {
            "name": jira_item["name"],
            "description": jira_item["description"]
        }
```

### Join the Development Team

**Contributing**:
1. Follow [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) standards
2. Write tests for all changes
3. Run quality checks: `make format && make lint && make dev-test`
4. Use conventional commits: `feat(scope):`, `fix(scope):`, etc.
5. Keep PRs small: <300 net LOC when possible

**Communication**:
- Use `bd` for work tracking
- Document decisions in ADRs (Architecture Decision Records)
- Update documentation with code changes

---

## Quick Reference Card

### Most Used Commands

```bash
# Development
make up                          # Start services
make dev-test-fast               # Fast tests
make format && make lint         # Quality checks

# Migration
uv run python -m src.main migrate --components users --no-confirm

# Debugging
make status                      # Service status
make logs                        # View logs
make start-rails ATTACH=true    # Rails console

# Work Tracking
bd ready                         # Available work
bd update <id> --status in_progress
bd close <id> --reason "Done"

# Testing
pytest tests/unit/test_user_migration.py -v
pytest -k "user" -v
pytest -m "not slow" -v
```

### Essential Paths

- Logs: `var/logs/`
- Data: `var/data/`
- Results: `var/results/`
- Mappings: `var/data/*_mapping.json`

### Getting Help

- Documentation: [KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md)
- Troubleshooting: [KNOWLEDGE_BASE.md#troubleshooting](KNOWLEDGE_BASE.md#troubleshooting)
- Architecture: [ARCHITECTURE.md](ARCHITECTURE.md)
- API Reference: [CLIENT_API.md](CLIENT_API.md)

---

**Welcome to j2o development!** 🎉

**Questions?** Check the [KNOWLEDGE_BASE.md](KNOWLEDGE_BASE.md) or consult [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md).
