# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Record CLI orchestration events in the opt-in dashboard snapshot."""

import re
from collections import Counter
from datetime import UTC, datetime
from logging import getLogger
from time import sleep
from typing import Any
from uuid import uuid4

from src.models import ComponentResult
from src.utils.dashboard_progress import progress_file, write_progress

_RESULT_COUNTS = {
    "total_count": "Total items",
    "success_count": "Successful items",
    "failed_count": "Failed items",
    "extracted": "Extracted items",
    "updated": "Updated items",
    "failed": "Failed operations",
}
_COMPARISON_LABELS = {
    "time_entries": {
        "comparison_by_project": "Jira worklogs on selected issues",
        "tempo_attributes_by_project": "Worklogs with Tempo attributes (target decrypted and verified)",
    },
    "attachments": {"comparison_by_project": "Attachments on selected issues (target SHA-256 verified)"},
}
_WORK_PACKAGE_COUNTS = {
    "work_packages_skeleton": {
        "total_created": "Created work packages",
        "total_skipped": "Skipped work packages",
        "total_failed": "Failed work packages",
    },
    "work_packages_content": {
        "total_updated": "Updated work packages",
        "total_skipped": "Skipped work packages",
        "total_failed": "Failed work packages",
        "descriptions_updated": "Updated descriptions",
        "comments_migrated": "Migrated comments",
        "comments_held": "Held comments",
        "comment_reads_failed": "Failed comment reads",
        "comments_failed": "Failed comment writes",
    },
}


def _reported_counts(name: str, result: ComponentResult) -> dict[str, int]:
    """Only publish enumerated numeric counters explicitly supplied by this component."""
    time_entries = result.details.get("total_time_entries")
    if name == "time_entries" and isinstance(time_entries, dict):
        counts = {
            "New time entries": time_entries.get("migrated"),
            "Skipped time entries": result.details.get("skipped"),
            "Failed time entries": time_entries.get("failed"),
        }
    else:
        counts = {
            label: getattr(result, field) for field, label in _RESULT_COUNTS.items() if field in result.model_fields_set
        }
    for field, label in _WORK_PACKAGE_COUNTS.get(name, {}).items():
        if field in result.details:
            counts[label] = result.details[field]
    return {key: value for key, value in counts.items() if type(value) is int and value >= 0}


def _comparison_sides(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {side: value[side] for side in ("source", "target") if type(value.get(side)) is int and value[side] >= 0}


def _comparison_projects(result: ComponentResult, field: str) -> dict[str, dict[str, int]]:
    raw = result.details.get(field)
    if not isinstance(raw, dict) or any(not isinstance(k, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", k) for k in raw):
        return {}
    return {project: _comparison_sides(pair) for project, pair in raw.items()}


class MigrationRunProgress:
    """Track ordered component attempts, including repeated names and unfinished phases."""

    def __init__(self) -> None:
        self.path = progress_file()
        self.dry_run = False
        self.snapshot: dict[str, Any] = {}
        self.phases: list[dict[str, str]] = []
        self.active: dict[str, str] | None = None
        self._observations: dict[str, dict[str, dict[str, int]]] = {}

    def start(self, *, dry_run: bool) -> None:
        """Record a fresh run before preflight or client construction."""
        if self.path is None:
            return
        self.dry_run = dry_run
        self._observations.clear()
        self.phases.clear()
        self.active = None
        mode = "dry-run" if dry_run else "run"
        self.snapshot = {
            "migration_id": f"{mode}-{uuid4()}",
            "status": "running",
            "start_time": datetime.now(UTC).isoformat(),
            "phases": self.phases,
            "verified_counts": {},
        }
        self._publish()

    def plan(self, names: list[str]) -> None:
        """List only components resolved by the actual factory registry."""
        if self.path is None:
            return
        visits: Counter[str] = Counter()
        for name in names:
            visits[name] += 1
            label = name if visits[name] == 1 else f"{name} (attempt {visits[name]})"
            self.phases.append({"name": label, "status": "pending"})
        self._publish()

    def start_component(self, index: int) -> None:
        """Mark the requested attempt active before calling its factory."""
        if self.path is not None:
            self.active = self.phases[index]
            self.active.update(status="running", started_at=datetime.now(UTC).isoformat())
            self.snapshot["current_component"] = self.active["name"]
            component = self.active["name"].partition(" (attempt ")[0]
            for label in _COMPARISON_LABELS.get(component, {}).values():
                self._observations.pop(label, None)
            self._publish_comparisons()
            self._publish()

    def end_component(self, name: str, result: ComponentResult, status: str) -> None:
        """Record the component's outcome without copying its messages, details, or entity data."""
        if self.active is None:
            return
        phase = self.active["name"]
        if not self.dry_run and not result.dry_run:
            for field, label in _COMPARISON_LABELS.get(name, {}).items():
                self._observations[label] = _comparison_projects(result, field)
            self._publish_comparisons()
            counts = _reported_counts(name, result)
            self.snapshot["verified_counts"].update({f"{phase} · {label}": value for label, value in counts.items()})
        self._close_phase(status)
        self._publish()

    def _publish_comparisons(self) -> None:
        projects: dict[str, dict[str, dict[str, int]]] = {}
        totals = {}
        for label, observations in self._observations.items():
            for project, pair in observations.items():
                if pair:
                    projects.setdefault(project, {})[label] = pair
            measured = {
                side: sum(pair[side] for pair in observations.values())
                for side in ("source", "target")
                if observations and all(side in pair for pair in observations.values())
            }
            if measured:
                totals[label] = measured
        self.snapshot.update(project_comparisons=projects, comparison_counts=totals)

    def _close_phase(self, status: str) -> None:
        if self.active is not None:
            self.active.update(status=status, finished_at=datetime.now(UTC).isoformat())
            self.active = None
            self.snapshot.pop("current_component", None)

    def finish(self, outcome: str) -> None:
        """Record the real terminal outcome, retaining unfinished attempts after an abort."""
        if not self.snapshot:
            return
        status = {"success": "completed", "interrupted": "paused"}.get(outcome, "failed")
        if status == "completed" and not self.phases:
            status = "pending"
            self.snapshot["pending_checks"] = ["No recognized migration components were selected"]
        self._close_phase(status)
        self.snapshot.update(status=status, finished_at=datetime.now(UTC).isoformat())
        self._publish()

    def _publish(self) -> None:
        self.snapshot["last_update"] = datetime.now(UTC).isoformat()
        try:
            # A later event refreshes an active run. Its final report has no successor.
            if "finished_at" in self.snapshot:
                for delay in (0.05, 0.1):
                    try:
                        write_progress(self.snapshot, path=self.path)
                        return
                    except OSError:
                        sleep(delay)
            write_progress(self.snapshot, path=self.path)
        except (OSError, ValueError) as exc:
            getLogger(__name__).warning("Could not publish dashboard progress (%s)", type(exc).__name__)
