# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""The content run writes the Jira due date and the configured start date (issue #489 finding)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src import config
from src.application.components.work_package_content_migration import WorkPackageContentMigration

START = "customfield_18690"


def _migration(monkeypatch):
    monkeypatch.setattr(config, "migration_config", {"batch_size": 10})
    mappings = MagicMock()
    mappings.get_mapping.return_value = {}
    monkeypatch.setattr(config, "mappings", mappings, raising=False)
    return WorkPackageContentMigration(jira_client=MagicMock(), op_client=MagicMock())


def _issue(**fields):
    return SimpleNamespace(key="K-1", fields=SimpleNamespace(**fields), raw={"fields": {}})


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"duedate": "2026-03-10"}, {"due_date": "2026-03-10"}),
        ({START: "2026-03-01T09:30:00.000+0100"}, {"start_date": "2026-03-01"}),
        ({"duedate": date(2026, 3, 10), START: "2026-03-01"}, {"due_date": "2026-03-10", "start_date": "2026-03-01"}),
        ({"duedate": "2026-02-01", START: "2026-03-01"}, {"start_date": "2026-03-01"}),
        ({"duedate": "", START: "not a date"}, {}),
        ({}, {}),
    ],
    ids=["due-only", "start-date-time", "both", "due-before-start", "unreadable", "none"],
)
def test_the_dates_of_an_issue(monkeypatch, fields, expected):
    assert _migration(monkeypatch)._date_updates(_issue(**fields)) == expected


def test_a_configured_start_date_field_is_read(monkeypatch):
    mig = _migration(monkeypatch)
    monkeypatch.setattr(config, "migration_config", {"start_date_custom_fields": ["customfield_77"]})
    assert mig._date_updates(_issue(customfield_77="2026-05-04")) == {"start_date": "2026-05-04"}


def test_dates_are_written_as_guarded_rows(monkeypatch):
    mig = _migration(monkeypatch)
    mig.op_client.batch_update_work_packages.side_effect = lambda rows: {
        "updated": len(rows),
        "results": [
            {"id": r["id"], "attr": r["_j2o_guard"]["attr"], "status": "updated", "digest": "a" * 64} for r in rows
        ],
    }
    item = {
        "wp_id": 5,
        "description_update": None,
        "custom_field_updates": {},
        "date_updates": {"start_date": "2026-03-01", "due_date": "2026-03-10"},
    }
    mig._write_work_package_fields([item], results := {})
    rows = mig.op_client.batch_update_work_packages.call_args.args[0]
    assert [(r["_j2o_guard"]["attr"], r.get("start_date", r.get("due_date"))) for r in rows] == [
        ("start_date", "2026-03-01"),
        ("due_date", "2026-03-10"),
    ]
    assert (results["dates_updated"], results["dates_failed"]) == (2, 0)


def test_the_collected_content_of_an_issue_carries_its_dates(monkeypatch):
    mig = _migration(monkeypatch)
    mig.jira_client.select_comments_for_migration.return_value = SimpleNamespace(comments=[], held=0)
    issue = SimpleNamespace(
        key="K-1", id="1", fields=SimpleNamespace(description=None, duedate="2026-03-10"), raw={"fields": {}}
    )
    collected = mig._collect_content_for_issue(issue, 5, watchers=False)
    assert collected["date_updates"] == {"due_date": "2026-03-10"}
