# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Round trip through the production adapters against a seeded, disposable OpenProject."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components.agile_board_migration import AgileBoardMigration
from src.application.components.sprint_epic_migration import SprintEpicMigration
from src.infrastructure.openproject.openproject_client import OpenProjectClient
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
def rails(tmp_path, request):
    container = os.getenv("J2O_TEST_OPENPROJECT_CONTAINER")
    if not container:
        pytest.skip("Set J2O_TEST_OPENPROJECT_CONTAINER to a seeded disposable target")
    # Share one Rails transaction across calls so the fixture can use actual IDs
    # and read results back, then roll back records, journals and enqueued jobs.
    runner = """
      $stdout.sync = true
      User.current = User.admin.first!
      session = lambda do
        STDIN.each_line do |line|
          begin
            result = eval(JSON.parse(line), binding)
            puts "J2O_RESULT " + JSON.generate({value: result})
          rescue StandardError, SyntaxError => e
            puts "J2O_RESULT " + JSON.generate({error: e.full_message})
          end
        end
      end
      if ENV["J2O_COMMITTED_FIXTURE"] == "1"
        session.call
      else
        ActiveRecord::Base.transaction { session.call; raise ActiveRecord::Rollback }
      end
    """
    command = ["timeout", "180", "bundle", "exec", "rails", "runner", runner]
    if getattr(request, "param", None) == "committed_archive":
        # File storage is finalized after COMMIT. Use a new database AND asset
        # directory so real HTTP uploads can finish without changing the seed.
        command = [
            "bash",
            "-c",
            """
            set -euo pipefail
            scratch=$(mktemp -d)
            db=j2o_test_$(ruby -rsecurerandom -e 'print SecureRandom.hex(10)')
            source_url=$DATABASE_URL
            createdb --maintenance-db="$source_url" "$db"
            trap 'dropdb --if-exists --force --maintenance-db="$source_url" "$db"; rm -rf -- "$scratch"' EXIT
            export DATABASE_URL=$(ruby -ruri -e 'u=URI(ENV.fetch("DATABASE_URL")); u.path="/"+ARGV.fetch(0); print u' "$db")
            pg_dump -Fc --no-owner "$source_url" | pg_restore --no-owner --exit-on-error -d "$DATABASE_URL"
            export OPENPROJECT_ATTACHMENTS__STORAGE__PATH=$scratch
            export J2O_COMMITTED_FIXTURE=1
            "$@"
        """,
            "j2o-test",
            *command,
        ]
    with subprocess.Popen(
        ["docker", "exec", "-i", "-e", "J2O_COMMITTED_FIXTURE=0", container, *command],
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

        def execute_container(command: str):
            result = subprocess.run(
                ["docker", "exec", container, "sh", "-c", command],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            return result.stdout, result.stderr, result.returncode

        client = Mock()
        client.docker_client.execute_command.side_effect = execute_container
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


@pytest.mark.parametrize("rails", ["committed_archive"], indirect=True)
def test_attachment_native_filename_download_and_repeat(rails, tmp_path):
    """Read the stored filename, work package and bytes from the native OpenProject API."""
    import hashlib
    from collections import Counter

    from src.application.components.attachments_migration import AttachmentsMigration
    from src.infrastructure.openproject.container_staging import private_container_directory

    client, query = rails
    wp = query("""
      template = WorkPackage.first!
      WorkPackage.create!(project: template.project, type: template.type, status: template.status,
                          priority: template.priority, author: User.admin.first!, subject: "Attachment fidelity").id
    """)
    required_name = "Prüfung (2) α.txt"
    payload = bytes(range(256))
    source = tmp_path / "payload"
    source.write_bytes(payload)
    loader = AttachmentsMigration.__new__(AttachmentsMigration)
    loader.op_client = client
    loader._loss_counters = Counter()

    def submit(script, ops):
        data = query(f"input_data = JSON.parse({json.dumps(json.dumps(ops))}); {script}\nresult")
        return {"status": "success", "data": data}

    client.execute_script_with_data.side_effect = submit
    with private_container_directory(client.docker_client) as remote:
        target = remote / "private-staging-id"
        client.transfer_file_to_container(source, target)
        op = {
            "jira_key": "DEMO-1",
            "work_package_id": wp,
            "filename": required_name,
            "digest": hashlib.sha256(payload).hexdigest(),
            "container_path": str(target),
        }
        create = loader._attach_files([op])
        assert create.success
        assert create.updated == 1
        attachment_id = create.data["attachment_mapping"]["DEMO-1"][required_name]
        again = loader._attach_files([op])
        assert again.success
        assert again.updated == 0
        assert again.data == create.data
        for unsafe in ("one;other.txt", "../other", "bad\nname"):
            rejected = loader._attach_files([{**op, "filename": unsafe}])
            assert not rejected.success
            assert rejected.failed == 1
        mismatched = loader._attach_files([{**op, "digest": "0" * 64}])
        assert not mismatched.success
        assert mismatched.failed == 1
        source.write_bytes(b"other")
        client.transfer_file_to_container(source, target)
        conflict = loader._attach_files([{**op, "digest": hashlib.sha256(b"other").hexdigest()}])
        assert not conflict.success
        assert conflict.failed == 1

    seen = query(f"""
      a = Attachment.find({attachment_id})
      token = Token::API.create!(user: User.admin.first!)
      begin
        opts = {{ "HTTP_HOST"=>"localhost",
                  "HTTP_AUTHORIZATION"=>ActionController::HttpAuthentication::Basic.encode_credentials("apikey", token.plain_value) }}
        rack = Rack::MockRequest.new(Rails.application)
        path = "http://localhost/api/v3/attachments/#{{a.id}}"
        meta = rack.get(path, opts)
        blob = rack.get(path + "/content", opts)
        raise "Attachment read failed" unless meta.status == 200 && blob.status == 200
        info = JSON.parse(meta.body)
        # 17.9.7 emits an unquoted filename in Content-Disposition. A semicolon would
        # start another header parameter and cannot round-trip as part of the filename.
        header_boundary = Attachment.new(filename: "one;other.txt").content_disposition.include?("filename=one;other.txt")
        [info.fetch("fileName"), info.fetch("_links").fetch("container").fetch("href"), info.fetch("fileSize"),
         Digest::SHA256.hexdigest(blob.body), blob["Content-Disposition"].split("; ", 2).last,
         header_boundary, Attachment.where(container_type: "WorkPackage", container_id: {wp}).count]
      ensure
        token.destroy!
      end
    """)
    assert seen == [
        required_name,
        f"/api/v3/work_packages/{wp}",
        len(payload),
        hashlib.sha256(payload).hexdigest(),
        f"filename={required_name}",
        True,
        1,
    ]


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
    jira.get_board_issue_keys.return_value = ["TEST-1"]
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
    mappings = {"project": {"TEST": {"openproject_id": project_id}}, "work_package": {"TEST-1": parent_id}}
    monkeypatch.setattr(
        "src.config.mappings", SimpleNamespace(get_mapping=mappings.get, set_mapping=mappings.__setitem__)
    )
    client.create_or_update_query.side_effect = OpenProjectContentService(client).create_or_update_query
    client.ensure_project_version.side_effect = setup.ensure_project_version
    boards = AgileBoardMigration(jira_client=jira, op_client=client)
    boards.change_detector = ChangeDetector(tmp_path)
    result = boards.run_with_change_detection("agile_boards")
    assert result.success, str(result.details)
    version = mappings["sprint"]["42"]
    assert query(f"Version.find({version['openproject_id']}).effective_date.iso8601") == "2026-10-14"
    assert query(f"Query.find_by!(project_id: {project_id}, name: '[Jira Board 17] J2O test board').public") is False
    saved = f"Query.find_by!(project_id: {project_id}, name: '[Jira Board 17] J2O test board')"
    assert query(f"{saved}.results.work_packages.pluck(:id)") == [parent_id]
    jira.get_board_issue_keys.return_value = []
    assert boards.run_with_change_detection("agile_boards").success
    assert query(f"{saved}.results.work_packages.pluck(:id)") == []
    mappings["work_package"] = {"TEST-1": parent_id, "TEST-2": child_id, "TEST-3": subtask_id}
    client.ensure_wp_custom_field_id.side_effect = fields.ensure_wp_custom_field_id
    client.bulk_set_wp_custom_field_values.side_effect = OpenProjectWorkPackageCustomFieldService(
        client
    ).bulk_set_wp_custom_field_values
    client.batch_update_work_packages.side_effect = OpenProjectWorkPackageService(client).batch_update_work_packages
    migration = SprintEpicMigration(jira_client=jira, op_client=client)
    for _ in range(2):
        result = migration.run()
        assert result.success, result
        assert result.updated == 5, result
        assert query(f"WorkPackage.find({subtask_id}).parent_id") == child_id
        assert query(f"""
          wp = WorkPackage.find({child_id})
          cf = WorkPackageCustomField.find_by!(name: 'Sprint')
          [wp.parent_id, wp.version_id, wp.available_custom_fields.any? {{ |f| f.id == cf.id }},
           CustomValue.where(customized: wp, custom_field: cf).pluck(:value)]
        """) == [parent_id, version["openproject_id"], True, ["J2O test sprint"]]
        assert query(f"""
          wp = WorkPackage.find({child_id})
          history = WorkPackageCustomField.find_by!(name: "Jira sprint membership history")
          [wp.available_custom_fields.any? {{ |f| f.id == history.id }}, JSON.parse(wp.custom_values.find_by!(custom_field: history).value)]
        """) == [True, [{"id": "42", "name": "J2O test sprint"}]]

    type_id = provenance_ids["type"]
    role_id, from_id, to_id = query("[ProjectRole.first!.id, *Status.order(:id).limit(2).ids]")
    transition = {"type_id": type_id, "from_status_id": from_id, "to_status_id": to_id}
    assert setup.sync_workflow_transitions([transition], [role_id]) == {"existing": 0, "created": 1, "errors": 0}
    assert setup.sync_workflow_transitions([transition], [role_id]) == {"existing": 1, "created": 0, "errors": 0}


def test_work_package_content_roundtrip(rails):
    """Read back imported content, option IDs and two backdated journals."""
    from src.infrastructure.openproject.openproject_bulk_create_service import OpenProjectBulkCreateService
    from src.infrastructure.openproject.openproject_work_package_content_service import (
        OpenProjectWorkPackageContentService,
    )

    client, query = rails
    seed = query("""
      wp = WorkPackage.first!
      {project_id: wp.project_id, type_id: wp.type_id, author_id: User.current.id,
       priority_id: wp.priority_id, status_id: wp.status_id}
    """)
    fields = OpenProjectCustomFieldService(client)
    text_id = fields.ensure_custom_field("J2O Test Text", field_format="string")["id"]
    user_id = fields.ensure_custom_field("J2O Test User", field_format="user")["id"]
    flag_id = fields.ensure_custom_field("J2O Test Flag", field_format="bool")["id"]
    query("""
      cf = WorkPackageCustomField.new(name: 'J2O Test List', field_format: 'list', is_for_all: true)
      cf.custom_options.build(value: 'One')
      cf.custom_options.build(value: 'Two')
      cf.save!
    """)
    list_id = fields.ensure_custom_field("J2O Test List", field_format="list")["id"]
    client.docker_client.transfer_file_to_container.side_effect = client.transfer_file_to_container.side_effect
    rows = [
        {
            **seed,
            "subject": f"Test item {n}",
            "custom_fields": [{"id": text_id, "value": "initial"}],
            "created_at": "2020-11-18T09:30:00Z",
        }
        for n in range(3)
    ]
    created = OpenProjectBulkCreateService(client)._create_work_packages_batch(rows)
    assert created["created"] == 3, created
    wp_id = created["results"][-1]["id"]

    def values(field_id):
        return query(
            f"CustomValue.where(customized: WorkPackage.find({wp_id}), custom_field_id: {field_id}).pluck(:value)"
        )

    assert values(text_id) == ["initial"]
    assert query(f"WorkPackage.find({wp_id}).created_at.utc.iso8601") == "2020-11-18T09:30:00Z"
    service = OpenProjectWorkPackageService(client)
    updates = [
        {
            "id": wp_id,
            "description": "Beschreibung ä",
            f"customField{text_id}": "übernommen",
            f"customField{flag_id}": False,
            f"customField{user_id}": seed["author_id"],
            f"customField{list_id}": {"id": "jira-99", "value": "Two"},
        }
    ]
    for _ in range(2):
        result = service.batch_update_work_packages(updates)
        assert result["updated"] == 1, result
        assert result["failed"] == 0, result
        assert values(text_id) == ["übernommen"]
        assert values(user_id) == [str(seed["author_id"])]
        assert query(f"WorkPackage.find({wp_id}).description") == "Beschreibung ä"
        assert query(f"WorkPackage.find({wp_id}).custom_value_for(CustomField.find({flag_id})).typed_value") is False
        option_id = query(f"CustomField.find({list_id}).custom_options.find_by!(value: 'Two').id")
        assert values(list_id) == [str(option_id)]
    failed = service.batch_update_work_packages(
        [{"id": wp_id, "description": "incomplete", f"customField{list_id}": {"id": "other", "value": "Missing"}}]
    )
    assert failed["failed"] == 1
    assert query(f"WorkPackage.find({wp_id}).description") == "Beschreibung ä"

    journals = OpenProjectWorkPackageContentService(client)
    notes = [
        {
            "work_package_id": wp_id,
            "comment": f"Test note {n}",
            "user_id": seed["author_id"],
            "jira_comment_id": str(n),
            "created_at": f"2021-01-0{n}T11:00:00Z",
        }
        for n in (1, 2)
    ]
    assert journals.bulk_create_work_package_activities(notes)["created"] == 2
    assert journals.bulk_create_work_package_activities(notes)["skipped"] == 2
    assert query(f"""
      Journal.where(journable_type: 'WorkPackage', journable_id: {wp_id}).where('notes LIKE ?', '%Test note%')
             .order(:id).map {{ |j| [j.notes.lines.first.strip, j.created_at.utc.iso8601, j.user_id] }}
    """) == [[note["comment"], note["created_at"], seed["author_id"]] for note in notes]


def test_comment_readers_on_private_work_package(rails, monkeypatch):
    """Migrate mixed synthetic Jira comments, then read the real API as five principals."""
    from unittest.mock import patch

    from src.application.components.work_package_content_migration import WorkPackageContentMigration
    from src.infrastructure.jira.comment_audience import select_comments
    from src.infrastructure.openproject.openproject_work_package_content_service import (
        OpenProjectWorkPackageContentService,
    )
    from tests.unit.test_jira_comment_audience import page, rows

    client, query = rails
    wp_id = query("""
      template = WorkPackage.first!
      project = template.project
      project.update!(public: false)
      root = User.admin.first!
      viewers = [root]
      %w[Member Reader].each_with_index do |role_name, i|
        user = User.create!(login: "j2o-audience-#{i}", firstname: "Audience", lastname: "Test#{i}",
                            mail: "audience#{i}@example.invalid", status: :active)
        Member.create!(project: project, principal: user, roles: [ProjectRole.find_by!(name: role_name)])
        viewers << user
      end
      viewers << User.create!(login: 'j2o-outsider', firstname: 'Outside', lastname: 'Project',
                              mail: 'outside@example.invalid', status: :active)
      wp = WorkPackage.create!(project: project, type: template.type, status: template.status,
                               priority: template.priority, author: root, subject: 'J2O audience test')
      # Keep tokens only in memory, never in test output.
      $audience_tokens = viewers.map { |u| [u.id, Token::API.create!(user: u).plain_value] }
      wp.id
    """)
    monkeypatch.setenv("J2O_COPY_PUBLIC_COMMENTS", "1")
    jira = Mock(base_url="https://jira.example")
    source = rows()
    jira._make_request.return_value = page(source)
    jira.select_comments_for_migration.side_effect = lambda key: select_comments(jira, key)
    jira.get_issue_watchers.return_value = []
    client.wp_content = OpenProjectWorkPackageContentService(client)
    client.bulk_create_work_package_activities.side_effect = client.wp_content.bulk_create_work_package_activities
    migration = WorkPackageContentMigration(jira_client=jira, op_client=client)
    issue = SimpleNamespace(key="DEMO-1", fields=SimpleNamespace(description=None), raw={})
    with patch("src.infrastructure.jira.comment_audience.requests.Session") as factory:
        factory.return_value.__enter__.return_value.get.return_value = page(source[:4])
        for attempt in range(2):
            collected = migration._collect_content_for_issue(issue, wp_id)
            result = migration._bulk_process_collected_content([collected])
            assert result["comments_held"] == 7, result
            assert result["comments_migrated"] == int(attempt == 0), result

    observations = query(f"""
      ($audience_tokens + [[nil, nil]]).map do |id, token|
        browser = ActionDispatch::Integration::Session.new(Rails.application)
        browser.host! "localhost"
        headers = {{ "ACCEPT" => "application/hal+json" }}
        headers["HTTP_AUTHORIZATION"] = ActionController::HttpAuthentication::Basic.encode_credentials("apikey", token) if token
        browser.get "/api/v3/users/me", headers: headers
        me = JSON.parse(browser.response.body)["id"]
        browser.get "/api/v3/work_packages/{wp_id}/activities", headers: headers
        [id == me, browser.response.status, browser.response.body]
      end
    """)
    for i, (identity_matches, status, body) in enumerate(observations):
        if i < 4:
            assert identity_matches, (i, status)
        if i < 3:
            assert status == 200, (i, status)
            assert source[0]["body"] in body, (i, status)
        else:
            assert status in (401, 403, 404), (i, status)
            assert source[0]["body"] not in body, (i, status)
        assert all(row["body"] not in body for row in source[1:])
    # A higher-level view or a later permission grant cannot reveal an absent body.
    persisted = query(f"Journal.where(journable: WorkPackage.find({wp_id})).pluck(:notes)")
    assert all(row["body"] not in str(persisted) for row in source[1:])

    # Jira now denies anonymous access to the previously copied comment.
    with patch("src.infrastructure.jira.comment_audience.requests.Session") as factory:
        factory.return_value.__enter__.return_value.get.return_value = page([], status=403)
        collected = migration._collect_content_for_issue(issue, wp_id)
    assert collected["comments_held"] == len(source)
    with pytest.raises(ValueError, match="restore a clean pre-import snapshot"):
        migration._bulk_process_collected_content([collected])


def test_unsupported_cleanup_preserves_every_work_package_type(rails):
    from src.infrastructure.exceptions import QueryExecutionError
    from src.infrastructure.openproject.openproject_admin_cleanup_service import OpenProjectAdminCleanupService

    client, query = rails
    original = query("Type.order(:id).pluck(:id, :name)")
    assert original
    assert query('(["is_default", "is_standard"] - Type.column_names).any?')
    service = OpenProjectAdminCleanupService(client)
    with pytest.raises(QueryExecutionError, match="all types were preserved"):
        service.delete_non_default_issue_types()
    assert query("Type.order(:id).pluck(:id, :name)") == original


def test_user_provenance_fields_create_in_sections_and_preserve_legacy_values(rails):
    from src.application.components.user_migration import UserMigration

    client, query = rails
    service = OpenProjectCustomFieldService(client)
    client.custom_fields = service
    client.ensure_custom_field.side_effect = lambda *args, **kwargs: OpenProjectClient.ensure_custom_field(
        client, *args, **kwargs
    )
    migration = UserMigration.__new__(UserMigration)
    migration.op_client = client
    migration.logger = client.logger
    names = ["J2O Origin System", "J2O User ID", "J2O User Key", "J2O External URL"]
    existing = query(f"""
      # The fixture transaction rolls back the field setup and all values.
      names = {json.dumps(names)}
      UserCustomField.where(name:names).each {{ |f| f.update_column(:name, "Seed field " + f.id.to_s) }}
      owned=UserCustomFieldSection.create!(name:"Retained user profile section")
      first=UserCustomField.create!(name:names.first,field_format:"string",custom_field_section:owned,admin_only:true)
      $legacy=["Jira user key","Tempo Account"].map do |name|
        f=UserCustomField.find_or_create_by!(name:name) {{ |v| v.field_format="string";v.custom_field_section=owned }}
        User.current.custom_values.find_or_initialize_by(custom_field:f).update!(value:"keep-"+name);f.id
      end
      [first.id, owned.id]
    """)
    migration._origin_cf_id_map = None
    first = migration.ensure_j2o_custom_fields()
    assert set(first) == set(names), client.logger.warning.call_args_list
    assert first[names[0]] == existing[0]
    migration._origin_cf_id_map = None
    assert migration.ensure_j2o_custom_fields() == first
    state = query(f"""
        fields = UserCustomField.where(id:{list(first.values())})
        {{"legacy"=>User.current.custom_values.where(custom_field_id:$legacy).order(:value).pluck(:value),
          "section"=>UserCustomField.find({existing[0]}).custom_field_section_id,
          "new"=>fields.where.not(id:{existing[0]}).map{{ |f| [f.custom_field_section.type,f.admin_only?] }}.uniq,
          "anonymous_visible"=>UserCustomField.visible(User.anonymous).where(id:fields.ids).exists?}}
    """)
    assert state == {
        "legacy": ["keep-Jira user key", "keep-Tempo Account"],
        "section": existing[1],
        "new": [["UserCustomFieldSection", True]],
        "anonymous_visible": False,
    }


def test_migrated_project_allows_native_work_package_creation(rails):
    from src.application.components.project_migration import ProjectMigration

    client, query = rails
    migration = ProjectMigration.__new__(ProjectMigration)
    migration.op_client = client
    migration.cf_jira_project_key_id = None
    migration.cf_jira_project_id_id = None
    migration.cf_jira_base_url_id = None
    source = {"jira_key": "MODCHECK", "jira_id": "8219", "name": "Project type fixture", "identifier": "modcheck"}
    target, created = migration._create_or_find_project(source)
    assert created
    project_id = int(target["id"])
    observations = query(f"""
      project = Project.find({project_id})
      rest = ActionDispatch::Integration::Session.new(Rails.application)
      rest.host! "localhost"
      token = Token::API.create!(user: User.admin.first!).plain_value
      headers = {{"ACCEPT" => "application/hal+json",
                  "HTTP_AUTHORIZATION" => ActionController::HttpAuthentication::Basic.encode_credentials("apikey", token)}}
      type = Type.order(:position).first!
      rest.get "/api/v3/projects/{project_id}/types", headers: headers
      types = JSON.parse(rest.response.body).fetch("_embedded").fetch("elements").pluck("id")
      payload = {{subject: "API type check", _links: {{type: {{href: "/api/v3/types/#{{type.id}}"}},
                  project: {{href: "/api/v3/projects/{project_id}"}}}}}}
      rest.post "/api/v3/work_packages", params: payload, headers: headers, as: :json
      [project.public?, project.module_enabled?(:work_package_tracking), types.include?(type.id),
       rest.response.status, JSON.parse(rest.response.body)["subject"]]
    """)
    assert observations == [False, True, True, 201, "API type check"]
    # A pilot with deliberately restricted or empty types keeps its administrator's choice on rerun.
    types = f"project = Project.find({project_id}); project.respond_to?(:project_types) ? project.project_types : project.types"
    query(f"({types}).clear; true")
    existing, created_again = migration._create_or_find_project(source)
    assert existing["id"] == project_id
    assert not created_again
    assert query(f"({types}).count") == 0


def test_category_policy_preserves_inherited_and_only_assigns_existing_verified_member(rails):
    from src.application.components.category_defaults_migration import _rule
    from src.infrastructure.openproject.category_defaults import apply_category_defaults

    client, query = rails
    base = "https://category-default.example.invalid"
    pid, uid, cat, members, users, notices = query("""
      $p, $u = Member.where.not(project_id: nil).filter_map { |m|
        [m.project, m.principal] if m.principal.is_a?(User) && m.principal.active? && m.project
      }.min_by { |project, user| user.admin? ? 1 : 0 }
      raise "Seed the disposable target with a project member" unless $p && $u
      $p.update_column(:public, false)
      policy = ProjectCustomField.find_by(name:"Jira category default policies")
      $p.custom_values.where(custom_field:policy).delete_all if policy
      section = ProjectCustomFieldSection.find_or_create_by!(name: "J2O Origin")
      origin = {"Jira Project Key"=>"ABC","Jira Project ID"=>"7","Jira Base URL"=>"https://category-default.example.invalid"}
      set_project = lambda do |name,val,fmt,admin|
        f=ProjectCustomField.find_or_create_by!(name:name) { |x|
          x.assign_attributes(field_format:fmt,admin_only:admin,is_for_all:false,custom_field_section:section)
        }
        $p.custom_values.find_or_initialize_by(custom_field:f).update!(value:val)
      end
      origin.each { |name,val| set_project.call(name,val,"string",false) }
      $category=$p.categories.create!(name:"Category default integration fixture")
      ref={"id"=>"17","name"=>$category.name,"url"=>origin["Jira Base URL"]+"/rest/api/2/component/17"}
      set_project.call("Jira component membership origins",
        {source:origin,components:{"17"=>{source:ref,category:$category.id}},issues:{}}.to_json,"text",true)
      {"J2O User Key"=>"fixed-key","J2O User ID"=>"policy-login","J2O Origin System"=>"Jira Data Center 9.12.3",
       "J2O External URL"=>origin["Jira Base URL"]+"/secure/ViewProfile.jspa?name=policy-login"}.each do |name,val|
        f=UserCustomField.find_or_create_by!(name:name) { |x|
          x.assign_attributes(field_format:"string", custom_field_section:UserCustomFieldSection.first!)
        }
        $u.custom_values.find_or_initialize_by(custom_field:f).update!(value:val)
      end
      [$p.id,$u.id,$category.id,Member.count,User.count,Notification.count]
    """)
    actor = {"key": "fixed-key", "name": "policy-login", "active": True}
    details = {"projectTypeKey": "software", "id": "7", "key": "ABC", "lead": actor}
    raw = {
        "id": "17",
        "name": "Category default integration fixture",
        "project": "ABC",
        "projectId": 7,
        "self": base + "/rest/api/2/component/17",
        "assigneeType": "COMPONENT_LEAD",
        "realAssigneeType": "COMPONENT_LEAD",
        "lead": actor,
        "assignee": actor,
        "realAssignee": actor,
        "isAssigneeTypeValid": True,
    }
    project = {
        "project_id": pid,
        "source": {"Jira Project Key": "ABC", "Jira Project ID": "7", "Jira Base URL": base},
        "project_policy": {"assigneeType": "UNASSIGNED", "lead": actor},
    }

    def apply(row, user=uid):
        project["rules"] = [_rule(row, details, base, {"fixed-key": {"openproject_id": user}})]
        return apply_category_defaults(client, project)

    def read_assignee():
        return query(f"""
            require "rack/mock"
            tok=Token::API.create!(user:User.admin.active.first!);auth=ActionController::HttpAuthentication::Basic.encode_credentials("apikey",tok.plain_value)
            req=Rack::MockRequest.new(Rails.application).get("/api/v3/categories/{cat}",{{"HTTP_HOST"=>"localhost","HTTP_ACCEPT"=>"application/hal+json","HTTP_AUTHORIZATION"=>auth}})
            raise "category API failure" unless req.status==200
            JSON.parse(req.body).dig("_links","defaultAssignee","href")
        """)

    assert apply(raw, 987654321)["held"] == 1
    assert read_assignee() is None
    assert apply(raw)["native"] == 1
    assert read_assignee() == f"/api/v3/users/{uid}"
    assert apply(raw)["updated"] == 0
    inherited = {**raw, "assigneeType": "PROJECT_DEFAULT", "realAssigneeType": "PROJECT_DEFAULT"}
    assert apply(inherited)["held"] == 1
    assert read_assignee() is None
    assert apply(inherited)["updated"] == 0
    # Independently maintained defaults remain owned by the migration operator.
    query(f"Category.find({cat}).update!(assigned_to_id:{uid}); true")
    assert (
        apply({**raw, "assigneeType": "UNASSIGNED", "realAssigneeType": "UNASSIGNED", "realAssignee": None})["held"]
        == 1
    )
    assert read_assignee() == f"/api/v3/users/{uid}"
    assert query("[Member.count,User.count,Notification.count]") == [members, users, notices]


def test_all_fix_versions_survive_native_readback_and_retry(rails):
    client, query = rails
    seed = query("""
        w = WorkPackage.first!
        a, b = ["Version A regression", "Version B regression"].map { |name| w.project.versions.create!(name: name) }
        [w.id, a.id, b.id, w.respond_to?(:target_version_ids_replacements=) && Setting.work_package_multiple_versions?]
    """)
    wid, first, second, multiple = seed
    writer = OpenProjectWorkPackageService(client)
    assert writer.batch_update_work_packages([{"id": wid, "target_version_ids": [first]}])["failed"] == 0
    request = [{"id": wid, "target_version_ids": [first, second]}]
    for _ in range(2):
        result = writer.batch_update_work_packages(request)
        assert result["failed"] == int(not multiple)
    actual = query(
        f"w=WorkPackage.find({wid});w.respond_to?(:target_version_ids) ? w.target_version_ids.sort : [w.version_id]"
    )
    assert actual == ([first, second] if multiple else [first])


@pytest.fixture
def native_sprint_projects(rails):
    _, query = rails
    source = Path(__file__).resolve().parents[2] / "src/ruby/import_completed_sprints.rb"
    query(source.read_text() + "\ntrue")
    query("""
      section = ProjectCustomFieldSection.find_or_create_by!(name: "J2O Origin")
      jf = ["Jira Project Key", "Jira Project ID", "Jira Base URL"].map do |n|
        ProjectCustomField.find_or_create_by!(name: n) { |f| f.assign_attributes(field_format: "string", custom_field_section_id: section.id) }
      end
      fields = ["J2O Origin ID", "J2O Origin URL", JiraCompletedSprints::HISTORY].map do |n|
        WorkPackageCustomField.find_or_create_by!(name: n) { |f| f.field_format = n == JiraCompletedSprints::HISTORY ? "text" : "string" }
      end
      seed = WorkPackage.first!
      $sprint_fixture = %w[EX FX].map.with_index do |key, i|
        p = Project.create!(name: key, identifier: "j2o-native-sprint-" + key.downcase, workspace_type: "project", public: false)
        p.enabled_modules.find_or_create_by!(name: "work_package_tracking")
        p.project_types = TypeVariant.default_variant.map { |v| ProjectType.new(type_id: v.type_id, variant: v) }
        jf.zip([key, (80+i).to_s, "https://jira.example.org"]).each do |field,value|
          p.custom_values.create!(custom_field: field, value: value)
        end
        v = p.versions.create!(name: "Release")
        wp = WorkPackage.create!(project: p, type: seed.type, status: seed.status, priority: seed.priority,
                                 author: User.admin.first!, subject: "Sprint fixture", version_id: v.id)
        fields.zip([(90+i).to_s, "https://jira.example.org/browse/#{key}-1", '[{"id":"2","name":"Same"},{"id":"1","name":"Same"}]']).each do |f,value|
          wp.custom_values.create!(custom_field: f, value: value)
        end
        [p.id, wp.id, v.id]
      end
      true
    """)
    return query, query("$sprint_fixture")


def _run_sprints(query, manifest):
    data = json.dumps(json.dumps(manifest))
    return query(f"begin; JiraCompletedSprints.new(JSON.parse({data})).apply; rescue => e; e.message; end")


def test_native_completed_sprints_preserve_versions_history_and_project_scope(native_sprint_projects):
    query, fixture = native_sprint_projects
    stamp = "2026-01-02T00:15:00+02:00"
    sprints = [
        {
            "id": sid,
            "originBoardId": 3,
            "name": "Same",
            "state": "closed",
            "goal": "",
            "startDate": stamp,
            "endDate": stamp,
            "activatedDate": stamp,
            "completeDate": stamp,
        }
        for sid in (1, 2)
    ]

    native_ids = []
    for i, (key, (pid, wid, vid)) in enumerate(zip(("EX", "FX"), fixture, strict=True)):
        manifest = {
            "targetProjectId": pid,
            "jira": "https://jira.example.org",
            "project": {"key": key, "id": str(80 + i)},
            "issues": [{"id": str(90 + i), "key": f"{key}-1", "sprintIds": [2, 1]}],
            "boardProjectKeys": {"3": ["EX", "FX"]},
            "sprints": sprints,
        }
        assert _run_sprints(query, {**manifest, "issues": []}) == "Incomplete work-package membership export"
        assert _run_sprints(query, {**manifest, "boardProjectKeys": {"3": ["OTHER"]}}) == "Unknown or mixed board scope"
        assert (
            _run_sprints(query, {**manifest, "sprints": [*sprints, {**sprints[0], "id": 3}]})
            == "Unknown or mixed board scope"
        )
        assert query(f"Sprint.where(project_id: {pid}).count") == 0
        conflict = query(f"""
          observed = nil
          Project.transaction(requires_new: true) do
            p = Project.find({pid}); wp = WorkPackage.find({wid})
            current = Sprint.create!(name: "Unrelated sprint", project: p)
            wp.update_columns(sprint_id: current.id)
            begin
              JiraCompletedSprints.new(JSON.parse({json.dumps(json.dumps(manifest))})).apply
            rescue JiraCompletedSprints::ImportConflict => e
              observed = [e.message, Sprint.where(project_id: p.id).count, wp.reload.sprint_id == current.id]
            end
            raise ActiveRecord::Rollback
          end
          observed
        """)
        assert conflict == ["Existing native sprint assignment conflicts", 1, True]
        assert _run_sprints(query, manifest) == {"sprints": 2, "work_packages": 1, "memberships": 2, "changes": 3}
        assert _run_sprints(query, manifest)["changes"] == 0
        assert (
            _run_sprints(query, {**manifest, "sprints": [{**sprints[0], "goal": "a goal"}, sprints[1]]})
            == "Frozen sprint export changed"
        )
        result = query(f"""
          p=Project.find({pid}); w=WorkPackage.find({wid})
          stored=JSON.parse(p.custom_values.joins(:custom_field).where(custom_fields: {{name: JiraCompletedSprints::REGISTER}}).sole.value)
          history=JSON.parse(w.custom_values.joins(:custom_field).where(custom_fields: {{name: JiraCompletedSprints::HISTORY}}).sole.value)
          [w.sprint_id, stored.fetch("targets").fetch("2"), w.version_id, w.sprint.project_id, w.sprint.start_date.iso8601, history.pluck("id")]
        """)
        current, registered = result[:2]
        assert current == registered
        assert result[2:] == [vid, pid, "2026-01-02", ["2", "1"]]
        api = query(f"""
          http = Rack::MockRequest.new(Rails.application)
          token = Token::API.create!(user: User.admin.first!)
          headers = {{"HTTP_HOST" => "localhost", "HTTP_ACCEPT" => "application/hal+json",
                     "HTTP_AUTHORIZATION" => ActionController::HttpAuthentication::Basic.encode_credentials("apikey", token.plain_value)}}
          path = "http://localhost/api/v3/sprints/{current}"
          sprint = JSON.parse(http.get(path, headers).body)
          wp = JSON.parse(http.get("http://localhost/api/v3/work_packages/{wid}", headers).body)
          [sprint.fetch("name"), sprint.fetch("_links").fetch("definingWorkspace").fetch("href"),
           wp.fetch("_links").fetch("sprint").fetch("href"), http.get(path, headers.except("HTTP_AUTHORIZATION")).status]
        """)
        assert api[:3] == ["Same", f"/api/v3/projects/{pid}", f"/api/v3/sprints/{current}"]
        assert api[3] in {401, 403, 404}
        native_ids.append(current)
    assert len(set(native_ids)) == 2


@pytest.mark.parametrize(
    ("assigned", "active_dates"), [("future", {}), ("active", {"activatedDate": "2026-02-02T15:00:00Z"})]
)
def test_native_open_sprints_and_goal_roundtrip(native_sprint_projects, assigned, active_dates):
    query, ((pid, wid, vid), _) = native_sprint_projects
    dates = {"startDate": "2026-02-02T01:00:00+02:00", "endDate": "2026-02-13T15:00:00Z"}
    active = {
        "id": 2,
        "originBoardId": 3,
        "name": "Same",
        "goal": "Überprüfung & rollout",
        "state": assigned,
        **(dates if assigned == "active" else {}),
        **active_dates,
    }
    closed = {
        "id": 1,
        "originBoardId": 3,
        "name": "Same",
        "state": "closed",
        "goal": "Shipped",
        **dates,
        "activatedDate": dates["startDate"],
        "completeDate": dates["endDate"],
    }
    manifest = {
        "jira": "https://jira.example.org",
        "project": {"id": "80", "key": "EX"},
        "targetProjectId": pid,
        "boardProjectKeys": {"3": ["EX", "FX"]},
        "sprints": [closed, active],
        "issues": [{"id": "90", "key": "EX-1", "sprintIds": [2, 1]}],
    }
    for changes, message in [
        ({"state": "invalid"}, "Duplicate or unsupported sprint"),
        ({"completeDate": dates["endDate"]}, "Sprint timestamps contradict its state"),
        ({"goal": "a" * 501}, "Sprint goal cannot be preserved exactly"),
        ({"goal": " changed "}, "Sprint goal cannot be preserved exactly"),
    ]:
        assert _run_sprints(query, {**manifest, "sprints": [closed, {**active, **changes}]}) == message
    ambiguous = {**manifest, "sprints": [{**active, "id": 1, "state": "future", "activatedDate": None}, active]}
    assert _run_sprints(query, ambiguous) == "Ambiguous open sprint memberships"
    assert query(f"Sprint.where(project_id: {pid}).count") == 0
    assert _run_sprints(query, manifest) == {"sprints": 2, "work_packages": 1, "memberships": 2, "changes": 5}
    assert _run_sprints(query, manifest)["changes"] == 0
    observed = query(f"""
        wp = WorkPackage.find({wid})
        sprint = wp.sprint
        project = wp.project
        token = Token::API.create!(user: User.admin.first!)
        headers = {{"HTTP_HOST" => "localhost", "HTTP_ACCEPT" => "application/hal+json",
                   "HTTP_AUTHORIZATION" => ActionController::HttpAuthentication::Basic.encode_credentials("apikey", token.plain_value)}}
        response = Rack::MockRequest.new(Rails.application).get("/api/v3/sprints/#{{sprint.id}}", headers)
        data = JSON.parse(response.body)
        User.current = User.admin.first!
        widget = Backlogs::SprintReports::Widgets::Goal.new(sprint, project, current_user: User.current)
        rendered = Nokogiri::HTML.fragment(ApplicationController.renderer.render(widget, layout: false)).text
        card = Backlogs::SprintComponent.new(sprint: sprint, project: project, current_user: User.current)
        html = ApplicationController.renderer.render(card, layout: false)
        goal_id = ActionView::RecordIdentifier.dom_target(sprint, :goal)
        card_goal = Nokogiri::HTML.fragment(html).at_css("##{{goal_id}}")
        [wp.version_id, response.status, data.dig("_links", "status", "href"), data["startDate"], data["finishDate"],
         data["startedAt"] && Time.iso8601(data["startedAt"]).iso8601, data["completedAt"],
         sprint.goal_text_for(project), rendered.include?(sprint.goal_text_for(project)),
         card_goal.text.squish.include?(sprint.goal_text_for(project)),
         Sprint.where(project: project, status: :completed).sole.goal_text_for(project)]
    """)
    status = "in_planning" if assigned == "future" else "active"
    assert observed[:2] == [vid, 200]
    assert observed[2].endswith(f":sprints:status:{status}")
    assert observed[3:7] == (["2026-02-02", "2026-02-13", "2026-02-02T15:00:00Z", None] if active_dates else [None] * 4)
    assert observed[7:] == [active["goal"], True, True, "Shipped"]
    native_id = query(f"w=WorkPackage.find({wid}); old=w.sprint_id; w.update_columns(sprint_id: nil); old")
    assert _run_sprints(query, manifest) == "Existing native sprint assignment conflicts"
    query(f"WorkPackage.find({wid}).update_columns(sprint_id: {native_id})")
    query(f'WorkPackage.find({wid}).sprint.goals.sole.update!(text: "Edited locally"); true')
    assert _run_sprints(query, manifest) == "Native sprint changed"
