# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""The operator-only readers file of a restricted-issue inventory (ADR-009, step 6).

``j2o-restricted inventory`` writes it next to the shared report: per level the static
readers, per ``per_issue`` issue its extra readers, as Jira user names. The importer
accepts it only when it belongs to the same inventory as the report (same archive digest,
same levels, the reader counts the report lists) and only when no other user can read it.
Reader names are never logged; messages name levels and issue keys only.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from typing import Any

from src import config
from src.application.components.security_level_projects_migration import LevelProjectError
from src.restricted_issues import READERS_FORMAT

READERS_SETTING = "security_level_readers"


def _require(condition: object, message: str) -> None:
    if not condition:
        raise LevelProjectError(message)


def _names(value: object, label: str) -> frozenset[str]:
    """A list of distinct, non-empty Jira user names."""
    message = f"Malformed readers of {label} in the readers file"
    if not isinstance(value, list) or not all(isinstance(name, str) and name for name in value):
        raise LevelProjectError(message)
    names = frozenset(value)
    _require(len(names) == len(value), message)
    return names


@dataclass(frozen=True)
class Readers:
    """Static readers per ``<Jira key>:<level ID>``, extra readers per ``per_issue`` issue key."""

    static: dict[str, frozenset[str]]
    extra: dict[str, frozenset[str]]
    # The level label of each ``per_issue`` issue key.
    levels: dict[str, str] = field(default_factory=dict)


def _read(path: object) -> dict[str, Any]:
    _require(isinstance(path, str) and path, "Readers file is not configured")
    try:
        # One descriptor for the mode check and the content: a file swapped between a stat and a
        # read of the path cannot pass; a symbolic link at the path is refused.
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            mode = os.fstat(handle.fileno()).st_mode
            value = json.loads(handle.read())
    except (OSError, ValueError) as exc:
        msg = f"Cannot read the readers file: {type(exc).__name__}"
        raise LevelProjectError(msg) from exc
    _require(stat.S_IMODE(mode) & 0o077 == 0, "The readers file is readable by other users; set mode 600")
    _require(isinstance(value, dict) and value.get("format") == READERS_FORMAT, "Not a readers file")
    return value


def _archived(report: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Per level label, the extra-reader count of each ``per_issue`` issue the report lists."""
    found: dict[str, dict[str, int]] = {}
    for row in report.get("archived") or []:
        if isinstance(row, dict) and row.get("class") == "per_issue":
            project = str(row.get("key", "")).rpartition("-")[0]
            found.setdefault(f"{project}:{row.get('level_id')}", {})[row["key"]] = row.get("extra_readers")
    return found


def load_readers(report: dict[str, Any]) -> Readers:
    """The readers file named by ``migration.security_level_readers``, checked against ``report``.

    Any difference to the report (another archive, another set of levels, a reader count or a
    ``per_issue`` issue the report does not list) refuses the whole file.
    """
    value = _read(config.migration_config.get(READERS_SETTING))
    _require(value.get("archive_sha256") == report["archive_sha256"], "The readers file belongs to another inventory")
    rows = value.get("levels")
    _require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), "Malformed readers file")
    expected = {f"{row['project']}:{row['level_id']}": row for row in report["levels"]}
    archived = _archived(report)
    static: dict[str, frozenset[str]] = {}
    extra: dict[str, frozenset[str]] = {}
    levels: dict[str, str] = {}
    for row in rows:
        label = f"{row.get('project')}:{row.get('level_id')}"
        _require(label in expected and label not in static, f"Level {label} of the readers file is not in the report")
        static[label] = _names(row.get("static_readers"), label)
        _require(len(static[label]) == expected[label].get("static_readers"), f"Static readers of {label} differ")
        issues = row.get("per_issue")
        _require(isinstance(issues, dict), f"Malformed readers of {label} in the readers file")
        counts = {key: len(_names(names, key)) for key, names in issues.items()}
        _require(counts == archived.get(label, {}), f"Extra readers of {label} differ from the report")
        extra |= {key: frozenset(names) for key, names in issues.items()}
        levels |= dict.fromkeys(issues, label)
    _require(set(static) == set(expected), "The readers file lacks levels of the report")
    return Readers(static, extra, levels)
