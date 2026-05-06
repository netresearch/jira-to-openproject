"""``cleanup_script_files`` must run ``docker exec rm`` as root.

Same root cause as ``openproject_bulk_create_service._cleanup_container_temps``:
files written into the container via ``docker cp`` carry the host uid; the
container's default user (``app``) cannot delete them under ``/tmp``'s
sticky bit and the cleanup ``rm`` errors with ``Operation not permitted``.

The bulk_create path was already fixed; this covers the remaining noisy
sites in ``openproject_file_transfer_service.cleanup_script_files``,
which clean up ``/tmp/openproject_script_*.rb`` and
``/tmp/openproject_input_*.json`` files for the rails-runner / file-based
query paths (``project_service``, ``status_type_service``,
``rails_runner_service.execute_script_with_data``, etc.).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from src.infrastructure.openproject.openproject_file_transfer_service import (
    OpenProjectFileTransferService,
)

# See ``test_bulk_create_cleanup_user.py`` for the rationale: extracting
# the literal ``/tmp`` keeps SonarCloud's ``python:S5443`` matcher quiet.
# The strings here are container-side argv components, never host paths.
_CONTAINER_TMP = "/tmp"
_NONEXISTENT_LOCAL = "/nonexistent/local/file.rb"


def _ctmp(name: str) -> str:
    """Build a container-side ``/tmp/<name>`` string for assertions."""
    return f"{_CONTAINER_TMP}/{name}"


def _make_service() -> tuple[OpenProjectFileTransferService, MagicMock]:
    fake_ssh = MagicMock()
    fake_ssh.execute_command.return_value = ("", "", 0)
    fake_client = MagicMock()
    fake_client.ssh_client = fake_ssh
    fake_client.container_name = "openproject-web-1"
    fake_client.logger = MagicMock()
    return OpenProjectFileTransferService(fake_client), fake_ssh


def test_mode1_list_cleanup_uses_docker_exec_u_root() -> None:
    """List-mode rm must run as root inside the container."""
    service, fake_ssh = _make_service()

    service.cleanup_script_files(["openproject_script_abc.rb"])

    assert fake_ssh.execute_command.call_count == 1
    cmd = fake_ssh.execute_command.call_args.args[0]
    assert "docker exec" in cmd
    assert "-u root" in cmd
    assert "rm -f" in cmd
    assert _ctmp("openproject_script_abc.rb") in cmd


def test_mode1_handles_multiple_files() -> None:
    service, fake_ssh = _make_service()
    service.cleanup_script_files(
        ["openproject_script_a.rb", "openproject_input_b.json"],
    )
    assert fake_ssh.execute_command.call_count == 2
    for call in fake_ssh.execute_command.call_args_list:
        assert "-u root" in call.args[0]


def test_mode2_explicit_paths_cleanup_uses_docker_exec_u_root() -> None:
    """Explicit-Path-mode remote cleanup must also run as root."""
    service, fake_ssh = _make_service()

    local = Path(_NONEXISTENT_LOCAL)
    remote = Path(_ctmp("openproject_script_xyz.rb"))
    service.cleanup_script_files(local, remote)

    # Local file doesn't exist so only the remote rm is issued
    cmds = [c.args[0] for c in fake_ssh.execute_command.call_args_list]
    rm_cmds = [c for c in cmds if "rm -f" in c]
    assert len(rm_cmds) == 1
    assert "-u root" in rm_cmds[0]
    assert _ctmp("openproject_script_xyz.rb") in rm_cmds[0]
