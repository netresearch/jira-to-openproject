# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Round-trip through Docker with the SSH host represented by the local test machine."""

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.infrastructure.openproject.docker_client import DockerClient

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


def test_private_transfer_roundtrip(tmp_path):
    container = os.environ.get("J2O_TEST_OPENPROJECT_CONTAINER")
    if not container:
        pytest.skip("Set J2O_TEST_OPENPROJECT_CONTAINER to a disposable container")
    host = Mock()

    def execute(command, **kwargs):
        completed = subprocess.run(["sh", "-c", command], capture_output=True, text=True, check=False, timeout=30)
        return completed.stdout, completed.stderr, completed.returncode

    stages = []

    def copy(source, dest):
        stage = Path(source if str(dest).startswith(str(tmp_path)) else dest).parent
        assert stage.stat().st_mode & 0o777 == 0o700
        stages.append(stage)
        return Path(shutil.copyfile(source, dest))

    host.execute_command.side_effect = execute
    host.copy_file_to_remote.side_effect = copy
    host.copy_file_from_remote.side_effect = copy
    host.check_remote_file_exists.side_effect = lambda path: Path(path).is_file()
    client = DockerClient(container_name=container, ssh_client=host)
    directory, _, status = client.execute_command("mktemp -d")
    assert status == 0
    target = Path(directory.strip()) / "sample"
    original = tmp_path / "source"
    original.write_bytes(bytes(range(256)))
    try:
        client.transfer_file_to_container(original, target)
        downloaded = client.copy_file_from_container(target, tmp_path / "download")
        assert downloaded.read_bytes() == original.read_bytes()
        assert len(set(stages)) == 2
        assert all(not stage.exists() for stage in stages)
    finally:
        client.execute_command(f"rm -f -- {shlex.quote(str(target))} && rmdir -- {shlex.quote(str(target.parent))}")
