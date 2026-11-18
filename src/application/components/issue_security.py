# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Require positive Jira issue-level visibility evidence for ordinary work-package imports."""

import json
from collections.abc import Collection, Mapping
from inspect import getattr_static
from typing import Any

from src.infrastructure.jira.issue_pagination import iter_issue_pages
from src.infrastructure.jira.jira_issue_service import fetch_issue_page_with_retry


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


def mapped_project_issues(project_key: str, mapping: Mapping, existing: Mapping | None = None) -> set[str]:
    """Prefer immutable source IDs and retain legacy Jira keys when the ID is unknown."""
    keys: set[str] = set()
    ids_by_key: dict[str, set[str]] = {}
    prefix = project_key.upper() + "-"
    for source, value in mapping.items():
        row = value if isinstance(value, Mapping) else {}
        key = str(row.get("jira_key") or source)
        if not key.upper().startswith(prefix) and row.get("project_key") != project_key:
            continue
        keys.add(key)
        if str(source).isdecimal():
            ids_by_key.setdefault(key, set()).add(str(source))

    for key, target in (existing or {}).items():
        keys.add(key)
        source_id = target.get("jira_source_id") if isinstance(target, Mapping) else None
        if str(source_id).isdecimal():
            ids_by_key.setdefault(key, set()).add(str(source_id))

    return keys.difference(ids_by_key).union(*ids_by_key.values())


def warn_if_restricted_mappings(jira_client: Any, project_key: str, known: Collection[str], logger: Any) -> None:
    """Flag earlier targets absent from a positive Jira search, including hidden or moved issues."""
    pending = set(known)
    if not pending:
        return
    # Searching for individual deleted or hidden keys can itself produce a Jira 400.
    # The project's visible list handles both legacy Jira key mappings and numeric source IDs.
    query = f"{unrestricted_project_jql(project_key)} ORDER BY id"

    def fetch_page(offset: int, size: int) -> list:
        return fetch_issue_page_with_retry(
            jira_client,
            jql=query,
            start_at=offset,
            max_results=size,
            fields="security",
            expand=None,
            project_key=project_key,
        )

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
