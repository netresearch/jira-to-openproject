# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Exercise the generated bulk Ruby and the real OpenProject assignment error."""

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.infrastructure.openproject.docker_client import DockerClient
from src.infrastructure.openproject.openproject_bulk_create_service import (
    LegacyJournalReplayError,
    OpenProjectBulkCreateService,
)
from src.infrastructure.openproject.openproject_work_package_service import OpenProjectWorkPackageService
from tests.integration.test_openproject_agile_roundtrip import rails as rails


@pytest.mark.integration
@pytest.mark.requires_docker
def test_invalid_type_attribute_leaves_no_partial_record(tmp_path, monkeypatch):
    container = os.getenv("J2O_TEST_OPENPROJECT_CONTAINER")
    if not container:
        pytest.skip("A disposable OpenProject container is required")
    monkeypatch.setenv("J2O_SCRIPT_LOAD_MODE", "runner")
    monkeypatch.setenv("J2O_BULK_RESULT_WAIT_SECONDS", "10")
    host = Mock()

    def execute(command, **kwargs):
        result = subprocess.run(["sh", "-c", command], capture_output=True, text=True, check=False, timeout=120)
        return result.stdout, result.stderr, result.returncode

    host.execute_command.side_effect = execute
    host.copy_file_from_remote.side_effect = shutil.copyfile
    host.copy_file_to_remote.side_effect = shutil.copyfile
    host.check_remote_file_exists.side_effect = lambda path: Path(path).is_file()
    client = Mock()
    client.file_manager.data_dir = tmp_path
    client.docker_client = DockerClient(container_name=container, ssh_client=host)
    client.transfer_file_to_container = client.docker_client.transfer_file_to_container
    client.transfer_file_from_container = client.docker_client.copy_file_from_container
    fixture = f"j2o-assignment-{uuid.uuid4().hex}"
    service = OpenProjectBulkCreateService(client)
    result = service.bulk_create_records("Type", [{"name": fixture, "j2o_unknown_attribute": True}])
    assert result["created_count"] == 0
    assert result["error_count"] == 1
    assert result["errors"][0]["index"] == 0
    assert "j2o_unknown_attribute" in result["errors"][0]["errors"][0]
    readback = subprocess.run(
        ["docker", "exec", container, "bundle", "exec", "rails", "runner", f"puts Type.where(name: '{fixture}').count"],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert readback.stdout.strip().splitlines()[-1] == "0"


@pytest.mark.integration
@pytest.mark.requires_docker
def test_noop_replay_allows_updates_and_pending_replay_preserves_existing_journals(rails):
    client, query = rails
    key = query("WorkPackage.first!.id")
    work_packages = OpenProjectWorkPackageService(client)
    for operations in (None, []):
        result = work_packages.update_work_package(key, {"_rails_operations": operations, "subject": "Fixture subject"})
        assert result["status"] == "updated", result
    before = query(f"WorkPackage.find({key}).journals.order(:id).pluck(:id, :version, :notes)")
    assert before
    changes = [
        {"id": key, "subject": "must not be applied"},
        {"id": key, "_rails_operations": [{"type": "add_journal", "notes": "held marker"}]},
    ]
    with pytest.raises(LegacyJournalReplayError):
        work_packages.batch_update_work_packages(changes)
    assert query(f"WorkPackage.find({key}).subject") == "Fixture subject"
    assert query(f"WorkPackage.find({key}).journals.order(:id).pluck(:id, :version, :notes)") == before
