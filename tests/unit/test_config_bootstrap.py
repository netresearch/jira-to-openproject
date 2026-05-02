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

import importlib
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from src import config, display
from src.config import _bootstrap

if TYPE_CHECKING:
    from collections.abc import Iterator


def _close_all_file_handlers() -> None:
    """Detach and close every FileHandler on root and the ``migration`` logger.

    Required between tests: each ``bootstrap()`` call attaches a handler
    that targets a tmp_path FileHandler. Without active cleanup later
    tests can inherit handlers pointing at previous tests' tmp paths,
    making the suite order-dependent / flaky and accumulating per-run
    handlers.
    """
    for logger in (logging.getLogger(), logging.getLogger("migration")):
        for handler in list(logger.handlers):
            if isinstance(handler, logging.FileHandler):
                logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass


def _reset_logging_guard() -> None:
    """Clear ``src.display``'s 'already configured' cache so reconfig works.

    ``configure_logging`` short-circuits when ``_LOGGING_CONFIGURED`` is
    set, so a test that mutated logging state needs to clear the guard
    before the next ``bootstrap()`` reconfigures cleanly.
    """
    display._LOGGING_CONFIGURED = False
    display._LOGGER = None
    display._LOG_LEVEL_NUM = None  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _reset_bootstrap_state() -> Iterator[None]:
    """Each test starts and ends with a clean bootstrap + logging state.

    The flag is module-level state on ``src.config._bootstrap``, so
    without this fixture tests would leak state into each other and the
    idempotency assertions would silently no-op. The companion logging
    cleanup ensures FileHandlers attached to tmp_path don't outlive the
    test that created them.
    """
    _bootstrap.reset_bootstrap_state()
    _close_all_file_handlers()
    _reset_logging_guard()
    yield
    _bootstrap.reset_bootstrap_state()
    _close_all_file_handlers()
    _reset_logging_guard()


def _redirect_all_var_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Redirect every ``var_dirs`` key under ``tmp_path``.

    Necessary because ``bootstrap()`` walks the full ``var_dirs`` dict
    and ``mkdir(exist_ok=True)`` each entry — a partial redirection
    leaks the un-redirected keys (``backups``, ``run``, ``snapshots``…)
    into the real repo ``var/`` tree, polluting the working copy and
    failing in read-only test environments.
    """
    redirected: dict[str, Path] = {name: tmp_path / name for name in config.var_dirs}
    monkeypatch.setattr(config, "var_dirs", redirected)
    return redirected


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
    _close_all_file_handlers()

    # ``src.config`` has already been imported by the test runner; the
    # import itself didn't add file handlers. The attribute access is a
    # no-op, but documents intent.
    assert config.logger is not None
    assert _file_handlers_on("migration") == []


def test_fresh_module_reload_attaches_no_file_handlers() -> None:
    """A *truly* fresh import of ``src.config`` is import-side-effect free.

    The plain ``test_module_import_is_inert`` test runs against a
    cached ``src.config`` module, so a regression that attaches handlers
    only at import time can slip through if the test simply removes
    handlers before asserting. This test re-imports ``src.config`` from
    scratch via ``importlib.reload`` (after detaching all FileHandlers
    and resetting the logging guard) and asserts the *reload itself*
    adds no FileHandler.
    """
    _close_all_file_handlers()
    _reset_logging_guard()
    handlers_before = _file_handlers_on("migration")

    importlib.reload(sys.modules["src.config"])

    handlers_after = _file_handlers_on("migration")
    assert handlers_after == handlers_before, (
        "src.config reload must not attach any FileHandler; got new handlers: "
        f"{set(handlers_after) - set(handlers_before)}"
    )


def test_bootstrap_attaches_file_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After ``bootstrap()``, the migration logger has a working FileHandler."""
    redirected = _redirect_all_var_dirs(monkeypatch, tmp_path)

    config.bootstrap()

    assert redirected["logs"].exists(), "bootstrap() should create the logs directory"
    handlers = _file_handlers_on("migration")
    assert handlers, "bootstrap() should attach at least one FileHandler"


def test_bootstrap_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling ``bootstrap()`` twice must not double-attach handlers."""
    _redirect_all_var_dirs(monkeypatch, tmp_path)

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
    redirected = _redirect_all_var_dirs(monkeypatch, tmp_path)

    # Skip the file-handler step too: without the directory, attaching a
    # FileHandler would itself raise — we are testing the mkdir flag in
    # isolation.
    config.bootstrap(mkdir=False, configure_log=False, prune_logs=False)

    # Every redirected dir must remain absent — the helper redirected
    # *all* of var_dirs, so this assertion proves no key escaped.
    for path in redirected.values():
        assert not path.exists(), f"bootstrap(mkdir=False) created {path}"
    assert config.is_bootstrapped()


def test_bootstrap_skips_logging_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bootstrap(configure_log=False)`` must not attach a FileHandler."""
    _redirect_all_var_dirs(monkeypatch, tmp_path)

    # Strip any pre-existing handlers so the assertion is meaningful.
    _close_all_file_handlers()

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
    _redirect_all_var_dirs(monkeypatch, tmp_path)

    config.bootstrap()
    assert config.is_bootstrapped()

    config.reset_bootstrap_state()
    assert not config.is_bootstrapped()

    # Second bootstrap actually runs again because the flag was reset.
    # Reset logging state too — the first bootstrap attached its
    # handler to the now-doomed tmp_path, and configure_logging will
    # short-circuit otherwise.
    _close_all_file_handlers()
    _reset_logging_guard()
    config.bootstrap()
    assert config.is_bootstrapped()
