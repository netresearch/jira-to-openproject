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

Additionally, the post-success snapshot path in ``ChangeAwareRunner.run()``
must NOT log at WARNING when the entity fetch fails because the migration is
transformation-only.  These migrations do not support snapshots by design —
``_get_current_entities_for_type`` raises ``ValueError`` which gets wrapped
into ``MigrationError`` by ``_get_cached_entities``.  The snapshot block
should treat ``MigrationError`` (i.e. no change-detection support) at debug
level, and only warn for genuine unexpected failures (e.g. ``IOError`` from
the snapshot writer).
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.models import ComponentResult, MigrationError
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


def _make_runner_for_snapshot_test(
    *,
    entity_fetch_exc: Exception | None = None,
    snapshot_exc: Exception | None = None,
) -> ChangeAwareRunner:
    """Build a runner whose migration succeeds but snapshot/fetch may fail.

    ``should_skip_migration`` returns ``(False, None)`` so ``run()`` proceeds
    to the actual migration and then attempts the snapshot.  ``migration.run()``
    returns a successful ``ComponentResult``.  Depending on the parameters:

    * ``entity_fetch_exc`` — ``_get_current_entities_for_type`` raises this,
      simulating a transformation-only migration.
    * ``snapshot_exc`` — ``create_snapshot`` raises this, simulating a genuine
      snapshot I/O failure (the fetch succeeds).
    """
    fake_cache = MagicMock()
    fake_cache.stats = {
        "hits": 0,
        "misses": 0,
        "evictions": 0,
        "memory_cleanups": 0,
        "total_size": 0,
    }
    fake_cache.global_size.return_value = 0

    if entity_fetch_exc is not None:
        # The cache passes through to the fetch function, which raises.
        def get_or_fetch_raises(entity_type: str, fetch_fn: Any, **_kwargs: Any) -> Any:
            return fetch_fn(entity_type)

        fake_cache.get_or_fetch.side_effect = get_or_fetch_raises
    else:
        fake_cache.get_or_fetch.return_value = []

    success_result = ComponentResult(
        success=True,
        message="ok",
        details={},
        success_count=1,
        failed_count=0,
        total_count=1,
    )

    fake_migration = MagicMock()
    fake_migration.entity_cache = fake_cache
    fake_migration.logger = logging.getLogger("test_change_aware_runner_log_levels")
    fake_migration.should_skip_migration.return_value = (False, None)
    fake_migration.run.return_value = success_result

    if entity_fetch_exc is not None:
        fake_migration._get_current_entities_for_type.side_effect = entity_fetch_exc
    else:
        fake_migration._get_current_entities_for_type.return_value = []

    if snapshot_exc is not None:
        fake_migration.create_snapshot.side_effect = snapshot_exc

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
        r for r in caplog.records if r.levelno >= logging.ERROR and "Failed to fetch entities" in r.getMessage()
    ]
    assert error_records == [], f"Expected no ERROR-level 'Failed to fetch entities' log, got: {error_records}"


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
        r for r in caplog.records if r.levelno >= logging.ERROR and "Failed to fetch entities" in r.getMessage()
    ]
    assert error_records == [], f"Expected no ERROR-level 'Failed to fetch entities' log, got: {error_records}"


# ---------------------------------------------------------------------------
# Snapshot-path tests (post-success snapshot in ChangeAwareRunner.run())
# ---------------------------------------------------------------------------


def test_snapshot_path_no_warning_for_transformation_only_migration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Transformation-only migrations must NOT produce a WARNING from the snapshot path.

    After a successful migration run, ``ChangeAwareRunner.run()`` tries to
    create a snapshot.  For transformation-only migrations,
    ``_get_current_entities_for_type`` raises ``ValueError`` which is wrapped
    into ``MigrationError`` by ``_get_cached_entities``.  This is expected
    by design — the migration does not support change detection — and must NOT
    produce a WARNING log line.
    """
    runner = _make_runner_for_snapshot_test(
        entity_fetch_exc=ValueError(
            "WatcherMigration is a transformation-only migration and does not "
            "support idempotent workflow. It operates on data from other migrations."
        ),
    )

    with caplog.at_level(logging.DEBUG, logger="test_change_aware_runner_log_levels"):
        result = runner.run("watchers")

    assert result.success, "Migration result must still be successful"

    warning_records = [
        r for r in caplog.records if r.levelno >= logging.WARNING and "snapshot" in r.getMessage().lower()
    ]
    assert warning_records == [], (
        f"Expected no WARNING-level snapshot log for transformation-only migration, got: {warning_records}"
    )


def test_snapshot_path_still_warns_for_genuine_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A genuine snapshot failure (e.g. I/O error) MUST still produce a WARNING.

    Only ``MigrationError`` (no change-detection support) should be silenced.
    An unexpected exception from ``create_snapshot`` itself (e.g. ``OSError``)
    indicates a real problem and must still be logged at WARNING.
    """
    runner = _make_runner_for_snapshot_test(
        snapshot_exc=OSError("disk full"),
    )

    with caplog.at_level(logging.DEBUG, logger="test_change_aware_runner_log_levels"):
        result = runner.run("some_entity")

    assert result.success, "Migration result must still be successful despite snapshot failure"

    warning_records = [
        r for r in caplog.records if r.levelno >= logging.WARNING and "snapshot" in r.getMessage().lower()
    ]
    assert warning_records, "Expected at least one WARNING-level log about snapshot failure for genuine I/O error"
