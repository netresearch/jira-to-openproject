# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Bulk retries discard partial transfer artifacts on both machines."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from src.infrastructure.openproject.openproject_bulk_create_service import OpenProjectBulkCreateService


def test_cleanup_after_failed_bulk_upload(tmp_path: Path) -> None:
    client = Mock()
    client.file_manager.data_dir = tmp_path
    staging = tmp_path / "j2o-stage.abcdefghij"
    client.docker_client.execute_command.side_effect = [(str(staging), "", 0), ("", "", 0)]
    client.transfer_file_to_container.side_effect = OSError("transfer interrupted")
    service = OpenProjectBulkCreateService(client)
    with pytest.raises(OSError, match="transfer interrupted"):
        service.bulk_create_records("User", [{"login": "fixture"}])

    local_input, remote_input = client.transfer_file_to_container.call_args.args
    assert remote_input.parent == staging
    assert not local_input.exists()
    client.docker_client.execute_command.assert_called_with(f"rm -rf -- {staging}")


def test_bulk_rows_continue_after_assignment_error(tmp_path: Path) -> None:
    import json
    import shutil
    import subprocess

    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required to exercise the generated bulk script")
    source = tmp_path / "input.json"
    result = tmp_path / "result.json"
    source.write_text('[{"name":"invalid"},{"name":"valid"}]')
    harness = """
class FixtureType
  def assign_attributes(attrs)
    raise ArgumentError, 'bad attribute' if attrs['name'] == 'invalid'
  end
  def save = true
  def id = 71
end
"""
    service = OpenProjectBulkCreateService(Mock())
    program = harness + service._bulk_script("FixtureType", source, result, tmp_path / "progress")
    subprocess.run([ruby, "-"], input=program, text=True, check=True, capture_output=True)
    envelope = json.loads(result.read_text())
    assert envelope["created"] == [{"index": 1, "id": 71}]
    assert envelope["errors"] == [{"index": 0, "errors": ["bad attribute"]}]
