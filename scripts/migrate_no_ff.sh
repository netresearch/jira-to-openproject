#!/usr/bin/env bash
# Run the work-package migration directly through Rails runner.
set -euo pipefail
export J2O_SCRIPT_LOAD_MODE=runner J2O_FORCE_RAILS_RUNNER=1 J2O_FAST_FORWARD=0
exec uv run --frozen --no-build --no-install-project python -m src.main migrate --components work_packages --no-confirm --no-backup "$@"
