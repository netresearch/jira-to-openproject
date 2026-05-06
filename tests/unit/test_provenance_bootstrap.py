"""Provenance custom-field bootstrap must run at migration startup.

Bug D: ``ensure_origin_custom_fields()`` is the only place that creates the
full set of WP/User/TimeEntry provenance CFs, but it was never called.
Only ``J2O Origin Key`` survived via lazy on-demand creation in
``work_package_migration``, leaving the User and TimeEntry CFs missing
entirely (which then prevents time-entry idempotency — Bug C — because
the dedup lookup needs ``J2O Origin Worklog Key``).

Fix: call it once at migration startup, after clients are initialised
but before the component loop starts.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.migration import _ensure_provenance_bootstrap


def test_bootstrap_calls_ensure_origin_custom_fields() -> None:
    op_client = MagicMock()
    op_client.ensure_origin_custom_fields.return_value = {"WorkPackageCustomField": []}
    logger = MagicMock()

    _ensure_provenance_bootstrap(op_client, logger)

    op_client.ensure_origin_custom_fields.assert_called_once_with()


def test_bootstrap_swallows_failures_so_migration_can_continue() -> None:
    """If CF creation fails (e.g. one CF already exists with a clash), the
    migration should still proceed — degraded provenance is better than no
    migration. The failure is logged at warning level.
    """
    op_client = MagicMock()
    op_client.ensure_origin_custom_fields.side_effect = RuntimeError("OP unhappy")
    logger = MagicMock()

    _ensure_provenance_bootstrap(op_client, logger)  # must not raise

    op_client.ensure_origin_custom_fields.assert_called_once_with()
    assert logger.warning.called
