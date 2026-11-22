# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Components read their counts from the runner's envelope, never from its top level."""

from __future__ import annotations

import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.application.components.attachment_provenance_migration import AttachmentProvenanceMigration
from src.application.components.native_tags_migration import NativeTagsMigration
from src.infrastructure.openproject.openproject_rails_runner_service import OpenProjectRailsRunnerService

UNCONFIRMED = [
    {"status": "error", "message": "JSON markers not found in Rails output"},
    {"updated": 1, "failed": 0},
]


@pytest.mark.parametrize("answer", UNCONFIRMED, ids=["no-markers", "unwrapped"])
def test_unconfirmed_attachment_provenance_fails(answer):
    op = MagicMock()
    op.execute_script_with_data.return_value = answer
    migration = AttachmentProvenanceMigration(jira_client=MagicMock(), op_client=op)
    updates = [{"work_package_id": 5, "filename": "a.txt", "author_id": None, "created_at": None}] * 2
    result = migration._load(SimpleNamespace(data={"updates": updates}))
    assert (result.success, result.failed) == (False, 2)


@pytest.mark.parametrize("answer", UNCONFIRMED, ids=["no-markers", "unwrapped"])
def test_unconfirmed_native_tags_fail(answer):
    op = MagicMock()
    op.execute_script_with_data.return_value = answer
    migration = NativeTagsMigration(jira_client=MagicMock(), op_client=op)
    updates = [{"work_package_id": 5, "tags": [{"name": "x"}]}]
    result = migration._load(SimpleNamespace(data={"updates": updates}, details={}))
    assert (result.success, result.failed) == (False, 1)


def test_the_native_tags_script_answers_between_the_runner_markers():
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required to exercise the generated tags script")
    op = MagicMock()
    op.execute_script_with_data.return_value = {"status": "success", "data": {"updated": 0, "failed": 0}}
    NativeTagsMigration(jira_client=MagicMock(), op_client=op)._load(
        SimpleNamespace(data={"updates": [{"work_package_id": 5, "tags": []}]}, details={})
    )
    script = op.execute_script_with_data.call_args.args[0]
    harness = (
        "require 'json'\n"
        "class WorkPackage; def self.find(_) = Object.new; end\n"
        "$j2o_start_marker, $j2o_end_marker = 'J2O_START', 'J2O_END'\n"
        "input_data = [{'work_package_id' => 5, 'tags' => []}]\n"
    )
    out = subprocess.run([ruby, "-e", harness + script], capture_output=True, text=True, check=True, timeout=60).stdout
    parsed = OpenProjectRailsRunnerService._structured_result(out, "J2O_START", "J2O_END")
    assert (parsed["status"], parsed["data"]) == ("success", {"updated": 0, "failed": 0})
