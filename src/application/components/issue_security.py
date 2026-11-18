# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Require positive Jira issue-level visibility evidence for ordinary work-package imports."""

import json
from collections.abc import Collection, Mapping
from inspect import getattr_static
from typing import Any

from src.infrastructure.jira.issue_pagination import iter_issue_pages


def unrestricted_project_jql(project_key: str) -> str:
    """Let Jira affirm an empty issue security level even where the field is omitted."""
    return f"project = {json.dumps(project_key)} AND level IS EMPTY"


def permits_unrestricted_result(issue: Any) -> bool:
    """Reject contradictory results of an explicitly level-IS-EMPTY search."""
    raw = issue if isinstance(issue, Mapping) else getattr(issue, "raw", None)
    if isinstance(raw, Mapping) and "fields" in raw:
        fields = raw["fields"]
        if not isinstance(fields, Mapping) or fields.get("security") is not None:
            return False
    fields = getattr(issue, "fields", None)
    if isinstance(fields, Mapping):
        return fields.get("security") is None
    return getattr_static(fields, "security", None) is None


def mapped_project_issues(project_key: str, mapping: Mapping) -> set[str]:
    """Select source IDs/keys from both current and historical work-package mappings."""
    known = set()
    prefix = project_key.upper() + "-"
    for source, value in mapping.items():
        key = str(value.get("jira_key") or source) if isinstance(value, Mapping) else str(source)
        if key.upper().startswith(prefix) or (isinstance(value, Mapping) and value.get("project_key") == project_key):
            known.add(key)
    return known


def warn_if_restricted_mappings(jira_client: Any, project_key: str, known: Collection[str], logger: Any) -> None:
    """Flag earlier targets absent from a positive Jira search, including hidden or moved issues."""
    pending = set(known)
    if not pending:
        return
    # Searching for individual deleted or hidden keys can itself produce a Jira 400.
    # The project's visible list handles both legacy Jira key mappings and numeric source IDs.
    query = f"{unrestricted_project_jql(project_key)} ORDER BY id"

    def fetch_page(offset: int, size: int) -> list:
        return jira_client.jira.search_issues(query, startAt=offset, maxResults=size, fields="security")

    for issue in iter_issue_pages(project_key, fetch_page, 100):
        if permits_unrestricted_result(issue):
            pending.difference_update((str(issue.id), str(issue.key)))
            if not pending:
                return
    logger.warning(
        "Jira did not confirm %d existing work-package mappings in %s as unrestricted; "
        "review access to the previous OpenProject targets before cutover.",
        len(pending),
        project_key,
    )
