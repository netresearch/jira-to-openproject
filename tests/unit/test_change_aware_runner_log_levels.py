"""Change-detection fetch must not pollute logs at ERROR level.

Several migrations are deliberately transformation-only (e.g.
``ResolutionMigration``, ``SecurityLevelsMigration``,
``AffectsVersionsMigration``, ``VotesMigration``, ``TimeEntryMigration``)
and signal that fact by raising ``ValueError`` from
``_get_current_entities_for_type``. ``ChangeAwareRunner._get_cached_entities``
catches the exception, wraps it in ``MigrationError``, and re-raises so that
``should_skip_migration`` can fall back to running the migration.

The wrapping should happen *quietly* — the upstream caller already logs at
WARNING ("Change detection failed for X. Proceeding with migration."). The
inner ``logger.exception`` was producing a duplicate ERROR-level traceback
dump per transformation-only component, polluting production logs with no
extra information.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.models import MigrationError
from src.utils.change_aware_runner import ChangeAwareRunner


def _make_runner_with_failing_migration(exc: Exception) -> ChangeAwareRunner:
    fake_cache = MagicMock()

    def get_or_fetch(entity_type: str, fetch_fn: Any, **_kwargs: Any) -> Any:
        return fetch_fn(entity_type)

    fake_cache.get_or_fetch.side_effect = get_or_fetch

    fake_migration = MagicMock()
    fake_migration.entity_cache = fake_cache
    fake_migration.logger = logging.getLogger("test_change_aware_runner_log_levels")
    fake_migration._get_current_entities_for_type.side_effect = exc

    return ChangeAwareRunner(fake_migration)


def test_fetch_does_not_log_at_error_for_value_error(caplog: pytest.LogCaptureFixture) -> None:
    """A transformation-only ``ValueError`` must NOT produce an ERROR log line."""
    runner = _make_runner_with_failing_migration(
        ValueError("ResolutionMigration is transformation-only ..."),
    )

    with caplog.at_level(logging.DEBUG, logger="test_change_aware_runner_log_levels"):
        with pytest.raises(MigrationError):
            runner._get_cached_entities(
                "resolutions",
                local={},
                invalidated=set(),
            )

    error_records = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and "Failed to fetch entities" in r.getMessage()
    ]
    assert error_records == [], (
        f"Expected no ERROR-level 'Failed to fetch entities' log, got: {error_records}"
    )


def test_fetch_does_not_log_at_error_for_arbitrary_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Even a real failure (e.g. network) must not log at ERROR here.

    The upstream caller (``should_skip_migration``) already logs at WARNING
    when it catches ``MigrationError``. Logging twice — at ERROR with a
    traceback inside, then at WARNING outside — produced confusing duplicate
    failure noise in the production run on 2026-05-04.
    """
    runner = _make_runner_with_failing_migration(RuntimeError("network down"))

    with caplog.at_level(logging.DEBUG, logger="test_change_aware_runner_log_levels"):
        with pytest.raises(MigrationError):
            runner._get_cached_entities(
                "anything",
                local={},
                invalidated=set(),
            )

    error_records = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and "Failed to fetch entities" in r.getMessage()
    ]
    assert error_records == [], (
        f"Expected no ERROR-level 'Failed to fetch entities' log, got: {error_records}"
    )
