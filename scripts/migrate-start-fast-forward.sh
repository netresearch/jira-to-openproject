#!/usr/bin/env bash
set -euo pipefail

# Start NRS migration with fast-forward enabled and larger TE batch size.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
cd "$repo_root"

mkdir -p var/logs var/run

: "${JIRA_PROJECT_FILTER:=NRS}"

export J2O_SCRIPT_LOAD_MODE=console
export J2O_SCRIPT_RUNNER_MAX_LINES=${J2O_SCRIPT_RUNNER_MAX_LINES:-10}
export J2O_SCRIPT_RUNNER_THRESHOLD=${J2O_SCRIPT_RUNNER_THRESHOLD:-200}
export J2O_BULK_RESULT_WAIT_SECONDS=${J2O_BULK_RESULT_WAIT_SECONDS:-240}
export J2O_QUERY_RESULT_WAIT_SECONDS=${J2O_QUERY_RESULT_WAIT_SECONDS:-120}
export J2O_FAST_FORWARD=${J2O_FAST_FORWARD:-1}
export J2O_TIME_ENTRY_BATCH_SIZE=${J2O_TIME_ENTRY_BATCH_SIZE:-200}

echo "[migrate-start] Starting migration for project ${JIRA_PROJECT_FILTER} (fast-forward=${J2O_FAST_FORWARD})" >&2

nohup bash -lc \
  "uv run python -m src.main migrate --jira-project-filter ${JIRA_PROJECT_FILTER} --no-backup --no-confirm" \
  > var/logs/nrs_migration_console_latest.log 2>&1 &
PID=$!
echo "$PID" > var/logs/nrs_migration.pid
echo "[migrate-start] PID=${PID}" >&2

exit 0

