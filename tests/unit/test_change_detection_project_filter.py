# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""A run limited to some Jira projects neither skips on a baseline nor writes one."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from src import config
from src.models import ComponentResult
from src.utils.change_aware_runner import ChangeAwareRunner


def _runner() -> tuple[ChangeAwareRunner, MagicMock]:
    migration = MagicMock()
    migration.logger = logging.getLogger("test_change_detection_project_filter")
    migration.run.return_value = ComponentResult(success=True)
    migration.should_skip_migration.return_value = (True, {"total_changes": 0})
    migration._get_current_entities_for_type.return_value = [{"key": "APP"}, {"key": "OTHER"}]
    return ChangeAwareRunner(migration), migration


def test_a_filtered_run_writes_no_snapshot_and_never_skips(monkeypatch):
    # The pilot case: a smoke run limited to one project recorded all 245 projects as the
    # baseline, so the next run found no changes and skipped the projects it never migrated.
    monkeypatch.setattr(config, "selected_jira_projects", lambda: ["APP"])
    runner, migration = _runner()
    result = runner.run("projects")
    assert result.success
    migration.run.assert_called_once_with()
    migration.should_skip_migration.assert_not_called()
    migration.create_snapshot.assert_not_called()


def test_an_unfiltered_run_still_skips_without_changes(monkeypatch):
    monkeypatch.setattr(config, "selected_jira_projects", list)
    runner, migration = _runner()
    runner.run("projects")
    migration.should_skip_migration.assert_called_once()
    migration.run.assert_not_called()


@pytest.mark.parametrize("scope", [["APP"], []])
def test_without_an_entity_type_the_migration_just_runs(monkeypatch, scope):
    monkeypatch.setattr(config, "selected_jira_projects", lambda: scope)
    runner, migration = _runner()
    runner.run(None)
    migration.run.assert_called_once_with()
    migration.should_skip_migration.assert_not_called()
