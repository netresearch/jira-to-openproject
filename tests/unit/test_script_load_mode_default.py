"""Default ``J2O_SCRIPT_LOAD_MODE`` should be ``console``.

The runner mode (``bundle exec rails runner``) costs ~5-10 s of Rails boot
per bulk batch. The console mode loads the same generated script via the
already-warm tmux Rails console (``load '/tmp/...rb'``) and is ~5x faster
end-to-end on the user phase.

This was confirmed live on the NRS migration on 2026-05-04: with
``J2O_SCRIPT_LOAD_MODE=console`` set in ``.env.local``, user-batch
throughput went from ~40 s/batch to ~7 s/batch. Making console the default
means future runs benefit without per-developer env tweaks.
"""

from __future__ import annotations

from src.infrastructure.openproject.openproject_bulk_create_service import (
    DEFAULT_SCRIPT_LOAD_MODE,
)


def test_default_script_load_mode_is_console() -> None:
    assert DEFAULT_SCRIPT_LOAD_MODE == "console"
