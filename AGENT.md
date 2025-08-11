# Jira → OpenProject Migration Tool — AGENT.md

This file is the single source of truth for AI coding tools. It describes how to build, test, run, and contribute to this project. It follows the AGENT.md guidance so any agent can work effectively in this codebase. See spec reference: [AGENT.md RFC](https://ampcode.com/AGENT.md).

## Project Overview

Jira to OpenProject (J2O) is a Python 3.13+ toolset for migrating data from Jira Server 9.11 into OpenProject 15. It supports users, projects, work packages, custom fields, statuses, workflows, attachments, and time logs with resilient, restartable processing.

- Language/runtime: Python 3.13+
- Packaging: `setuptools`, lock/install via `uv`
- CLI entry point: `j2o` (configured via `pyproject.toml`)
- Containerization: Docker + Docker Compose, non-root user by default
- Optional services: PostgreSQL, Redis (for caching/idempotency)
- Web dashboard: FastAPI + Uvicorn (optional admin/ops UI)

Key references: @README.md, @src/README.md, @.env.example

## Repository Structure

```
src/
  clients/                 # Jira/OpenProject/SSH/Docker/Rails clients
  migrations/              # Extract → Map → Load component migrations
  models/                  # Dataclasses and schemas
  mappings/                # Mapping helpers & loaders
  utils.py                 # Shared utilities
  config.py                # Access point to runtime configuration
  config_loader.py         # Loads YAML + .env + environment
  main.py                  # CLI entry (also exposed as `j2o`)

tests/
  unit/ functional/ integration/ end_to_end/ utils/

Dockerfile, Dockerfile.test, Makefile, pyproject.toml, .env.example
```

## Build, Run, and Common Commands

The project uses `uv` for dependency management and Docker Compose for dev/test environments. A Makefile provides convenient targets. All commands should be run from the repository root.

### Local (no containers)

- Install deps (locked):
  - `uv sync --frozen`
- Run tests quickly:
  - `python -m pytest -n auto`
- Type-checking:
  - `mypy src`
- Lint/format:
  - `flake8 src tests`
  - `black src tests`
  - `isort src tests`
- Full quick loop (recommended):
  - `make dev-test` (fast local tests with `-n auto`)

### Docker/Compose development

- Build images: `docker compose build` or `make build`
- Start app only: `make dev`
- Start stack: `make up`
- Stop: `make down` (or `make stop` to stop without removing)
- Logs: `make logs` or `make logs-app`
- Shell into app: `make shell`
- Execute command: `make exec CMD="python --version"`

### Testing (Compose-managed test container)

- Run tests: `make test` (spins up test service, runs `pytest -n auto`)
- Verbose: `make test-verbose`
- Coverage: `make test-coverage`
- Slow (integration/E2E): `make test-slow`
- Fast (unit only): `make test-fast`

### Application execution

- CLI entry point (after install): `j2o --help`
- Direct module:
  - `python src/main.py migrate --components users,projects,workpackages`
  - Dry run: `python src/main.py migrate --dry-run --components users`

## Development Environment

### Requirements

- Python 3.13+
- Docker and Docker Compose (for containerized dev/test)
- `uv` (installed in containers and used locally)

### Environment variables

Copy `.env.example` to `.env` and update values. Minimum required for service profiles:

- PostgreSQL: `POSTGRES_PASSWORD`
- Jira: `J2O_JIRA_URL`, `J2O_JIRA_USERNAME`, `J2O_JIRA_API_TOKEN`
- OpenProject: `J2O_OPENPROJECT_URL`, `J2O_OPENPROJECT_API_TOKEN` (or `J2O_OPENPROJECT_API_KEY`)

Optional (remote operations): `J2O_OPENPROJECT_SERVER`, `J2O_OPENPROJECT_USER`, `J2O_OPENPROJECT_CONTAINER`, `J2O_OPENPROJECT_TMUX_SESSION_NAME`

Directories: `J2O_DATA_DIR`, `J2O_BACKUP_DIR`, `J2O_RESULTS_DIR`

See @.env.example for full documentation.

## Code Style and Conventions

This project enforces modern Python standards (Python ≥3.13):

- Formatting: Black (line length 88 for code; Ruff line length 120)
- Imports: isort (profile "black")
- Linting: Ruff (select = ["ALL"]) and Flake8
- Types: Mypy (target 3.13) with strict-ish defaults (opted non-strict project wide, strict where helpful)

Language-level guidance:

- Use built-in generic types: `list[str]`, `dict[str, int]`, etc.
- Use the union pipe operator: `int | str`, optionals as `T | None`.
- Import ABCs from `collections.abc` (e.g., `Callable`, `Mapping`).

Error handling and execution style:

- Exceptions, not return codes, for error paths.
- Optimistic execution pattern: assume success, diagnose on failure with targeted `try/except` and rich diagnostics.
- Use `tenacity` for retries on transient failures; use `pybreaker` for circuit breaking of flaky dependencies.
- Log with `structlog` and preserve context.

Security and validation:

- Validate all external inputs (Jira keys, URLs, file paths). Use whitelists and length limits where applicable.
- Avoid interpolation in generated code/scripts; prefer safe builders/serialization.
- Never commit secrets; use environment variables or Docker secrets.

## Architecture and Design Patterns

High-level layered design (see @src/README.md):

- `clients/`:
  - `JiraClient`: Jira REST interactions
  - `OpenProjectClient`: High-level OpenProject operations; generates Ruby scripts executed via Rails console
  - `SSHClient`: SSH connectivity, command execution
  - `DockerClient`: Container ops (copy/exec) over SSH
  - `RailsConsoleClient`: tmux-backed Rails console execution
- `migrations/`:
  - `BaseMigration`: abstract E→M→L flow (`run`, `_extract`, `_map`, `_load`, `_test`)
  - Concrete migrations: users, projects, work packages, custom fields, statuses, workflows, links, issue types
- Resilience: retries, checkpointing, partial-proceed on failures, structured error files in `var/data` and logs in `var/logs/`

## Testing Guidelines

- Test framework: `pytest`
- Concurrency: `-n auto` (pytest-xdist) by default in Makefile targets
- Markers (configured in `pyproject.toml`): `unit`, `functional`, `integration`, `end_to_end`, `slow`, plus infra markers
- Discovery patterns:
  - Files: `tests/**/test_*.py`
  - Classes: `Test*`
  - Functions: `test_*`
- Typical flows:
  - Quick local loop: `make dev-test` or `python -m pytest -n auto`
  - Full coverage: `make test-coverage`
  - Targeted suite: `python -m pytest tests/unit -n auto -q`
- In tests, prefer explicit assertions and minimal fixture coupling. Use environment isolation for external-service tests.

## Security Considerations

- Do not commit `.env`. Use `.env.example` as template. Secrets must be supplied at runtime or via Docker secrets.
- Containers run as non-root (`DOCKER_UID`/`DOCKER_GID`). Ensure these map to your host user to avoid permission issues.
- Network operations (SSH, Rails console) should be guarded with timeouts, retries, and explicit allowlists.
- Validate user-supplied identifiers and paths before remote execution.
- Use HTTPS for Jira/OpenProject endpoints and keep `J2O_SSL_VERIFY=true` in production.

## Configuration Schema

Configuration is loaded by `src/config_loader.py` from (in precedence order):

1. Environment variables (`.env`, process env)
2. Optional YAML file (e.g., `config/config.yaml`) if present
3. Sensible defaults in code

Key knobs:

- `J2O_BATCH_SIZE` (default 100)
- `J2O_LOG_LEVEL` (INFO by default)
- `J2O_SSL_VERIFY` (true by default)
- Data directories: `J2O_DATA_DIR`, `J2O_BACKUP_DIR`, `J2O_RESULTS_DIR`

## Dependencies (selected)

- Core: `requests`, `pydantic`, `python-dotenv`, `pyyaml`, `rich`
- Reliability: `tenacity`, `pybreaker`, `structlog`
- Storage/State: `sqlalchemy`, `redis`, `psutil`, `jsonschema`
- Web (optional): `fastapi`, `uvicorn`, `websockets`, `jinja2`, `aiofiles`
- Security: `cryptography`, `bcrypt`
- Tests/Dev: `pytest`, `pytest-xdist`, `pytest-asyncio`, `pytest-cov`, `black`, `isort`, `flake8`, `mypy`, `ruff`, `pre-commit`

## Git and CI

- Run `make check` locally (lint + test) before committing.
- Prefer small, focused commits with accompanying tests.
- Avoid force-pushing shared branches.

## Agent Integration Notes

- This AGENT.md is the canonical configuration for AI coding tools.
- A symlink `.cursorrules` points to this file so Cursor will read it directly.
- Subsystems may add their own `AGENT.md` files in subdirectories for more specific guidance; tools should merge them with this root config taking lower precedence.

## Quickstart Checklist for Agents

1. If containers are needed, ensure `.env` exists (copy from `.env.example`) and set `POSTGRES_PASSWORD` at minimum.
2. For local dev without containers: `uv sync --frozen`, then `make dev-test`.
3. For full environment: `make up` then `make test`.
4. Use `j2o --help` to discover CLI commands for migration.

---

References used while composing this file: [AGENT.md RFC](https://ampcode.com/AGENT.md).

## Engineering Principles & Practices

- **Error handling with exceptions**: Never return error objects/status codes; raise exceptions with rich context. Favor small, clear try/except blocks around external I/O and network calls. See `docs/DEVELOPER_GUIDE.md`.
- **Optimistic execution**: Execute the happy path and diagnose only on failure (collect diagnostics in the exception handler). See `docs/DEVELOPER_GUIDE.md`.
- **Modern typing**: Use built-in generics (`list[str]`, `dict[str, Any]`), union pipe (`T | None`), and ABCs from `collections.abc`. Target Python 3.13 per `pyproject.toml`.
- **Resilience & logging**: Use `tenacity` for retries, `pybreaker` for circuit breaking, and `structlog` for structured logs.
- **Security-first**: Validate all external inputs (e.g., Jira keys), escape dynamic data in generated Ruby scripts, and maintain security tests. See `docs/SECURITY.md`.

## Development Workflow (Taskmaster)

- **Loop**: list → next → show <id> → expand <id> → implement → update-subtask → set-status.
- **Usage**: Prefer MCP tools in Cursor; CLI is the fallback. Keep tasks small, log decisions in subtasks, and use tags for feature branches/experiments.
- **Quality gates**: Before marking tasks done, run tests and lint/type checks (see Makefile targets and sections above).

## Planning & Code Review

- **Deepthink when planning** major work; document decisions in tasks/subtasks.
- **Automate reviews**: Capture review suggestions as follow-up tasks.
- **Continuously improve**: When you see repeated patterns or issues, update this AGENT.md and supporting docs. See `docs/DEVELOPER_GUIDE.md`.

## Legacy Removal Policy (YOLO)

- Remove legacy adapters/compat layers aggressively; simplify code paths.
- Mitigate with tests: ensure comprehensive coverage before/after removal.
- Keep architecture clean and current; update documentation accordingly.

## Continuous Improvement

- Add or refine rules when new tech/patterns appear in multiple places, or recurring bugs surface.
- Prefer actionable rules with real code examples; keep docs and AGENT.md in sync.

## Configuration & Environment Rules

- Load order: CLI args → env vars → `.env.local` → `.env` → `config/config.yaml`. Access configuration via `src/config.py` helper.
- Keep secrets out of VCS. Use `.env.local` or Docker secrets in production. See `docs/configuration.md` and `.env.example`.

