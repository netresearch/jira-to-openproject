# Repository Structure

## Root Directory Structure
```
j2o/
├── src/                         # Main Python package (core application)
├── tests/                       # Test suite (unit, integration, end-to-end)
├── scripts/                     # Operational helpers and utilities
├── docs/                        # Documentation and guides
├── config/                      # Configuration files and schemas
├── contrib/                     # Contributed files (e.g., openproject.irbrc)
├── examples/                    # Example configurations and scripts
├── var/                         # Runtime data (logs, results, data, backups)
├── .github/                     # GitHub workflows and actions
├── .devcontainer/              # Dev container configuration
├── api-specs/                   # API specifications
├── test-specs/                  # Test specifications
├── .serena/                     # Serena MCP project configuration
├── .beads/                      # bd work tracking data
├── Makefile                     # Build automation and dev commands
├── pyproject.toml              # Project metadata and dependencies
├── uv.lock                      # Locked dependency versions
├── compose.yml                  # Docker Compose configuration
├── .env.example                # Environment variable template
└── AGENTS.md                    # Root agent configuration
```

## src/ Directory (Core Application)
```
src/
├── main.py                      # CLI entry point (j2o command)
├── migration.py                 # Migration orchestration
├── config_loader.py            # Configuration loading
├── display.py                   # CLI display utilities
├── type_definitions.py         # Shared type definitions
├── clients/                     # Client layer
│   ├── jira_client.py          # Jira API client
│   ├── enhanced_jira_client.py # Enhanced Jira client
│   ├── openproject_client.py   # OpenProject orchestration
│   ├── enhanced_openproject_client.py
│   ├── ssh_client.py           # SSH foundation layer
│   ├── docker_client.py        # Docker operations
│   ├── rails_console_client.py # Rails console interaction
│   └── exceptions.py           # Client exceptions
├── migrations/                  # Migration components (40+ modules)
│   ├── base_migration.py       # Base class for migrations
│   ├── user_migration.py       # User migration
│   ├── group_migration.py      # Group migration
│   ├── project_migration.py    # Project migration
│   ├── work_package_migration.py # Work package migration
│   ├── attachments_migration.py
│   ├── time_entry_migration.py
│   ├── workflow_migration.py
│   ├── agile_board_migration.py
│   ├── admin_scheme_migration.py
│   └── ... (30+ other migration modules)
├── mappings/                    # Data transformation logic
│   └── mappings.py
├── models/                      # Pydantic data models
├── utils/                       # Utility functions
│   ├── enhanced_audit_trail_migrator.py
│   ├── enhanced_timestamp_migrator.py
│   ├── enhanced_user_association_migrator.py
│   └── ... (other utilities)
├── config/                      # Configuration utilities
├── performance/                 # Performance monitoring
├── dashboard/                   # FastAPI web dashboard (optional)
│   └── AGENTS.md               # Dashboard-specific rules
└── AGENTS.md                    # src-specific agent rules
```

## tests/ Directory
```
tests/
├── unit/                        # Fast, isolated unit tests
│   ├── migrations/             # Migration unit tests
│   ├── clients/                # Client unit tests
│   └── ... (component tests)
├── integration/                 # External service tests (mocked)
│   ├── debug/                  # Debug helpers
│   └── ... (integration tests)
├── end_to_end/                 # Complete workflow tests
├── utils/                       # Shared test utilities
├── test_data/                  # Test fixtures
├── conftest.py                 # Pytest configuration
└── AGENTS.md                    # Test-specific agent rules
```

## scripts/ Directory
```
scripts/
├── run_rehearsal.py            # Orchestrate mock-stack rehearsals
├── run_mock_migration.py       # Run mock migration
├── data_qa.py                  # Post-migration data validation
├── start_rails_tmux.py        # Start Rails console tmux session
├── migrate-start-fast-forward.sh
├── migrate-stop.sh
├── migrate-status.sh
└── AGENTS.md                    # Scripts-specific agent rules
```

## docs/ Directory
```
docs/
├── DEVELOPER_GUIDE.md          # Development standards and patterns
├── ARCHITECTURE.md             # System architecture
├── SECURITY.md                 # Security documentation
├── WORKFLOW_STATUS_GUIDE.md    # Status and workflow migration
├── configuration.md            # Configuration guide
├── decisions/                  # Architecture decision records
│   ├── 2025-10-02-project-issue-metadata.md
│   ├── 2025-10-03-project-issue-validation.md
│   ├── 2025-10-04-start-date-from-history.md
│   └── 2025-10-05-fast-forward-and-groups.md
└── plans/                      # Planning documents
```

## config/ Directory
```
config/
├── config.yaml                 # Default configuration
└── schemas/                    # Configuration schemas
    ├── base.py
    └── settings.py
```

## var/ Directory (Runtime Data)
```
var/
├── data/                       # Migration data files
├── logs/                       # Log files
├── results/                    # Migration results
├── backups/                    # Backup files
└── rehearsal/                  # Rehearsal artifacts (timestamped)
```

## Key Configuration Files

### Root Level
- **AGENTS.md**: Root agent configuration, global rules
- **Makefile**: Build automation, all dev commands
- **pyproject.toml**: Project metadata, dependencies, tool configs
- **uv.lock**: Locked dependency versions
- **compose.yml**: Docker Compose services
- **.env.example**: Environment variable template
- **.env**: Actual environment (git-ignored)
- **.env.local**: Developer-specific overrides (git-ignored)
- **pytest.ini**: Pytest configuration
- **.pre-commit-config.yaml**: Pre-commit hooks

### Scoped AGENTS.md Files
- **AGENTS.md** (root): Global rules, precedence hierarchy
- **src/AGENTS.md**: Core package conventions
- **src/dashboard/AGENTS.md**: Dashboard-specific rules
- **tests/AGENTS.md**: Test patterns and markers
- **scripts/AGENTS.md**: Script-specific guidance

## Important Git-Ignored Paths
```
.env                            # Environment secrets
.env.local                      # Developer overrides
var/                            # Runtime data
.venv/                          # Virtual environment
__pycache__/                    # Python bytecode
.pytest_cache/                  # Pytest cache
.mypy_cache/                    # Mypy cache
.ruff_cache/                    # Ruff cache
*.egg-info/                     # Package info
.migration_checkpoints.db       # SQLite checkpoint database
```

## Navigation Tips

### Find Migration Modules
```bash
ls src/migrations/*_migration.py          # List all migration modules
```

### Find Tests for Component
```bash
# Unit tests
find tests/unit -name "test_*migration*.py"

# Integration tests
find tests/integration -name "test_*.py"
```

### Find Documentation
```bash
ls docs/*.md                              # Core documentation
ls docs/decisions/*.md                    # Architecture decisions
```

### Find Configuration
```bash
cat .env.example                          # Environment template
cat config/config.yaml                    # YAML configuration
```

## Entry Points

### CLI Entry
- `src/main.py`: Main CLI entry point
- Command: `uv run python -m src.main` or `j2o`

### Migration Orchestration
- `src/migration.py`: Pipeline coordination

### Dashboard (Optional)
- `src/dashboard/`: FastAPI web interface

### Testing
- `pytest` from root directory
- Makefile targets: `make dev-test`, `make container-test`

## Precedence Rules

### AGENTS.md Hierarchy
Closest file wins: `src/migrations/AGENTS.md` > `src/AGENTS.md` > root `AGENTS.md`

### Configuration Precedence
1. Environment variables (highest)
2. `.env.local`
3. `.env`
4. `config/config.yaml`
5. Code defaults (lowest)
