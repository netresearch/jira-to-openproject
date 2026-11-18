# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Follow one ordinary identity, one issue and one personal key across Jira group changes."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
from pyrage import x25519

from src.comment_archive.archive import enrollment, migrate, unpack
from src.comment_archive.transport import ArchiveError, Endpoint, attachments, download, visible_comments
from tests.integration.jira_group_source import NativeGroupSource
from tests.integration.test_openproject_agile_roundtrip import rails  # noqa: F401
from tests.integration.test_protected_comment_cutover import openproject_response

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

JIRA = "https://jira.invalid"
OPENPROJECT = "https://openproject.invalid"
PUBLIC_BODY = "Fixture public comment"
RESTRICTED_BODY = "Fixture group-only comment"


class GroupSource:
    """Model Jira's membership predicate. The caller cannot supply a per-user comment list."""

    issue = "GROUP-1"
    issue_id = "100"
    public_id = "101"
    protected_id = "102"
    tokens = {"anchor": "anchor-token", "reader": "reader-token"}
    principals = {"anchor": "fixture-anchor", "reader": "fixture-reader"}
    inventory_token = "inventory-token"

    def __init__(self):
        self.members = {"fixture-anchor", "inventory"}
        self.offline = False

    def set_membership(self, *, member):
        if member:
            self.members.add(self.principals["reader"])
        else:
            self.members.discard(self.principals["reader"])

    def respond(self, request):
        assert not self.offline, "Offline target reads must not contact Jira"
        identities = {token: self.principals[name] for name, token in self.tokens.items()}
        identities[self.inventory_token] = "inventory"
        principal = identities[request.headers["Authorization"].removeprefix("Bearer ")]
        path = urlsplit(request.url).path
        if path.endswith("/myself"):
            result = {"key": principal}
        elif path.endswith("/project/GROUP"):
            result = {"key": "GROUP", "projectTypeKey": "software"}
        elif path.endswith("/search"):
            result = {"startAt": 0, "total": 1, "issues": [{"key": self.issue, "id": self.issue_id}]}
        else:
            assert path == f"/rest/api/2/issue/{self.issue}/comment"
            common = {"created": "2020-01-01T00:00:00Z", "author": {"displayName": "Fixture author"}}
            rows = [{**common, "id": self.public_id, "body": PUBLIC_BODY}]
            if principal in self.members:
                rows.append(
                    {
                        **common,
                        "id": self.protected_id,
                        "body": RESTRICTED_BODY,
                        "visibility": {"type": "group", "value": "fixture-reviewers"},
                    }
                )
            result = {"startAt": 0, "total": len(rows), "comments": rows}
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response._content = json.dumps(result).encode()
        return response


@pytest.fixture
def group_source():
    native = os.getenv("J2O_TEST_GROUP_JIRA_FIXTURE")
    return NativeGroupSource(Path(native)) if native else GroupSource()


def setup_target(query, source):
    return query(f"""
      template = WorkPackage.first!
      project = template.project
      project.update!(public: false)
      project.enabled_modules.find_or_create_by!(name: "work_package_tracking")
      admin = User.admin.first!
      wp = WorkPackage.create!(project: project, type: template.type, status: template.status,
                               priority: template.priority, author: admin, subject: "Group transition fixture")
      readers = %w[anchor reader].to_h do |name|
        u = User.create!(login: "group-test-#{{name}}", firstname: "Group test", lastname: name,
                         mail: "group-test-#{{name}}@example.invalid", status: :active)
        raise "Expected ordinary project member" if u.admin?
        Member.create!(project: project, principal: u, roles: [ProjectRole.find_by!(name: "Reader")])
        [name, [u.id, Token::API.create!(user: u).plain_value]]
      end
      fields = [{json.dumps(source.issue_id)}, {json.dumps(f"{JIRA}/browse/{source.issue}")}].each_with_index.map do |value, i|
        cf = WorkPackageCustomField.create!(name: "Group fixture origin #{{i}}", field_format: "string", is_for_all: true)
        cf.type_variant_ids = TypeVariant.default_variant.ids
        CustomValue.create!(customized: wp, custom_field: cf, value: value)
        cf.id
      end
      {{wp: wp.id, readers: readers, fields: fields, publisher: Token::API.create!(user: admin).plain_value}}
    """)


def enrolled_readers(source, target, monkeypatch):
    records = []
    keys = {}
    directory = []
    for name, (op_id, op_token) in target["readers"].items():
        jira_token = source.tokens[name]
        keys[name] = x25519.Identity.generate()
        env_jira = f"J2O_TEST_SOURCE_{name.upper()}"
        env_op = f"J2O_TEST_TARGET_{name.upper()}"
        monkeypatch.setenv(env_jira, jira_token)
        monkeypatch.setenv(env_op, op_token)
        record = enrollment(
            Endpoint(JIRA, jira_token, jira=True),
            Endpoint(OPENPROJECT, op_token, jira=False),
            str(keys[name].to_public()),
            jira_token,
            op_token,
        )
        records.append({"jira_token_env": env_jira, "op_token_env": env_op, "enrollment": record})
        directory.append({"jira_key": source.principals[name], "op_id": op_id})
    monkeypatch.setenv("J2O_ARCHIVE_INVENTORY_TOKEN", source.inventory_token)
    monkeypatch.setenv("J2O_ARCHIVE_PUBLISHER_TOKEN", target["publisher"])
    return (
        keys,
        directory,
        {
            "jira": JIRA,
            "openproject": OPENPROJECT,
            "origin_id_field": target["fields"][0],
            "origin_url_field": target["fields"][1],
            "readers": records,
        },
    )


def read_snapshot(op, wp, key):
    files = attachments(op, wp)
    assert len(files) == 1
    blob = download(op, files[0])
    assert RESTRICTED_BODY.encode() not in blob
    view = unpack(blob, key, wp)
    return files[0]["id"], blob, {row["id"]: row["body"] for row in view["comments"]}


@pytest.mark.parametrize("rails", ["committed_archive"], indirect=True)
def test_group_changes_require_a_new_frozen_archive(rails, group_source, monkeypatch, tmp_path):  # noqa: F811
    query = rails[1]
    source = group_source

    def transport(session, request, **kwargs):
        if urlsplit(request.url).hostname == "jira.invalid":
            return source.respond(request)
        return openproject_response(query, request)

    monkeypatch.setattr(requests.Session, "send", transport)

    target = setup_target(query, source)
    keys, directory, config = enrolled_readers(source, target, monkeypatch)
    wp = target["wp"]
    mapping = {source.issue_id: {"jira_key": source.issue, "openproject_id": wp}}
    audit = {"jira": JIRA, "project": "GROUP", "issues": {source.issue_id: [source.public_id, source.protected_id]}}

    def capture(state):
        return migrate(config, mapping, "GROUP", state, True, directory=directory, inventory_file=audit)

    reader_jira = Endpoint(JIRA, source.tokens["reader"], jira=True)
    reader_op = Endpoint(OPENPROJECT, target["readers"]["reader"][1], jira=False)
    publisher = Endpoint(OPENPROJECT, target["publisher"], jira=False)
    old = None
    snapshots = []
    for index, member in enumerate((False, True, False)):
        source.set_membership(member=member)
        assert reader_jira.get("/rest/api/2/myself")["key"] == source.principals["reader"]
        assert reader_op.get("/api/v3/users/me")["id"] == target["readers"]["reader"][0]
        assert reader_op.get(f"/api/v3/work_packages/{wp}")["id"] == wp
        expected = {source.public_id: PUBLIC_BODY}
        if member:
            expected[source.protected_id] = RESTRICTED_BODY
        visible = visible_comments(reader_jira, source.issue, "software", reader=True)
        assert {cid: row["body"] for cid, row in visible.items()} == expected
        state = tmp_path / f"snapshot-{index}.sqlite"
        if old is not None:
            with pytest.raises(ArchiveError, match="audience or text changed"):
                capture(old)
            with pytest.raises(ArchiveError, match="Stored archive differs"):
                capture(state)
            attachment_id, previous, previous_view = snapshots[-1]
            assert read_snapshot(reader_op, wp, keys["reader"]) == (attachment_id, previous, previous_view)
            # Restore only this disposable fixture's archive slot before publishing the fresh snapshot.
            reset = publisher.session.delete(f"{OPENPROJECT}/api/v3/attachments/{attachment_id}", timeout=30)
            assert reset.status_code == 204
            assert attachments(reader_op, wp) == []
        counts = capture(state)
        assert counts == {"issues": 1, "comments": 2, "reader_copies": 3 + int(member), "archives": 1}
        source.offline = True
        result = read_snapshot(reader_op, wp, keys["reader"])
        assert result[2] == expected
        snapshots.append(result)
        old = state
        source.offline = False

    assert len({item[0] for item in snapshots}) == 3
    assert len({item[1] for item in snapshots}) == 3
    source.offline = True
    # Removing group membership cannot recall a file already downloaded with its matching private key.
    retained = unpack(snapshots[1][1], keys["reader"], wp)
    assert {row["id"]: row["body"] for row in retained["comments"]} == {
        source.public_id: PUBLIC_BODY,
        source.protected_id: RESTRICTED_BODY,
    }
