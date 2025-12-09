<!-- Managed by agent: keep sections and order; edit content, not structure. Last updated: 2025-10-10 -->
# AGENTS.md — src

## Overview
- Core Python package for the Jira→OpenProject migration CLI, clients, mappings, and migrations.
- Entry points: `src/main.py` (`j2o` CLI) and migration orchestration in `src/migration.py`.
- Architecture: `clients/` (Jira/OpenProject/SSH/Docker), `migrations/` (extract-map-load pipeline), `mappings/`, `models/`, `utils/`.

## Setup & environment
- Require Python 3.13+, `uv`, and a configured `.env`; copy `.env.example` then set real Jira/OpenProject credentials. `.env.local` is automatically loaded for developer-specific overrides.
- `J2O_OPENPROJECT_SERVER`, `J2O_OPENPROJECT_USER`, and `J2O_OPENPROJECT_CONTAINER` must point at the SSH host/container used by the tmux-backed Rails console (`start_rails_tmux.py` honours them).
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
- **Compute Location Principle**: Python does computation, Ruby does minimum INSERT only. Pre-compute version numbers, validity_period ranges, field mappings, and state snapshots in Python; Ruby only reads WP initial state and executes bulk INSERT. This leverages Python's superior parallelism (ThreadPoolExecutor) and reduces SSH/tmux overhead.
- When composing Rails console Ruby from Python, split the script into a parameterized head (f-string interpolation) and a literal body block so escaping stays predictable.
- User provenance flow now removes the obsolete "Jira user key" and "Tempo Account" custom fields so J2O_* attributes remain the canonical origin source.
- On user updates, also map Jira locale → OpenProject `UserPreference.language` and backfill avatars through `Avatars::UpdateService` (skip silently when Jira exposes no avatar URLs).
- Project migration assigns Jira project leads to the OpenProject project (Project admin role), persists lead/category/type/URL/avatar provenance, and enables modules adaptively (Tempo-linked projects get time tracking/costs; projects with categories/types enable calendar/news).
- Work package migration maps configured Jira start-date custom fields into OpenProject `start_date` while keeping the raw custom fields for auditing.
- If those custom fields are absent, derive `start_date` from the first Jira status transition whose category equals *In Progress* to keep planning timelines meaningful.
- Prefer running long-lived migrations via `python scripts/run_rehearsal.py --use-container --collect --stop` to reuse the tmux Rails console and capture artefacts.
- When fast-forwarding work packages manually, pass `--reset-wp-checkpoints` to the CLI if `.migration_checkpoints.db` may be stale or corrupted.
- Follow `BaseMigration` conventions for logging (`self.logger`), idempotent JSON caches (`_load_from_json`/`_save_to_json`), and mapping updates via `config.mappings` so downstream components stay consistent.

## Security & safety
- Never hardcode credentials; load secrets from env or Docker secrets (`.env` excluded from VCS).
- Validate Jira/OpenProject identifiers (length, whitelist) before remote calls; reuse helpers in the sanitization helpers under `src/utils/enhanced_*`.
- Authentication is limited to Jira/OpenProject API tokens; the former `SecurityManager` and audit modules were retired for the admin-only workflow.
- Keep `migration.enable_rails_meta_writes` set to `true` so Rails helpers can preserve authorship/timestamps/audit history when replaying metadata.
- Keep remote Rails interactions idempotent; persist results to `var/data` and capture logs under `var/logs`.
- Guard file operations with safe paths under `var/`; avoid deleting OpenProject data outside dedicated cleanup utilities.
- SSH/ tmux configuration lives in env (`J2O_OPENPROJECT_SERVER`, `J2O_OPENPROJECT_CONTAINER`, `J2O_OPENPROJECT_TMUX_SESSION_NAME`); verify before using `start_rails_tmux.py` or containerized migrations.

## PR/commit checklist
- Update or add migration/unit tests covering new paths (`tests/unit/...`).
- For project or work-package metadata adjustments run `uv run --active --no-cache pytest -q tests/unit/test_enhanced_timestamp_migrator.py tests/unit/test_work_package_start_date.py tests/functional/test_project_migration.py::TestProjectMigration::test_assign_project_lead_happy_path`.
- Confirm JSON payload sanitization removes `_links` and respects ID flattening before invoking Rails scripts.
- Run the commands in this section plus `uv run python -m pytest tests/unit/test_wp_json_clean.py -q` when touching work-package flows.
- Refresh docs (`README.md`, `docs/DEVELOPER_GUIDE.md`) if workflows or required env vars change.
- Call out rehearsal runs (dry-run, logs review, state reset) when updating guides or release notes.
- When modifying checkpoint/fast-forward logic, re-run `make container-test TEST_OPTS="-k work_package_checkpoint"` and `python scripts/run_rehearsal.py --use-container --collect --stop`.

## Good vs. bad examples
- Good: `src/migrations/work_package_migration.py` — demonstrates chunked extract/map/load with retries and rich diagnostics.
- Good: `src/clients/openproject_client.py` — encapsulates Rails console execution with structured logging and input validation.
- Caution: `src/cleanup_openproject.py` — legacy direct-deletion script; do not model new migrations on this ad-hoc pattern.

## When stuck
- Check `src/README.md` for module-level map and `docs/DEVELOPER_GUIDE.md` for architecture/error-handling guidance.
- Inspect recent Taskmaster subtasks or `.github/instructions/taskmaster.md` to align with current sprint context.
- Ask for redis/postgres container status via `make status` before debugging client/network errors.
- Use `uv run --active --no-cache python scripts/data_qa.py --projects <KEY>` to confirm module snapshots and start-date coverage; heed any warnings printed by the script.
- For full rehearsals, prefer `python scripts/run_rehearsal.py --use-container --collect --stop` so logs, mappings, and checkpoints are archived automatically.

## Decision Log
- Consolidated build/test commands from `pyproject.toml` and Makefile into per-scope checks.
- Captured sanitization and security guidance from `docs/SECURITY.md` and legacy root `AGENTS.md`.
- Flagged cleanup script as legacy to steer new work toward migration abstractions.
- Limited mypy coverage to `src/config_loader.py` pending broader cleanup of type errors across migrations and clients.
- Host sandbox lacks PyPI connectivity, so run pytest targets inside the project containers instead of locally.
- Retired the advanced security and large-scale optimizer modules; keep migrations simple and focused on single-operator runs.
- Target environment tested against Jira Server 9.x and OpenProject 16.x; other versions are “best effort”.
- The automated test-suite generator remains experimental; treat it as optional scaffolding until requirements solidify.
- Group migration synchronizes Jira groups and role-driven memberships; ensure user/account flows populate J2O origin custom fields and timezones for provenance.
- User provenance flow now removes the obsolete "Jira user key" and "Tempo Account" custom fields so J2O_* attributes remain the canonical origin source.
- Added ADR 2025-10-03 documenting the validation strategy for project modules and start-date precedence; tests and docs now guard these flows.
- Containerised targets `make container-test` / `make container-test-integration` now gate unit/integration smoke tests and run in CI; use them instead of host pytest for repeatable results.
- `scripts/run_rehearsal.py` supersedes ad-hoc rehearsal commands and captures artefacts under `var/rehearsal/<timestamp>`.
