<!-- Managed by agent: keep sections and order; edit content, not structure. Last updated: 2026-02-09 -->
# AGENTS.md — scripts

## Overview
- 60+ operational helpers for the migration toolchain: tmux bootstrap (`start_rails_tmux.py`), rehearsal orchestration (`run_rehearsal.py`), migration shells (`migrate-*.sh`), data QA, and ad-hoc test/validation scripts.
- Scripts assume the canonical environment variables (`J2O_*`) and work alongside the Docker/compose stack.
- Outputs (logs, artefacts) land under `var/` so they can be archived or inspected after runs.

## Setup & environment
- Ensure Python 3.14+ and `uv` are available; install deps with `uv sync --frozen` before invoking Python scripts.
- `.env` is mandatory, `.env.local` provides developer overrides (e.g., `J2O_OPENPROJECT_SERVER`, `J2O_OPENPROJECT_CONTAINER`, `J2O_OPENPROJECT_TMUX_SESSION_NAME`).
- `start_rails_tmux.py` requires `tmux` and SSH access to the OpenProject host; ensure your agent socket is forwarded (`SSH_AUTH_SOCK`).
- Bash utilities (`scripts/migrate-*.sh`) expect POSIX shell, GNU coreutils, and Docker CLI to be present.

## Key scripts
| Script | Purpose |
|--------|---------|
| `start_rails_tmux.py` | Bootstrap persistent tmux session to remote Rails console |
| `run_rehearsal.py` | Orchestrate full/partial migration rehearsals with artefact collection |
| `data_qa.py` | Validate migration quality, check start-date coverage per project |
| `migrate-stop.sh` | Stop all local/remote migration processes |
| `migrate-start-fast-forward.sh` | Start migration with fast-forward and larger TE batches |
| `migrate-status.sh` | Show current migration status |
| `cache_jira_issues.py` | Cache Jira issue data locally for offline processing |
| `cache_jira_metadata.py` | Cache Jira metadata (fields, statuses, etc.) |
| `bulk_update_wp_metadata.py` | Bulk update work package metadata in OpenProject |
| `migration_quality_analysis.py` | Analyze migration quality across projects |
| `cleanup_var.py` | Clean var/ directory artifacts |

## Build & tests (prefer file-scoped)
- Python lint/format check: `python -m compileall scripts`
- Static lint: `uv run --no-cache ruff check scripts`
- Rails console bootstrap smoke: `python scripts/start_rails_tmux.py --attach`
- Rehearsal dry-run: `python scripts/run_rehearsal.py --components users --reset-wp-checkpoints`
- Full container rehearsal: `python scripts/run_rehearsal.py --use-container --collect --stop`

## Code style & conventions
- Python scripts should use `subprocess.run(..., check=True)` and log meaningful errors before exiting.
- Keep env lookups (`os.environ.get`) centralized near the top; fail fast with actionable messages when configuration is incomplete.
- Shell scripts should be POSIX-compliant, `set -euo pipefail`, and echo commands when they mutate infrastructure.
- Route temporary files and logs through `var/` or `$TMPDIR` (never inline /tmp names without cleanup).

## Security & safety
- Never hardcode credentials; rely on `.env`/`.env.local` and SSH agent forwarding.
- `start_rails_tmux.py` writes logs to `~/rails_console.tmux.log` by default—treat it as sensitive (contains console output).
- Review shell scripts for destructive commands (`docker exec rm`, etc.) and keep them scoped to the OpenProject container specified by env vars.

## PR/commit checklist
- Run `python -m compileall scripts` and `uv run --no-cache ruff check scripts` after editing Python helpers.
- For shell changes, run `shellcheck scripts/migrate-*.sh` when available.
- Update `docs/DEVELOPER_GUIDE.md` or AGENTS.md references if CLI flags, usage, or outputs change.
- Verify `python scripts/run_rehearsal.py --use-container --collect --stop` completes when adding new rehearsal steps.

## Good vs. bad examples
- Good: `scripts/start_rails_tmux.py` — validates env, sets up tmux logging, and surfaces actionable errors.
- Good: `scripts/run_rehearsal.py` — orchestrates compose services, migration components, and artefact collection with clear flags.
- Caution: legacy ad-hoc bash snippets that ssh directly without env abstraction; migrate them to the structured helpers above when touched.

## When stuck
- Re-read `docs/DEVELOPER_GUIDE.md` and the root Makefile for canonical targets (`make start-rails`, `make container-test`).
- Check `scripts/run_rehearsal.py --help` for the latest options; extend there before introducing new orchestration scripts.
- Confirm tmux session availability via `tmux ls` and ensure SSH keys are loaded (`ssh-add -l`) before debugging console connectivity.

## Decision Log
- Scripts now include `run_rehearsal.py`, replacing ad-hoc compose commands and recording artefacts in `var/rehearsal/<timestamp>`.
- Documented tmux/SSH expectations so `start_rails_tmux.py` and migration clients share the same configuration surface.
- Many test/validation scripts in this directory are one-off operational tools; prefer extending `run_rehearsal.py` over creating new orchestration scripts.
