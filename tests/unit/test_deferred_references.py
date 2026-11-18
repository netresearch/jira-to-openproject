# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import json
from unittest.mock import Mock

import pyrage
import pytest
from pyrage import x25519

from src.deferred_references import inventory, mappings, read_snapshot, write_snapshot
from src.deferred_references_cli import main
from src.deferred_references_replay import apply


def records():
    link_type = {"id": "7", "name": "Blocks", "outward": "blocks", "inward": "is blocked by"}
    external = {"id": "12", "key": "EXT-3", "fields": {"summary": "restricted foreign title"}}
    local = {"id": "10", "key": "APP-1"}
    return [
        {
            **local,
            "fields": {
                "project": {"key": "APP"},
                "customfield_42": "EXT-4",
                "issuelinks": [
                    {"id": "91", "type": link_type, "inwardIssue": external},
                    {"id": "90", "type": link_type, "outwardIssue": local},
                ],
            },
        }
    ]


def sample():
    return inventory(
        records(),
        {"APP-1": ("10", 31)},
        project="APP",
        jira="https://jira.example",
        epic_field="customfield_42",
        identities={"EXT-4": "13"},
    )


def test_cli_capture_is_private_recoverable_and_repeatable(tmp_path, capsys):
    identity = x25519.Identity.generate()
    keyfile, archive = tmp_path / "operator", tmp_path / "links.age"
    keyfile.write_text(str(identity))
    src, mapping, extra = (tmp_path / n for n in ["issues", "mapping", "extra"])
    src.write_text(json.dumps(records()))
    mapping.write_text(json.dumps({"10": {"jira_key": "APP-1", "openproject_id": 31}}))
    extra.write_text('{"EXT-4":"13"}')
    args = [
        "--archive",
        str(archive),
        "--identity",
        str(keyfile),
        "--mapping",
        str(mapping),
        "capture",
        "--issues",
        str(src),
        "--jira",
        "https://jira.example",
        "--project",
        "APP",
        "--epic-field",
        "customfield_42",
        "--known-ids",
        str(extra),
    ]
    main(args)
    original = archive.read_bytes()
    main(args)
    assert archive.read_bytes() == original
    restored = read_snapshot(archive, x25519.Identity.from_str(keyfile.read_text()))
    assert restored == sample()
    capsys.readouterr()
    main(["--archive", str(archive), "--identity", str(keyfile), "read"])
    assert json.loads(capsys.readouterr().out) == restored
    assert len(restored["references"]) == 2
    assert "restricted foreign title" not in json.dumps(restored)
    with pytest.raises(pyrage.DecryptError):
        read_snapshot(archive, x25519.Identity.generate())
    with pytest.raises(ValueError, match="differs"):
        write_snapshot(archive, {**restored, "project": "OTHER"}, identity)


def test_reject_an_archive_created_by_someone_who_only_knows_public_key(tmp_path):
    identity = x25519.Identity.generate()
    path = tmp_path / "untrusted.age"
    path.write_bytes(
        pyrage.encrypt(json.dumps({"snapshot": sample(), "proof": "invalid"}).encode(), [identity.to_public()])
    )
    with pytest.raises(ValueError, match="Invalid deferred"):
        read_snapshot(path, identity)


def test_partial_or_conflicting_source_continues_to_block_writes(tmp_path):
    with pytest.raises(ValueError, match="complete source"):
        inventory(
            [], {"APP-1": ("10", 31)}, project="APP", jira="https://jira.example", epic_field="epicLink", identities={}
        )
    path = tmp_path / "mapping"
    path.write_text(json.dumps({str(n): {"jira_key": f"APP-{n}", "openproject_id": 33} for n in [10, 11]}))
    with pytest.raises(ValueError, match="Multiple keys"):
        mappings([path])
    client = Mock()
    data = sample()
    assert apply(data, {"APP-1": ("10", 31)}, client) == {"held": 2, "created": 0, "confirmed": 0}
    with pytest.raises(ValueError, match="different immutable"):
        apply(data, {"APP-1": ("10", 31), "EXT-3": ("100", 33)}, client)
    client.execute_script_with_data.assert_not_called()


def test_applying_requires_exact_target_acknowledgement():
    data = sample()
    client = Mock()
    client.execute_script_with_data.return_value = {"status": "success", "data": {"confirmed": 1, "created": 0}}
    maps = {"APP-1": ("10", 31), "EXT-3": ("12", 33), "EXT-4": ("13", 34)}
    with pytest.raises(ValueError, match="Incomplete target"):
        apply(data, maps, client)


def test_rejects_missing_requested_epic_field_and_stale_internal_link():
    mapped = {"APP-1": ("10", 31)}
    issues = records()
    options = {"project": "APP", "jira": "https://jira.example", "epic_field": "customfield_42", "identities": {}}
    with pytest.raises(ValueError, match="immutable Jira ID"):
        inventory(issues, {**mapped, "EXT-3": ("111", 43)}, **options)
    del issues[0]["fields"]["customfield_42"]
    with pytest.raises(KeyError, match="customfield_42"):
        inventory(issues, mapped, **options)


def test_refuses_to_reuse_an_archive_symlink(tmp_path):
    identity = x25519.Identity.generate()
    original = tmp_path / "original.age"
    snapshot = sample()
    write_snapshot(original, snapshot, identity)
    alias = tmp_path / "alias.age"
    alias.symlink_to(original)
    with pytest.raises(ValueError, match="regular archive destination"):
        write_snapshot(alias, snapshot, identity)


def test_key_only_epic_object_uses_verified_identity_and_rejects_conflicting_ids():
    issues = records()
    epic = {"key": "EXT-4"}
    issues[0]["fields"]["customfield_42"] = epic
    options = {
        "project": "APP",
        "jira": "https://jira.example",
        "epic_field": "customfield_42",
        "identities": {"EXT-4": "13"},
    }
    mapped = {"APP-1": ("10", 31)}
    assert inventory(issues, mapped, **options) == sample()
    epic["id"] = "14"
    with pytest.raises(ValueError, match="conflicting immutable"):
        inventory(issues, mapped, **options)
