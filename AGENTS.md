<!-- Managed by agent: keep sections & order; edit content, not structure. Last updated: 2025-12-11 -->

# AGENTS.md (root)

**Precedence:** The **closest AGENTS.md** to changed files wins. Root holds global defaults only.

## Additional guidance files
- **AGENTS.tasks.md** — Task management with bd (beads) - read for all work tracking

## Global rules
- Keep PRs small (~≤300 net LOC), land tests with code
- Conventional Commits: `feat(scope):`, `fix:`, `docs:`, `refactor:`
- Ask before: heavy deps, full e2e suites, repo-wide rewrites
- Never commit secrets or PII; use `.env.local` for overrides

## Pre-commit checks
- Typecheck: `uv run --no-cache mypy src/config_loader.py`
- Lint: `uv run ruff check src tests` or `make lint`
- Format: `uv run ruff format src tests` or `make format`

## Running tests

### Quick reference
| Command | Description | Speed |
|---------|-------------|-------|
| `make dev-test` | Unit tests locally (fastest feedback) | ~2s |
| `make dev-test-fast` | Unit tests only, locally | ~2s |
| `make container-test` | Unit tests in Docker (full deps) | ~30s |
| `make container-test-integration` | Integration tests in Docker (mocked) | ~30s |
| `make test-slow` | Integration + E2E in Docker | ~30s |

### Recommended workflow
```bash
# Daily development: fast local feedback
make dev-test                    # Run unit tests locally

# Before commit: full validation
make container-test              # Unit tests with all deps
make lint                        # Code quality

# Full suite (CI equivalent)
make container-test              # Unit tests (79 tests)
make container-test-integration  # Integration tests (156 tests, mocked)
```

### Test environment variables
Tests use markers that can be enabled via environment variables:
- `J2O_RUN_INTEGRATION=true` — Enable integration tests
- `J2O_RUN_E2E=true` — Enable end-to-end tests
- `J2O_ENABLE_DOCKER=true` — Enable Docker-dependent tests
- `J2O_ENABLE_SSH=true` — Enable SSH-dependent tests
- `J2O_ENABLE_RAILS=true` — Enable Rails-dependent tests
- `J2O_RUN_ALL_TESTS=true` — Enable all test categories

### Running specific tests
```bash
# Single test file
make container-test TEST_OPTS="-k test_group_migration"

# With verbose output
make test-verbose

# Integration tests with live SSH (requires infrastructure)
make test-live-ssh
```

See `tests/AGENTS.md` for detailed testing conventions and fixtures.

## Index of scoped AGENTS.md
| Scope | Description |
|-------|-------------|
| `src/AGENTS.md` | Core Python package, migrations, clients, CLI |
| `src/dashboard/AGENTS.md` | FastAPI dashboard, websockets, frontend |
| `tests/AGENTS.md` | Pytest suites (unit/functional/integration) |
| `scripts/AGENTS.md` | Operational helpers, tmux bootstrap, rehearsals |

## When instructions conflict
Nearest AGENTS.md wins. User prompts override files.

## Key documentation
- [ENTITY_MAPPING.md](docs/ENTITY_MAPPING.md) — Jira→OpenProject field mappings
- [MIGRATION_COMPONENTS.md](docs/MIGRATION_COMPONENTS.md) — Module catalog with dev state
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — Client layer design
- [DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md) — Development standards
- [SECURITY.md](docs/SECURITY.md) — Security practices

## Quick start
```bash
cp .env.example .env        # Configure credentials
uv sync --frozen            # Install dependencies
make dev-test               # Verify setup (run unit tests)
make start-rails            # Start tmux Rails console
j2o --help                  # Discover CLI commands
```
