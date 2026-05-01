"""Explicit, idempotent bootstrap for the migration config side effects.

Phase 6a of ADR-002 makes :mod:`src.config` import-time side-effect-free.
The directory creation, log file handler attachment, and per-run log file
pruning that previously ran at module import are gathered here behind a
single :func:`bootstrap` call invoked from CLI entry points.

Calling ``bootstrap()`` is idempotent: subsequent invocations are no-ops.
The keyword-only flags allow tests and special entry points to skip
individual side effects.

This module is intentionally separate from :mod:`src.config` so that
``from src.config import Settings`` (and friends) does not trigger
filesystem I/O — which would otherwise reproduce the import-time
``PermissionError`` failure that affected PR #155 under container UID
mismatches.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from src.display import configure_logging

if TYPE_CHECKING:
    from src.display import ExtendedLogger

# Module-level idempotency flag. Once bootstrap has run, repeat calls are
# no-ops regardless of which side effects they request.
_BOOTSTRAPPED: bool = False


def _ensure_var_dirs() -> list[str]:
    """Create all ``var/`` subdirectories and return human-readable status lines.

    Returns:
        A list of strings describing whether each directory was created or
        already existed. The caller logs them at ``DEBUG`` level once the
        logger is configured.

    """
    # Local import to avoid a circular dependency with ``src.config`` which
    # owns the ``var_dirs`` mapping. ``src.config`` may import this module
    # eagerly in the future for type hints; routing the lookup through a
    # local import keeps the import graph one-directional today.
    from src.config import var_dirs

    messages: list[str] = []
    for dir_path in var_dirs.values():
        dir_existed = dir_path.exists()
        dir_path.mkdir(parents=True, exist_ok=True)
        if dir_existed:
            messages.append(f"Using existing directory: {dir_path}")
        else:
            messages.append(f"Created directory: {dir_path}")
    return messages


def _configure_logging_with_file_handler() -> ExtendedLogger:
    """Configure the rich logger and attach the aggregate ``migration.log`` handler.

    Mirrors the pre-Phase-6a behaviour: rich handler + a single aggregate
    log file in ``var/logs/migration.log``.
    """
    from src.config import migration_config, var_dirs

    log_level = migration_config.get("log_level", "DEBUG")
    latest_log_file = var_dirs["logs"] / "migration.log"
    return configure_logging(log_level, latest_log_file)


def _attach_per_run_log_handler(logger: ExtendedLogger) -> None:
    """Attach a per-run timestamped log handler to the root logger.

    A per-run log file ``var/logs/migration_<timestamp>.log`` makes each
    run's output easy to inspect in isolation. Failures are swallowed —
    they must never abort startup.
    """
    from src.config import migration_config, var_dirs

    log_level = migration_config.get("log_level", "DEBUG")
    try:
        timestamp = datetime.now(tz=UTC).strftime("%Y-%m-%d_%H-%M-%S")
        per_run_log_file = var_dirs["logs"] / f"migration_{timestamp}.log"

        file_formatter = logging.Formatter(
            "%(asctime)s.%(msecs)03d - %(name)s - %(levelname)s - %(message)s",
        )
        file_handler = logging.FileHandler(per_run_log_file)
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(getattr(logging, str(log_level).upper(), logging.INFO))
        logging.getLogger().addHandler(file_handler)
        logger.info("Per-run log file: %s", per_run_log_file)
    except OSError:
        logger.exception("Failed to attach per-run log handler")


def _prune_old_log_files(logger: ExtendedLogger) -> None:
    """Keep only the most recent N per-run log files, configurable via ``log_retention_count``."""
    from src.config import migration_config, var_dirs

    try:
        retention_count = int(migration_config.get("log_retention_count", 20))
    except (TypeError, ValueError) as _exc:
        # ``log_retention_count`` is malformed; fall back to a safe default
        # rather than aborting startup. Workaround for a ruff-format bug
        # that drops the parens around the exception tuple when no
        # ``as`` binding is present (ruff 0.15.12).
        del _exc
        retention_count = 20

    if retention_count <= 0:
        return

    per_run_logs = sorted(var_dirs["logs"].glob("migration_*.log"))
    if len(per_run_logs) <= retention_count:
        return

    to_delete = per_run_logs[: len(per_run_logs) - retention_count]
    pruned = 0
    for old_log in to_delete:
        try:
            old_log.unlink()
            pruned += 1
        except OSError:
            logger.debug("Failed to remove old per-run log: %s", old_log)
    if pruned:
        logger.info(
            "Per-run log rotation: kept %d, pruned %d",
            retention_count,
            pruned,
        )


def bootstrap(
    *,
    mkdir: bool = True,
    configure_log: bool = True,
    prune_logs: bool = True,
) -> None:
    """Initialize ``var/`` directories, logging, and log file management.

    Idempotent — the first call performs the requested side effects and
    flips a module-level flag; subsequent calls return immediately. Tests
    that need to bootstrap multiple times should reset the flag explicitly
    via :func:`reset_bootstrap_state`.

    Args:
        mkdir: Create the ``var/`` directory tree (``var/data``, ``var/logs``,
            ``var/run`` etc.). Set ``False`` in tests that mount their own
            tmp tree.
        configure_log: Attach the rich console handler and the aggregate
            ``migration.log`` file handler. Set ``False`` in tests that
            assert on import-time logger state or use ``caplog``.
        prune_logs: Run the per-run log retention rotation. Set ``False``
            when you want to inspect every historical log file.

    """
    global _BOOTSTRAPPED  # noqa: PLW0603
    if _BOOTSTRAPPED:
        return

    created_messages: list[str] = []
    if mkdir:
        created_messages = _ensure_var_dirs()

    logger: ExtendedLogger | None = None
    if configure_log:
        logger = _configure_logging_with_file_handler()
        _attach_per_run_log_handler(logger)
        for message in created_messages:
            logger.debug(message)
        if prune_logs:
            _prune_old_log_files(logger)
    elif prune_logs:
        # No logger to log against; fall back to the stdlib root logger.
        # This branch is mainly exercised by tests with ``configure_log=False``
        # but ``prune_logs=True`` — production paths set both flags.
        from typing import cast

        fallback_logger = cast("ExtendedLogger", logging.getLogger("migration"))
        _prune_old_log_files(fallback_logger)

    _BOOTSTRAPPED = True


def reset_bootstrap_state() -> None:
    """Reset the idempotency flag. Test-only helper.

    Production code never calls this. Tests use it to verify that
    :func:`bootstrap` is in fact idempotent and to exercise the various
    flag combinations from a clean state.
    """
    global _BOOTSTRAPPED  # noqa: PLW0603
    _BOOTSTRAPPED = False


def is_bootstrapped() -> bool:
    """Return ``True`` if :func:`bootstrap` has run successfully at least once."""
    return _BOOTSTRAPPED
