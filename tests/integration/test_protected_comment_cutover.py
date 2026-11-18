# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Migrate restricted comments and read them through the real OpenProject Rack application."""

import base64
import json
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from pyrage import x25519

from src.comment_archive.archive import enrollment, migrate, unpack
from src.comment_archive.transport import ArchiveError, Endpoint, attachments, download
from tests.integration.test_openproject_agile_roundtrip import rails  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]


@pytest.mark.parametrize("rails", ["committed_archive"], indirect=True)
@pytest.mark.parametrize("kind", ["software", "service_desk"])
def test_encrypted_comments_after_jira_shutdown(rails, monkeypatch, tmp_path, kind):  # noqa: F811
    _, query = rails
    seed = query("""
      template = WorkPackage.first!
      project = template.project
      project.update!(public: false)
      admin = User.admin.first!
      users = %w[finance agent member outsider].map do |name|
        u = User.create!(login: "archive-#{name}", firstname: "Archive", lastname: name,
                         mail: "archive-#{name}@example.invalid", status: :active)
        Member.create!(project: project, principal: u, roles: [ProjectRole.find_by!(name: "Reader")]) unless name == "outsider"
        u
      end
      wp = WorkPackage.create!(project: project, type: template.type, status: template.status, priority: template.priority,
                               author: admin, subject: "Archive integration test")
      fields = ["100", "https://jira.invalid/browse/DEMO-1"].each_with_index.map do |value, i|
        cf = WorkPackageCustomField.create!(name: "Cutover origin #{i}", field_format: "string", is_for_all: true)
        cf.type_variant_ids = TypeVariant.default_variant.ids
        CustomValue.create!(customized: wp, custom_field: cf, value: value)
        cf.id
      end
      {wp: wp.id, fields: fields, users: (users + [admin]).map { |u| [u.id, Token::API.create!(user: u).plain_value] }}
    """)
    wp = seed["wp"]
    people = ["finance", "agent", "member", "outsider", "admin"]
    roles = {
        "inventory": ["1", "2", "3"],
        "finance": ["1", "3"],
        "agent": ["2", "3"],
        "member": [],
        "outsider": [],
        "admin": [],
    }
    bodies = {"1": "J2O-CANARY finance only", "2": "J2O-CANARY agent only", "3": "J2O-CANARY shared"}
    jira_online = True
    conflicting_core = False

    def transport(session, request, **kwargs):
        url = urlsplit(request.url)
        response = requests.Response()
        response.url = request.url
        if url.hostname == "jira.invalid":
            assert jira_online
            who = request.headers["Authorization"].removeprefix("Bearer ")
            status = 200
            if url.path.endswith("/myself"):
                payload = {"key": who}
            elif url.path.endswith("/project/DEMO"):
                payload = {"key": "DEMO", "projectTypeKey": kind}
            elif url.path.endswith("/search"):
                payload = {"startAt": 0, "total": 1, "issues": [{"key": "DEMO-1", "id": "100"}]}
            elif url.path.endswith("/comment"):
                servicedesk = "/servicedeskapi/" in url.path
                offset = int(parse_qs(url.query)["start" if servicedesk else "startAt"][0])
                allowed = roles[who]
                if conflicting_core and who == "member" and not servicedesk:
                    allowed = ["2"]
                rows = []
                for cid in allowed[offset : offset + 1]:
                    row = {
                        "id": cid,
                        "body": bodies[cid],
                        "created": "2020-11-18T10:00:00Z",
                        "author": {"displayName": "Original author"},
                    }
                    if servicedesk:
                        row["public"] = cid != "2"
                        row["created"] = {"iso8601": row["created"]}
                    elif kind == "software" or cid == "2":
                        row["visibility"] = {"type": "group", "value": "private"}
                    rows.append(row)
                if servicedesk:
                    payload = {
                        "start": offset,
                        "size": len(rows),
                        "isLastPage": offset + len(rows) == len(allowed),
                        "values": rows,
                    }
                else:
                    payload = {"startAt": offset, "total": len(allowed), "comments": rows}
                if who == "outsider" or (kind == "service_desk" and who == "finance" and not servicedesk):
                    status = 404
                    payload = {"errorMessages": ["Issue unavailable"]}
            elif url.path.endswith(("/issue/DEMO-1", "/request/DEMO-1")):
                assert who in ("finance", "outsider")
                status = 404
                payload = {"errorMessages": ["Issue unavailable"]}
            else:
                pytest.fail(url.path)
            response.status_code = status
            response._content = json.dumps(payload).encode()
        else:
            assert url.hostname == "openproject.invalid"
            body = base64.b64encode(request.body or b"").decode()
            status, contents = query(f"""
              body = Base64.strict_decode64({json.dumps(body)})
              opts = {{input: body, "HTTP_HOST" => "localhost",
                      "CONTENT_TYPE" => {json.dumps(request.headers.get("Content-Type", ""))},
                      "HTTP_AUTHORIZATION" => {json.dumps(request.headers.get("Authorization", ""))} }}
              res = Rack::MockRequest.new(Rails.application).request({json.dumps(request.method)},
                      {json.dumps("http://localhost" + request.path_url)}, opts)
              [res.status, Base64.strict_encode64(res.body)]
            """)
            response.status_code = status
            response._content = base64.b64decode(contents)
        return response

    monkeypatch.setattr(requests.Session, "send", transport)
    monkeypatch.setenv("J2O_ARCHIVE_INVENTORY_TOKEN", "inventory")
    monkeypatch.setenv("J2O_ARCHIVE_PUBLISHER_TOKEN", seed["users"][-1][1])
    config = {
        "jira": "https://jira.invalid",
        "openproject": "https://openproject.invalid",
        "origin_id_field": seed["fields"][0],
        "origin_url_field": seed["fields"][1],
        "readers": [],
    }
    identities = {}
    for name, (op_id, token) in zip(people, seed["users"], strict=True):
        identity = x25519.Identity.generate()
        identities[name] = identity
        record = enrollment(
            Endpoint(config["jira"], name, jira=True),
            Endpoint(config["openproject"], token, jira=False),
            str(identity.to_public()),
            name,
            token,
        )
        assert record["op_id"] == op_id and record["jira_key"] == name
        monkeypatch.setenv("SOURCE_" + name, name)
        monkeypatch.setenv("TARGET_" + name, token)
        config["readers"].append(
            {"enrollment": record, "jira_token_env": "SOURCE_" + name, "op_token_env": "TARGET_" + name}
        )

    mapping = {"100": {"jira_key": "DEMO-1", "openproject_id": wp}}
    audit = {
        "directory": [{"jira_key": k, "op_id": v[0]} for k, v in zip(people, seed["users"], strict=True)],
        "inventory_file": {"jira": config["jira"], "project": "DEMO", "issues": {"100": list(bodies)}},
    }
    state = tmp_path / "frozen.sqlite"
    with pytest.raises(ArchiveError, match="independently mapped"):
        migrate(config, mapping, "DEMO", state, True, **{**audit, "directory": audit["directory"][:-1]})
    incomplete = {**audit["inventory_file"], "issues": {"100": ["1", "2"]}}
    with pytest.raises(ArchiveError, match="Independent comment inventory differs"):
        migrate(config, mapping, "DEMO", state, True, **{**audit, "inventory_file": incomplete})
    assert not attachments(Endpoint(config["openproject"], seed["users"][-1][1], jira=False), wp)
    for _ in range(2):
        assert migrate(config, mapping, "DEMO", state, True, **audit) == {
            "issues": 1,
            "comments": 3,
            "reader_copies": 4,
            "archives": 1,
        }

    jira_online = False
    for name, (_, token) in zip(people, seed["users"], strict=True):
        op = Endpoint(config["openproject"], token, jira=False)
        if name == "outsider":
            with pytest.raises(ArchiveError, match="HTTP (403|404)"):
                attachments(op, wp)
            continue
        files = attachments(op, wp)
        assert len(files) == 1
        blob = download(op, files[0])
        activities = op.get(f"/api/v3/work_packages/{wp}/activities")
        assert all(text.encode() not in blob and text not in json.dumps(activities) for text in bodies.values())
        if roles[name]:
            view = unpack(blob, identities[name], wp)
            assert {row["body"] for row in view["comments"]} == {bodies[cid] for cid in roles[name]}
            assert all(
                row["author"] == "Original author" and row["created"].startswith("2020") for row in view["comments"]
            )
        else:
            with pytest.raises(ArchiveError, match="No readable comments"):
                unpack(blob, identities[name], wp)

    jira_online = True
    if kind == "service_desk":
        conflicting_core = True
        with pytest.raises(ArchiveError, match="reader access disagree"):
            migrate(config, mapping, "DEMO", state, True, **audit)
        conflicting_core = False
    roles["finance"] = ["1"]
    with pytest.raises(ArchiveError, match="audience or text changed"):
        migrate(config, mapping, "DEMO", state, True, **audit)
