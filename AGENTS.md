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

## Minimal pre-commit checks
- Typecheck: `uv run --no-cache mypy src/config_loader.py`
- Lint: `uv run ruff check src tests`
- Format: `uv run ruff format src tests`
- Tests: `make container-test` (Docker) or `make dev-test` (local)

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
make start-rails            # Start tmux Rails console
j2o --help                  # Discover CLI commands
```
