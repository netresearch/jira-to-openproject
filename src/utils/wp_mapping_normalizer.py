"""Normalisation helpers for the polymorphic ``work_package`` mapping.

The persisted ``work_package`` mapping pre-dates ADR-002 phase 3c and
holds values in two shapes — see
:mod:`src.models.mapping.work_package_entry` for the history. This
module provides a single coercion entry point,
:func:`normalize_wp_mapping`, that flattens both shapes into a uniform
dict-shape suitable for re-persisting.

The function is intentionally lossy on the *value* axis: malformed
entries (e.g. strings, lists, ``None``) are *dropped* rather than
raising, because the caller (a one-shot CLI script in
``scripts/normalize_wp_mapping.py``) runs against user-owned files
that may have accumulated corruption from older migrations. The
dropped count is returned alongside the normalised mapping so the
caller can surface it in logs.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from src.models.mapping import WorkPackageMappingEntry

logger = logging.getLogger(__name__)


def normalize_wp_mapping(
    raw: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], int]:
    """Normalise a legacy ``work_package`` mapping into a uniform dict-shape.

    Each entry is coerced through
    :meth:`WorkPackageMappingEntry.from_legacy` and then dumped via
    :meth:`pydantic.BaseModel.model_dump` so the result is a plain
    ``dict[str, dict[str, Any]]`` — i.e. JSON-serialisable and free of
    Pydantic objects. ``None``-valued optional fields are preserved in
    the output to keep the shape uniform across rows; downstream
    consumers can treat ``None`` and "missing" identically because
    :class:`WorkPackageMappingEntry` defaults the optional fields to
    ``None``.

    Args:
        raw: A flat mapping of ``jira_key -> entry`` where each entry
            is either an ``int`` (legacy) or a ``dict`` (modern).

    Returns:
        A two-tuple of ``(normalized, dropped_count)`` where:

        * ``normalized`` is a ``dict[str, dict[str, Any]]`` with one
          entry per successfully coerced row;
        * ``dropped_count`` is the number of rows that could not be
          coerced (logged at ``WARNING`` level with the offending key
          and shape).

    """
    normalized: dict[str, dict[str, Any]] = {}
    dropped = 0

    for key, value in raw.items():
        try:
            entry = WorkPackageMappingEntry.from_legacy(key, value)
        except (ValueError, TypeError) as exc:
            # Pydantic raises ``ValidationError`` (a ``ValueError``
            # subclass) on dict shapes that fail validation, and
            # ``from_legacy`` raises plain ``ValueError`` on unsupported
            # primitive shapes. ``TypeError`` covers exotic inputs
            # (e.g. ``model_validate`` choking on a non-mapping arg).
            logger.warning(
                "Dropping unsupported wp_map entry for key %r: %s",
                key,
                exc,
            )
            dropped += 1
            continue

        normalized[key] = entry.model_dump(mode="json")

    return normalized, dropped


__all__ = ["normalize_wp_mapping"]
