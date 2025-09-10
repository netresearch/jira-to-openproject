#!/usr/bin/env bash
set -euo pipefail

# Stop all local and remote (if configured) migration processes and watchers.

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
cd "$repo_root"

LOG_FILE="var/logs/nrs_migration_console_latest.log"
PID_FILE_APP="var/logs/nrs_migration.pid"
PID_FILE_LOCK="var/run/j2o_migrate.pid"

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$PID_FILE_LOCK")"

echo "[migrate-stop] Killing local migration processes..." >&2
# Match common invocations
patterns=(
  "python[0-9.]* .* -m src\\.main migrate"
  "uv run python .* -m src\\.main migrate"
  "\\.venv/bin/python3 -m src\\.main migrate"
  "j2o .* migrate"
)

for pat in "${patterns[@]}"; do
  pkill -f -TERM "$pat" 2>/dev/null || true
done
sleep 0.5
for pat in "${patterns[@]}"; do
  pkill -f -KILL "$pat" 2>/dev/null || true
done

echo "[migrate-stop] Killing local watchers (tail/sed/ssh/tmux)..." >&2
watchers=(
  "tail -F $LOG_FILE"
  "tail -n +1 -f $LOG_FILE"
  "sed -n 1,160p"
  "ssh sobol\\.nr"
  "tmux new-session -s rails_console"
)
for w in "${watchers[@]}"; do
  pkill -f -KILL "$w" 2>/dev/null || true
done

echo "[migrate-stop] Clearing PID files..." >&2
rm -f "$PID_FILE_APP" "$PID_FILE_LOCK" || true

# Load env if present for remote cleanup
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ -n "${J2O_OPENPROJECT_SERVER:-}" && -n "${J2O_OPENPROJECT_USER:-}" ]]; then
  if [[ -n "${J2O_OPENPROJECT_CONTAINER:-}" ]]; then
    echo "[migrate-stop] Killing remote rails runner processes in container '$J2O_OPENPROJECT_CONTAINER'..." >&2
    ssh -o BatchMode=yes -o ConnectTimeout=6 "${J2O_OPENPROJECT_USER}@${J2O_OPENPROJECT_SERVER}" \
      "C=\"${J2O_OPENPROJECT_CONTAINER}\"; if docker ps --format '{{.Names}}' | grep -q '^'\"\$C\"'$'; then \
         docker exec -i \$C sh -lc \"pkill -f -9 'rails runner .*j2o_runner_' || true; pkill -f -9 'j2o_runner_.*\\.rb' || true\"; \
       fi" || true
  fi
  if [[ -n "${J2O_OPENPROJECT_TMUX_SESSION_NAME:-}" ]]; then
    echo "[migrate-stop] Killing remote tmux session '$J2O_OPENPROJECT_TMUX_SESSION_NAME'..." >&2
    ssh -o BatchMode=yes -o ConnectTimeout=6 "${J2O_OPENPROJECT_USER}@${J2O_OPENPROJECT_SERVER}" \
      "tmux kill-session -t ${J2O_OPENPROJECT_TMUX_SESSION_NAME} 2>/dev/null || true" || true
  fi
fi

echo "[migrate-stop] Done." >&2

