# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Restore exact field values while withholding comment and ambiguous records."""

import json
from unittest.mock import Mock

import pyrage
import pytest
from pyrage import x25519

from src.comment_archive.transport import ArchiveError
from src.operator_history import Destination, decrypt, main, publish_snapshot, seal, snapshot

pytestmark = pytest.mark.unit
_JIRA = "https://jira.example.invalid"


def change(field, **overrides):
    return {"field": field, "fieldtype": "jira", "from": None, "toString": "value", **overrides}


def issue(items):
    return {
        "key": "EX-1",
        "id": "10",
        "self": _JIRA + "/rest/api/2/issue/10",
        "fields": {"project": {"key": "EX"}},
        "changelog": {
            "startAt": 0,
            "total": 1,
            "histories": [{"id": "27", "created": "2026-01-01", "author": {"key": "person"}, "items": items}],
        },
    }


def test_selects_recognized_fields_without_exposing_comments():
    field = {
        "id": "customfield_1",
        "name": "Scope",
        "schema": {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:textarea"},
    }
    ambiguous = {**field, "id": "customfield_2"}
    rows = [
        change("summary"),
        change("summary", toString="revised"),
        change("Comment", toString="private"),
        change("WorklogTimeSpent", visibility={"type": "role"}),
        change("summary", fieldId="comment"),
        change("Custom", fieldtype="custom"),
        change("Scope", fieldtype="custom"),
        change("Workflow"),
        change("Scope", fieldId="customfield_1", fieldtype="custom"),
        change("WorklogId", raw={"field": "comment"}),
    ]
    result = snapshot([issue(rows)], [field, ambiguous], "EX", _JIRA, {"customfield_1"})
    kept = result["issues"]["10"]["events"][0]["changes"]
    assert [c["position"] for c in kept] == [0, 1, 7, 8]
    assert kept[1]["toString"] == "revised"
    assert "private" not in json.dumps(result)
    restricted = issue(rows)
    restricted["changelog"]["histories"][0]["historyMetadata"] = {"extraData": {"internal": True}}
    assert snapshot([restricted], [], "EX", _JIRA, set())["issues"]["10"]["events"] == []


def test_scopes_the_export_and_requires_complete_changelog():
    row = issue([])
    for rows, project, jira in (
        ([row, row], "EX", _JIRA),
        ([row], "OTHER", _JIRA),
        ([row], "EX", "https://other.invalid"),
    ):
        with pytest.raises(ArchiveError):
            snapshot(rows, [], project, jira, set())
    row["changelog"]["total"] = 2
    with pytest.raises(ArchiveError):
        snapshot([row], [], "EX", _JIRA, set())


def test_native_archive_restoration_retry_and_drift(tmp_path):
    private = tmp_path / "operator.key"
    identity = x25519.Identity.generate()
    private.write_text(str(identity))
    fields = tmp_path / "fields.json"
    fields.write_text("[]")
    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([issue([change("summary")])]))
    archive = tmp_path / "snapshot.age"
    args = [
        "seal",
        "--identity",
        str(private),
        "--archive",
        str(archive),
        "--issues",
        str(issues),
        "--fields",
        str(fields),
        "--project",
        "EX",
        "--jira",
        _JIRA,
    ]
    assert main(args) == 0
    original = archive.read_bytes()
    assert main(args) == 0
    assert archive.read_bytes() == original
    restored = tmp_path / "restored.json"
    assert main(["restore", "--identity", str(private), "--archive", str(archive), "--output", str(restored)]) == 0
    assert json.loads(restored.read_text()) == decrypt(original, identity)
    with pytest.raises(pyrage.DecryptError):
        decrypt(original, x25519.Identity.generate())
    with pytest.raises(ArchiveError):
        seal({"format": "different"}, identity, archive)
    assert archive.read_bytes() == original


def test_checks_project_and_work_package_origin_before_upload(monkeypatch):
    op = Mock()
    value = snapshot([issue([])], [], "EX", _JIRA, set())
    target = {
        "_links": {"project": {"href": "/api/v3/projects/4"}},
        "customField2": "10",
        "customField3": _JIRA + "/browse/EX-1",
    }
    send = Mock()
    monkeypatch.setattr("src.operator_history.publish", send)
    d = Destination(50, 4, 2, 3)
    op.get.side_effect = [target, {"public": True}]
    with pytest.raises(ArchiveError):
        publish_snapshot(op, d, value, b"sealed")
    op.get.side_effect = [{**target, "customField2": "11"}, {"public": False}]
    with pytest.raises(ArchiveError):
        publish_snapshot(op, d, value, b"sealed")
    send.assert_not_called()
    op.get.side_effect = [target, {"public": False}]
    publish_snapshot(op, d, value, b"sealed")
    send.assert_called_once_with(op, 50, b"sealed", filename="jira-EX-operator-field-history.age")
