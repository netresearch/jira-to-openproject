"""Unit tests for OpenProjectFileTransferService.

Focused regression tests for the bug Copilot caught in PR #113:
``cleanup_script_files`` mode 2 (``(local_path, remote_path)`` form) ran
``rm -f /path`` directly via ``ssh_client.execute_command`` — which
executes on the *host*, not inside the Docker container. For container
paths (``/tmp/...``) the temp file would never be cleaned up, leading to
``/tmp`` accumulation on the container side.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.infrastructure.openproject.openproject_file_transfer_service import OpenProjectFileTransferService


@pytest.fixture
def service() -> OpenProjectFileTransferService:
    client = MagicMock()
    client.logger = MagicMock()
    client.container_name = "openproject-web-1"
    client.ssh_client = MagicMock()
    client.ssh_client.execute_command = MagicMock()
    return OpenProjectFileTransferService(client)


class TestCleanupScriptFilesMode2:
    """Mode 2 (``(local_path, remote_path)``) must use docker exec for the remote side."""

    def test_remote_cleanup_uses_docker_exec(self, service: OpenProjectFileTransferService, tmp_path: Path) -> None:
        local_file = tmp_path / "script.rb"
        local_file.write_text("puts 'hi'", encoding="utf-8")
        remote_file = Path("/tmp/openproject_script_xyz.rb")

        service.cleanup_script_files(local_file, remote_file)

        # Local cleanup
        assert not local_file.exists(), "local script file should have been unlinked"

        # Remote cleanup MUST go through docker exec, not raw ssh `rm`.
        ssh_calls = service._client.ssh_client.execute_command.call_args_list
        assert len(ssh_calls) == 1, f"expected 1 ssh execute_command call, got {len(ssh_calls)}"
        cmd = ssh_calls[0][0][0]
        assert cmd.startswith("docker exec "), (
            f"remote cleanup must use docker exec to reach the container; got: {cmd!r}"
        )
        assert "openproject-web-1" in cmd, "container name should be in the command"
        assert "/tmp/openproject_script_xyz.rb" in cmd, "remote path should be in the command"
        assert "rm -f" in cmd, "should use rm -f for idempotent cleanup"

    def test_local_only_no_ssh_call(self, service: OpenProjectFileTransferService, tmp_path: Path) -> None:
        """If remote_path is None, only the local file is cleaned up."""
        local_file = tmp_path / "script.rb"
        local_file.write_text("puts 'hi'", encoding="utf-8")

        service.cleanup_script_files(local_file, None)

        assert not local_file.exists()
        service._client.ssh_client.execute_command.assert_not_called()

    def test_remote_path_is_shell_quoted(self, service: OpenProjectFileTransferService, tmp_path: Path) -> None:
        """A remote path with shell metacharacters must be safely quoted."""
        local_file = tmp_path / "script.rb"
        local_file.write_text("x", encoding="utf-8")
        # Path containing a space (a tame shell-meta character; ; and $ are
        # blocked by Path's normalisation but space round-trips).
        remote_file = Path("/tmp/dir with spaces/file.rb")

        service.cleanup_script_files(local_file, remote_file)

        cmd = service._client.ssh_client.execute_command.call_args[0][0]
        # shlex.quote either single-quotes or escapes; either way the path
        # should not appear unquoted.
        assert "/tmp/dir with spaces/file.rb" not in cmd or "'/tmp/dir with spaces/file.rb'" in cmd, (
            f"remote path should be shell-quoted; got: {cmd!r}"
        )


class TestCleanupScriptFilesMode1:
    """Mode 1 (list-of-filenames) was already correct; smoke-test it stays that way."""

    def test_list_mode_uses_docker_exec(self, service: OpenProjectFileTransferService) -> None:
        service.cleanup_script_files(["script1.rb", "script2.rb"])
        calls = service._client.ssh_client.execute_command.call_args_list
        assert len(calls) == 2
        for call in calls:
            cmd = call[0][0]
            assert cmd.startswith("docker exec "), f"list-mode must use docker exec; got: {cmd!r}"
            assert "rm -f" in cmd
