# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import json
import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import pytest
from pyrage import x25519

from src.comment_archive.archive import cached_blob, enrollment, readers_for, recover_mapping, unpack
from src.comment_archive.cli import main
from src.comment_archive.transport import (
    ArchiveError,
    Endpoint,
    check_core_metadata,
    publish,
    request_comments,
    verified_denial,
)


def test_offline_reader_and_idempotent_sealing(capsys):
    with TemporaryDirectory() as private:
        verify_offline_key_lifecycle(Path(private), capsys)


def verify_offline_key_lifecycle(tmp_path, capsys):
    keys = [tmp_path / str(i) for i in range(3)]
    for key in keys:
        assert main(["keygen", "--identity", str(key)]) == 0
        if os.name == "posix":
            assert key.stat().st_mode & 0o077 == 0
    contents = keys[0].read_bytes()
    assert main(["keygen", "--identity", str(keys[0])]) == 1
    assert keys[0].read_bytes() == contents
    capsys.readouterr()
    ids = [x25519.Identity.from_str(key.read_text().strip()) for key in keys]
    views = [{"recipient": str(i.to_public()), "wp": 22, "comments": [{"body": "private text"}]} for i in ids[:2]]
    with sqlite3.connect(":memory:") as db:
        db.execute("create table archives(wp integer primary key, digest text, blob blob)")
        blob = cached_blob(db, 22, views)
        assert cached_blob(db, 22, views) == blob
        assert len(json.loads(blob)["envelopes"]) == 1
        assert b"private text" not in blob
        with pytest.raises(ArchiveError, match="audience or text changed"):
            cached_blob(db, 22, views[:1])
    path = tmp_path / "archived"
    path.write_bytes(blob)
    for i in range(3):
        code = main(["read", "--file", str(path), "--identity", str(keys[i]), "--work-package", "22"])
        output = capsys.readouterr()
        assert code == int(i == 2)
        assert ("private text" in output.out) == (i < 2)
    with pytest.raises(ArchiveError, match="identity mismatch"):
        unpack(blob, ids[0], 24)


def test_enrollment_rejects_key_substitution(monkeypatch):
    jira = Mock(url="https://jira.invalid")
    jira.get.return_value = {"key": "JIRAUSER1"}
    op = Mock(url="https://op.invalid")
    op.get.return_value = {"id": 3, "_type": "User"}
    identity = x25519.Identity.generate()
    record = enrollment(jira, op, str(identity.to_public()), "source", "target")
    record["recipient"] = str(x25519.Identity.generate().to_public())
    config = {
        "jira": jira.url,
        "openproject": op.url,
        "readers": [{"enrollment": record, "jira_token_env": "SOURCE", "op_token_env": "TARGET"}],
    }
    monkeypatch.setenv("SOURCE", "source")
    monkeypatch.setenv("TARGET", "target")
    source = jira
    monkeypatch.setattr("src.comment_archive.archive.Endpoint", lambda url, token, jira, ca: source if jira else op)
    with pytest.raises(ArchiveError, match="enrollment"):
        readers_for(config, True)


@pytest.mark.parametrize(
    "metadata",
    [{"properties": [{"key": "unknown"}]}, {"jsdPublic": False}, {"visibility": {"type": "plugin", "value": "active"}}],
)
def test_uncertain_historical_audiences_block(metadata):
    with pytest.raises(ArchiveError):
        check_core_metadata([{"id": "12", **metadata}], service=False)


def test_service_pagination_and_unverified_denial_fail_closed():
    jira = Mock()
    jira.request.return_value.json.return_value = {"values": [], "start": 0, "size": 0, "isLastPage": False}
    with pytest.raises(ArchiveError, match="Incomplete"):
        request_comments(jira, "A-1", reader=True)
    response = Mock(status_code=403)
    response.json.return_value = {"errorMessages": ["denied"]}
    jira.request.return_value.status_code = 200
    with pytest.raises(ArchiveError, match="disagrees"):
        verified_denial(jira, response, "/rest/api/2/issue/A-1")


def test_foreign_or_duplicate_attachment_prevents_upload():
    op = Mock()
    item = {"fileName": "jira-protected-comments.j2o", "id": 4}
    op.get.return_value = {"_embedded": {"elements": [item]}, "total": 1}
    op.request.return_value.content = b"previous archive"
    with pytest.raises(ArchiveError, match="differs"):
        publish(op, 4, b"new archive")
    assert all(call.args[0] == "GET" for call in op.request.call_args_list)


@pytest.mark.parametrize("internal", [1, True, "true"])
def test_service_flags_must_be_well_formed_and_match_request(internal):
    prop = {"key": "sd.public.comment", "value": {"internal": internal}}
    error = "Unverified internal comment flag" if type(internal) is int else "reader access disagree"
    with pytest.raises(ArchiveError, match=error):
        check_core_metadata([{"id": "5", "properties": [prop]}], service=True, request_view={"5": {"public": True}})
    if type(internal) is not int:
        assert check_core_metadata(
            [{"id": "5", "properties": [prop]}], service=True, request_view={"5": {"public": False}}
        ) == {"5"}
    assert check_core_metadata([], service=True, request_view={"5": {"public": False}}) == {"5"}


def test_archive_retries_only_reads(monkeypatch):
    op = Endpoint("https://op.invalid", "fixture", jira=False)
    send = Mock(side_effect=[Mock(status_code=503), Mock(status_code=200)])
    monkeypatch.setattr(op.session, "request", send)
    assert op.request("GET", "/read").status_code == 200
    assert send.call_count == 2
    send.reset_mock(side_effect=True)
    send.return_value = Mock(status_code=503)
    with pytest.raises(ArchiveError, match="HTTP 503"):
        op.request("POST", "/upload")
    send.assert_called_once()


def test_recovery_refuses_duplicate_provenance():
    op = Mock()
    rows = [{"id": 1}, {"id": 2}]
    duplicate = {"customField1": "100", "customField2": "https://jira.invalid/browse/DEMO-1"}
    op.get.side_effect = [{"_embedded": {"elements": rows}, "offset": 1, "total": 2}, duplicate, duplicate]
    with pytest.raises(ArchiveError, match="ambiguous"):
        recover_mapping(op, {"jira": "https://jira.invalid", "origin_id_field": 1, "origin_url_field": 2}, "DEMO")
