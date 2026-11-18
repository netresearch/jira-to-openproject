# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Offset pagination for Jira Server issue searches."""

from collections.abc import Callable, Iterator

from jira import Issue
from src.infrastructure.jira.jira_client import JiraApiError


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
    while limit is None or offset < limit:
        size = page_size if limit is None else min(page_size, limit - offset)
        context = f"Jira project {project_key} at startAt={offset}"
        try:
            page = fetch_page(offset, size)
        except Exception as exc:
            msg = f"{context}: search failed: {exc}"
            raise JiraApiError(msg) from exc

        # python-jira substitutes len(page) when Jira omits total. Only a
        # larger value distinguishes a server total from that SDK fallback.
        total = getattr(page, "total", None)
        if isinstance(total, int) and total > len(page):
            known_total = total

        if not page:
            if known_total is not None and offset < known_total:
                msg = f"{context}: empty page before the reported total of {known_total}"
                raise JiraApiError(msg)
            return

        if getattr(page, "startAt", offset) != offset:
            msg = f"{context}: Jira returned a page from the wrong offset"
            raise JiraApiError(msg)
        keys = {issue.key for issue in page}
        if len(keys) != len(page) or not keys.isdisjoint(previous_keys):
            msg = f"{context}: duplicate issue keys across Jira search results"
            raise JiraApiError(msg)
        previous_keys = keys

        for issue in page:
            yield issue
            offset += 1
            if limit is not None and offset >= limit:
                return
        if getattr(page, "isLast", False) is True:
            if known_total is not None and offset < known_total:
                msg = f"{context}: final page before the reported total of {known_total}"
                raise JiraApiError(msg)
            return
        if known_total is not None and offset >= known_total:
            return
