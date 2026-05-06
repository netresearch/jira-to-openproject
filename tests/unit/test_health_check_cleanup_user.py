"""``HealthCheckClient.cleanup_temp_files`` must run as root.

Same root cause as the other cleanup paths: ``find /tmp -name j2o_* -delete``
inside the OP container fails with EPERM for files transferred via
``docker cp`` (host uid) when run as the container's default user under
``/tmp``'s sticky bit. The end-of-run post-migration cleanup tripped on
this once per run, producing a single ``ssh_client.py:351`` ERROR with a
truncated traceback right before the success line.

Fix: add ``-u root`` to every ``docker exec`` in this method.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.infrastructure.health_check_client import HealthCheckClient


def _make_client() -> tuple[HealthCheckClient, MagicMock]:
    fake_ssh = MagicMock()
    fake_ssh.execute_command.return_value = ("0\n", "", 0)
    fake_docker = MagicMock()
    return (
        HealthCheckClient(
            ssh_client=fake_ssh,
            docker_client=fake_docker,
            container_name="openproject-web-1",
        ),
        fake_ssh,
    )


def test_cleanup_temp_files_runs_docker_exec_as_root() -> None:
    """Every docker exec invocation in cleanup_temp_files must use -u root."""
    hc, fake_ssh = _make_client()
    hc.cleanup_temp_files(pattern="j2o_*", max_age_minutes=5)

    docker_cmds = [c.args[0] for c in fake_ssh.execute_command.call_args_list]
    assert docker_cmds, "expected at least one docker exec invocation"
    for cmd in docker_cmds:
        assert "docker exec" in cmd
        assert "-u root" in cmd, f"docker exec missing '-u root': {cmd}"


def test_cleanup_temp_files_returns_count() -> None:
    """Sanity: a successful cleanup returns the right shape."""
    hc, fake_ssh = _make_client()
    fake_ssh.execute_command.side_effect = [
        ("3\n", "", 0),  # files_before
        ("", "", 0),  # delete
        ("0\n", "", 0),  # files_after
    ]
    result = hc.cleanup_temp_files(pattern="j2o_*", max_age_minutes=5)
    assert result.success
    assert result.files_before == 3
    assert result.files_after == 0
    assert result.files_removed == 3
