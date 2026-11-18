# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT
"""Determine whether a legacy issue has journal work without inspecting source text."""

from typing import Any


def _field(source: Any, name: str) -> Any:
    return source.get(name) if isinstance(source, dict) else getattr(source, name, None)


def _empty_page(page: Any, field: str, *, require_total: bool) -> bool:
    total = _field(page, "total")
    if total is None and require_total:
        return False
    if total is not None and (type(total) is not int or total != 0):
        return False
    return _field(page, field) == [] and _field(page, "startAt") in (None, 0) and _field(page, "isLast") is not False


def has_no_source_journals(issue: Any, *, require_complete: bool) -> bool:
    """Check every supplied SDK/raw surface, optionally requiring explicit zero totals for both collections."""
    seen: set[str] = set()
    raw = None if isinstance(issue, dict) else getattr(issue, "raw", None)
    for source in (raw, issue):
        if source is None:
            continue
        if _field(source, "_rails_operations"):
            return False
        surfaces = (
            ("histories", _field(source, "changelog")),
            ("comments", _field(_field(source, "fields"), "comment")),
        )
        for name, page in surfaces:
            if page is None:
                continue
            if not _empty_page(page, name, require_total=require_complete):
                return False
            seen.add(name)
    return not require_complete or seen == {"histories", "comments"}
