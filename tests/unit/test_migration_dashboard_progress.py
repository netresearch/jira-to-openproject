# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Exercise dashboard publication through the regular migration command, without external services."""

import argparse
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from src import main, migration
from src.models import ComponentResult
from src.utils.change_aware_runner import ChangeAwareRunner
from src.utils.dashboard_progress import read_progress


@pytest.fixture
def local_run(monkeypatch, tmp_path):
    """Replace remote services while retaining the CLI, component invocation, and stop decisions."""
    target = tmp_path / "progress.json"
    monkeypatch.setenv("J2O_DASHBOARD_PROGRESS_FILE", str(target))
    monkeypatch.setattr(migration.config, "migration_config", {"no_backup": True})
    monkeypatch.setattr(migration.config, "mappings", Mock())
    health = Mock()
    health.run_pre_migration_checks.return_value = (True, [])
    health.run_during_migration_check.return_value = {"healthy": True}
    clients = Mock(return_value=(Mock(), Mock(), health))
    monkeypatch.setattr(migration, "_create_migration_clients", clients)
    monkeypatch.setattr(migration.data_handler, "save_results", Mock())
    monkeypatch.setattr(main, "_ensure_singleton_lock", Mock())
    factories = {}
    monkeypatch.setattr(migration, "_build_component_factories", lambda **_: factories)
    return SimpleNamespace(target=target, factories=factories, clients=clients, health=health)


def run_command(*names, stop_on_error=False, no_confirm=True):
    args = argparse.Namespace(
        components=list(names),
        profile=None,
        restore=None,
        tmux=False,
        stop_on_error=stop_on_error,
        no_confirm=no_confirm,
    )
    with pytest.raises(SystemExit) as caught:
        main._run_migrate_command(args)
    return caught.value.code


def test_cli_publishes_observed_component_boundaries_and_counts(local_run) -> None:
    active = []

    def run_skeleton():
        active.append(read_progress(local_run.target))
        return ComponentResult(
            success=True,
            details={"total_created": 3, "total_skipped": 0, "total_failed": "unknown", "notes": "private-text"},
        )

    def run_content():
        active.append(read_progress(local_run.target))
        return ComponentResult(success=True, details={"comments_migrated": 2, "comments_held": 1})

    local_run.factories.update(
        work_packages_skeleton=lambda: SimpleNamespace(run=run_skeleton),
        work_packages_content=lambda: SimpleNamespace(run=run_content),
    )
    assert run_command("work_packages_skeleton", "work_packages_content") == 0
    first, second = active
    assert first.status == "running"
    assert first.current_component == "work_packages_skeleton"
    assert [phase.status for phase in first.phases] == ["running", "pending"]
    assert [phase.status for phase in second.phases] == ["completed", "running"]
    assert second.current_component == "work_packages_content"
    assert second.verified_counts == {
        "work_packages_skeleton · Created work packages": 3,
        "work_packages_skeleton · Skipped work packages": 0,
    }
    finished = read_progress(local_run.target)
    assert finished.migration_id == first.migration_id == second.migration_id
    assert finished.status == "completed"
    assert finished.current_component is None
    assert all(phase.started_at <= phase.finished_at for phase in finished.phases)
    assert (
        finished.start_time <= first.last_update <= second.last_update <= finished.finished_at <= finished.last_update
    )
    assert finished.verified_counts == {
        **second.verified_counts,
        "work_packages_content · Migrated comments": 2,
        "work_packages_content · Held comments": 1,
    }
    assert finished.total_entities is None
    assert finished.processed_entities is None
    assert "private-text" not in local_run.target.read_text()


def test_failed_component_leaves_unstarted_work_pending_and_excludes_errors(local_run) -> None:
    private = "sensitive diagnostic from upstream"
    local_run.factories["labels"] = lambda: SimpleNamespace(run=Mock(side_effect=RuntimeError(private)))
    local_run.factories["versions"] = Mock()
    assert run_command("labels", "versions", stop_on_error=True) == 1
    failed = read_progress(local_run.target)
    assert failed.status == "failed"
    assert [phase.status for phase in failed.phases] == ["failed", "pending"]
    assert failed.phases[1].started_at is None
    assert failed.current_component is None
    assert failed.verified_counts == {}
    assert private not in local_run.target.read_text()
    local_run.factories["versions"].assert_not_called()


def test_continue_after_error_preserves_the_first_failure(local_run) -> None:
    local_run.factories["labels"] = lambda: SimpleNamespace(run=lambda: ComponentResult(success=False, failed_count=2))
    local_run.factories["versions"] = lambda: SimpleNamespace(run=lambda: ComponentResult(success=True))
    assert run_command("labels", "versions") == 1
    finished = read_progress(local_run.target)
    assert finished.status == "failed"
    assert [phase.status for phase in finished.phases] == ["failed", "completed"]
    assert finished.verified_counts == {"labels · Failed items": 2}


@pytest.mark.parametrize("failure", ["health", "factory", "finalization"])
def test_setup_and_finalization_errors_close_the_run(local_run, monkeypatch, failure) -> None:
    local_run.factories["labels"] = lambda: SimpleNamespace(run=lambda: ComponentResult(success=True))
    if failure == "health":
        local_run.health.run_pre_migration_checks.return_value = (False, ["CRITICAL private-text"])
    elif failure == "factory":
        local_run.factories["labels"] = Mock(side_effect=RuntimeError("private-text"))
    else:
        monkeypatch.setattr(migration.data_handler, "save_results", Mock(side_effect=OSError("private-text")))
    assert run_command("labels") == 1
    snapshot = read_progress(local_run.target)
    assert snapshot.status == "failed"
    assert snapshot.finished_at
    assert snapshot.current_component is None
    assert [phase.status for phase in snapshot.phases] == {
        "health": [],
        "factory": ["failed"],
        "finalization": ["completed"],
    }[failure]
    assert "private-text" not in local_run.target.read_text()


def test_refusing_to_continue_preserves_completed_and_pending_phases(monkeypatch, local_run) -> None:
    local_run.factories.update(
        labels=lambda: SimpleNamespace(run=lambda: ComponentResult(success=True)), versions=Mock()
    )
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert run_command("labels", "versions", no_confirm=False) == 1
    snapshot = read_progress(local_run.target)
    assert snapshot.status == "paused"
    assert [phase.status for phase in snapshot.phases] == ["completed", "pending"]


def test_unsafe_dry_run_is_reported_before_any_client_opens(local_run, monkeypatch) -> None:
    monkeypatch.setattr(migration.config, "migration_config", {"dry_run": True, "no_backup": True})
    with pytest.raises(SystemExit, match="Aborting --dry-run"):
        asyncio.run(migration.run_migration(components=["work_packages_skeleton"]))
    snapshot = read_progress(local_run.target)
    assert snapshot.status == "failed"
    assert snapshot.migration_id.startswith("dry-run-")
    local_run.clients.assert_not_called()


def test_disabled_or_broken_destination_does_not_affect_migration(local_run, monkeypatch) -> None:
    local_run.factories["labels"] = lambda: SimpleNamespace(run=lambda: ComponentResult(success=True))
    monkeypatch.delenv("J2O_DASHBOARD_PROGRESS_FILE")
    local_run.target.write_text("previous generation")
    assert run_command("labels") == 0
    assert local_run.target.read_text() == "previous generation"
    monkeypatch.setenv("J2O_DASHBOARD_PROGRESS_FILE", str(local_run.target / "invalid"))
    assert run_command("labels") == 0


def test_unrecognized_component_does_not_claim_migration_or_disclose_input(local_run) -> None:
    assert run_command("private-text") == 0
    snapshot = read_progress(local_run.target)
    assert snapshot.status == "pending"
    assert snapshot.phases == []
    assert snapshot.pending_checks
    assert "private-text" not in local_run.target.read_text()


def test_change_aware_skip_and_repeat_attempts_remain_distinguishable(local_run) -> None:
    no_changes = MagicMock()
    no_changes.should_skip_migration.return_value = (True, None)
    skipped = SimpleNamespace(run=lambda: ChangeAwareRunner(no_changes).run("labels"))
    updated = SimpleNamespace(run=lambda: ComponentResult(success=True, updated=4))
    local_run.factories["labels"] = Mock(side_effect=[skipped, updated])
    assert run_command("labels", "labels") == 0
    no_changes.run.assert_not_called()
    snapshot = read_progress(local_run.target)
    assert [(phase.name, phase.status) for phase in snapshot.phases] == [
        ("labels", "skipped"),
        ("labels (attempt 2)", "completed"),
    ]
    assert snapshot.verified_counts["labels · Successful items"] == 0
    assert snapshot.verified_counts["labels (attempt 2) · Updated items"] == 4


def test_simulation_counts_are_excluded_from_verified_counts(local_run, monkeypatch) -> None:
    monkeypatch.setattr(migration.config, "migration_config", {"no_backup": True, "dry_run": True})
    local_run.factories["projects"] = lambda: SimpleNamespace(
        run=lambda: ComponentResult(success=True, success_count=32)
    )
    assert run_command("projects") == 0
    snapshot = read_progress(local_run.target)
    assert snapshot.status == "completed"
    assert snapshot.migration_id.startswith("dry-run-")
    assert snapshot.verified_counts == {}


def test_interruption_in_active_component_is_paused(local_run) -> None:
    local_run.factories["labels"] = lambda: SimpleNamespace(run=Mock(side_effect=KeyboardInterrupt))
    assert run_command("labels") == 1
    snapshot = read_progress(local_run.target)
    assert snapshot.status == "paused"
    assert snapshot.phases[0].status == "paused"


def test_new_invocation_replaces_previous_snapshot_and_counters(local_run) -> None:
    local_run.factories["labels"] = lambda: SimpleNamespace(run=lambda: ComponentResult(success=True, updated=5))
    assert run_command("labels") == 0
    previous = read_progress(local_run.target)
    local_run.factories["versions"] = lambda: SimpleNamespace(run=lambda: ComponentResult(success=True))
    assert run_command("versions") == 0
    snapshot = read_progress(local_run.target)
    assert snapshot.migration_id != previous.migration_id
    assert [phase.name for phase in snapshot.phases] == ["versions"]
    assert snapshot.verified_counts == {}


@pytest.mark.parametrize(
    ("errors", "expected_attempts", "expected_file_status"),
    [
        ([OSError("transient")], 2, "completed"),
        ([OSError("unavailable")] * 3, 3, "running"),
        ([ValueError("invalid")], 1, "running"),
    ],
)
def test_only_a_terminal_io_failure_retries_the_same_report(
    local_run, monkeypatch, errors, expected_attempts, expected_file_status
) -> None:
    from src import migration_progress

    real_write = migration_progress.write_progress
    terminal_timestamps = []
    running_attempts = []
    component = Mock(return_value=ComponentResult(success=True, updated=1))
    local_run.factories["labels"] = lambda: SimpleNamespace(run=component)

    def intermittent(snapshot, *, path):
        if snapshot.get("finished_at"):
            terminal_timestamps.append(snapshot["last_update"])
            if len(terminal_timestamps) <= len(errors):
                raise errors[len(terminal_timestamps) - 1]
        elif snapshot.get("current_component"):
            running_attempts.append(snapshot["last_update"])
            raise OSError("temporary loss of running state")
        real_write(snapshot, path=path)

    monkeypatch.setattr(migration_progress, "write_progress", intermittent)
    monkeypatch.setattr(migration_progress, "sleep", Mock())
    assert run_command("labels") == 0
    component.assert_called_once()
    assert len(running_attempts) == 1
    assert len(terminal_timestamps) == expected_attempts
    assert len(set(terminal_timestamps)) == 1
    assert read_progress(local_run.target).status == expected_file_status


def test_cli_comparison_requires_complete_scope_and_replaces_previous_attempt(local_run):
    results = iter(
        [
            {"DEMO": {"source": 2, "target": 2}, "OTHER": {"source": 1, "target": 1}},
            {"DEMO": {"source": 2, "target": 2}, "OTHER": {"source": 1, "actor": "private-text"}},
        ]
    )
    label = "Jira worklogs on selected issues"
    local_run.factories["time_entries"] = lambda: SimpleNamespace(
        run=lambda: ComponentResult(success=True, success_count=99, details={"comparison_by_project": next(results)})
    )
    local_run.factories["attachments"] = lambda: SimpleNamespace(
        run=lambda: ComponentResult(
            success=True, updated=99, details={"comparison_by_project": {"DEMO": {"source": 0}, "OTHER": {}}}
        )
    )
    assert run_command("time_entries", "attachments", "time_entries") == 0
    result = read_progress(local_run.target)
    assert result.comparison_counts[label].source == 3
    assert result.comparison_counts[label].target is None
    assert result.project_comparisons["DEMO"][label].target == 2
    assert result.project_comparisons["OTHER"][label].target is None
    attachment_label = "Attachments on selected issues (target SHA-256 verified)"
    assert attachment_label not in result.comparison_counts
    assert result.project_comparisons["DEMO"][attachment_label].source == 0
    assert result.project_comparisons["DEMO"][attachment_label].target is None
    assert "private-text" not in local_run.target.read_text()


def test_cli_dry_result_never_publishes_comparisons(local_run):
    local_run.factories["time_entries"] = lambda: SimpleNamespace(
        run=lambda: ComponentResult(
            success=True, dry_run=True, details={"comparison_by_project": {"DEMO": {"source": 6, "target": 6}}}
        )
    )
    assert run_command("time_entries") == 0
    result = read_progress(local_run.target)
    assert result.comparison_counts == {}
    assert result.project_comparisons == {}


def test_receipts_exclude_target_collisions_across_projects():
    from src.utils.scoped_receipts import ScopedReceipts

    observed = ScopedReceipts(["ONE-1", "TWO-1"])
    observed.observe("ONE-1", ["1"])
    observed.observe("TWO-1", ["2"])
    observed.confirm("ONE-1", "1", 7)
    observed.confirm("TWO-1", "2", 7)
    assert observed.counts() == {"ONE": {"source": 1}, "TWO": {"source": 1}}


def test_cli_global_dry_run_never_publishes_source_or_target(local_run, monkeypatch):
    monkeypatch.setattr(
        migration.config, "migration_config", {"dry_run": True, "no_backup": True, "allow_unsafe_dry_run": True}
    )
    local_run.factories["time_entries"] = lambda: SimpleNamespace(
        run=lambda: ComponentResult(
            success=True, details={"comparison_by_project": {"DEMO": {"source": 6, "target": 6}}}
        )
    )
    assert run_command("time_entries") == 0
    result = read_progress(local_run.target)
    assert result.migration_id.startswith("dry-run-")
    assert result.comparison_counts == {}
    assert result.project_comparisons == {}
