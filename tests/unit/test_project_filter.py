# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""One effective Jira project filter for every component that selects projects."""

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import config
from src.application.components.project_migration import ProjectMigration
from tests.helpers.cli_project_filter import apply_cli_project_filter

SRC = Path(__file__).resolve().parents[2] / "src"


@pytest.fixture
def no_filter(monkeypatch):
    monkeypatch.setattr(config, "_cli_args", {})
    monkeypatch.setitem(config.jira_config, "projects", [])
    monkeypatch.delitem(config.migration_config, "jira_project_filter", raising=False)


def test_live_invocation_selects_only_its_project(monkeypatch):
    # j2o migrate --profile full --jira-project-filter NR created all 245 projects.
    apply_cli_project_filter(monkeypatch, "NR")
    assert config.selected_jira_projects() == ["NR"]


@pytest.mark.usefixtures("no_filter")
def test_cli_value_wins_over_both_config_keys(monkeypatch):
    monkeypatch.setitem(config.migration_config, "jira_project_filter", ["MIG"])
    apply_cli_project_filter(monkeypatch, "cli, OTHER ,cli")
    monkeypatch.setitem(config.jira_config, "projects", ["YAML"])
    assert config.selected_jira_projects() == ["CLI", "OTHER"]


@pytest.mark.usefixtures("no_filter")
def test_jira_projects_win_over_the_migration_key(monkeypatch):
    monkeypatch.setitem(config.jira_config, "projects", ["yaml"])
    monkeypatch.setitem(config.migration_config, "jira_project_filter", "MIG")
    assert config.selected_jira_projects() == ["YAML"]


@pytest.mark.usefixtures("no_filter")
def test_migration_key_applies_alone(monkeypatch):
    monkeypatch.setitem(config.migration_config, "jira_project_filter", "a,b")
    assert config.selected_jira_projects() == ["A", "B"]


@pytest.mark.usefixtures("no_filter")
def test_no_filter_selects_every_project():
    assert config.selected_jira_projects() == []


def test_project_migration_skips_unselected_jira_projects(monkeypatch):
    apply_cli_project_filter(monkeypatch, "NR")
    migration = ProjectMigration(MagicMock(), MagicMock())
    assert migration._plan_project({"key": "OTHER", "name": "Other"}, {}, {}) is None
    existing = {"id": 7, "name": "NR", "identifier": "nr"}
    assert migration._plan_project({"key": "NR", "name": "NR"}, {}, {"nr": existing})[1] == existing


def test_only_the_config_module_reads_the_filter_keys():
    pattern = re.compile(r"""jira_project_filter|jira_config\b[^\n]*["']projects["']""")
    allowed = {SRC / "config" / "__init__.py", SRC / "main.py"}
    readers = sorted(
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if path not in allowed and pattern.search(path.read_text(encoding="utf-8"))
    )
    assert readers == []
