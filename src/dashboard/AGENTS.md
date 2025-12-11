<!-- Managed by agent: keep sections and order; edit content, not structure. Last updated: 2025-12-11 -->
# AGENTS.md — src/dashboard

## Overview
- FastAPI admin dashboard exposing migration metrics, websocket feeds, and HTML templates.
- Primary entry: `src/dashboard/app.py`; static assets live under `src/dashboard/static`, templates under `src/dashboard/templates`.
- Depends on Redis for live metrics fan-out and uses Vue + Chart.js on the client.

## Setup & environment
- Ensure `uv sync --frozen` has installed FastAPI, redis, and frontend dependencies.
- Dashboard expects Redis on `localhost:6379`; override connection details before shipping (see `startup_event`).
- Run locally via `uv run uvicorn src.dashboard.app:app --reload --host 0.0.0.0 --port 8001` after the main stack is up (`make dev`).

## Build & tests (prefer file-scoped)
- Typecheck: Deferred — dashboard modules are not mypy-clean; coordinate with platform team before enabling.
- Lint/format: `python -m compileall src/dashboard`
- Container smoke: `make container-test TEST_OPTS="-k test_dashboard_simple"`
- Local run: `uv run uvicorn src.dashboard.app:app --reload --host 0.0.0.0 --port 8001`

## Code style & conventions
- Keep endpoints async and reuse `ConnectionManager` helpers; prefer dependency injection for shared state.
- Model payloads with Pydantic (`MigrationProgress`, `MigrationMetrics`) and respond with structured JSON or templated HTML.
- Frontend scripts should stay in `static/js/dashboard.js`; avoid inline JS in templates beyond Vue bindings.
- Use `configure_logging` for structured logs and log websocket lifecycle events.

## Security & safety
- Do not expose the dashboard on the public internet without TLS, auth, and rate limiting; default config assumes trusted operator network.
- Sanitize websocket inputs; only accept recognized control messages and guard Redis access.
- Avoid introducing new CDN dependencies without pinning versions and documenting why they are necessary.

## PR/commit checklist
- Update templates and static assets together; ensure cache-busting if filenames change.
- Verify Redis connection fallback paths and broadcast loops via the targeted pytest above.
- Exercise local run (`uvicorn`) to confirm websockets and metrics streams render correctly.
- Document new controls or metrics in `README.md` or `docs/dashboard.md` if you add them.

## Good vs. bad examples
- Good: `src/dashboard/app.py` — shows async route structure, websocket broadcasting, and Redis lifecycle management.
- Good: `src/dashboard/static/js/dashboard.js` — encapsulates Vue components and websocket handling cleanly.
- Caution: `src/dashboard/templates/dashboard.html` — relies on CDN assets; prefer bundling or pinning hashes for new dependencies.

## When stuck
- Inspect `tests/test_dashboard_simple.py` for expected routes/resources; extend it when adding endpoints.
- Review `docs/DEVELOPER_GUIDE.md` for operational expectations and logging patterns.
- Coordinate UI work through Taskmaster context (`.github/instructions/taskmaster.md`) to keep sprint artifacts aligned.

## Decision Log
- Derived runtime expectations and commands from existing Makefile targets and FastAPI module setup.
- Highlighted CDN usage from current template to ensure future work documents security posture.
- Documented lack of mypy coverage because `src/dashboard/app.py` currently fails strict type checking.
- Host sandbox lacks PyPI connectivity, so rely on container pytest runs and keep local checks to `compileall`/static analysis.
- Added container smoke guidance (`make container-test …`) to align with the docker-first testing workflow.
