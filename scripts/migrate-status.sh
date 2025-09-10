#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
cd "$repo_root"

LOG_FILE="var/logs/nrs_migration_console_latest.log"
PID_FILE="var/logs/nrs_migration.pid"

echo "[migrate-status] PID file:"
if [[ -f "$PID_FILE" ]]; then
  pid=$(cat "$PID_FILE")
  echo "  $PID_FILE => $pid"
  echo "[migrate-status] Process:"
  ps -p "$pid" -o pid,ppid,pgid,etime,cmd= || true
else
  echo "  (missing)"
fi

echo "[migrate-status] Recent markers:"
if [[ -f "$LOG_FILE" ]]; then
  egrep -n "RUNNING COMPONENT|SUCCESS  Component|FAILED  Component" "$LOG_FILE" | tail -n 20 || true
else
  echo "  (no log)"
fi

echo "[migrate-status] Provenance tails:"
if [[ -f "$LOG_FILE" ]]; then
  egrep -n "# j2o:" "$LOG_FILE" | tail -n 10 || true
fi

exit 0

