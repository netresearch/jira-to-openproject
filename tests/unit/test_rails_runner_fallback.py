# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""A Ruby error from the script surfaces as itself; only console transport failures reach rails runner."""

import json
from unittest.mock import MagicMock

import pytest

from src import config
from src.infrastructure.exceptions import QueryExecutionError
from src.infrastructure.openproject.openproject_bulk_create_service import OpenProjectBulkCreateService
from src.infrastructure.openproject.openproject_rails_runner_service import OpenProjectRailsRunnerService
from src.infrastructure.openproject.rails_console_client import ConsoleNotReadyError, RubyError

# The console error simpletasks hit in the live run before the runner masked it.
LIVE_ERROR = "Ruby error: RuntimeError: Stage checklist changes before granting project access"
GUARD = "Previous Rails execution did not finish; verify the target before retrying"


class FakeDocker:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def execute_command(self, command, timeout=None):
        self.commands.append(command)
        if command.startswith("env -u TMPDIR mktemp"):
            return "/tmp/j2o-stage.AbCdEfGhIj\n", "", 0
        if "rails runner" in command:
            # A fallback after the console started the script meets the once-only guard.
            return "", GUARD, 1
        return "", "", 0

    def transfer_file_to_container(self, source, destination) -> None:
        pass

    @property
    def runner_calls(self) -> list[str]:
        return [c for c in self.commands if "rails runner" in c]


@pytest.fixture
def service(monkeypatch, tmp_path):
    for name in ("J2O_FORCE_RAILS_RUNNER", "J2O_SCRIPT_LOAD_MODE", "J2O_SCRIPT_RUNNER_MAX_LINES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(config.migration_config, "enable_runner_fallback", True)
    client = MagicMock()
    client.docker_client = FakeDocker()
    client.file_manager.data_dir = str(tmp_path)
    client.container_name = "openproject-web-1"
    client.ssh_client.execute_command.return_value = (json.dumps({"ok": 1}), "", 0)
    return OpenProjectRailsRunnerService(client), client


def test_live_ruby_error_surfaces_instead_of_the_runner_guard(service):
    runner, client = service
    client.rails_client.execute.side_effect = RubyError(LIVE_ERROR)
    with pytest.raises(RubyError, match="Stage checklist changes before granting project access"):
        runner.execute_large_query_to_json_file("Project.count")
    assert client.docker_client.runner_calls == []


def test_ruby_error_in_a_short_console_script_is_not_retried(service, monkeypatch):
    monkeypatch.setenv("J2O_SCRIPT_RUNNER_MAX_LINES", "1000")
    monkeypatch.setenv("J2O_SCRIPT_RUNNER_THRESHOLD", "100000")
    runner, client = service
    client.rails_client.execute.side_effect = RubyError(LIVE_ERROR)
    with pytest.raises(RubyError, match="Stage checklist"):
        runner.execute_large_query_to_json_file("Project.count")
    assert client.docker_client.runner_calls == []


@pytest.mark.parametrize(
    ("console", "error"),
    [
        ({"side_effect": RubyError(LIVE_ERROR)}, RubyError),
        ({"return_value": "--EXEC_ERROR-- RuntimeError: boom"}, QueryExecutionError),
    ],
)
def test_ruby_error_in_a_catalog_read_is_not_retried(service, console, error):
    runner, client = service
    client.rails_client.execute.configure_mock(**console)
    with pytest.raises(error) as raised:
        runner.read_catalog("Project.all", "projects")
    assert GUARD not in str(raised.value)
    assert client.docker_client.runner_calls == []


def test_console_transport_failure_still_falls_back_to_rails_runner(service):
    runner, client = service
    client.docker_client.execute_command = MagicMock(
        side_effect=lambda command, timeout=None: (
            ("/tmp/j2o-stage.AbCdEfGhIj\n", "", 0) if command.startswith("env -u TMPDIR mktemp") else ("", "", 0)
        )
    )
    client.rails_client.execute.side_effect = ConsoleNotReadyError("Console could not be made ready")
    assert runner.execute_large_query_to_json_file("Project.count") == {"ok": 1}
    commands = [call.args[0] for call in client.docker_client.execute_command.call_args_list]
    assert any("rails runner" in command for command in commands)


def test_ruby_error_in_a_bulk_console_load_is_not_retried(monkeypatch, tmp_path):
    monkeypatch.setenv("J2O_ALLOW_RUNNER_FALLBACK", "1")
    monkeypatch.setenv("J2O_SCRIPT_LOAD_MODE", "console")
    client = MagicMock()
    client.docker_client = FakeDocker()
    client.rails_client.execute.side_effect = RubyError(LIVE_ERROR)
    bulk = OpenProjectBulkCreateService(client)
    monkeypatch.setattr(bulk, "_run_bulk_runner", MagicMock())
    with pytest.raises(QueryExecutionError, match="Stage checklist"):
        bulk._execute_bulk_script("puts 1\n" * 20, tmp_path, tmp_path / "result.json.progress", 30)
    bulk._run_bulk_runner.assert_not_called()
