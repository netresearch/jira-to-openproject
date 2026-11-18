# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Round trip through the production adapters against a seeded, disposable OpenProject."""

import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components.agile_board_migration import AgileBoardMigration
from src.application.components.sprint_epic_migration import SprintEpicMigration
from src.infrastructure.openproject.openproject_content_service import OpenProjectContentService
from src.infrastructure.openproject.openproject_custom_field_service import OpenProjectCustomFieldService
from src.infrastructure.openproject.openproject_project_setup_service import OpenProjectProjectSetupService
from src.infrastructure.openproject.openproject_provenance_service import OpenProjectProvenanceService
from src.infrastructure.openproject.openproject_rails_runner_service import OpenProjectRailsRunnerService
from src.infrastructure.openproject.openproject_work_package_custom_field_service import (
    OpenProjectWorkPackageCustomFieldService,
)
from src.infrastructure.openproject.openproject_work_package_service import OpenProjectWorkPackageService
from src.utils.change_detector import ChangeDetector

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


@pytest.fixture
def rails(tmp_path):
    container = os.getenv("J2O_TEST_OPENPROJECT_CONTAINER")
    if not container:
        pytest.skip("Set J2O_TEST_OPENPROJECT_CONTAINER to a seeded disposable target")
    # Share one Rails transaction across calls so the fixture can use actual IDs
    # and read results back, then roll back records, journals and enqueued jobs.
    runner = """
      $stdout.sync = true
      User.current = User.admin.first!
      ActiveRecord::Base.transaction do
        STDIN.each_line do |line|
          begin
            result = eval(JSON.parse(line), binding)
            puts "J2O_RESULT " + JSON.generate({value: result})
          rescue StandardError, SyntaxError => e
            puts "J2O_RESULT " + JSON.generate({error: e.full_message})
          end
        end
        raise ActiveRecord::Rollback
      end
    """
    with subprocess.Popen(
        ["docker", "exec", "-i", container, "timeout", "180", "bundle", "exec", "rails", "runner", runner],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as proc:

        def query(source, timeout=None):
            proc.stdin.write(json.dumps(source) + "\n")
            proc.stdin.flush()
            for line in proc.stdout:
                if line.startswith("J2O_RESULT "):
                    result = json.loads(line.removeprefix("J2O_RESULT "))
                    assert "error" not in result, result.get("error")
                    return result["value"]
            pytest.fail(f"Rails runner exited before returning a result ({proc.poll()})")

        client = Mock()
        client.execute_query.side_effect = query
        client.execute_large_query_to_json_file.side_effect = lambda source, **kwargs: query(f"({source}).as_json")
        runner_service = OpenProjectRailsRunnerService(client)
        client.execute_query_to_json_file.side_effect = runner_service.execute_query_to_json_file
        client.execute_json_query.side_effect = runner_service.execute_json_query
        client.file_manager.data_dir = tmp_path
        files = []

        def transfer(source, dest):
            files.extend([str(dest), str(dest) + ".result"])
            with source.open("rb") as data:
                subprocess.run(
                    ["docker", "exec", "-i", container, "tee", str(dest)],
                    stdin=data,
                    stdout=subprocess.DEVNULL,
                    check=True,
                    timeout=20,
                )

        client.transfer_file_to_container.side_effect = transfer
        client._read_result_file.side_effect = lambda source, local: query(
            f"JSON.parse(File.read({json.dumps(str(source))}))"
        )
        try:
            yield client, query
        finally:
            proc.stdin.close()
            proc.wait(timeout=30)
            if files:
                subprocess.run(["docker", "exec", container, "rm", "-f", "--", *files], check=True, timeout=20)


def test_agile_metadata_roundtrip(rails, monkeypatch, tmp_path):
    client, query = rails
    setup = OpenProjectProjectSetupService(client)
    fields = OpenProjectCustomFieldService(client)
    project_id, parent_id, child_id, subtask_id = query("""
      template = WorkPackage.first!
      issues = %w[Epic Story Subtask].map do |name|
        WorkPackage.create!(project: template.project, type: template.type, status: template.status,
                            priority: template.priority, author: User.current, subject: "J2O test #{name}")
      end
      [template.project_id, *issues.map(&:id)]
    """)
    provenance = OpenProjectProvenanceService(client)
    provenance_ids = provenance.ensure_provenance_types(project_id)
    assert len(provenance_ids) == len(provenance.ENTITY_TYPES)
    origin = fields.ensure_custom_field("J2O Test Origin Key")
    assert query(f"WorkPackage.find({child_id}).available_custom_fields.exists?({origin['id']})")

    jira = Mock()
    jira.get_boards.return_value = [{"id": 17, "name": "J2O test board", "type": "scrum"}]
    jira.get_board_configuration.return_value = {}
    jira.get_board_projects.return_value = [{"key": "TEST"}]
    jira.get_board_sprints.return_value = [
        {
            "id": 42,
            "name": "J2O test sprint",
            "state": "closed",
            "startDate": "2026-10-01",
            "endDate": "2026-10-14",
        }
    ]
    jira.get_custom_fields.return_value = []
    jira.batch_get_issues.return_value = {
        "TEST-1": {"fields": {}},
        "TEST-3": {"fields": {"parent": {"key": "TEST-2"}, "epicLink": "TEST-1"}},
        "TEST-2": {"fields": {"sprint": [{"id": 42, "name": "J2O test sprint"}], "epicLink": "TEST-1"}},
    }
    mappings = {"project": {"TEST": {"openproject_id": project_id}}}
    monkeypatch.setattr(
        "src.config.mappings", SimpleNamespace(get_mapping=mappings.get, set_mapping=mappings.__setitem__)
    )
    client.create_or_update_query.side_effect = OpenProjectContentService(client).create_or_update_query
    client.ensure_project_version.side_effect = setup.ensure_project_version
    boards = AgileBoardMigration(jira_client=jira, op_client=client)
    boards.change_detector = ChangeDetector(tmp_path)
    result = boards.run_with_change_detection("agile_boards")
    assert result.success, result
    version = mappings["sprint"]["42"]
    assert query(f"Version.find({version['openproject_id']}).effective_date.iso8601") == "2026-10-14"
    assert query(f"Query.find_by!(project_id: {project_id}, name: '[Board] J2O test board').public") is False
    mappings["work_package"] = {"TEST-1": parent_id, "TEST-2": child_id, "TEST-3": subtask_id}
    client.ensure_wp_custom_field_id.side_effect = fields.ensure_wp_custom_field_id
    client.bulk_set_wp_custom_field_values.side_effect = OpenProjectWorkPackageCustomFieldService(
        client
    ).bulk_set_wp_custom_field_values
    client.batch_update_work_packages.side_effect = OpenProjectWorkPackageService(client).batch_update_work_packages
    migration = SprintEpicMigration(jira_client=jira, op_client=client)
    for _ in range(2):
        result = migration.run()
        assert result.success and result.updated == 4, result
        assert query(f"WorkPackage.find({subtask_id}).parent_id") == child_id
        assert query(f"""
          wp = WorkPackage.find({child_id})
          cf = WorkPackageCustomField.find_by!(name: 'Sprint')
          [wp.parent_id, wp.version_id, wp.available_custom_fields.exists?(cf.id),
           CustomValue.where(customized: wp, custom_field: cf).pluck(:value)]
        """) == [parent_id, version["openproject_id"], True, ["J2O test sprint"]]

    type_id = provenance_ids["type"]
    role_id, from_id, to_id = query("[ProjectRole.first!.id, *Status.order(:id).limit(2).ids]")
    transition = {"type_id": type_id, "from_status_id": from_id, "to_status_id": to_id}
    assert setup.sync_workflow_transitions([transition], [role_id]) == {"existing": 0, "created": 1, "errors": 0}
    assert setup.sync_workflow_transitions([transition], [role_id]) == {"existing": 1, "created": 0, "errors": 0}
