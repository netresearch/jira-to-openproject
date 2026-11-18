# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Offset pagination for Jira Server issue searches."""

from collections.abc import Callable, Iterator
from itertools import islice

from jira import Issue
from src.infrastructure.jira.jira_client import JiraApiError


def _check_reported_total(kind: str, total: int | None, offset: int, context: str) -> None:
    if total is not None and offset < total:
        msg = f"{context}: {kind} page before the reported total of {total}"
        raise JiraApiError(msg)


def _check_page_keys(page: list[Issue], previous_keys: set[str], offset: int, context: str) -> set[str]:
    if getattr(page, "startAt", offset) != offset:
        msg = f"{context}: Jira returned a page from the wrong offset"
        raise JiraApiError(msg)
    keys = set()
    for row in page:
        key = row.get("key") if isinstance(row, dict) else getattr(row, "key", None)
        if not isinstance(key, str) or not key.strip():
            msg_0 = f"{context}: Jira returned an issue without a key"
            raise JiraApiError(msg_0)
        keys.add(key)
    if len(keys) != len(page) or not keys.isdisjoint(previous_keys):
        msg = f"{context}: duplicate issue keys across Jira search results"
        raise JiraApiError(msg)
    return keys


def _fetch_issue_page(
    fetch_page: Callable[[int, int], list[Issue]], offset: int, size: int, context: str
) -> list[Issue]:
    try:
        return fetch_page(offset, size)
    except Exception as exc:
        msg = f"{context}: search failed: {exc}"
        raise JiraApiError(msg) from exc


def _reported_total(page: list[Issue], known_total: int | None) -> int | None:
    # python-jira substitutes len(page) when the response omits its total.
    total = getattr(page, "total", None)
    return total if isinstance(total, int) and total > len(page) else known_total


def _reached_limit(offset: int, limit: int | None) -> bool:
    return limit is not None and offset >= limit


def iter_issue_pages(
    project_key: str,
    fetch_page: Callable[[int, int], list[Issue]],
    page_size: int,
    limit: int | None = None,
) -> Iterator[Issue]:
    """Yield issues in Jira order, advancing by the number the server returned."""
    offset = 0
    known_total = None
    previous_keys: set[str] = set()
    if _reached_limit(0, limit):
        return
    while True:
        size = page_size if limit is None else min(page_size, limit - offset)
        context = f"Jira project {project_key} at startAt={offset}"
        page = _fetch_issue_page(fetch_page, offset, size, context)
        known_total = _reported_total(page, known_total)

        if not page:
            _check_reported_total("empty", known_total, offset, context)
            return

        previous_keys.update(_check_page_keys(page, previous_keys, offset, context))
        count = len(page) if limit is None else min(len(page), limit - offset)
        yield from islice(page, count)
        offset += count
        if _reached_limit(offset, limit):
            return
        if getattr(page, "isLast", False) is True:
            _check_reported_total("final", known_total, offset, context)
            return
        if _reached_limit(offset, known_total):
            return
