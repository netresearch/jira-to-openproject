# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT
"""Read back watcher eligibility and source identity from a disposable OpenProject."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components.work_package_migration import WorkPackageMigration
from src.application.transformers.issue_transformer import IssueTransformer
from src.domain import provenance_fields
from src.infrastructure.openproject.container_staging import private_container_directory
from src.infrastructure.openproject.openproject_bulk_create_service import OpenProjectBulkCreateService
from src.infrastructure.openproject.openproject_custom_field_service import OpenProjectCustomFieldService
from src.infrastructure.openproject.openproject_project_attribute_service import OpenProjectProjectAttributeService
from tests.integration.test_openproject_agile_roundtrip import rails as rails

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


def test_native_watchers_and_reconciled_source_identity(rails, tmp_path):
    client, query = rails
    template, member_id, outsider_id = query("""
      template = WorkPackage.first!
      project = template.project
      project.update!(public: false)
      users = %w[member outsider].map do |name|
        User.create!(login: "watch-test-#{name}", firstname: "Watcher", lastname: name,
                     mail: "watch-test-#{name}@example.invalid", status: :active)
      end
      Member.create!(project: project, principal: users.first, roles: [ProjectRole.find_by!(name: "Member")])
      [template.slice(:project_id, :type_id, :status_id, :priority_id, :author_id), *users.map(&:id)]
    """)
    migration = WorkPackageMigration.__new__(WorkPackageMigration)
    migration.data_dir = tmp_path
    migration.op_client = client
    migration.logger = Mock()
    migration.issue_type_id_mapping = {"1": template["type_id"]}
    migration.status_mapping = {}
    migration._transformer = IssueTransformer(owner=migration)
    migration._resolve_start_date = Mock(return_value=None)
    migration._apply_source_timestamps = Mock()
    migration._apply_source_users = lambda issue, record: record.update(watcher_ids=issue.watcher_users)
    client.ensure_custom_field.side_effect = OpenProjectCustomFieldService(client).ensure_custom_field

    def project_issue(number, subject, watchers):
        fields = SimpleNamespace(summary=subject, issuetype=SimpleNamespace(id="1", name="Task"), description="")
        source = SimpleNamespace(
            id=str(500 + number), key=f"WATCH-{number}", fields=fields, raw={"fields": {}}, watcher_users=watchers
        )
        return {**template, **migration.prepare_work_package(source, template["project_id"])}

    records = [
        project_issue(1, "Watcher test one", [member_id]),
        project_issue(2, "Watcher test two", [member_id, outsider_id]),
    ]
    with private_container_directory(client.docker_client) as stage:
        source = tmp_path / "watchers.json"
        source.write_text(json.dumps(records))
        client.transfer_file_to_container(source, stage / source.name)
        script = OpenProjectBulkCreateService(client)._bulk_script(
            "WorkPackage", stage / source.name, stage / "result.json", stage / "progress"
        )
        result = query(script + "\nresult")
    assert [record["index"] for record in result["errors"]] == [1], result
    [created] = result["created"]
    target_id = int(created["id"])
    assert query(f"WorkPackage.find({target_id}).watchers.pluck(:user_id)") == [member_id]
    assert query("WorkPackage.where(subject: 'Watcher test two').count") == 0
    assert query(f"Member.where(project_id: {int(template['project_id'])}, user_id: {int(outsider_id)}).count") == 0

    migration.work_package_mapping = {
        "501": {"jira_key": "WATCH-1", "openproject_id": target_id, "openproject_project_id": template["project_id"]}
    }
    client.get_project_wp_cf_snapshot.side_effect = OpenProjectProjectAttributeService(
        client
    ).get_project_wp_cf_snapshot
    existing = migration._get_existing_work_packages(template["project_id"])["WATCH-1"]
    assert existing["jira_source_id"] == "501"
    system_field = migration._j2o_wp_cf_ids_full[provenance_fields.ORIGIN_SYSTEM]
    source_value = f"CustomValue.find_by!(customized_type: 'WorkPackage', customized_id: {target_id}, custom_field_id: {system_field})"
    assert query(f"{source_value}.value") == provenance_fields.JIRA_SERVER_SOURCE_LABEL
    with pytest.raises(RuntimeError, match="disagrees"):
        migration._update_existing_work_package(
            SimpleNamespace(id="999", key="WATCH-1"), existing, template["project_id"]
        )
    query(f"{source_value}.update!(value: 'GitHub')")
    assert "WATCH-1" not in migration._get_existing_work_packages(template["project_id"])
    query(f"{source_value}.update!(value: {json.dumps(provenance_fields.JIRA_SERVER_SOURCE_LABEL)})")
    query(f"""CustomValue.joins(:custom_field).where(customized_type: "WorkPackage", customized_id: {target_id},
      custom_fields: {{name: ["Jira Issue Key", "J2O Origin Key"]}}).destroy_all.length""")
    assert migration._get_existing_work_packages(template["project_id"])["WATCH-1"]["id"] == target_id
