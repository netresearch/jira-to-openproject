# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Who may hold a membership row in a level sub-project (ADR-009, step 6; ADR-012).

The audience of a level sub-project is the OpenProject users of its static readers, and on
Enterprise, per ``per_issue`` issue, the users of its extra readers. Reader names come from the
operator-only readers file and are matched to OpenProject users through the user mapping's
``jira_name``, case-insensitively, as ``j2o-restricted admins`` does; a name without exactly one
OpenProject user is counted, never guessed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from src import config
from src.application.components.level_readers import READERS_SETTING, Readers, load_readers
from src.application.components.security_level_projects_migration import load_reports


def openproject_users(user_mapping: Mapping[str, Any]) -> dict[str, int | None]:
    """Casefolded Jira user name to its OpenProject user ID; None when entries disagree.

    Entries restored from OpenProject (``restored_from_op``) guess the Jira name from the login
    and do not count.
    """
    users: dict[str, int | None] = {}
    for entry in user_mapping.values():
        if not isinstance(entry, Mapping) or entry.get("restored_from_op"):
            continue
        name, user_id = entry.get("jira_name"), entry.get("openproject_id")
        if isinstance(name, str) and name and type(user_id) is int:
            key = name.casefold()
            users[key] = user_id if users.get(key, user_id) == user_id else None
    return users


@dataclass
class Audience:
    """The OpenProject users of each level's static readers and of each issue's extra readers."""

    members: dict[str, frozenset[int]] = field(default_factory=dict)
    shares: dict[str, frozenset[int]] = field(default_factory=dict)
    # The level label of each issue key in ``shares``.
    share_levels: dict[str, str] = field(default_factory=dict)
    # Reader names without exactly one OpenProject user, per level label and per issue key.
    unmapped: dict[str, int] = field(default_factory=dict)

    def of_level(self, label: str) -> dict[str, Any]:
        """The Ruby input of one sub-project: its static readers, and per issue key its extra readers."""
        return {
            "members": sorted(self.members.get(label, ())),
            "shares": {
                key: sorted(users) for key, users in sorted(self.shares.items()) if self.share_levels.get(key) == label
            },
        }


def _resolve(names: frozenset[str], users: Mapping[str, int | None]) -> tuple[frozenset[int], int]:
    ids = [users.get(name.casefold()) for name in names]
    found = frozenset(user_id for user_id in ids if user_id is not None)
    return found, len(ids) - len(found)


def audience(readers: Readers, user_mapping: Mapping[str, Any], *, shares: bool) -> Audience:
    """Map the readers to OpenProject users; extra readers only when ``shares`` (Enterprise)."""
    users = openproject_users(user_mapping)
    result = Audience()
    for label, names in readers.static.items():
        result.members[label], result.unmapped[label] = _resolve(names, users)
    if shares:
        for key, names in readers.extra.items():
            result.shares[key], result.unmapped[key] = _resolve(names, users)
            result.share_levels[key] = readers.levels[key]
    return result


def load_audience(mappings: Any) -> tuple[Audience, str]:
    """Both reports, the readers file of the same inventory and the user mapping; any mismatch raises."""
    report, edition = load_reports()
    readers = load_readers(report)
    return audience(readers, mappings.get_mapping("user") or {}, shares=edition == "enterprise"), edition


def staging_audience(mappings: Any) -> Audience:
    """Who may already be a member while a level stage writes (ADR-012).

    Without a configured readers file nobody: the sub-projects must be memberless, as before the
    late access grant existed. A configured file that does not match the reports raises.
    """
    if not config.migration_config.get(READERS_SETTING):
        return Audience()
    return load_audience(mappings)[0]
