#!/bin/bash
# Create dummy tmux for initialization, then use runner mode for execution
tmux kill-session -t rails_console 2>/dev/null || true
tmux new-session -d -s rails_console "sleep 86400" 2>/dev/null || true
sleep 1

# Use Rails runner mode for ALL command execution (bypasses tmux communication)
export J2O_SCRIPT_LOAD_MODE=runner
export J2O_FAST_FORWARD=0
rm -f var/run/j2o_migrate.pid
uv run --no-cache python -m src.main migrate --components work_packages --no-confirm --no-backup
