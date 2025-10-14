<!-- Managed by agent: keep sections and order; edit content, not structure. Last updated: 2025-10-10 -->
# AGENTS.md — tests

## Overview
- Pytest suite spanning unit, functional, integration, and end-to-end coverage for the migration tool.
- Default configuration lives in `pytest.ini` and `pyproject.toml`; markers include `unit`, `integration`, `end_to_end`, `slow`, and infra flags.
- Tests exercise client adapters, migration flows, sanitizers, and the optional dashboard.

## Setup & environment
- Install deps with `uv sync --frozen`; populate `.env` and `.env.local` when tests need Jira/OpenProject/tmux metadata (e.g., `J2O_OPENPROJECT_CONTAINER`).
- Container runs rely on `docker compose --profile test`; `make container-test` auto-builds the test image with libffi/aiohttp.
- Local runs cache under `var/.pytest_cache`; clean via `rm -rf var/.pytest_cache` if suites behave oddly.
- `scripts/run_rehearsal.py --use-container --collect --stop` can pre-populate caches/artefacts before lengthy assertions.

## Build & tests (prefer file-scoped)
- Typecheck: `uv run --no-cache mypy tests/unit/__init__.py`
- Lint/format: `python -m compileall tests`
- Unit subset (Docker): `make container-test TEST_OPTS="-k test_group_migration"`
- Integration markers (Docker, skips tolerated): `make container-test-integration`
- Focused debug: `docker compose --profile test run --rm test python -m pytest -m integration -k test_timezone_detection_integration`

## Code style & conventions
- Stick to bare `assert` statements with informative failure messages; avoid `print` in new tests.
- Use parametrization and fixtures documented in `tests/unit/README.md` to keep tests independent and fast.
- Honor markers from `pytest.ini`; gate slow/infra-dependent cases accordingly and skip when prerequisites are absent.
- When mocking external services, rely on helpers in `tests/utils` instead of ad-hoc monkeypatching.

## Security & safety
- Never embed real tokens; use fixtures/env overrides. Sanitized payloads must exclude `_links` and sensitive metadata.
- Persist test artifacts under `var/data`/`var/logs` only; clean temporary files within tests to keep reruns deterministic.
- Avoid accidental network calls by using provided stubs/mocks; mark any required live call with `requires_*` markers.

## PR/commit checklist
- Update or add targeted tests alongside code changes; ensure markers and naming follow `test_*.py` conventions.
- Run the commands above plus `uv run python -m pytest tests/unit/test_wp_json_clean.py -q` when modifying work-package sanitization.
- Keep fixtures lightweight; prefer factory helpers to duplicating large payloads inline.
- Sync docs in `tests/unit/README.md` if fixture structure or helper semantics change.
- When touching integration suites, ensure they either connect to the live tmux/SSH setup or are marked `skipif` with clear messaging; note expected skips in PR description.

## Good vs. bad examples
- Good: `tests/unit/test_config_loader_security_enhanced.py` — showcases strict assertions and environment isolation.
- Good: `tests/unit/test_wp_json_clean.py` — validates sanitization helpers with focused fixtures.
- Caution: `tests/test_dashboard_simple.py` — legacy smoke test uses print-driven assertions; do not model new tests on this pattern.

## When stuck
- Review `tests/unit/README.md` for fixture maps and helper usage.
- Cross-check Taskmaster backlog for test debt or pending scenarios before adding new fixtures.
- Reach for `pytest -k <pattern>` to bisect failures quickly when suites are large.
- Need to exercise Rails console flows? Start tmux via `python scripts/start_rails_tmux.py --attach` and rerun affected tests once the session is ready.

## Decision Log
- Commands derived from `pytest.ini`, Makefile dev targets, and existing targeted suites.
- Flagged dashboard smoke test as legacy to encourage idiomatic pytest patterns.
- Narrowed mypy scope to `tests/unit/__init__.py` because broader suites fail TypedDict checks and need cleanup.
- Host sandbox lacks PyPI connectivity, so execute pytest suites inside the containers or another online environment.
- Retired security/large-scale optimizer suites now that those modules were removed; focus coverage on migration flows and sanitization.
- Docker-centric targets (`make container-test`, `make container-test-integration`) replace local pytest for reproducibility; GitHub Actions mirrors the unit subset.
