# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Read actual Rails catalog rows with --live-docker and J2O_TEST_OPENPROJECT_CONTAINER."""

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.infrastructure.openproject.docker_client import DockerClient
from src.infrastructure.openproject.openproject_project_service import OpenProjectProjectService
from src.infrastructure.openproject.openproject_rails_runner_service import OpenProjectRailsRunnerService
from src.infrastructure.openproject.openproject_status_type_service import OpenProjectStatusTypeService

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


def test_catalog_exports_use_private_staging(tmp_path, monkeypatch):
    container = os.getenv("J2O_TEST_OPENPROJECT_CONTAINER")
    if not container:
        pytest.skip("A disposable OpenProject container is required")
    monkeypatch.setenv("J2O_FORCE_RAILS_RUNNER", "1")
    host = Mock()

    def execute(command, **kwargs):
        result = subprocess.run(["sh", "-c", command], capture_output=True, text=True, check=False, timeout=90)
        return result.stdout, result.stderr, result.returncode

    host.execute_command.side_effect = execute
    host.copy_file_to_remote.side_effect = shutil.copyfile
    host.check_remote_file_exists.side_effect = lambda path: Path(path).is_file()
    client = Mock()
    client.container_name = container
    client.file_manager.data_dir = tmp_path
    client.ssh_client = host
    client.docker_client = DockerClient(container_name=container, ssh_client=host)
    client.rails_runner = OpenProjectRailsRunnerService(client)

    statuses = OpenProjectStatusTypeService(client).get_statuses()
    assert statuses and all(row["id"] and row["name"] for row in statuses)
    projects = OpenProjectProjectService(client).get_projects()
    assert projects and all("public" in row and "enabled_modules" in row for row in projects)
    # The currently executing Ruby script and file must be in the same private directory.
    result = client.rails_runner.execute_large_query_to_json_file(
        "[File.dirname(__FILE__), File.stat(File.dirname(__FILE__)).mode & 0777]"
    )
    directory, mode = result
    assert mode == 0o700
    assert Path(directory).name.startswith("j2o-stage.")
    assert not client.docker_client.check_file_exists_in_container(directory)
