# Suggested Commands - Development Workflow

## Quick Reference

### Fast Development Loop (Recommended for Daily Use)
```bash
# Local testing (fastest, requires dependencies installed)
make dev-test                    # Run all tests locally with pytest-xdist
make dev-test-fast              # Run only fast unit tests locally
```

### Container-Based Testing (Full Environment)
```bash
# Unit tests in Docker (ensures libffi/aiohttp dependencies)
make container-test              # Run unit tests in Docker container

# Integration tests in Docker (with mocks)
make container-test-integration  # Run integration tests (tolerates skipped)

# Full rehearsal with mocks
python scripts/run_rehearsal.py --use-container --collect --stop
```

### Code Quality
```bash
# Linting and formatting
make lint                        # Run ruff linting + mypy type checking
make format                      # Format code with ruff
make type-check                  # Run mypy type checking only

# Pre-commit hooks
make pre-commit                  # Run all pre-commit hooks
```

### Development Environment
```bash
# Start/stop services
make up                          # Start development stack (app + services)
make down                        # Stop all services
make restart                     # Restart services
make status                      # Show running containers

# Shell access
make shell                       # Open bash in development container
make exec CMD="python --version" # Execute command in container
```

### Remote Operations (OpenProject)
```bash
# Rails console setup
make install-irbrc               # Install .irbrc into remote container
make start-rails ATTACH=true     # Start and attach to Rails console tmux
make attach-rails                # Attach to existing Rails console session
```

### Migration Execution
```bash
# Run migrations
uv run python -m src.main migrate --components users,projects,work_packages --no-confirm
uv run python -m src.main migrate --profile full --no-confirm

# Dry run
uv run python -m src.main migrate --dry-run --components users --no-confirm

# Reset work package checkpoints (after snapshot restore)
uv run python -m src.main migrate --components work_packages --reset-wp-checkpoints
```

### Data Quality & Validation
```bash
# Post-migration validation
uv run --active --no-cache python scripts/data_qa.py --projects <KEY>

# Test specific components
make container-test TEST_OPTS="-k test_group_migration"
make container-test-integration TEST_OPTS="-k timezone_detection"
```

### Dependency Management
```bash
# Install dependencies
make local-install               # Install locally with uv sync
make install                     # Install in Docker container

# Update lock file
uv lock                          # Update uv.lock file
```

### Cleanup
```bash
make clean                       # Remove containers, volumes, cache
make clean-all                   # Nuclear option: remove everything including images
```

## Common Workflows

### Starting New Development Session
```bash
# 1. Check git status
git status
git branch

# 2. Start services if needed
make up

# 3. Run fast tests to ensure baseline
make dev-test-fast

# 4. Start coding...
```

### Before Committing Changes
```bash
# 1. Format code
make format

# 2. Run linting
make lint

# 3. Run relevant tests
make dev-test-fast              # For quick feedback
make container-test             # For full validation

# 4. Commit changes
git add .
git commit -m "feat(component): description"
```

### Running Full Quality Gates
```bash
# CI-equivalent checks
make ci                         # format + lint + test
```

### Debugging Migration Issues
```bash
# 1. Check service status
make status

# 2. View logs
make logs                       # All services
make logs-app                   # App service only

# 3. Open shell for investigation
make shell

# 4. Check Rails console connectivity
make start-rails ATTACH=true
```

### Work Package Fast-Forward Issues
```bash
# Reset checkpoints if database is stale/corrupted
uv run python -m src.main migrate --reset-wp-checkpoints --components work_packages

# Verify checkpoint integrity
make container-test TEST_OPTS="-k work_package_checkpoint"
```

## System Utilities (Linux)
Standard Linux commands available:
- `git`, `ls`, `cd`, `pwd`, `cat`, `grep`, `find`
- `docker`, `docker compose`
- `ssh`, `scp`, `tmux`
- `make`, `python`, `uv`

## Test Markers
```bash
# Run specific test types
pytest -m unit                  # Unit tests only
pytest -m integration           # Integration tests only
pytest -m slow                  # Slow-running tests
pytest -m "not slow"            # Fast tests only
```

## Performance Targets
- **Unit tests**: <30 seconds
- **Smoke tests**: 2-3 minutes
- **Full suite**: 5-10 minutes
