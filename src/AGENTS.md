<!-- Managed by agent: keep sections and order; edit content, not structure. Last updated: 2026-02-09 -->
# AGENTS.md — src

## Overview
- Core Python package for the Jira→OpenProject migration CLI, clients, mappings, and migrations.
- Entry points: `src/main.py` (`j2o` CLI) and migration orchestration in `src/migration.py`.
- Architecture: `clients/` (Jira/OpenProject/SSH/Docker/RailsConsole), `application/components/` (41 extract-map-load migration modules), `mappings/`, `models/`, `utils/`, `ruby/`, `dashboard/`.

## Setup & environment
- Require Python 3.14+, `uv`, and a configured `.env`; copy `.env.example` then set real Jira/OpenProject credentials. `.env.local` is automatically loaded for developer-specific overrides.
- `J2O_OPENPROJECT_SERVER`, `J2O_OPENPROJECT_USER`, and `J2O_OPENPROJECT_CONTAINER` must point at the SSH host/container used by the tmux-backed Rails console (`start_rails_tmux.py` honours them).
- `J2O_FORCE_RAILS_RUNNER=1` bypasses tmux console and uses `rails runner` directly (useful when tmux is unavailable).
- `J2O_SKIP_WATCHERS=1` skips watcher migration during content migration (for performance isolation).
- Install dependencies with `uv sync --frozen`; container workflows rely on `docker compose --profile test …` targets in the root Makefile.
- Configuration precedence: env var overrides → `.env.local` → `.env` → YAML defaults under `config/`.

## Build & tests (prefer file-scoped)
- Typecheck: `uv run --no-cache mypy src/config_loader.py`
- Lint/format: `python -m compileall src`
- Targeted unit tests: `make container-test TEST_OPTS="-k test_group_migration"` (uses Docker image with libffi/aiohttp).
- Integration smoke (skips when infra unavailable): `make container-test-integration TEST_OPTS="-k timezone_detection_integration"`
- Full rehearsal against mocks: `python scripts/run_rehearsal.py --use-container --collect --stop`

## Code style & conventions
- Use `ruff` for linting/formatting (line length 120); follow mypy non-strict defaults but annotate new code fully.
- Prefer built-in generics (`list[str]`), `|` unions, and ABCs from `collections.abc`.
- Implement migrations via `BaseMigration` with extract → map → load methods, optimistic execution, and structured logging via `structlog`.
- Wrap external I/O with `tenacity` retries and `pybreaker` circuit breakers; raise exceptions with rich context instead of returning error codes.
- Generate Rails console scripts via `OpenProjectClient` helpers; sanitize payloads first and keep Ruby minimal per `docs/SECURITY.md`.

## Security & safety
- Never hardcode credentials; load secrets from env or Docker secrets (`.env` excluded from VCS).
- Validate Jira/OpenProject identifiers (length, whitelist) before remote calls; reuse helpers in the sanitization helpers under `src/utils/enhanced_*`.
- Authentication is limited to Jira/OpenProject API tokens; the former `SecurityManager` and audit modules were retired for the admin-only workflow.
- Keep `migration.enable_rails_meta_writes` set to `true` so Rails helpers can preserve authorship/timestamps/audit history when replaying metadata.
- Keep remote Rails interactions idempotent; persist results to `var/data` and capture logs under `var/logs`.
- Guard file operations with safe paths under `var/`; avoid deleting OpenProject data outside dedicated cleanup utilities.

## PR/commit checklist
- Update or add migration/unit tests covering new paths (`tests/unit/...`).
- Confirm JSON payload sanitization removes `_links` and respects ID flattening before invoking Rails scripts.
- Run `uv run python -m pytest tests/unit/test_wp_json_clean.py -q` when touching work-package flows.
- Refresh docs (`README.md`, `docs/DEVELOPER_GUIDE.md`) if workflows or required env vars change.
- When modifying checkpoint/fast-forward logic, re-run `make container-test TEST_OPTS="-k work_package_checkpoint"`.

## Good vs. bad examples
- Good: `src/application/components/work_package_migration.py` — demonstrates chunked extract/map/load with retries and rich diagnostics.
- Good: `src/infrastructure/openproject/openproject_client.py` — encapsulates Rails console execution with structured logging and input validation.
- Caution: `src/cleanup_openproject.py` — legacy direct-deletion script; do not model new migrations on this ad-hoc pattern.

## When stuck
- Check `src/README.md` for module-level map and `docs/DEVELOPER_GUIDE.md` for architecture/error-handling guidance.
- Inspect `.github/instructions/taskmaster.md` to align with current sprint context.
- Ask for redis/postgres container status via `make status` before debugging client/network errors.
- Use `uv run --active --no-cache python scripts/data_qa.py --projects <KEY>` to confirm module snapshots and start-date coverage.
- For full rehearsals, prefer `python scripts/run_rehearsal.py --use-container --collect --stop` so logs, mappings, and checkpoints are archived automatically.

## House Rules (Critical Architectural Requirements)

### Rails Console via Tmux is REQUIRED
- **Persistent tmux session is MANDATORY** for Rails console operations (50-100x faster than one-off sessions)
- Pre-migration: `make install-irbrc` then `make start-rails`
- Verify session: `tmux list-sessions | grep rails_console`
- Fallback: `J2O_FORCE_RAILS_RUNNER=1` uses `rails runner` directly (slower, no persistent session)

### OpenProject 15+ Journal Pattern
- **`wp.journal_notes = text; wp.journal_user = user; wp.save!`** is REQUIRED for creating journals
- Direct `wp.journals.create!(...)` fails with `PG::NotNullViolation` on `data_type` column
- OpenProject 15+ schema requires `data_type`, `data_id`, `validity_period` — only `save!` populates them correctly

### REST API is NOT Suitable for Bulk Migration
- **Rails console via ActiveRecord is REQUIRED** for all data import operations
- REST API cannot handle bulk operations efficiently
- Direct ActiveRecord access needed for validation bypass, transactions, and features not exposed via REST

### Idempotency is MANDATORY
- All migration components MUST support re-runs without creating duplicates
- **Provenance metadata is authoritative**: J2O custom fields (Origin System/ID/Key/URL)
- Mapping files are CACHE ONLY: can be deleted and rebuilt from provenance
- Never block migration on missing mapping files—query provenance instead

### Compute Location Principle
- **Python does computation, Ruby does minimum INSERT only**
- Pre-compute version numbers, validity periods, field mappings in Python
- Ruby only reads WP initial state and executes bulk INSERT
- Use `insert_all` for bulk inserts, `pluck(:id).to_set` for pre-fetched ID validation
- Rails runner timeout is 300s (configurable via `openproject_client.py`)
- This leverages Python's ThreadPoolExecutor and reduces SSH/tmux overhead

## Decision Log
- Consolidated build/test commands from `pyproject.toml` and Makefile into per-scope checks.
- Captured sanitization and security guidance from `docs/SECURITY.md` and legacy root `AGENTS.md`.
- Flagged cleanup script as legacy to steer new work toward migration abstractions.
- Limited mypy coverage to `src/config_loader.py` pending broader cleanup of type errors across migrations and clients.
- Host sandbox lacks PyPI connectivity, so run pytest targets inside the project containers instead of locally.
- Target environment tested against Jira Server 9.x and OpenProject 17.3+; other versions are "best effort".
- Journal creation changed from `journals.create!` to `journal_notes`/`journal_user` + `save!` for OP 15+ compatibility (2026-01).
- Watcher migration rewritten to use batch `insert_all` with pre-fetched ID sets (`pluck(:id).to_set`) for 100x+ speedup (2026-01).
- Rails runner timeout increased from 120s to 300s for large projects like ADKP (872 WPs) (2026-01).
- Added `J2O_FORCE_RAILS_RUNNER` and `J2O_SKIP_WATCHERS` env vars for operational flexibility (2026-01).
