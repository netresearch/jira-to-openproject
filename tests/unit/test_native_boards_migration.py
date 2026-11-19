# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import native_boards
from src.application.components.native_boards_migration import NativeBoardsMigration
from src.application.components.registry import DEFAULT_COMPONENT_SEQUENCE, PREDEFINED_PROFILES
from src.config_loader import ConfigLoader
from src.migration import _build_component_factories, _component_status, _run_component
from src.migration_progress import _reported_counts

COLUMNS = [{"name": "Open", "statuses": [{"id": "1"}]}, {"name": "Done", "statuses": [{"id": "2"}]}]


class FakeTarget:
    """Accept board revisions the way the native writer does: create once, verify afterwards."""

    def __init__(self, failing_boards=()):
        self.payloads = []
        self.saved = set()
        self.failing_boards = set(failing_boards)

    def execute_script_with_data(self, _script, data, timeout=None):
        self.payloads.append(data)
        if data["board"] in self.failing_boards:
            return {"status": "error", "message": "Card provenance differs"}
        key = (data["archive"], data["revision"])
        created = key not in self.saved
        self.saved.add(key)
        return {"status": "success", "data": {"created": created, "grid_id": data["board"], "columns": 2, "cards": 1}}


def fake_jira(boards):
    jira = Mock(base_url="https://jira.invalid")
    jira.get_boards_for_project.side_effect = lambda key: [b for b in boards if b["project"] == key]
    jira.get_board_configuration.return_value = {"columnConfig": {"columns": COLUMNS}}
    jira.agile.ranked_board_issues.side_effect = lambda board_id, key: [
        {"id": board_id * 10, "key": f"{key}-{board_id}", "fields": {"status": {"id": "1"}}}
    ]
    return jira


@pytest.fixture
def configured(monkeypatch):
    mappings = {"project": {"APP": {"jira_key": "APP", "openproject_id": 4, "openproject_identifier": "app"}}}
    settings = {"native_board_archive_project": "board-archive"}
    monkeypatch.setattr("src.config.mappings", SimpleNamespace(get_mapping=mappings.get))
    monkeypatch.setattr("src.config.migration_config", settings)
    monkeypatch.setattr("src.config.jira_config", {})
    return mappings, settings


def component(jira, target):
    return NativeBoardsMigration(jira_client=jira, op_client=target)


@pytest.mark.parametrize("sequence", [DEFAULT_COMPONENT_SEQUENCE, PREDEFINED_PROFILES["full"]])
def test_full_profile_imports_boards_after_card_provenance(sequence):
    assert "native_boards" in sequence
    assert sequence.index("work_packages_skeleton") < sequence.index("native_boards")
    assert sequence.index("wp_metadata_backfill") < sequence.index("native_boards")


def test_factory_builds_the_board_component():
    factories = _build_component_factories(Mock(), Mock())
    assert isinstance(factories["native_boards"](), NativeBoardsMigration)


def test_repeat_run_verifies_the_same_revisions(configured):
    jira = fake_jira([{"id": 1, "name": "Scrum", "project": "APP"}, {"id": 2, "name": "Kanban", "project": "APP"}])
    target = FakeTarget()
    first = component(jira, target).run()
    second = component(jira, target).run()
    assert (first.success, first.total_count, first.success_count, first.failed_count) == (True, 2, 2, 0)
    assert (first.details["created"], first.details["verified"]) == (2, 0)
    assert (second.details["created"], second.details["verified"]) == (0, 2)
    assert target.payloads[:2] == target.payloads[2:]
    assert {(p["target"], p["archive"], p["project"]) for p in target.payloads} == {("app", "board-archive", "APP")}
    assert _component_status(second) == "completed"


def test_orchestrator_runs_the_component_and_publishes_its_counts(configured):
    jira = fake_jira([{"id": 1, "name": "Scrum", "project": "APP"}, {"id": 2, "name": "Kanban", "project": "APP"}])
    result = _run_component("native_boards", component(jira, FakeTarget()))
    assert result.success, result
    assert _reported_counts("native_boards", result) == {"Total items": 2, "Successful items": 2, "Failed items": 0}


def test_documented_config_setting_reaches_the_component(configured, tmp_path, monkeypatch):
    shipped = (Path(__file__).resolve().parents[2] / "config" / "config.yaml").read_text()
    example = '  # native_board_archive_project: "board-archive"\n'
    assert example in shipped
    path = tmp_path / "config.yaml"
    path.write_text(shipped.replace(example, example.replace("# ", "", 1)))
    settings = ConfigLoader(path).get_migration_config()
    assert settings["native_board_archive_project"] == "board-archive"
    monkeypatch.setattr("src.config.migration_config", settings)
    target = FakeTarget()
    component(fake_jira([{"id": 1, "name": "Scrum", "project": "APP"}]), target).run()
    assert [p["archive"] for p in target.payloads] == ["board-archive"]


def test_board_listing_failure_does_not_stop_other_projects(configured):
    mappings, _ = configured
    mappings["project"]["OPS"] = {"jira_key": "OPS", "openproject_id": 5, "openproject_identifier": "ops"}
    jira = fake_jira([{"id": 2, "name": "O", "project": "OPS"}])
    listing = jira.get_boards_for_project.side_effect
    jira.get_boards_for_project.side_effect = lambda key: (
        (_ for _ in ()).throw(OSError("down")) if key == "APP" else listing(key)
    )
    target = FakeTarget()
    result = component(jira, target).run()
    assert (result.success, result.success_count, result.failed_count) == (False, 1, 1)
    assert [p["project"] for p in target.payloads] == ["OPS"]


def test_board_failure_is_counted_and_other_boards_continue(configured):
    jira = fake_jira([{"id": 1, "name": "Scrum", "project": "APP"}, {"id": 2, "name": "Kanban", "project": "APP"}])
    result = component(jira, FakeTarget(failing_boards={1})).run()
    assert (result.success, result.success_count, result.failed_count) == (False, 1, 1)
    assert _component_status(result) == "failed"


def test_board_changing_during_capture_is_a_failure(configured):
    jira = fake_jira([{"id": 1, "name": "Scrum", "project": "APP"}])
    reads = iter([[{"id": 1, "key": "APP-1", "fields": {"status": {"id": "1"}}}], []])
    jira.agile.ranked_board_issues.side_effect = lambda *_: next(reads)
    target = FakeTarget()
    result = component(jira, target).run()
    assert (result.success, result.failed_count) == (False, 1)
    assert target.payloads == []


def test_skips_without_archive_project(configured):
    _, settings = configured
    settings.pop("native_board_archive_project")
    jira, target = fake_jira([{"id": 1, "name": "Scrum", "project": "APP"}]), FakeTarget()
    result = component(jira, target).run()
    assert result.success
    assert result.details["status"] == "skipped"
    assert result.details["reason"] == "native_board_archive_project_not_configured"
    assert _component_status(result) == "skipped"
    jira.get_boards_for_project.assert_not_called()
    assert target.payloads == []


def test_skips_without_migrated_projects(configured):
    mappings, _ = configured
    mappings["project"] = {}
    jira, target = fake_jira([{"id": 1, "name": "Scrum", "project": "APP"}]), FakeTarget()
    result = component(jira, target).run()
    assert (result.details["status"], result.details["reason"]) == ("skipped", "no_migrated_projects")
    jira.get_boards_for_project.assert_not_called()
    assert target.payloads == []


def test_project_filter_limits_the_boards(configured):
    mappings, settings = configured
    mappings["project"]["OPS"] = {"jira_key": "OPS", "openproject_id": 5, "openproject_identifier": "ops"}
    settings["jira_project_filter"] = ["OPS"]
    jira = fake_jira([{"id": 1, "name": "A", "project": "APP"}, {"id": 2, "name": "O", "project": "OPS"}])
    target = FakeTarget()
    result = component(jira, target).run()
    assert result.success_count == 1
    assert [(p["project"], p["target"]) for p in target.payloads] == [("OPS", "ops")]


def test_cli_project_filter_limits_the_boards(configured, monkeypatch):
    mappings, _ = configured
    mappings["project"]["OPS"] = {"jira_key": "OPS", "openproject_id": 5, "openproject_identifier": "ops"}
    monkeypatch.setattr("src.config.jira_config", {"projects": ["OPS"]})  # where --jira-project-filter lands
    jira = fake_jira([{"id": 1, "name": "A", "project": "APP"}, {"id": 2, "name": "O", "project": "OPS"}])
    target = FakeTarget()
    result = component(jira, target).run()
    assert result.success_count == 1
    assert [(p["project"], p["target"]) for p in target.payloads] == [("OPS", "ops")]


def test_project_without_identifier_is_a_failure(configured):
    mappings, _ = configured
    mappings["project"]["APP"].pop("openproject_identifier")
    jira, target = fake_jira([{"id": 1, "name": "Scrum", "project": "APP"}]), FakeTarget()
    result = component(jira, target).run()
    assert (result.success, result.failed_count) == (False, 1)
    jira.get_boards_for_project.assert_not_called()


def test_cli_writes_one_result_per_board(monkeypatch, capsys):
    jira = fake_jira([{"id": 1, "name": "Scrum", "project": "APP"}, {"id": 2, "name": "Kanban", "project": "APP"}])
    target = FakeTarget()
    monkeypatch.setattr("src.infrastructure.jira.jira_client.JiraClient", lambda: jira)
    monkeypatch.setattr("src.infrastructure.openproject.openproject_client.OpenProjectClient", lambda: target)
    native_boards.main(["--jira-project", "APP", "--target-project", "app", "--archive-project", "board-archive"])
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["grid_id"] for line in lines] == [1, 2]
    assert {(p["target"], p["archive"]) for p in target.payloads} == {("app", "board-archive")}


def test_cli_stops_when_a_board_changes_during_capture(monkeypatch):
    jira = fake_jira([{"id": 1, "name": "Scrum", "project": "APP"}])
    reads = iter([[{"id": 1, "key": "APP-1", "fields": {"status": {"id": "1"}}}], []])
    jira.agile.ranked_board_issues.side_effect = lambda *_: next(reads)
    monkeypatch.setattr("src.infrastructure.jira.jira_client.JiraClient", lambda: jira)
    monkeypatch.setattr("src.infrastructure.openproject.openproject_client.OpenProjectClient", FakeTarget)
    with pytest.raises(RuntimeError, match="changed during capture"):
        native_boards.main(["--jira-project", "APP", "--target-project", "app", "--archive-project", "board-archive"])
