# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Exercise protected Tempo data through the standard time-entry migration and writer receipt."""

import json
from unittest.mock import Mock

import pytest
from pyrage import x25519

from src.infrastructure.openproject.openproject_time_entry_service import OpenProjectTimeEntryService
from src.utils.tempo_attribute_archive import KEY_SETTING, seal_values, verify_packets
from src.utils.time_entry_migrator import TimeEntryMigrator


@pytest.fixture
def identity(tmp_path, monkeypatch):
    path = tmp_path / "operator.agekey"
    path.write_text(str(x25519.Identity.generate()))
    path.chmod(0o600)
    monkeypatch.setenv(KEY_SETTING, str(path))
    return path


def source(value):
    flat = [{"key": "rate", "value": value}]
    return {
        "issue_key": "TEST-1",
        "issue_id": 42,
        "jira_worklog_id": 15,
        "work_attributes": flat,
        "work_attribute_values": [{"worklogId": 15, "workAttribute": {"key": "rate"}, "value": value}],
    }


def test_verified_age_packet_and_existing_receipt(identity):
    jira = "https://jira.example.test"
    original = seal_values(source(False), jira)["tempo_archive"]
    repeated = seal_values(source(False), jira)["tempo_archive"]
    changed = seal_values(source(0), jira)["tempo_archive"]
    assert original["packet"] != repeated["packet"]
    row = {"index": 0, "tempo_archive": repeated, "jira_worklog_key": "TEST-1:15"}

    def result(packet):
        return {
            "failed": 0,
            "reused": 1,
            "created": 0,
            "results": [
                {
                    "success": True,
                    "reused": True,
                    "index": 0,
                    "id": 4,
                    "verified_worklog_key": "TEST-1:15",
                    "tempo_packet": packet,
                    "tempo_attributes_written": False,
                }
            ],
        }

    existing = result(original["packet"])
    OpenProjectTimeEntryService._verify_tempo_receipts([row], existing)
    assert existing["results"][0]["tempo_attributes_verified"] and "tempo_packet" not in existing["results"][0]
    for packet in (changed["packet"], "broken"):
        conflict = result(packet)
        OpenProjectTimeEntryService._verify_tempo_receipts([row], conflict)
        assert conflict["failed"] == 1 and conflict["reused"] == 0 and not conflict["results"][0]["success"]
    with pytest.raises(ValueError, match="different|differ"):
        verify_packets(original["packet"], repeated["packet"], "TEST-2:15")
    identity.write_text(str(x25519.Identity.generate()))
    with pytest.raises(ValueError, match="decrypted"):
        verify_packets(original["packet"], original["packet"], "TEST-1:15")


def test_standard_migration_seals_before_cache_and_enriches_one_canonical_row(tmp_path, identity, monkeypatch):
    jira = Mock(base_url="https://jira.example.test")
    author = {"key": "worker", "time_zone": "UTC"}
    jira.get_work_logs_for_issue.return_value = [
        {"id": "15", "started": "2024-01-01T00:00:00Z", "author": author, "time_spent_seconds": 60, "comment": ""}
    ]
    jira.get_tempo_time_entries.return_value = [
        {**source("private accounting example"), "author": author, "time_spent_seconds": 60, "comment": ""}
    ]
    migration = TimeEntryMigrator(jira, Mock(), tmp_path)
    migration.user_mapping = {"worker": 7}
    migration.work_package_mapping = {"TEST-1": 8}
    migration._target_projects = {"TEST-1": 3}
    migration.default_activity_id = 2
    migration.extract_jira_work_logs_for_issues(["TEST-1"])
    migration.extract_tempo_time_entries(["TEST"])
    cached = (tmp_path / "tempo_time_entries.json").read_text()
    assert "private accounting example" not in cached and "work_attribute_values" not in cached
    (entry,) = migration.transform_all_time_entries()
    row = OpenProjectTimeEntryService._entry_row(entry, 0)
    assert "private accounting example" not in json.dumps(row)
    assert row["work_package_id"] == 8 and row["target_project_id"] == 3
    assert row["tempo_archive"]["issue_url"] == "https://jira.example.test/browse/TEST-1"
    monkeypatch.delenv(KEY_SETTING)
    assert migration.extract_tempo_time_entries(["TEST"]) == [] and migration._tempo_read_failed
    assert (
        not seal_values({}, "")
        and "private accounting example" not in (tmp_path / "tempo_time_entries.json").read_text()
    )
