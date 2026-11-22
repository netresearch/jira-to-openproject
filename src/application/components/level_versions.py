# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Each level sub-project's own copies of the Jira versions its work packages use (ADR-010).

A version of the parent project cannot be assigned to a work package of the private sub-project
unless the parent shares it, and a shared version counts every targeted work package for
everyone who sees the version. So the sub-project gets its own copy, with sharing ``none``,
recorded in the ``security_level_version`` mapping under ``<sub-project label>:<copy ID>``.
A work package shared with extra readers gets a copy of its own audience (update 2026-11-22),
whose copy ID is ``<Jira version ID>:audience:<token>`` (see ``version_catalog.level_copy_id``). The
token is keyed with the operator's secret in the data directory, so OpenProject shows no user ID
and nothing derived from user IDs without that key.
The parent's versions and their sharing are never changed.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterable, Mapping
from typing import Any, NamedTuple

from src import config
from src.application.components import level_issues
from src.application.components.security_level_projects_migration import LevelProjectError
from src.infrastructure.openproject.version_catalog import (
    AUDIENCE_KEY_BYTES,
    audience_token,
    level_copy_id,
    sync_level_sprints,
    sync_level_versions,
)

MAPPING = "security_level_version"
# The operator's secret of the audience tokens, next to the mappings (ADR-010).
AUDIENCE_KEY_FILE = "level_version_audience.key"


class CopyAudience(NamedTuple):
    """Who a copy is for: no token and no users for the static copy."""

    token: str | None
    users: tuple[int, ...]


STATIC = CopyAudience(None, ())


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


def _known_audience(target: level_issues.LevelTarget, key: str) -> tuple[int, ...] | None:
    """The sorted users a ``per_issue`` work package is shared with; None when unknown or empty."""
    users = target.audience.get("shares", {}).get(key)
    return tuple(sorted(users)) if users else None


def audience_key(level: level_issues.LevelWorkPackages) -> bytes | None:
    """The secret of the audience tokens, when a selected work package has a known audience.

    It is 32 random bytes, hex-encoded in ``level_version_audience.key`` in the data directory,
    created with mode 0600 when missing. An unreadable or malformed file stops the run: a new
    key would silently give every audience new copies.
    """
    if not any(t.kind == "per_issue" and _known_audience(t, k) for k, t in level.targets.items()):
        return None
    path = config.get_path("data") / AUDIENCE_KEY_FILE
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    except OSError as exc:
        msg = f"Cannot create the audience key {path}: {exc}"
        raise LevelProjectError(msg) from exc
    else:
        with os.fdopen(fd, "w") as handle:
            handle.write(secrets.token_hex(AUDIENCE_KEY_BYTES) + "\n")
    try:
        text = path.read_text(encoding="ascii").strip()
        key = bytes.fromhex(text)
    except (OSError, ValueError) as exc:
        msg = f"Unreadable or malformed audience key {path}; restore it from the backup of the mappings"
        raise LevelProjectError(msg) from exc
    if len(key) != AUDIENCE_KEY_BYTES:
        msg = f"Malformed audience key {path}; restore it from the backup of the mappings"
        raise LevelProjectError(msg)
    return key


def copy_audience(level: level_issues.LevelWorkPackages, key: bytes | None, issue_key: str) -> CopyAudience | None:
    """The audience whose copies the work package of ``issue_key`` targets; None when it is unknown.

    On Enterprise a ``per_issue`` issue is shared with its own extra readers (ADR-009). A user who
    sees a work package through a share sees every version it targets (``Version.visible`` via
    ``shared_via_work_packages``), and a version counts all its targeted work packages. So such
    a work package targets the copies of its audience: the sorted OpenProject user IDs it is
    shared with, which only work packages with the same users share, named by their token
    under ``key`` (ADR-010). Every other work package gets the static copy. Without a known,
    non-empty set of users (no readers file, or none of the readers has an OpenProject user) the
    issue gets no target version: a later share would show the static copy to its readers.
    """
    target = level.targets[issue_key]
    if target.kind != "per_issue":
        return STATIC
    users = _known_audience(target, issue_key)
    if users is None or key is None:
        return None
    return CopyAudience(audience_token(users, key), users)


def report_withheld(result: Any, keys: list[str], logger: Any) -> None:
    """Record the target versions withheld from shared level work packages; they are no failure."""
    if keys:
        logger.warning("Target versions withheld from %d shared level work packages: %s", len(keys), ", ".join(keys))
    result.details["level_versions_withheld"] = len(keys)


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
        self,
        target: level_issues.LevelTarget,
        project_key: str,
        catalogue: list[dict[str, Any]],
        wanted: Mapping[str, set[CopyAudience]],
    ) -> dict[str, int] | None:
        """Copy ID to the copy in the sub-project of ``target``, or None when it failed.

        ``wanted`` maps each Jira version ID to the audiences that need a copy of it (see
        ``copy_audience``).
        """
        rows = [row for row in catalogue if str(row.get("id")) in wanted]
        try:
            if len(rows) != len(wanted):
                msg = "a version is not in the Jira project catalogue"
                raise ValueError(msg)
            if any(len({a.token for a in audiences}) != len(audiences) for audiences in wanted.values()):
                msg = "two audiences have the same token"
                raise ValueError(msg)
            copies = sync_level_versions(
                self.op_client,
                (target.project_id, target.parent_id, target.audience),
                self.source,
                project_key,
                rows,
                audiences={sid: {a.token for a in audiences} for sid, audiences in wanted.items()},
            )
        except Exception:
            self.logger.exception("Could not create the version copies of %s", target.label)
            return None
        entries = {}
        for row in rows:
            sid = str(row["id"])
            for audience in wanted[sid]:
                copy_id = level_copy_id(sid, audience.token)
                entry = {"openproject_id": copies[copy_id], "jira_version_id": sid, "name": row["name"]}
                entries[copy_id] = entry | _audience_field(audience)
        self._record(target, project_key, entries)
        return copies

    def ensure_sprints(
        self,
        target: level_issues.LevelTarget,
        origin: tuple[str, str],
        sprints: dict[str, int],
        audiences: Mapping[str, CopyAudience] | None = None,
    ) -> dict[str, int] | None:
        """Copy ID to the copy of the parent's sprint version, or None when it failed.

        ``origin`` is the Jira project key and ID; ``sprints`` maps each copy ID (the Jira sprint
        ID, or ``level_copy_id`` of it and an audience) to the parent's version of that sprint,
        and ``audiences`` each audience copy ID to its audience.
        """
        project_key, audiences = origin[0], audiences or {}
        try:
            copies = sync_level_sprints(
                self.op_client,
                (target.project_id, target.parent_id, target.audience),
                (self.source, *origin),
                sprints,
                {cid: audience.token for cid, audience in audiences.items() if audience.token},
            )
        except Exception:
            self.logger.exception("Could not create the sprint copies of %s", target.label)
            return None
        entries = {
            f"sprint:{cid}": {
                "openproject_id": copy,
                "jira_sprint_id": cid.partition(":")[0],
                "parent_version_id": sprints[cid],
            }
            | _audience_field(audiences.get(cid, STATIC))
            for cid, copy in copies.items()
        }
        self._record(target, project_key, entries)
        return copies

    def _record(self, target: level_issues.LevelTarget, project_key: str, entries: dict[str, dict[str, Any]]) -> None:
        current = self.mappings.get_mapping(MAPPING) or {}
        mapping = dict(current)
        for suffix, entry in entries.items():
            mapping[f"{target.label}:{suffix}"] = {
                "openproject_id": entry["openproject_id"],
                "openproject_project_id": target.project_id,
                "security_level_project": target.label,
                "jira_project_key": project_key,
            } | {key: value for key, value in entry.items() if key != "openproject_id"}
        if mapping != current:
            self.mappings.set_mapping(MAPPING, mapping)


def _audience_field(audience: CopyAudience) -> dict[str, Any]:
    """The operator-side fields of an audience copy: its OpenProject user IDs; nothing for a static copy."""
    return {"audience": list(audience.users)} if audience.users else {}
