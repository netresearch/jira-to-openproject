# j2o Knowledge Base Index

**Project**: Jira to OpenProject Migration Tool
**Version**: 1.0
**Last Updated**: 2025-10-14

## Table of Contents

- [Getting Started](#getting-started)
- [Architecture & Design](#architecture--design)
- [API Reference](#api-reference)
- [Migration Guide](#migration-guide)
- [Development](#development)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Configuration](#configuration)
- [Testing](#testing)
- [Decision Records](#decision-records)

---

## Getting Started

### Quick Start

**→ [Quick Start Guide](QUICK_START.md)** - Developer onboarding and first migration

**Prerequisites**:
- Python 3.13+
- Docker & Docker Compose
- SSH access to OpenProject server
- tmux for Rails console

**First Steps**:
```bash
# 1. Setup environment
cp .env.example .env
# Edit .env with credentials

# 2. Install dependencies
make local-install

# 3. Start services
make up

# 4. Run first migration
uv run python -m src.main migrate --components users --dry-run --no-confirm
```

### Essential Reading

1. **[README.md](../README.md)** - Project overview and features
2. **[QUICK_START.md](QUICK_START.md)** - Developer quick start guide
3. **[DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md)** - Development standards and patterns
4. **[ARCHITECTURE.md](ARCHITECTURE.md)** - System architecture overview

---

## Architecture & Design

### System Architecture

**→ [ARCHITECTURE.md](ARCHITECTURE.md)** - Complete system architecture documentation

**Key Concepts**:
- Layered client architecture (SSH → Docker → Rails Console)
- Extract-Map-Load migration pipeline
- Exception-based error handling
- Optimistic execution pattern

**Architecture Diagrams**:
```
Local Migration Tool (Python 3.13)
    ↓ SSH Connection
Remote OpenProject Server
    ↓ Docker Commands
OpenProject Container
    ↓ tmux + Rails Console
OpenProject Application (ActiveRecord)
```

### Design Patterns

**BaseMigration Pattern**:
```python
extract() → map() → load()
```

**Client Hierarchy**:
```
OpenProjectClient (orchestration)
    ├── SSHClient (foundation)
    ├── DockerClient (container ops)
    └── RailsConsoleClient (console interaction)
```

**Exception Hierarchy**:
- SSHConnectionError, SSHCommandError, SSHFileTransferError
- RailsConsoleError (TmuxSessionError, ConsoleNotReadyError, CommandExecutionError)
- OpenProjectError (ConnectionError, QueryExecutionError, RecordNotFoundError)

**Related**:
- **[Developer Guide - Architecture Components](DEVELOPER_GUIDE.md#architecture-components)**
- **[Client API - Usage Patterns](CLIENT_API.md#usage-patterns)**

---

## API Reference

### Client Layer API

**→ [CLIENT_API.md](CLIENT_API.md)** - Comprehensive client layer documentation

**Components**:

1. **SSHClient** - Foundation layer for SSH connections
   - `execute_command()` - Execute remote commands
   - `copy_file_to_remote()` - Upload files via SCP
   - `copy_file_from_remote()` - Download files via SCP
   - `test_connection()` - Verify connectivity

2. **DockerClient** - Container operations layer
   - `execute_in_container()` - Run commands in container
   - `copy_file_to_container()` - Upload to container
   - `copy_file_from_container()` - Download from container

3. **RailsConsoleClient** - Rails console interaction
   - `start_session()` - Initialize tmux session
   - `execute_ruby_code()` - Execute Ruby in console
   - `execute_ruby_script_with_file_result()` - Run script with file-based results
   - `stop_session()` - Terminate tmux session

4. **OpenProjectClient** - High-level orchestration
   - `create_work_packages_batch()` - Batch work package creation
   - `create_users_batch()` - Batch user creation
   - `create_projects_batch()` - Batch project creation

**Quick Links**:
- [SSHClient Methods](CLIENT_API.md#sshclient)
- [DockerClient Methods](CLIENT_API.md#dockerclient)
- [RailsConsoleClient Methods](CLIENT_API.md#railsconsoleclient)
- [OpenProjectClient Methods](CLIENT_API.md#openprojectclient)
- [Exception Classes](CLIENT_API.md#exception-classes)

### Migration Components

**→ [MIGRATION_COMPONENTS.md](MIGRATION_COMPONENTS.md)** - Complete migration module catalog

**Core Entities**:
- [UserMigration](MIGRATION_COMPONENTS.md#usermigration) - User accounts with provenance
- [GroupMigration](MIGRATION_COMPONENTS.md#groupmigration) - Groups and memberships
- [ProjectMigration](MIGRATION_COMPONENTS.md#projectmigration) - Projects with lead assignment
- [WorkPackageMigration](MIGRATION_COMPONENTS.md#workpackagemigration) - Issues to work packages

**Configuration**:
- [StatusMigration](MIGRATION_COMPONENTS.md#statusmigration) - Status definitions
- [PriorityMigration](MIGRATION_COMPONENTS.md#prioritymigration) - Priority levels
- [IssueTypeMigration](MIGRATION_COMPONENTS.md#issuetypemigration) - Work package types
- [CustomFieldMigration](MIGRATION_COMPONENTS.md#customfieldmigration) - Custom field definitions

**Attachments & Files**:
- [AttachmentsMigration](MIGRATION_COMPONENTS.md#attachmentsmigration) - File attachments
- [AttachmentProvenanceMigration](MIGRATION_COMPONENTS.md#attachmentprovenancemigration) - Attachment metadata

**Relationships**:
- [RelationMigration](MIGRATION_COMPONENTS.md#relationmigration) - Work package relations
- [WatcherMigration](MIGRATION_COMPONENTS.md#watchermigration) - Watchers

**Agile & Sprint**:
- [SprintEpicMigration](MIGRATION_COMPONENTS.md#sprintepicmigration) - Sprints and epics
- [AgileBoardMigration](MIGRATION_COMPONENTS.md#agileboardmigration) - Agile boards

**Time Tracking**:
- [TimeEntryMigration](MIGRATION_COMPONENTS.md#timeentrymigration) - Tempo worklogs

**Workflow & Permissions**:
- [WorkflowMigration](MIGRATION_COMPONENTS.md#workflowmigration) - Workflow transitions
- [AdminSchemeMigration](MIGRATION_COMPONENTS.md#adminschemigration) - Role memberships

**Component Count**: 40+ specialized migration modules

---

## Migration Guide

### Pre-Migration

**Planning**:
1. Review [Workflow & Status Guide](WORKFLOW_STATUS_GUIDE.md)
2. Plan status and workflow mapping
3. Identify custom fields to migrate
4. Document project hierarchy

**Environment Setup**:
```bash
# 1. Configure environment
cp .env.example .env
# Edit: J2O_JIRA_URL, J2O_OPENPROJECT_URL, credentials

# 2. Test connectivity
ssh -i ~/.ssh/key user@openproject-server "docker ps"

# 3. Install .irbrc for Rails console stability
make install-irbrc

# 4. Start Rails console session
make start-rails ATTACH=true
```

**Related**:
- [Configuration Guide](configuration.md)
- [Security Guidelines](SECURITY.md)

### Migration Execution

**Component Dependencies**:
```
1. Foundation: users, groups
2. Configuration: status, priority, issue_types, custom_fields
3. Structure: projects, components, versions
4. Content: work_packages, attachments
5. Relationships: relations, watchers, links
6. Time: time_entries
7. Agile: sprints, boards
8. Workflow: workflow, admin_schemes
9. Reporting: reporting
```

**Execution Commands**:
```bash
# Dry run
uv run python -m src.main migrate --dry-run --components users --no-confirm

# Single component
uv run python -m src.main migrate --components users --no-confirm

# Multiple components
uv run python -m src.main migrate --components users,projects,work_packages --no-confirm

# Full migration
uv run python -m src.main migrate --profile full --no-confirm
```

**Related**:
- [Migration Components - Execution](MIGRATION_COMPONENTS.md#migration-execution)
- [Migration Components - Dependencies](MIGRATION_COMPONENTS.md#component-dependencies)

### Post-Migration

**Validation**:
```bash
# Run data QA script
uv run --active --no-cache python scripts/data_qa.py --projects <KEY>

# Check mapping files
ls var/data/*_mapping.json

# Review logs
ls var/logs/migration_*.log

# Inspect summary
cat var/results/migration_summary_*.json
```

**Troubleshooting**:
- [Troubleshooting Guide](#troubleshooting)
- [Migration Components - Troubleshooting](MIGRATION_COMPONENTS.md#troubleshooting)

---

## Development

### Development Standards

**→ [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md)** - Complete development guidelines

**Core Principles**:

1. **Exception-Based Error Handling**
   - Always raise exceptions, never return error codes
   - Use specific exception types
   - Include rich context in exceptions
   - Chain exceptions with `raise ... from e`

2. **Optimistic Execution**
   - Execute first, validate in exception handlers
   - Avoid excessive precondition checking
   - Collect diagnostics only on failure

3. **Modern Python Typing**
   - Use built-in types: `list[str]`, `dict[str, int]`
   - Use pipe operator for unions: `User | None`
   - Use ABCs from `collections.abc`

4. **YOLO Development**
   - Remove legacy code immediately
   - No backward compatibility layers
   - Clean, direct implementations only

**Code Style**:
- Line length: 120 characters
- Formatter: ruff format
- Linter: ruff check
- Type checker: mypy

**Related**:
- [Developer Guide - Development Standards](DEVELOPER_GUIDE.md#development-standards)
- [Developer Guide - Code Organization](code_style_conventions.md)

### Development Workflow

**Daily Workflow**:
```bash
# 1. Check status
git status && git branch

# 2. Start services
make up

# 3. Fast tests
make dev-test-fast

# 4. Make changes...

# 5. Before committing
make format && make lint && make dev-test-fast

# 6. Commit
git add . && git commit -m "feat(component): description"
```

**Work Tracking**:
**Use `bd` (not markdown) for work tracking:**
```bash
# Find work
bd ready --json | jq '.[0]'

# Update status
bd update <id> --status in_progress

# Create discovered issues
bd create "Bug: description" -t bug -p 0

# Complete work
bd close <id> --reason "Implemented"
```

**Quality Gates**:
```bash
# Minimal
make format && make lint && make dev-test-fast

# Standard
make container-test

# Comprehensive
make container-test && make container-test-integration
```

**Related**:
- [AGENTS.md - Development Workflow](../AGENTS.md#development-workflow-bd)
- [Suggested Commands](suggested_commands.md)

### Testing

**Test Organization**:
```
tests/
├── unit/          # Fast, isolated (<30s)
├── integration/   # External services mocked (2-3min)
├── end_to_end/    # Complete workflows (5-10min)
└── utils/         # Shared fixtures
```

**Test Commands**:
```bash
# Fast local tests
make dev-test-fast

# All local tests
make dev-test

# Container tests (full deps)
make container-test

# Integration tests
make container-test-integration

# Specific tests
make container-test TEST_OPTS="-k test_user_migration"
```

**Test Markers**:
- `@pytest.mark.unit` - Unit tests
- `@pytest.mark.integration` - Integration tests
- `@pytest.mark.slow` - Slow-running tests
- `@pytest.mark.requires_docker` - Docker required
- `@pytest.mark.requires_ssh` - SSH required
- `@pytest.mark.requires_rails` - Rails console required

**Related**:
- [Developer Guide - Testing](DEVELOPER_GUIDE.md#test-organization)
- [AGENTS.md - Testing](../AGENTS.md#build--tests-prefer-file-scoped)

---

## Security

### Security Guidelines

**→ [SECURITY.md](SECURITY.md)** - Complete security documentation

**Key Practices**:

1. **Input Validation**
   - Validate all Jira keys (length, charset)
   - Whitelist characters: `[A-Z0-9\-]`
   - Max length: 100 characters
   - Block control characters

2. **Injection Prevention**
   - Use Ruby `.inspect` for safe escaping
   - Never directly interpolate in Ruby scripts
   - Sanitize JSON payloads before Rails execution
   - Remove `_links` objects from OpenProject responses

3. **Secret Management**
   - Never commit `.env` files
   - Use SSH keys over passwords
   - Set key permissions: `chmod 600 ~/.ssh/key`
   - Rotate API tokens regularly

4. **Rails Script Safety**
   - Split scripts: interpolated head + literal body
   - Minimal Ruby: only load JSON, create records, save
   - File-based result flow (no stdout parsing)
   - Validate JSON before Rails execution

**Example - Safe Ruby Script Generation**:
```python
# Head: interpolated parameters (f-string)
head = f"""
results_file = '{results_path}'
data = JSON.parse(File.read('{data_path}'))
"""

# Body: literal Ruby (no interpolation)
body = """
data.each do |item|
  record = Model.create!(item)
  results << {id: record.id}
end
"""

script = head + body
```

**Related**:
- [Security Guidelines](SECURITY.md)
- [Developer Guide - Security Requirements](DEVELOPER_GUIDE.md#security-requirements)
- [src/AGENTS.md - Security](../src/AGENTS.md#security--safety)

---

## Troubleshooting

### Common Issues

#### Connection Problems

**SSH Connection Failed**:
```bash
# Verify SSH access
ssh -i ~/.ssh/key user@server "echo test"

# Check key permissions
chmod 600 ~/.ssh/key

# Test Docker access
ssh user@server "docker ps"
```

**Rails Console Not Ready**:
```bash
# Install .irbrc for stability
make install-irbrc

# Restart Rails console session
make start-rails ATTACH=true
```

**Related**:
- [README - Troubleshooting](../README.md#troubleshooting)
- [Client API - Connection Testing](CLIENT_API.md#connection-testing)

#### Migration Errors

**Checkpoint Corruption**:
```bash
# Reset work package checkpoints
uv run python -m src.main migrate --reset-wp-checkpoints --components work_packages
```

**Missing Start Dates**:
```bash
# Verify custom field configuration
# Check Jira status history for "In Progress" transitions
# See: docs/decisions/2025-10-04-start-date-from-history.md
```

**Rails Script Errors**:
- Review `var/logs/rails_console_*.log`
- Check JSON sanitization (no `_links`)
- Verify required AR attributes present

**Related**:
- [Migration Components - Troubleshooting](MIGRATION_COMPONENTS.md#troubleshooting)

#### Performance Issues

**Slow Migrations**:
- Adjust batch size: `J2O_BATCH_SIZE=50`
- Use parallel workers: `J2O_PARALLEL_WORKERS=4`
- Monitor system resources: `make status`

**Memory Issues**:
- Reduce batch size
- Enable incremental processing
- Check checkpoint cleanup

**Related**:
- [Client API - Performance](CLIENT_API.md#performance-considerations)

### Debug Commands

```bash
# Test connections
python scripts/test_connections.py

# Debug specific migration
uv run python -m src.main migrate --debug --components users --limit 10

# Check service status
make status

# View logs
make logs
make logs-app

# Data validation
uv run python scripts/data_qa.py --projects <KEY>
```

---

## Configuration

### Configuration System

**→ [configuration.md](configuration.md)** - Detailed configuration guide

**Configuration Precedence**:
1. Environment variables (highest)
2. `.env.local` (developer overrides)
3. `.env` (project defaults)
4. `config/config.yaml` (structured config)
5. Code defaults (lowest)

**Key Environment Variables**:
```bash
# Jira
J2O_JIRA_URL=https://jira.example.com
J2O_JIRA_USERNAME=admin
J2O_JIRA_API_TOKEN=xxx

# OpenProject
J2O_OPENPROJECT_URL=https://openproject.example.com
J2O_OPENPROJECT_API_KEY=xxx

# SSH
J2O_OPENPROJECT_SERVER=openproject.example.com
J2O_OPENPROJECT_USER=admin
J2O_OPENPROJECT_CONTAINER=openproject-web-1

# Migration
J2O_BATCH_SIZE=100
J2O_LOG_LEVEL=INFO
J2O_SSL_VERIFY=true

# Rails Console
J2O_OPENPROJECT_TMUX_SESSION_NAME=rails_console
```

**Configuration Files**:
- `.env.example` - Template with all variables
- `.env` - Main configuration (git-ignored)
- `.env.local` - Developer overrides (git-ignored)
- `config/config.yaml` - Structured settings

**Related**:
- [Configuration Guide](configuration.md)
- [Configuration Rules](configuration-rules.md)
- [.env.example](../.env.example)

---

## Testing

### Test Infrastructure

**Test Organization**:
- `tests/unit/` - Fast, isolated tests (<30s)
- `tests/integration/` - External service tests (2-3min)
- `tests/end_to_end/` - Complete workflows (5-10min)
- `tests/utils/` - Shared utilities

**Test Execution**:
```bash
# Local (fastest)
make dev-test-fast      # Unit tests only
make dev-test           # All local tests

# Container (full deps)
make container-test              # Unit tests
make container-test-integration  # Integration tests

# Specific tests
pytest tests/unit/test_user_migration.py -v
pytest -m "unit and not slow"
pytest -k "test_jira_client"
```

**Test Markers**:
```python
@pytest.mark.unit
@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.requires_docker
@pytest.mark.requires_ssh
@pytest.mark.requires_rails
```

**Performance Targets**:
- Unit tests: <30 seconds
- Smoke tests: 2-3 minutes
- Full suite: 5-10 minutes

**Related**:
- [Developer Guide - Testing](DEVELOPER_GUIDE.md#test-organization)
- [tests/AGENTS.md](../tests/AGENTS.md)
- [pytest.ini](../pytest.ini)

---

## Decision Records

### Architecture Decision Records (ADRs)

**Location**: `docs/decisions/`

**Available ADRs**:

1. **[2025-10-02: Project Issue Metadata](decisions/2025-10-02-project-issue-metadata.md)**
   - Project lead attribution
   - Jira provenance fields
   - Module enablement logic

2. **[2025-10-03: Project Issue Validation](decisions/2025-10-03-project-issue-validation.md)**
   - Validation strategy for projects
   - Module configuration validation
   - Start date validation approach

3. **[2025-10-04: Start Date from History](decisions/2025-10-04-start-date-from-history.md)**
   - Start date derivation logic
   - Custom field precedence
   - Status transition fallback

4. **[2025-10-05: Fast Forward and Groups](decisions/2025-10-05-fast-forward-and-groups.md)**
   - Work package checkpoint system
   - Fast-forward optimization
   - Group synchronization patterns

**ADR Format**:
- Context: Problem description
- Decision: Chosen approach
- Consequences: Impacts and trade-offs
- Alternatives: Options considered

---

## Additional Resources

### Scripts

**Operational Scripts** (`scripts/`):
- `run_rehearsal.py` - Orchestrate mock-stack rehearsals
- `run_mock_migration.py` - Run mock migration
- `data_qa.py` - Post-migration validation
- `start_rails_tmux.py` - Rails console session management
- `migrate-start-fast-forward.sh` - Start with fast-forward
- `migrate-stop.sh` - Stop migration processes
- `migrate-status.sh` - Check migration status

**Related**:
- [scripts/AGENTS.md](../scripts/AGENTS.md)

### Documentation Files

**Root Documentation**:
- [README.md](../README.md) - Project overview
- [AGENTS.md](../AGENTS.md) - Agent configuration
- [LICENSE](../LICENSE) - MIT License

**Technical Documentation**:
- [ARCHITECTURE.md](ARCHITECTURE.md) - System architecture
- [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) - Development guide
- [SECURITY.md](SECURITY.md) - Security guidelines
- [WORKFLOW_STATUS_GUIDE.md](WORKFLOW_STATUS_GUIDE.md) - Workflow configuration
- [configuration.md](configuration.md) - Configuration reference

**API Documentation**:
- [CLIENT_API.md](CLIENT_API.md) - Client layer API
- [MIGRATION_COMPONENTS.md](MIGRATION_COMPONENTS.md) - Migration catalog

**Configuration Documentation**:
- [configuration-rules.md](configuration-rules.md)
- [configuration-system-analysis.md](configuration-system-analysis.md)
- [configuration-system-improvement.md](configuration-system-improvement.md)

### External References

**Official Documentation**:
- [Jira Server REST API](https://docs.atlassian.com/software/jira/docs/api/REST/)
- [OpenProject API](https://www.openproject.org/docs/api/)
- [Python 3.13 Documentation](https://docs.python.org/3.13/)

**Related Projects**:
- [tmux](https://github.com/tmux/tmux/wiki)
- [Docker](https://docs.docker.com/)
- [pytest](https://docs.pytest.org/)

---

## Quick Reference

### Essential Commands

```bash
# Development
make up                  # Start services
make dev-test-fast       # Fast tests
make format && make lint # Quality checks

# Migration
uv run python -m src.main migrate --components users --no-confirm

# Troubleshooting
make status              # Check services
make logs               # View logs
bd ready                # Check work tracking
```

### Essential Files

- `.env` - Configuration
- `AGENTS.md` - Agent rules
- `Makefile` - Commands
- `pyproject.toml` - Dependencies
- `pytest.ini` - Test config

### Essential Directories

- `src/` - Source code
- `tests/` - Test suite
- `docs/` - Documentation
- `var/` - Runtime data
- `config/` - Configuration
- `scripts/` - Utilities

---

**Last Updated**: 2025-10-14
**Maintained By**: j2o Development Team
**License**: MIT
