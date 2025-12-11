import json
import os
import re
from pathlib import Path

from src.clients.openproject_client import OpenProjectClient


class FakeRailsConsoleClient:
    def __init__(self) -> None:
        self.last_script: str | None = None

    def execute(self, script: str, timeout: int = 30, suppress_output: bool = False) -> str:
        self.last_script = script
        # Extract data_path and result_path assigned in Ruby header
        m_data = re.search(r"data_path\s*=\s*'([^']+)'", script)
        m_result = re.search(r"result_path\s*=\s*'([^']+)'", script)
        data_path = m_data.group(1) if m_data else None
        result_path = m_result.group(1) if m_result else None
        # Generate a simple success result written to the container file
        created = 0
        results: list[dict] = []
        if data_path and os.path.exists(data_path):
            with open(data_path, encoding="utf-8") as f:
                entries = json.load(f)
            created = len(entries)
            for i, _ in enumerate(entries):
                results.append({"index": i, "success": True, "id": 1000 + i})
        payload = {"created": created, "failed": 0, "results": results}
        if result_path:
            # Ensure parent dir exists (it is /tmp inside host during tests)
            Path(result_path).parent.mkdir(parents=True, exist_ok=True)
            with open(result_path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
        return ""


class FakeDockerClient:
    def __init__(self) -> None:
        pass

    # Simulate copying to container by copying to target path on host FS
    def transfer_file_to_container(self, local_path: Path, container_path: Path) -> None:
        Path(container_path).parent.mkdir(parents=True, exist_ok=True)
        with open(local_path, "rb") as src, open(container_path, "wb") as dst:
            dst.write(src.read())

    # Simulate copying from container by copying from source path on host FS
    def copy_file_from_container(self, container_path: Path, local_path: Path) -> Path:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        with open(container_path, "rb") as src, open(local_path, "wb") as dst:
            dst.write(src.read())
        return local_path


class FakeSSHClient:
    def __init__(self) -> None:
        pass


def make_client(tmp_path: Path) -> OpenProjectClient:
    # Point data dir used by FileManager to tmp_path/var/data
    os.environ["J2O_DATA_DIR"] = str(tmp_path / "var" / "data")
    (tmp_path / "var" / "data").mkdir(parents=True, exist_ok=True)
    client = OpenProjectClient(
        container_name="test-container",
        ssh_host="localhost",
        ssh_user="tester",
        ssh_client=FakeSSHClient(),
        docker_client=FakeDockerClient(),
        rails_client=FakeRailsConsoleClient(),
    )
    return client


def test_batch_create_time_entries_file_based(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    entries = [
        {
            "_embedded": {
                "workPackage": {"href": "/api/v3/work_packages/123"},
                "user": {"href": "/api/v3/users/456"},
                "activity": {"href": "/api/v3/time_entries/activities/789"},
            },
            "hours": 0.25,
            "spentOn": "2024-01-02",
            "comment": {"raw": "batch test"},
            "_meta": {"jira_worklog_key": "JWL-1"},
        },
    ]

    result = client.batch_create_time_entries(entries)

    assert isinstance(result, dict)
    assert result.get("created") == 1
    assert result.get("failed") == 0
    assert result.get("results") and result["results"][0]["success"] is True

    # Verify our runner script included logged_by assignment
    rails = client.rails_client  # type: ignore[attr-defined]
    assert hasattr(rails, "last_script") and "logged_by_id" in rails.last_script
