# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Require positive Jira issue-level visibility evidence for ordinary work-package imports."""

import json
from collections.abc import Collection, Mapping
from inspect import getattr_static
from itertools import batched
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


def _confirmed_identities(jira_client: Any, project_key: str, identities: tuple[str, ...]) -> set[str]:
    terms = ", ".join(json.dumps(str(value)) for value in identities)
    predicate = unrestricted_project_jql(project_key)
    query = f"{predicate} AND issue IN ({terms}) ORDER BY id"

    def fetch_page(offset: int, size: int) -> list:
        return jira_client.jira.search_issues(query, startAt=offset, maxResults=size, fields="security")

    confirmed = set()
    for issue in iter_issue_pages(project_key, fetch_page, len(identities)):
        if permits_unrestricted_result(issue):
            confirmed.update((str(issue.id), str(issue.key)))
    return confirmed


def warn_if_restricted_mappings(jira_client: Any, project_key: str, known: Collection[str], logger: Any) -> None:
    """Flag old targets Jira no longer confirms as visible and without an issue level."""
    for identities in batched(sorted(set(known)), 75, strict=False):
        missing = set(identities) - _confirmed_identities(jira_client, project_key, identities)
        if missing:
            logger.warning(
                "Jira did not confirm %d existing work-package mappings in %s as unrestricted; "
                "review access to the previous OpenProject targets before cutover.",
                len(missing),
                project_key,
            )
