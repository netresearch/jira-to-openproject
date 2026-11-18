# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Measure unique source IDs and their confirmed target records for a selected issue scope."""

import re
from collections import Counter
from collections.abc import Iterable
from threading import Lock


class ScopedReceipts:
    """Count fully observed issue inventories and their distinct, verified native counterparts."""

    def __init__(self, issue_keys: Iterable[str]) -> None:
        self.scope = set(issue_keys)
        self.inventory: dict[str, set[str]] = {}
        self.targets: dict[tuple[str, str], set[int]] = {}
        self.lock = Lock()

    def observe(self, issue_key: str, identifiers: list) -> None:
        """An empty list is an observed empty issue. A missing or duplicate ID invalidates its inventory."""
        ids = {str(value) for value in identifiers if type(value) in (int, str) and str(value).strip()}
        if len(ids) == len(identifiers):
            self.inventory[issue_key] = ids

    def confirm(self, issue_key: str, identifier: object, target_id: object) -> None:
        if issue_key in self.scope and type(target_id) is int and target_id > 0:
            with self.lock:
                self.targets.setdefault((issue_key, str(identifier)), set()).add(target_id)

    def counts(self) -> dict[str, dict[str, int]]:
        projects: dict[str, set[str]] = {}
        for issue in self.scope:
            match = re.fullmatch(r"([A-Z][A-Z\d_]*)-\d+", issue, flags=re.ASCII)
            if match is None:
                return {}
            projects.setdefault(match[1], set()).add(issue)
        source_counts = Counter(identifier for ids in self.inventory.values() for identifier in ids)
        target_counts = Counter(identifier for ids in self.targets.values() for identifier in ids)
        return {
            project: self._project_counts(issues, source_counts, target_counts) for project, issues in projects.items()
        }

    def _project_counts(self, issues: set[str], sources: Counter[str], targets: Counter[int]) -> dict[str, int]:
        if not issues.issubset(self.inventory):
            return {}
        keys = {(issue, identifier) for issue in issues for identifier in self.inventory[issue]}
        if any(sources[identifier] != 1 for _, identifier in keys):
            return {}
        pair = {"source": len(keys)}
        receivers = [self.targets.get(key, set()) for key in keys]
        if receivers and all(len(ids) == 1 for ids in receivers):
            target_ids = set.union(*receivers)
            if all(targets[identifier] == 1 for identifier in target_ids):
                pair["target"] = len(target_ids)
        return pair
