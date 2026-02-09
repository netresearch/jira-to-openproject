<!-- Managed by agent: keep sections & order; edit content, not structure. Last updated: 2026-02-09 -->

# AGENTS.md (root)

**Precedence:** The **closest AGENTS.md** to changed files wins. Root holds global defaults only.

## Global rules
- Keep PRs small (~≤300 net LOC), land tests with code
- Conventional Commits: `feat(scope):`, `fix:`, `docs:`, `refactor:`
- Ask before: heavy deps, full e2e suites, repo-wide rewrites
- Never commit secrets or PII; use `.env.local` for overrides

## Commands (verified 2026-02-09)
| Command | Purpose | ~Time |
|---------|---------|-------|
| `make dev-test` | Unit tests locally (fastest feedback) | ~2s |
| `make dev-test-fast` | Unit tests only, locally | ~2s |
| `make container-test` | Unit tests in Docker (full deps) | ~30s |
| `make container-test-integration` | Integration tests in Docker (mocked) | ~30s |
| `make lint` | Ruff + mypy in container | ~10s |
| `make format` | Ruff format in container | ~5s |
| `make start-rails` | Start tmux Rails console (installs irbrc first) | ~10s |
| `make install-irbrc` | Install contrib/openproject.irbrc to remote container | ~5s |
| `make migrate-stop` | Stop all migration processes | ~3s |
| `make migrate-start-ff` | Start migration with fast-forward | ~5s |
| `make migrate-status` | Show migration status | ~2s |

## File map
```
src/               → Core Python package (clients, migrations, mappings, models, utils)
src/clients/       → Jira/OpenProject/SSH/Docker/RailsConsole adapters
src/migrations/    → 41 extract→map→load migration modules
src/dashboard/     → FastAPI admin dashboard (Vue + Chart.js + WebSocket)
src/ruby/          → Ruby template scripts for Rails console execution
src/utils/         → Shared helpers (retry, checkpoint, timezone, markdown)
tests/             → Pytest suites: 79 unit, 17 functional, 16 integration
scripts/           → Operational helpers (tmux bootstrap, rehearsal, migration shells)
config/            → YAML configuration, schemas, environment templates
docs/              → Architecture, entity mapping, developer guide, security
contrib/           → OpenProject .irbrc and contributed assets
var/               → Runtime data, logs, caches, checkpoints (gitignored)
```

## Golden samples
| For | Reference | Key patterns |
|-----|-----------|-------------|
| Migration module | `src/migrations/work_package_migration.py` | Chunked extract/map/load, retries, diagnostics |
| Client adapter | `src/clients/openproject_client.py` | Rails console exec, structured logging, validation |
| Unit test | `tests/unit/test_config_loader_security_enhanced.py` | Strict assertions, environment isolation |
| Utility | `src/utils/enhanced_timestamp_migrator.py` | Timestamp mapping, timezone handling |

## Heuristics
| When | Do |
|------|-----|
| Adding migration module | Extend `BaseMigration`, implement extract→map→load |
| Modifying Rails scripts | Pre-compute in Python, Ruby does INSERT only |
| Adding env var | Add to `.env.example`, document in `docs/DEVELOPER_GUIDE.md` |
| Touching work-package flows | Run `make container-test TEST_OPTS="-k work_package"` |
| Adding custom field | Update `src/mappings/mappings.py` and verify provenance |
| Performance issue in Rails | Use `insert_all`/`pluck(:id).to_set` patterns, batch operations |

## Boundaries
**Always:** Check `git status` first · Use feature branches · Run `make dev-test` before commit
**Ask first:** Heavy deps · Repo-wide rewrites · Full e2e suites · Deleting migration data
**Never:** Commit secrets · Work on main · Skip tests · Hardcode credentials

## Codebase state
- Python 3.14+, `uv` package manager, Docker Compose test profile
- Target: Jira Server 9.x → OpenProject 15+/16.x
- OpenProject 15+ requires `journal_notes`/`journal_user` + `save!` pattern (not `journals.create!`)
- `J2O_FORCE_RAILS_RUNNER=1` bypasses tmux console for `rails runner` mode
- 41 migration modules, 307 projects migrated, 65K+ work packages
- Rails runner timeout: 300s (increased from 120s for large projects like ADKP)
- Watcher migration uses batch `insert_all` with pre-fetched ID sets

## Terminology
| Term | Means |
|------|-------|
| J2O | Jira to OpenProject (this project) |
| Provenance | J2O custom fields (Origin System/ID/Key/URL) — authoritative source |
| ETL | Extract→Map→Load pipeline in each migration module |
| Rails console | Remote ActiveRecord session via SSH→Docker→tmux |
| irbrc | Ruby init file loaded by Rails console (`contrib/openproject.irbrc`) |
| Fast-forward | Resume migration from checkpoint, skip completed items |

## Scope index
| Scope | Description |
|-------|-------------|
| [`src/AGENTS.md`](src/AGENTS.md) | Core Python package, migrations, clients, CLI |
| [`src/dashboard/AGENTS.md`](src/dashboard/AGENTS.md) | FastAPI dashboard, websockets, frontend |
| [`tests/AGENTS.md`](tests/AGENTS.md) | Pytest suites (unit/functional/integration) |
| [`scripts/AGENTS.md`](scripts/AGENTS.md) | Operational helpers, tmux bootstrap, rehearsals |

## Key documentation
- [`docs/ENTITY_MAPPING.md`](docs/ENTITY_MAPPING.md) — Jira→OpenProject field mappings
- [`docs/MIGRATION_COMPONENTS.md`](docs/MIGRATION_COMPONENTS.md) — Module catalog with dev state
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — Client layer design
- [`docs/DEVELOPER_GUIDE.md`](docs/DEVELOPER_GUIDE.md) — Development standards
- [`docs/SECURITY.md`](docs/SECURITY.md) — Security practices

## Quick start
```bash
cp .env.example .env        # Configure credentials
uv sync --frozen            # Install dependencies
make dev-test               # Verify setup (run unit tests)
make start-rails            # Start tmux Rails console
j2o --help                  # Discover CLI commands
```

## When instructions conflict
Nearest AGENTS.md wins. User prompts override files.
