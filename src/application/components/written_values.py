# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""The digest of each value J2O last wrote into a work package (ADR-013).

A re-run must not replace a description or a custom field value that someone changed in
OpenProject after J2O wrote it. Rails compares the stored value with the digest recorded
here and skips a changed target; ``migration.overwrite_edited_targets`` lets the operator
replace it anyway. The store is a SQLite file in the data directory, next to the mappings.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src import config

SETTING = "overwrite_edited_targets"
_FILE = "written_values.sqlite"
_GUARD = "_j2o_guard"
# A precondition Rails checks before it writes, not a written attribute.
_CHECKS = {"id", _GUARD, "jira_version_issue_origin"}
_STATUSES = ("updated", "unchanged", "target_changed")


def store_path() -> Path:
    """The store of this migration's data directory."""
    return config.get_path("data") / _FILE


_open: dict[Path, WrittenValues] = {}


def store() -> WrittenValues:
    """The store of this migration, opened once per process."""
    path = store_path()
    if path not in _open:
        _open[path] = WrittenValues(path)
    return _open[path]


def overwrite_allowed() -> bool:
    """Whether the operator allowed replacing values changed in OpenProject."""
    return config.migration_config.get(SETTING) is True


class WrittenValuesError(RuntimeError):
    """The digest store could not be read or written."""


class WrittenValues:
    """Digests keyed by work package id and attribute (``description``, ``customField<id>``)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(path, timeout=120)
            with self._db:
                self._db.execute(
                    "CREATE TABLE IF NOT EXISTS written("
                    "wp INTEGER NOT NULL, attr TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(wp, attr))"
                )
        except (OSError, sqlite3.Error) as exc:
            msg = f"Cannot open the written-values store {path}"
            raise WrittenValuesError(msg) from exc

    def expected(self, keys: Iterable[tuple[int, str]]) -> dict[tuple[int, str], str]:
        """The recorded digests of the given keys; a key never written has none."""
        found: dict[tuple[int, str], str] = {}
        try:
            for wp, attr in set(keys):
                row = self._db.execute("SELECT digest FROM written WHERE wp=? AND attr=?", (wp, attr)).fetchone()
                if row:
                    found[wp, attr] = row[0]
        except sqlite3.Error as exc:
            msg = f"Cannot read the written-values store {self._path}"
            raise WrittenValuesError(msg) from exc
        return found

    def record(self, rows: Iterable[tuple[int, str, str]]) -> None:
        """Remember the digest of each value J2O wrote or found unchanged."""
        try:
            with self._db:
                self._db.executemany(
                    "INSERT INTO written(wp, attr, digest) VALUES(?, ?, ?) "
                    "ON CONFLICT(wp, attr) DO UPDATE SET digest=excluded.digest",
                    list(rows),
                )
        except sqlite3.Error as exc:
            msg = f"Cannot record in the written-values store {self._path}"
            raise WrittenValuesError(msg) from exc

    def close(self) -> None:
        """Close the database connection."""
        with contextlib.suppress(sqlite3.Error):
            self._db.close()


@dataclass
class Outcome:
    """What Rails did with a batch of guarded rows."""

    updated: int = 0
    unchanged: int = 0
    changed: list[str] = field(default_factory=list)
    failed: int = 0
    # The work packages with at least one written row.
    written: set[int] = field(default_factory=set)

    @property
    def acknowledged(self) -> int:
        """Rows Rails answered for, written or not."""
        return self.updated + self.unchanged + len(self.changed)


def _report(outcome: Outcome, what: str, logger: Any) -> Outcome:
    if outcome.changed:
        logger.warning(
            "Skipped %d %s changed in OpenProject since J2O wrote them (work package:attribute): %s",
            len(outcome.changed),
            what,
            ", ".join(outcome.changed),
        )
    return outcome


def write_rows(op_client: Any, rows: list[dict[str, Any]], *, what: str, logger: Any) -> Outcome:
    """Write work package attributes through the guard, one guarded row per attribute.

    A failed or unreadable Rails call counts every row as failed.
    """
    single = []
    for row in rows:
        checks = {key: value for key, value in row.items() if key in _CHECKS}
        single.extend({**checks, key: value} for key, value in row.items() if key not in _CHECKS)
    if not single:
        return Outcome()
    written = store()
    sent = guarded(single, written, overwrite=overwrite_allowed())
    try:
        outcome = settle(written, sent, op_client.batch_update_work_packages(sent))
    except Exception:
        logger.exception("Could not confirm %s writes", what)
        return Outcome(failed=len(sent))
    outcome.failed = len(sent) - outcome.acknowledged
    return _report(outcome, what, logger)


def write_custom_values(op_client: Any, values: list[dict[str, Any]], *, what: str, logger: Any) -> Outcome:
    """Write ``bulk_set_wp_custom_field_values`` rows through the guard."""
    if not values:
        return Outcome()
    written = store()
    overwrite = overwrite_allowed()
    keys = [(int(row["work_package_id"]), f"customField{int(row['custom_field_id'])}") for row in values]
    expect = written.expected(keys)
    guards = [{"attr": attr, "expect": expect.get((wp, attr)), "overwrite": overwrite} for wp, attr in keys]
    sent = [{**row, _GUARD: guard} for row, guard in zip(values, guards, strict=True)]
    rows = [{"id": wp, _GUARD: guard} for (wp, _), guard in zip(keys, guards, strict=True)]
    try:
        outcome = settle(written, rows, op_client.bulk_set_wp_custom_field_values(sent))
    except Exception:
        logger.exception("Could not confirm %s writes", what)
        return Outcome(failed=len(sent))
    outcome.failed = len(sent) - outcome.acknowledged
    return _report(outcome, what, logger)


def attribute_of(row: dict[str, Any]) -> str:
    """The one attribute a guarded row writes."""
    keys = [key for key in row if key not in _CHECKS]
    if len(keys) != 1:
        msg = f"A guarded row writes exactly one attribute, not {sorted(keys)}"
        raise ValueError(msg)
    return keys[0]


def guarded(rows: list[dict[str, Any]], store: WrittenValues, *, overwrite: bool) -> list[dict[str, Any]]:
    """The rows with the digest Rails must find before it writes."""
    keys = [(int(row["id"]), attribute_of(row)) for row in rows]
    expect = store.expected(keys)
    return [
        {**row, _GUARD: {"attr": attr, "expect": expect.get((wp, attr)), "overwrite": overwrite}}
        for row, (wp, attr) in zip(rows, keys, strict=True)
    ]


def settle(store: WrittenValues, rows: list[dict[str, Any]], result: dict[str, Any]) -> Outcome:
    """Record the digests Rails returned and count what it did with each guarded row."""
    sent = {(int(row["id"]), row[_GUARD]["attr"]) for row in rows}
    outcome = Outcome()
    digests: list[tuple[int, str, str]] = []
    for answer in result.get("results", []):
        status = answer.get("status")
        if status not in _STATUSES:
            continue
        key = (int(answer["id"]), str(answer.get("attr")))
        if key not in sent:
            msg = f"Rails answered for a row it was not sent: {key}"
            raise ValueError(msg)
        if status == "target_changed":
            outcome.changed.append(f"{key[0]}:{key[1]}")
            continue
        digest = answer.get("digest")
        if not isinstance(digest, str) or len(digest) != 64:
            msg = f"Rails returned no digest for {key}"
            raise ValueError(msg)
        digests.append((*key, digest))
        setattr(outcome, status, getattr(outcome, status) + 1)
        if status == "updated":
            outcome.written.add(key[0])
    if outcome.acknowledged > len(rows) or int(result.get("updated", outcome.updated)) != outcome.updated:
        msg = "Rails answered for more rows than it was sent, or its counts disagree"
        raise ValueError(msg)
    store.record(digests)
    return outcome
