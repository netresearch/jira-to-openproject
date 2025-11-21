<!-- Managed by agent: keep sections and order; edit content, not structure. Last updated: 2025-10-15 -->
# AGENTS.md (root)
This file explains repo-wide conventions and where to find scoped rules.
**Precedence:** the *closest* `AGENTS.md` to your changes wins. Root holds global defaults only.

## Documentation Structure

**IMPORTANT**: This file includes additional agent guidance files. All `AGENTS.*.md` files in the root directory are part of the agent instructions:

- **AGENTS.tasks.md** - Task management with bd (beads) - **CRITICAL: Read this for all work tracking**
- Additional AGENTS.*.md files may be added for specific domains (testing, security, etc.)

AI agents MUST read and follow ALL AGENTS.*.md files in scope.

## Global rules
- Keep diffs small, land tests with code, and ask before heavy deps, e2e suites, or repo-wide rewrites.
- **Use `bd` for ALL work tracking and task management** - See AGENTS.tasks.md for complete details.
- Merge guidance from tool-specific files (`.github/instructions/*.md`, `.cursor/rules/*.mdc`, `docs/SECURITY.md`) with the nearest scoped file.

## Index of scoped AGENTS.md
- `src/AGENTS.md` — core Python package, migrations, clients, utilities
- `src/dashboard/AGENTS.md` — FastAPI dashboard & assets
- `tests/AGENTS.md` — pytest suites (unit/integration)
- `scripts/AGENTS.md` — operational helpers (`start_rails_tmux.py`, `run_rehearsal.py`, maintenance scripts)

## Minimal pre-commit checks
- Typecheck: `uv run --no-cache mypy src/config_loader.py`
- Lint/format: `python -m compileall src tests`
- Unit tests: Container-only via `make dev-test` (host sandbox blocks PyPI access)

## House Rules (Defaults)
- Commits: atomic, Conventional Commits (`feat(scope): …`, `fix: …`), keep PRs <≈300 net LOC.
- Types & design: treat warnings as errors for touched code, embrace SOLID/KISS/DRY/YAGNI, prefer composition and Law of Demeter.
- Dependencies: add only stable, compatible releases; document justification for new transitive risk.
- Verification: rely on primary sources/specs; choose safer path when guidance conflicts and record it.
- API/versioning: public APIs follow SemVer; update OpenAPI and migration notes on breaking changes.
- Security/compliance: keep secrets out of VCS, run/heed secret scans, validate and sanitize all external input.
- Observability: emit structured logs/metrics/traces; keep runbooks current for migrations and seeds.
- Accessibility: target WCAG 2.2 AA for any UI changes; use automated checks when available.
- Licensing: respect upstream licenses and update third-party notices with SPDX identifiers as required.

## Decision Log
- Trimmed legacy root narrative and delegated specifics to scoped files per repo agentization plan.
- Incorporated Taskmaster, Cursor rule, and security guidance as global references only.
- Standardized minimal checks on `uv` commands; narrowed mypy to `src/config_loader.py` because the wider codebase is not yet type-clean.
- Documented that pytest-based suites require the containerized environment (host sandbox lacks PyPI access for build deps).
- Retired the advanced security and large-scale optimizer stacks; the migration runs as a single-operator tool authenticated by API tokens.
- Rehearsal runs are expected: document snapshots, dry-runs, and state resets for operators.
- Supported/tested stack is Jira Server 9.x and OpenProject 16.x; other versions are best effort only.
- Optional subsystems (dashboard, automated testing suite, integration framework, comprehensive logging) are “opt-in”; operators can skip them without impacting the core migration CLI.
- Rails metadata writes (`migration.enable_rails_meta_writes`) stay enabled by default to preserve author/timestamp/audit history; only disable when explicitly testing without Rails access.
- Added `scripts/data_qa.py` for post-run sanity checks (counts, attachments) based on cached artefacts.
- User migration now writes J2O origin custom fields/timezone metadata for accounts; group migration synchronizes Jira groups and role-based memberships into OpenProject.
- Removed the legacy "Jira user key" and "Tempo Account" user custom fields during provenance updates so J2O_* fields stay the single source of origin metadata.
- Synced Jira user locale → OpenProject language preferences and enabled optional avatar backfills via the Avatars module (local uploads).
- Containerised test workflows land via `make container-test` (unit subset) and `make container-test-integration` (integration markers, tolerant of all-skipped suites); GitHub Actions mirrors the unit target.
- Introduced `scripts/run_rehearsal.py` to orchestrate mock-stack rehearsals (local or container) and collect artefacts under `var/rehearsal/<timestamp>`.
- Project migration now assigns Jira project leads as OpenProject members (Project admin role), records the lead in a project attribute, and ensures core modules (`work_package_tracking`, `wiki`, `time_tracking`, `costs`) stay enabled.
- Work package migration maps Jira start-date custom fields into OpenProject `start_date` while preserving the original custom fields for auditing.
- Standardized Rails helper generation: compose scripts with an interpolated head section and a literal body block to avoid Python/Ruby escaping conflicts.

## Index of scoped AGENTS.md
- `src/AGENTS.md` — migrations, clients, CLI, sanitization rules
- `src/dashboard/AGENTS.md` — FastAPI dashboard, websockets, frontend assets
- `tests/AGENTS.md` — pytest suites, fixtures, markers

## When instructions conflict
- The nearest `AGENTS.md` wins. Explicit user prompts override files.
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
- Security: `cryptography`
- Tests/Dev: `pytest`, `pytest-xdist`, `pytest-asyncio`, `pytest-cov`, `black`, `isort`, `flake8`, `mypy`, `ruff`, `pre-commit`

## Git and CI

- Run `make check` locally (lint + test) before committing.
- Prefer small, focused commits with accompanying tests.
- Avoid force-pushing shared branches.

## Agent Integration Notes

- This AGENT.md is the canonical configuration for AI coding tools.
- A symlink `.cursorrules` points to this file so Cursor will read it directly.
- Subsystems may add their own `AGENT.md` files in subdirectories for more specific guidance; tools should merge them with this root config taking lower precedence.

### Developer-Internal Mode

This application is internal and developer-only. All users are engineers with full source access. When this mode is active, the assistant:

- Speaks as a peer developer while keeping guidance concise, actionable, and usable.
- May freely reference internal functions, variables, logs, file paths, and configuration, and propose direct code edits.
- Assumes a polished UI/UX with good contextual help; no need to simplify concepts for non-technical audiences (tooltips and inline docs are expected).

Boundaries and safety:
- Do not expose real secrets, API tokens, private keys, or PII; redact or mask sensitive values in examples and logs.
- Avoid copying large code sections or sensitive operational logs verbatim; cite files/lines and include minimal necessary snippets.
- Propose destructive operations or cleanup steps but require explicit confirmation before executing.

Mode keywords:
- Canonical: `developer-internal-mode`
- Aliases: `dev-exclusive-transparent` (still redacts secrets/PII), `devX`, `talk-nerdy-to-me`

Behavioral implications:
- Prefer deep technical explanations over end-user walkthroughs.
- Use precise internal references (e.g., code paths, config keys, log event names).
- When suggesting edits, align with project style, typing, linting, and test practices.

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

## Development Workflow

**Task Management**: See **AGENTS.tasks.md** for complete bd workflow documentation.

**Quick Summary**:
- ALL work tracked in `bd` (not markdown, not TODO comments)
- Create bd issue for any discovered work immediately
- Update bd during development (planning, progress, completion)
- Close bd issues with detailed completion information
- When asked "what's next?", check: `bd list --status open -p 1`

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

## Journal Migration - Critical Knowledge

**IMPORTANT**: Journal migration for work packages is complex with multiple discovered bugs. Before working on journal-related code, READ the authoritative documentation.

**Authoritative Reference**: [ADR_003: Journal Migration Complete Journey](claudedocs/ADR_003_journal_migration_complete_journey.md)

### Bug Chain Summary (Bugs #17-25)

This represents days of debugging work. Each bug fix revealed the next issue:

1. **Bug #17**: Missing `author_id` in WorkPackageJournal → PostgreSQL NOT NULL violation
   - **Fix**: Added `author_id: rec.author_id` to journal data
   - **Location**: `src/clients/openproject_client.py:2711-2737`

2. **Bug #18**: No error logging from Ruby → Silent failures
   - **Fix**: Enhanced error logging in Ruby scripts
   - **Status**: ✅ FIXED

3. **Bug #22**: Only creates operations when notes exist → 95% of history lost
   - **Fix**: Always create operations for ALL changelogs (workflow transitions, field changes)
   - **Location**: `src/migrations/work_package_migration.py:1758-1776`
   - **Verification**: `grep -n "\[BUG23\]" src/migrations/work_package_migration.py`

4. **Bug #23**: Console output suppressed → No visibility into Ruby execution
   - **Fix**: Enable console output and log with `[RUBY]` prefix
   - **Location**: `src/clients/openproject_client.py:2849-2862`
   - **Verification**: `grep -n "\[RUBY\]" src/clients/openproject_client.py`

5. **Bug #24**: Config setting ignored → Manual environment variable required
   - **Fix**: Check both env var AND config file setting
   - **Location**: `src/clients/openproject_client.py:2846-2850`
   - **Side Effect**: Introduced Bug #25

6. **Bug #25**: `self.config` doesn't exist on OpenProjectClient → CURRENT BLOCKER
   - **Symptom**: Migration reports "success" but creates ZERO work packages
   - **Error**: `'OpenProjectClient' object has no attribute 'config'`
   - **Location**: `src/clients/openproject_client.py:2849`
   - **Status**: ❌ **NOT YET FIXED - BLOCKS MIGRATION**

### Critical Patterns to Follow

**ALWAYS create operations for ALL changelogs** (Bug #22 lesson):
```python
# ✅ GOOD: Always create operation
notes = "\n".join(changelog_notes) if changelog_notes else ""
work_package["_rails_operations"].append({...})

# ❌ BAD: Conditional creation loses history
if changelog_notes:
    work_package["_rails_operations"].append({...})
```

**ALWAYS log console output** (Bug #23 lesson):
```python
# ✅ GOOD: Log all output with prefix
console_output = self.rails_client.execute(..., suppress_output=False)
if console_output:
    for line in console_output.splitlines():
        if line.strip():
            self.logger.info(f"[RUBY] {line}")
```

**ALWAYS verify attribute access patterns** (Bug #25 lesson):
```python
# ⚠️ CURRENT BUG: self.config doesn't exist
allow_runner_fallback = (
    ...
    or self.config.migration_config.get("enable_runner_fallback", False)  # Bug #25
)

# TODO: Determine correct config access pattern for OpenProjectClient
```

### OpenProject Journal Architecture

Two-table design with strict PostgreSQL constraints:

1. **journals** table: Journal metadata (user_id, notes, version, validity_period)
2. **work_package_journals** table: Work package state snapshot (author_id REQUIRED)

**Critical Constraints**:
- `work_package_journals.author_id`: NOT NULL (Bug #17)
- `journals.version`: UNIQUE per (journable_id, journable_type)
- `journals.validity_period`: TSTZRANGE with exclusion constraint preventing overlaps
- `journals.data_type`: NOT NULL polymorphic association field

### Test Strategy

**Always test with NRS-182** (23 journals) to catch bugs early.

Test issues: NRS-171, NRS-182, NRS-191, NRS-198, NRS-204, NRS-42, NRS-59, NRS-66, NRS-982, NRS-4003

### Related Documentation

- [ADR_003: Complete Journey](claudedocs/ADR_003_journal_migration_complete_journey.md) - **READ THIS FIRST**
- [ADR_001: Initial Discovery](claudedocs/ADR_001_openproject_journal_creation.md) - Bugs #17, #18
- [ADR_002: Three Fixes](claudedocs/ADR_002_journal_migration_three_bug_fixes.md) - Bugs #22, #23, #24

## Rails Console & OpenProject Integration Rules

### REST API is NOT Suitable for Bulk Migration

**CRITICAL RULE**: The OpenProject REST API is **NOT suitable** for the j2o bulk migration use case. We **MUST use Rails console via ActiveRecord** for all data import operations.

**This is not negotiable** - it is an architectural requirement based on technical constraints:

- **Performance**: File-based batch operations required to avoid timeouts and memory issues
- **Transactions**: ActiveRecord transaction support not available via REST API
- **Validation Control**: Ability to bypass validations for migration scenarios not available via REST API
- **Bulk Operations**: REST API cannot handle thousands of records efficiently
- **Capability**: Direct ActiveRecord access required for features not exposed through REST API

**Stop recurring REST API discussions**: Any suggestion to use REST API for bulk operations should be immediately rejected with reference to this rule.

**Evidence**: See [ADR 2025-10-20: Rails Console Requirement](docs/decisions/2025-10-20-rails-console-requirement.md) for comprehensive technical justification.

### Tmux Session is REQUIRED for Rails Console

**CRITICAL RULE**: A **persistent tmux session is REQUIRED** for Rails console operations. This is an architectural requirement, not optional.

**Performance impact**: Persistent tmux session is 50-100x faster than one-off sessions or rails runner.

**Pre-migration requirements** (operators MUST complete before migration):
1. Install tmux on migration host (if not present)
2. Install IRB configuration: `make install-irbrc`
3. Start Rails console session: `make start-rails`
4. Verify session exists: `tmux list-sessions | grep rails_console`

**Failure to establish tmux session** will result in:
```
TmuxSessionError: tmux session 'rails_console' does not exist
```

**No alternatives**: One-off sessions or rails runner-only approaches result in 50-100x performance degradation and are **not architecturally supported**.

**Evidence**: See [ADR 2025-10-20: Tmux Session Requirement](docs/decisions/2025-10-20-tmux-session-requirement.md) for detailed justification.

## Idempotency Requirements for All Migration Components

**CRITICAL RULE**: Idempotency is **MANDATORY** for all migration components that create or modify OpenProject entities. This is an architectural requirement, not optional.

**Authoritative Source**: Provenance metadata (custom fields + description markers) is the **single source of truth** for entity relationships. Mapping files (`var/data/*_mapping.json`) are **CACHE ONLY** and can be deleted and rebuilt at any time.

**Architecture violation**: Any migration component that blocks execution due to missing mapping files is **violating this architectural principle** and must be fixed to query provenance metadata instead.

### Provenance Metadata System

All migrated OpenProject entities **MUST** include provenance custom fields:

- `J2O Origin System` (e.g., "jira")
- `J2O Origin ID` (e.g., "10523")
- `J2O Origin Key` (e.g., "SRVAC-42")
- `J2O Origin URL` (e.g., "https://jira.example.com/browse/SRVAC-42")

**Backup provenance**: HTML comment markers in description fields (`<!-- J2O_ORIGIN_START -->...`) provide fallback when custom fields are unavailable.

### Idempotent Migration Pattern

```python
def migrate_entity(jira_entity: dict) -> dict:
    """Idempotent migration pattern using provenance metadata."""

    # 1. Check if entity already exists via provenance metadata
    existing = find_by_provenance(
        origin_system="jira",
        origin_id=jira_entity["id"],
        origin_key=jira_entity["key"],
    )

    if existing:
        logger.info(f"Entity {jira_entity['key']} already exists")
        return existing

    # 2. Create new entity with provenance metadata
    op_entity["custom_fields"] = [
        {"id": cf_id("J2O Origin System"), "value": "jira"},
        {"id": cf_id("J2O Origin ID"), "value": str(jira_entity["id"])},
        {"id": cf_id("J2O Origin Key"), "value": jira_entity["key"]},
    ]

    created = create_entity(op_entity)

    # 3. Update mapping cache for performance (optional)
    update_mapping_cache(jira_entity["key"], created["id"])

    return created
```

### Mapping Files are Cache Only

**Never** block migration on missing mapping files. Instead:

1. Query OpenProject for entities with provenance metadata
2. Build mapping dynamically from provenance custom fields
3. Cache mapping to disk for performance (optional)

**Good example**: `user_migration.py:206` - queries by J2O provenance custom fields
**Bad example**: Blocking on missing `custom_field_mapping.json` file

### Validation Criteria

A migration component satisfies idempotency requirements if:

1. ✅ Running migration twice creates no duplicates
2. ✅ Deleting mapping cache and re-running succeeds
3. ✅ Entity relationships preserved after mapping rebuild
4. ✅ Provenance metadata present on all migrated entities
5. ✅ Component documents transformation-only status (if applicable)

### Transformation-Only Components

Some components operate on already-migrated data and explicitly raise `ValueError` in `_get_current_entities_for_type()` to document this design choice. This is acceptable for components like:
- `versions`, `components`, `labels` (operate on work package mapping)
- `attachments` (operates on work package data)

**Evidence**: See [ADR 2025-10-20: Idempotency Requirement](docs/decisions/2025-10-20-idempotency-requirement.md) for comprehensive technical details.

## Rails Console IRB Configuration

To stabilize the tmux-backed Rails console session used by `RailsConsoleClient`, install an `.irbrc` into the OpenProject container. This disables multiline and relines interactive features which can break non-interactive execution flows:

- Source file: `contrib/openproject.irbrc`
- Install command: `make install-irbrc`
- Destination in container: `/app/.irbrc`

This uses `J2O_OPENPROJECT_SERVER`, `J2O_OPENPROJECT_USER`, and `J2O_OPENPROJECT_CONTAINER` from environment to perform an SSH → docker cp transfer.

### Rails Console tmux Session

Create a local tmux session that runs a remote Rails console inside the OpenProject container with IRB stabilized via `/app/.irbrc`:

- Start and attach: `make start-rails ATTACH=true`
- Start only: `make start-rails`
- Attach later: `make attach-rails`

Under the hood this mirrors:

```bash
tmux new-session -s rails_console \; \
  pipe-pane -o 'cat >>~/tmux.log' \; \
  send-keys 'ssh -t $J2O_OPENPROJECT_USER@$J2O_OPENPROJECT_SERVER "docker exec -e IRBRC=/app/.irbrc -e RELINE_OUTPUT_ESCAPES=false -e RELINE_INPUTRC=/dev/null -ti $J2O_OPENPROJECT_CONTAINER bundle exec rails console"' C-m
```

- Load order: CLI args → env vars → `.env.local` → `.env` → `config/config.yaml`. Access configuration via `src/config.py` helper.
- Keep secrets out of VCS. Use `.env.local` or Docker secrets in production. See `docs/configuration.md` and `.env.example`.

### Rails Console Script Handling Policy

- Minimal Ruby scripts: only load JSON, instantiate ActiveRecord models, assign attributes, save. Do not implement mapping, sanitation, branching logic, or result analysis in Ruby. Keep the script small, deterministic, and side-effect free beyond record creation.
- JSON must be fully compliant before invoking Rails:
  - Sanitize exclusively in Python. Remove non-AR keys like `_links`, `watcher_ids`, or any OpenProject API-style link structures. Extract and flatten IDs in Python (e.g., from `_links.type.href`).
  - Ensure required AR attributes are present (e.g., `project_id`, `type_id`, `subject`) prior to writing JSON.
- Output and debugging handling:
  - Use a single file-based result flow. The Ruby script writes a results JSON file in the container; the migration copies it to `var/data` and stores a timestamped copy. Prefer this file as the sole authoritative output path. Avoid parallel “direct return value” branches.
  - Capture and persist raw console stdout/stderr for each run under `var/data` (or `var/logs`) for postmortem analysis.
  - When unexpected AR errors arise, temporarily log `sanitized_attrs.keys` for the first failing item to pinpoint offending attributes, then remove once resolved.
- Testing requirements:
  - Add tests that assert generated `work_packages_*.json` contain no `_links` (payload cleanliness). Keep these tests fast and independent of Rails.
  - Retain unit tests for Python sanitization helpers (e.g., `_sanitize_wp_dict`), ensuring extraction of IDs and removal of `_links`.
- Simplicity principle: if the Ruby script grows beyond minimal load-and-save responsibilities, refactor logic back into Python. Prefer analyzing and classifying errors in Python, not in Ruby.
