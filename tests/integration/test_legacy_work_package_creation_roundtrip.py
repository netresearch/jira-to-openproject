# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Read a history-free legacy creation back from OpenProject and the on-disk ID mapping."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.application.components.work_package_migration import WorkPackageMigration
from src.infrastructure.openproject.openproject_bulk_create_service import OpenProjectBulkCreateService
from tests.integration.test_openproject_agile_roundtrip import rails as rails

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


def test_legacy_create_preserves_timestamps_and_records_confirmed_target(rails, monkeypatch, tmp_path):
    client, query = rails
    # Run the production bulk adapter in the shared rollback transaction.
    monkeypatch.setenv("J2O_SCRIPT_RUNNER_THRESHOLD", "150000")
    monkeypatch.setenv("J2O_SCRIPT_RUNNER_MAX_LINES", "10000")
    monkeypatch.setenv("J2O_BULK_MAX_WAIT", "3")
    client.rails_client.execute.side_effect = lambda source, **kwargs: query(source + "\ntrue")
    client.docker_client.check_file_exists_in_container.side_effect = lambda path: query(
        f"File.exist?({json.dumps(str(path))})"
    )

    def download_result(remote, local):
        location = json.dumps(str(remote))
        Path(local).write_text(query(f"File.read({location})"), encoding="utf-8")
        query(f"File.unlink({location})")

    client.transfer_file_from_container.side_effect = download_result
    client.bulk_create_records.side_effect = OpenProjectBulkCreateService(client).bulk_create_records
    app = WorkPackageMigration.__new__(WorkPackageMigration)
    app.op_client = client
    app.logger = Mock()
    app.data_dir = tmp_path
    app.work_package_mapping = {}
    template = query("WorkPackage.first!.slice(:project_id, :type_id, :status_id, :priority_id, :author_id)")
    source = {"jira_id": "1823425", "jira_key": "TEST-101", "project_key": "TEST"}
    payload = {
        **template,
        "subject": "History-free work package",
        "created_at": "2020-02-01T12:30:00Z",
        "updated_at": "2020-05-02T13:40:00Z",
        "jira_id": source["jira_id"],
        "jira_key": source["jira_key"],
        "jira_issue_id": source["jira_id"],
        "jira_issue_key": source["jira_key"],
    }
    assert app._write_new_work_packages([payload], [source], "TEST", template["project_id"]) == 1
    stored = json.loads((tmp_path / "work_package_mapping.json").read_text(encoding="utf-8"))
    assert stored[source["jira_id"]] == app.work_package_mapping[source["jira_id"]]
    work_package_id = stored[source["jira_id"]]["openproject_id"]
    record = query(
        f"wp = WorkPackage.find({int(work_package_id)}); [wp.subject, wp.created_at.utc.iso8601, wp.updated_at.utc.iso8601]"
    )
    assert record == [payload["subject"], payload["created_at"], payload["updated_at"]]
