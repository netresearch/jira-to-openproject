#!/usr/bin/env bash
# Run the work-package migration directly through Rails runner.
set -euo pipefail
export J2O_SCRIPT_LOAD_MODE=runner J2O_FORCE_RAILS_RUNNER=1 J2O_FAST_FORWARD=0
# Use the installed CLI in Compose, an activated environment, or the project venv.
cli=j2o
if [[ -x .venv/bin/j2o ]]; then cli=.venv/bin/j2o; fi
exec "$cli" migrate --components work_packages --no-confirm --no-backup "$@"
