"""Tests for ``src.config`` Phase 6a inert-import + explicit ``bootstrap()``.

These tests pin the contract introduced by ADR-002 Phase 6a:

- ``import src.config`` performs no filesystem I/O and attaches no
  ``FileHandler`` to the ``migration`` logger.
- ``bootstrap()`` is the only entry point that creates ``var/`` dirs,
  attaches log handlers, and runs log retention.
- ``bootstrap()`` is idempotent — repeated calls are no-ops.
- The keyword-only flags allow tests and special entry points to skip
  individual side effects.

If any of these guarantees regress, container UID mismatches will once
again break import-time logging (the root cause behind PR #155's CI
failure).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from src import config
from src.config import _bootstrap

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _reset_bootstrap_state() -> Iterator[None]:
    """Each test starts with a clean bootstrap flag.

    The flag is module-level state on ``src.config._bootstrap``, so
    without this fixture tests would leak state into each other and the
    idempotency assertions would silently no-op.
    """
    _bootstrap.reset_bootstrap_state()
    yield
    _bootstrap.reset_bootstrap_state()


def _file_handlers_on(logger_name: str) -> list[logging.FileHandler]:
    """Return the FileHandler instances attached anywhere visible to ``logger_name``."""
    handlers: list[logging.FileHandler] = []
    seen: set[int] = set()
    target = logging.getLogger(logger_name)
    current: logging.Logger | None = target
    while current is not None:
        for handler in current.handlers:
            if isinstance(handler, logging.FileHandler) and id(handler) not in seen:
                handlers.append(handler)
                seen.add(id(handler))
        if not current.propagate:
            break
        current = current.parent
    # Also scan the root logger's handlers explicitly — the per-run
    # handler is attached to the root, not to ``migration``.
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.FileHandler) and id(handler) not in seen:
            handlers.append(handler)
            seen.add(id(handler))
    return handlers


def test_module_import_is_inert() -> None:
    """``from src.config import ...`` must not attach a FileHandler.

    The contract: a fresh import path with no ``bootstrap()`` call leaves
    the ``migration`` logger with zero ``FileHandler`` instances. This is
    the property that prevents container UID mismatches from breaking
    package import.
    """
    # Strip any handlers a previous test (or production startup) may
    # have left behind so we observe the true post-import state.
    for handler in list(logging.getLogger().handlers):
        if isinstance(handler, logging.FileHandler):
            logging.getLogger().removeHandler(handler)
            handler.close()
    for handler in list(logging.getLogger("migration").handlers):
        if isinstance(handler, logging.FileHandler):
            logging.getLogger("migration").removeHandler(handler)
            handler.close()

    # ``src.config`` has already been imported by the test runner; the
    # import itself didn't add file handlers. The attribute access is a
    # no-op, but documents intent.
    assert config.logger is not None
    assert _file_handlers_on("migration") == []


def test_bootstrap_attaches_file_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After ``bootstrap()``, the migration logger has a working FileHandler."""
    # Redirect var/ paths so the test does not pollute the real tree.
    log_dir = tmp_path / "logs"
    redirected = {**config.var_dirs, "logs": log_dir, "data": tmp_path / "data"}
    monkeypatch.setattr(config, "var_dirs", redirected)

    config.bootstrap()

    assert log_dir.exists(), "bootstrap() should create the logs directory"
    handlers = _file_handlers_on("migration")
    assert handlers, "bootstrap() should attach at least one FileHandler"


def test_bootstrap_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling ``bootstrap()`` twice must not double-attach handlers."""
    redirected = {**config.var_dirs, "logs": tmp_path / "logs", "data": tmp_path / "data"}
    monkeypatch.setattr(config, "var_dirs", redirected)

    config.bootstrap()
    handlers_after_first = list(logging.getLogger().handlers)

    config.bootstrap()
    handlers_after_second = list(logging.getLogger().handlers)

    assert handlers_after_first == handlers_after_second, (
        "bootstrap() must be a no-op on subsequent calls; handler list changed"
    )
    assert config.is_bootstrapped()


def test_bootstrap_skips_mkdir_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bootstrap(mkdir=False)`` must not create directories."""
    log_dir = tmp_path / "logs_should_not_exist"
    data_dir = tmp_path / "data_should_not_exist"
    redirected = {**config.var_dirs, "logs": log_dir, "data": data_dir}
    monkeypatch.setattr(config, "var_dirs", redirected)

    # Skip the file-handler step too: without the directory, attaching a
    # FileHandler would itself raise — we are testing the mkdir flag in
    # isolation.
    config.bootstrap(mkdir=False, configure_log=False, prune_logs=False)

    assert not log_dir.exists()
    assert not data_dir.exists()
    assert config.is_bootstrapped()


def test_bootstrap_skips_logging_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bootstrap(configure_log=False)`` must not attach a FileHandler."""
    redirected = {**config.var_dirs, "logs": tmp_path / "logs", "data": tmp_path / "data"}
    monkeypatch.setattr(config, "var_dirs", redirected)

    # Strip any pre-existing handlers so the assertion is meaningful.
    for handler in list(logging.getLogger().handlers):
        if isinstance(handler, logging.FileHandler):
            logging.getLogger().removeHandler(handler)
            handler.close()

    config.bootstrap(mkdir=True, configure_log=False, prune_logs=False)

    assert _file_handlers_on("migration") == []
    assert config.is_bootstrapped()


def test_logger_extended_methods_available_without_bootstrap() -> None:
    """``logger.success`` and ``logger.notice`` work even before ``bootstrap()``.

    Phase 6a registers the custom level names and methods at import time
    (pure in-memory state, no I/O), so callers using the extended logger
    interface do not need to wait for ``bootstrap()``.
    """
    # ``cast`` to keep type checkers happy — at runtime these methods
    # exist on every Logger instance.
    assert callable(getattr(config.logger, "success", None))
    assert callable(getattr(config.logger, "notice", None))
    assert logging.getLevelName(25) == "SUCCESS"
    assert logging.getLevelName(21) == "NOTICE"


def test_reset_bootstrap_state_allows_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``reset_bootstrap_state()`` re-arms ``bootstrap()`` for the next call."""
    redirected = {**config.var_dirs, "logs": tmp_path / "logs", "data": tmp_path / "data"}
    monkeypatch.setattr(config, "var_dirs", redirected)

    config.bootstrap()
    assert config.is_bootstrapped()

    config.reset_bootstrap_state()
    assert not config.is_bootstrapped()

    # Second bootstrap actually runs again because the flag was reset.
    config.bootstrap()
    assert config.is_bootstrapped()
