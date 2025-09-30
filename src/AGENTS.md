<!-- Managed by agent: keep sections and order; edit content, not structure. Last updated: 2025-09-30 -->
# AGENTS.md — src

## Overview
- Core Python package for the Jira→OpenProject migration CLI, clients, mappings, and migrations.
- Entry points: `src/main.py` (`j2o` CLI) and migration orchestration in `src/migration.py`.
- Architecture: `clients/` (Jira/OpenProject/SSH/Docker), `migrations/` (extract-map-load pipeline), `mappings/`, `models/`, `utils/`.

## Setup & environment
- Require Python 3.13+, `uv`, and a configured `.env`; copy `.env.example` and fill Jira/OpenProject tokens plus `POSTGRES_PASSWORD`.
- Install dependencies locally with `uv sync --frozen`; container workflows use `make build`/`make dev` from repo root.
- Access configuration via `src/config.py`; runtime precedence: env vars → `.env.local` → `.env` → YAML in `config/`.

## Build & tests (prefer file-scoped)
- Typecheck: `uv run --no-cache mypy src/config_loader.py`
- Lint/format: `python -m compileall src`
- Targeted tests: Container-only via `make dev-test TEST_OPTS="-k test_main"`

## Code style & conventions
- Use `ruff` for linting/formatting (line length 120); follow mypy non-strict defaults but annotate new code fully.
- Prefer built-in generics (`list[str]`), `|` unions, and ABCs from `collections.abc`.
- Implement migrations via `BaseMigration` with extract → map → load methods, optimistic execution, and structured logging via `structlog`.
- Wrap external I/O with `tenacity` retries and `pybreaker` circuit breakers; raise exceptions with rich context instead of returning error codes.
- Generate Rails console scripts via `OpenProjectClient` helpers; sanitize payloads first and keep Ruby minimal per `docs/SECURITY.md`.

## Security & safety
- Never hardcode credentials; load secrets from env or Docker secrets (`.env` excluded from VCS).
- Validate Jira/OpenProject identifiers (length, whitelist) before remote calls; reuse helpers in `src/utils/enhanced_*` modules.
- Keep remote Rails interactions idempotent; persist results to `var/data` and capture logs under `var/logs`.
- Guard file operations with safe paths under `var/`; avoid deleting OpenProject data outside dedicated cleanup utilities.

## PR/commit checklist
- Update or add migration/unit tests covering new paths (`tests/unit/...`).
- Confirm JSON payload sanitization removes `_links` and respects ID flattening before invoking Rails scripts.
- Run the commands in this section plus `uv run python -m pytest tests/unit/test_wp_json_clean.py -q` when touching work-package flows.
- Refresh docs (`README.md`, `docs/DEVELOPER_GUIDE.md`) if workflows or required env vars change.

## Good vs. bad examples
- Good: `src/migrations/work_package_migration.py` — demonstrates chunked extract/map/load with retries and rich diagnostics.
- Good: `src/clients/openproject_client.py` — encapsulates Rails console execution with structured logging and input validation.
- Caution: `src/cleanup_openproject.py` — legacy direct-deletion script; do not model new migrations on this ad-hoc pattern.

## When stuck
- Check `src/README.md` for module-level map and `docs/DEVELOPER_GUIDE.md` for architecture/error-handling guidance.
- Inspect recent Taskmaster subtasks or `.github/instructions/taskmaster.md` to align with current sprint context.
- Ask for redis/postgres container status via `make status` before debugging client/network errors.

## Decision Log
- Consolidated build/test commands from `pyproject.toml` and Makefile into per-scope checks.
- Captured sanitization and security guidance from `docs/SECURITY.md` and legacy root `AGENTS.md`.
- Flagged cleanup script as legacy to steer new work toward migration abstractions.
- Limited mypy coverage to `src/config_loader.py` pending broader cleanup of type errors across migrations and clients.
- Host sandbox lacks PyPI connectivity, so run pytest targets inside the project containers instead of locally.
