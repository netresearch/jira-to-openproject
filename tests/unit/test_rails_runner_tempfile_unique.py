"""Issue #260: the Rails query tempfile name must be unique per call.

``execute_query_to_json_file`` writes the Ruby result to a container tempfile
and reads it back. The name was ``/tmp/j2o_query_{int(time.time())}_{pid}.json``
— second-resolution time plus PID, with no random component. Two calls landing
in the same wall-clock second in the same process produced the *same* path, so
a later call could read back the *previous* call's stale JSON (e.g. a groups
list where a project was expected → "Unexpected response when ensuring
reporting project"). A per-call random token makes the path collision-free.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.infrastructure.openproject.openproject_rails_runner_service import (
    OpenProjectRailsRunnerService,
)


def _service_capturing_container_path() -> OpenProjectRailsRunnerService:
    client = MagicMock()
    client.logger = MagicMock()
    # Echo back the container_file the service generated so the test can inspect it.
    client.execute_large_query_to_json_file.side_effect = lambda query, container_file, timeout=None: container_file
    return OpenProjectRailsRunnerService(client)


def test_same_second_calls_get_unique_container_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as _time

    # Freeze wall-clock to a single second to force the historically-colliding case.
    monkeypatch.setattr(_time, "time", lambda: 1000.0)

    svc = _service_capturing_container_path()
    path1 = svc.execute_query_to_json_file("query one")
    path2 = svc.execute_query_to_json_file("query two")

    assert "j2o_query_" in path1
    assert path1.endswith(".json")
    assert path1 != path2, "same-second calls must not collide on the container tempfile path"
