# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Each level sub-project's own copy of the Jira versions its work packages use (ADR-010).

A version of the parent project cannot be assigned to a work package of the private sub-project
unless the parent shares it, and a shared version counts every targeted work package for
everyone who sees the version. So the sub-project gets its own copy, with sharing ``none``,
recorded in the ``security_level_version`` mapping under ``<sub-project label>:<Jira version ID>``.
The parent's versions and their sharing are never changed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.application.components import level_issues
from src.infrastructure.openproject.version_catalog import sync_level_versions

MAPPING = "security_level_version"


def selected_level_issues(
    level: level_issues.LevelWorkPackages, jira_client: Any, project: str, keys: Iterable[str], fields: str
) -> dict[str, Any]:
    """The selected issues of one Jira project as one level search returns them now.

    An issue whose level changed since the selection, or that the search omits, is refused.
    """
    wanted, found, changed = set(keys), {}, set()
    for issue in level_issues.iter_level_issues(jira_client, project, 100, fields):
        key = issue.key
        if key not in wanted or key in found or key in changed:
            continue
        if level_issues.issue_level_id(issue) != level.targets[key].level_id:
            changed.add(key)
            level.refused.append(f"{key}: changed its level after the selection")
        else:
            found[key] = issue
    level.refused.extend(f"{key}: not returned by the level search" for key in sorted(wanted - set(found) - changed))
    return found


def version_ids(refs: object) -> list[str] | None:
    """The Jira version IDs of an issue field in their order, or None when they are malformed."""
    if not isinstance(refs, list) or not all(isinstance(ref, Mapping) for ref in refs):
        return None
    ids = [str(ref.get("id")) for ref in refs]
    return list(dict.fromkeys(ids)) if all(sid.isdecimal() for sid in ids) else None


class LevelVersionCopies:
    """Create or adopt the copies of one run and record them in their own mapping."""

    def __init__(self, op_client: Any, mappings: Any, source: str, logger: Any) -> None:
        """Write through ``op_client``; ``source`` is the Jira base URL of the version origins."""
        self.op_client = op_client
        self.mappings = mappings
        self.source = source
        self.logger = logger

    def ensure(
        self, target: level_issues.LevelTarget, project_key: str, catalogue: list[dict[str, Any]], ids: Iterable[str]
    ) -> dict[str, int] | None:
        """Jira version ID to the copy in the sub-project of ``target``, or None when it failed."""
        wanted = set(ids)
        rows = [row for row in catalogue if str(row.get("id")) in wanted]
        try:
            if len(rows) != len(wanted):
                msg = "a version is not in the Jira project catalogue"
                raise ValueError(msg)
            copies = sync_level_versions(
                self.op_client, (target.project_id, target.parent_id), self.source, project_key, rows
            )
        except Exception:
            self.logger.exception("Could not create the version copies of %s", target.label)
            return None
        self._record(target, project_key, copies, rows)
        return copies

    def _record(
        self, target: level_issues.LevelTarget, project_key: str, copies: dict[str, int], rows: list[dict[str, Any]]
    ) -> None:
        current = self.mappings.get_mapping(MAPPING) or {}
        mapping = dict(current)
        for row in rows:
            source_id = str(row["id"])
            mapping[f"{target.label}:{source_id}"] = {
                "openproject_id": copies[source_id],
                "openproject_project_id": target.project_id,
                "security_level_project": target.label,
                "jira_project_key": project_key,
                "jira_version_id": source_id,
                "name": row["name"],
            }
        if mapping != current:
            self.mappings.set_mapping(MAPPING, mapping)
