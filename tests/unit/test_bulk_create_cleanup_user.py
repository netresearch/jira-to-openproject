"""Container ``/tmp`` cleanup must run as root.

When ``OpenProjectBulkCreateService.bulk_create_records`` finishes, it cleans
up the input JSON, the result JSON, and the progress file inside the
container's ``/tmp``. The input JSON was uploaded via ``docker cp`` which
preserves the host uid (which doesn't map to the container's ``app`` user).
Combined with ``/tmp``'s sticky bit, ``app`` cannot delete it — yielding
``rm: cannot remove '/tmp/user_bulk_*.json': Operation not permitted`` after
every batch (~28 batches x per-run = >100 noisy ERROR lines in production).

Running the cleanup as ``root`` (via ``user="root"`` on
``docker_client.execute_command``) lets the rm succeed regardless of who owns
the file.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from src.infrastructure.openproject.openproject_bulk_create_service import (
    OpenProjectBulkCreateService,
)


def _make_service_with_fake_docker() -> tuple[OpenProjectBulkCreateService, MagicMock]:
    fake_docker = MagicMock()
    fake_docker.execute_command.return_value = ("", "", 0)
    fake_client = MagicMock()
    fake_client.docker_client = fake_docker
    fake_client.logger = MagicMock()
    return OpenProjectBulkCreateService(fake_client), fake_docker


def test_cleanup_container_temps_runs_as_root() -> None:
    """The cleanup helper must pass ``user="root"`` on every rm call."""
    service, fake_docker = _make_service_with_fake_docker()

    service._cleanup_container_temps(
        service._client,
        (Path("/tmp/user_bulk_abc.json"),),
    )

    assert fake_docker.execute_command.call_count == 1
    call_args, call_kwargs = fake_docker.execute_command.call_args
    rm_cmd = call_args[0]
    assert "rm -f" in rm_cmd
    assert "/tmp/user_bulk_abc.json" in rm_cmd
    assert call_kwargs.get("user") == "root"


def test_cleanup_container_temps_iterates_all_paths() -> None:
    """Each path in the tuple gets its own rm call."""
    service, fake_docker = _make_service_with_fake_docker()

    service._cleanup_container_temps(
        service._client,
        (
            Path("/tmp/user_bulk_a.json"),
            Path("/tmp/bulk_result_user_b.json"),
            Path("/tmp/bulk_result_user_b.json.progress"),
        ),
    )

    assert fake_docker.execute_command.call_count == 3
    for call in fake_docker.execute_command.call_args_list:
        assert call.kwargs.get("user") == "root"


def test_cleanup_swallows_per_path_failures() -> None:
    """A failed rm on one path must not block the rest — best-effort."""
    service, fake_docker = _make_service_with_fake_docker()

    fake_docker.execute_command.side_effect = [
        Exception("boom"),
        ("", "", 0),
    ]

    service._cleanup_container_temps(
        service._client,
        (Path("/tmp/a.json"), Path("/tmp/b.json")),
    )

    assert fake_docker.execute_command.call_count == 2
