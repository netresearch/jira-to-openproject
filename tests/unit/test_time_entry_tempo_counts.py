# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""The migration summary counts the Tempo fields changed on every path (#490)."""

from __future__ import annotations

import pytest

from src.utils.scoped_receipts import ScopedReceipts
from src.utils.time_entry_migrator import TimeEntryMigrator


def _migrator():
    migrator = TimeEntryMigrator.__new__(TimeEntryMigrator)
    migrator._worklog_receipts = ScopedReceipts([])
    migrator._verified_attribute_origins = set()
    migrator._written_attribute_origins = set()
    migrator._tempo_fields_changed = 0
    return migrator


def test_every_successful_receipt_adds_its_changed_fields():
    migrator = _migrator()
    for receipt in (
        {"success": True, "id": 1, "tempo_fields_changed": 2},
        {"success": True, "id": 2, "reused": True, "tempo_fields_changed": 1},
        {"success": True, "id": 3, "tempo_fields_changed": 0},
        {"success": False, "tempo_fields_changed": 2},
        {"success": True, "id": 4, "tempo_fields_changed": "2"},
    ):
        migrator._confirm_worklog(receipt)
    assert migrator._tempo_fields_changed == 3


def test_a_single_created_entry_counts_too(monkeypatch):
    migrator = _migrator()

    class Op:
        def create_time_entry(self, entry):
            return {"success": True, "id": 9, "tempo_fields_changed": 2}

    migrator.op_client = Op()
    monkeypatch.setattr(migrator, "_validate_time_entry_with_reason", lambda entry: (True, ""))
    assert migrator._create_time_entry({}, parallel=False) == ("created", 9, None)
    assert migrator._tempo_fields_changed == 2


@pytest.mark.parametrize(
    "extra", [{"jira_started": "2026-01-02T10:00:00.000+0100"}, {"tempo_archive": {"packet": "p"}}]
)
def test_a_tempo_row_consumed_by_its_jira_entry_hands_over_its_billing(extra):
    migrator = _migrator()
    migrator.migration_results = {"successful_transformations": 0}
    row = {"issue_key": "K-1", "jira_worklog_id": "10", "billable_seconds": 60, "approval_status": "APPROVED", **extra}
    migrator.extracted_tempo_entries = [row]
    entry = {"_meta": {"jira_worklog_key": "K-1:10"}}
    assert migrator._attach_tempo_archives([entry]) == []
    assert (entry["_meta"]["tempo_billable_hours"], entry["_meta"]["tempo_approval_status"]) == (0.0167, "APPROVED")
    assert "tempo_attributes" not in entry["_meta"]
