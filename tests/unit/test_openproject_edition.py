# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import argparse
import json
import logging
from pathlib import Path

import pyrage
import pytest
from pyrage import x25519

from src.comment_archive.transport import ArchiveError
from src.infrastructure.openproject.openproject_edition import EditionError, administrators, detect_edition
from src.restricted_issues import admin_report, admins, main

pytestmark = pytest.mark.unit
_HOST = "openproject.example.invalid"


class FakeRails:
    def __init__(self, edition, admins=()):
        self.answers = {"EnterpriseToken": edition, "User.where": list(admins)}
        self.queries = []

    def execute_json_query(self, query, timeout=None):
        self.queries.append(query)
        return next(answer for marker, answer in self.answers.items() if marker in query)


class FakeApi:
    def __init__(self, configuration):
        self.configuration = configuration

    def get(self, path, **kwargs):
        assert path == "/api/v3/configuration"
        return self.configuration


def _rails(sharing, comments=False):
    return FakeRails({"host_name": _HOST, "work_package_sharing": sharing, "internal_comments": comments})


def _api(*features):
    return FakeApi({"hostName": _HOST, "availableFeatures": list(features)})


def test_community_without_a_token():
    rails = _rails(False)
    edition = detect_edition(rails, _api())
    assert edition.name == "community"
    assert edition.rails == {"work_package_sharing": False, "internal_comments": False}
    assert edition.available_features == ()
    # The console memoises the tokens per RequestStore; the query must clear them first.
    assert rails.queries[0].startswith("EnterpriseToken.clear_current_tokens_cache;")


def test_enterprise_when_both_sources_allow_sharing(caplog):
    caplog.set_level(logging.INFO)
    edition = detect_edition(_rails(True, True), _api("internal_comments", "work_package_sharing", "board_view"))
    assert edition.name == "enterprise"
    assert edition.available_features == ("board_view", "internal_comments", "work_package_sharing")
    assert "OpenProject edition: enterprise" in caplog.text
    assert "disagree" not in caplog.text


@pytest.mark.parametrize(("rails", "features"), [(True, ()), (False, ("work_package_sharing",))])
def test_disagreement_on_sharing_means_community(caplog, rails, features):
    edition = detect_edition(_rails(rails), _api(*features))
    assert edition.name == "community"
    assert "disagree on work_package_sharing" in caplog.text


@pytest.mark.parametrize(
    ("rails", "configuration"),
    [
        ({"result": "NameError"}, {"hostName": _HOST, "availableFeatures": []}),
        ({"host_name": _HOST, "work_package_sharing": "true", "internal_comments": False}, None),
        ({"host_name": _HOST, "work_package_sharing": True}, None),
        (None, {"hostName": _HOST}),
        (None, {"hostName": _HOST, "availableFeatures": None}),
        (None, {"hostName": _HOST, "availableFeatures": [1]}),
        (None, ["work_package_sharing"]),
        (None, {"hostName": "other.example.invalid", "availableFeatures": []}),
    ],
)
def test_malformed_answers_raise(rails, configuration):
    rails = FakeRails(rails) if rails is not None else _rails(True)
    api = FakeApi(configuration) if configuration is not None else _api("work_package_sharing")
    with pytest.raises(EditionError):
        detect_edition(rails, api)


def test_failed_reads_propagate():
    class Broken:
        def execute_json_query(self, query, timeout=None):
            raise RuntimeError("console gone")

    with pytest.raises(RuntimeError):
        detect_edition(Broken(), _api())


_ADMINS = [
    {"id": 1, "login": "admin", "status": "active"},
    {"id": 2, "login": "ops", "status": "locked"},
    {"id": 3, "login": "lone", "status": "active"},
]


def test_administrators_are_validated_and_exclude_builtin_users():
    rails = FakeRails(None, _ADMINS)
    assert administrators(rails) == _ADMINS
    assert 'type: "User", admin: true' in rails.queries[0]
    for broken in ([{"id": "1", "login": "a", "status": "active"}], [{"id": 1, "login": "a", "status": "deleted"}]):
        with pytest.raises(EditionError):
            administrators(FakeRails(None, broken))
    with pytest.raises(EditionError):
        administrators(FakeRails(None, [_ADMINS[0], _ADMINS[0]]))


def _level(project, level_id, readers, grants=(("group", "leads"),)):
    return {
        "project": project,
        "id": level_id,
        "name": f"L{level_id}",
        "grants": [list(g) for g in grants],
        "static_readers": readers,
    }


_ARCHIVE = {
    "levels": {
        "ABC:1": _level("ABC", "1", ["Alice"]),
        "ABC:2": _level("ABC", "2", ["bob"]),
        "XYZ:3": _level("XYZ", "3", [], grants=(("unsupported", "Current user"),)),
    }
}
_USERS = {
    "k1": {"jira_name": "alice", "openproject_id": 1},
    "k2": {"jira_name": "bob", "openproject_id": 2},
    "k3": {"jira_name": "carol", "openproject_id": 9},  # not an administrator
    "k4": {"jira_name": "lone", "openproject_id": 3, "restored_from_op": True},  # a guessed name
}


def _edition():
    return detect_edition(_rails(False), _api())


def test_admin_report_lists_non_readers_per_level_and_unmapped_admins():
    value = admin_report(_ARCHIVE, _USERS, _ADMINS, _edition(), "sha")
    by_level = {row["level_id"]: row for row in value["levels"]}
    # alice reads level 1 (case differs in the mapping), bob reads level 2.
    assert by_level["1"]["admins"] == [{"login": "ops", "status": "locked"}]
    assert by_level["2"]["admins"] == [{"login": "admin", "status": "active"}]
    assert by_level["1"]["admins_not_readers"] == by_level["2"]["admins_not_readers"] == 1
    assert value["unmapped_admins"] == [{"login": "lone", "status": "active"}]
    assert by_level["3"] == {
        "project": "XYZ",
        "level_id": "3",
        "level": "L3",
        "readers_computed": False,
        "admins_not_readers": 0,
        "admins": [],
    }
    text = json.dumps(value)
    assert "carol" not in text and "Alice" not in text and "bob" not in text
    assert value["edition"] == "community" and value["administrators"] == 3
    assert value["mapped_openproject_users"] == 3


@pytest.mark.parametrize(
    "mapping",
    [
        {},
        {"k4": _USERS["k4"]},
        {"k1": {"jira_name": "alice", "openproject_id": None}},
        [],
        {"k1": {"openproject_id": 1}},
    ],
)
def test_admin_report_stops_without_a_usable_user_mapping(mapping):
    with pytest.raises(ArchiveError):
        admin_report(_ARCHIVE, mapping, _ADMINS, _edition(), "sha")


def test_admins_command_reads_the_archive_and_writes_a_private_report(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    identity = x25519.Identity.generate()
    archive = {"format": "j2o-restricted-issues/1", **_ARCHIVE}
    (tmp_path / "restricted.age").write_bytes(pyrage.encrypt(json.dumps(archive).encode(), [identity.to_public()]))
    monkeypatch.setattr("src.restricted_issues.Endpoint", lambda *a, **kw: _api("work_package_sharing"))
    (tmp_path / "user_mapping.json").write_text(json.dumps(_USERS))
    rails = FakeRails({"host_name": _HOST, "work_package_sharing": True, "internal_comments": False}, _ADMINS)
    args = argparse.Namespace(
        archive=Path("restricted.age"),
        report=Path("admins.json"),
        user_mapping=Path("user_mapping.json"),
        openproject="https://" + _HOST,
    )
    value = admins(args, identity, rails)
    assert value["edition"] == "enterprise"
    assert json.loads((tmp_path / "admins.json").read_text()) == value
    assert (tmp_path / "admins.json").stat().st_mode & 0o777 == 0o600


def test_admins_subcommand_warns_and_stops_only_on_an_unverified_answer(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    identity = x25519.Identity.generate()
    (tmp_path / "operator.agekey").write_text(str(identity))
    archive = {"format": "j2o-restricted-issues/1", **_ARCHIVE}
    (tmp_path / "restricted.age").write_bytes(pyrage.encrypt(json.dumps(archive).encode(), [identity.to_public()]))
    monkeypatch.setattr("src.restricted_issues.Endpoint", lambda *a, **kw: _api())
    (tmp_path / "user_mapping.json").write_text(json.dumps(_USERS))
    rails = FakeRails({"host_name": _HOST, "work_package_sharing": False, "internal_comments": False}, _ADMINS)
    client = "src.infrastructure.openproject.openproject_client.OpenProjectClient"
    monkeypatch.setattr(client, lambda: rails)
    argv = ["admins", "--identity", "operator.agekey", "--archive", "restricted.age", "--user-mapping"]
    argv += ["user_mapping.json", "--openproject"]
    assert main([*argv, "https://" + _HOST, "--report", "admins.json"]) == 0
    out = capsys.readouterr()
    # The repository's log handler writes to standard output; the summary is the last line.
    assert json.loads(out.out.splitlines()[-1]) == {"edition": "community", "administrators": 3}
    assert '"unmapped": 1' in out.err and '"ABC:1": 1' in out.err
    rails.answers["EnterpriseToken"] = {"result": "NameError"}
    assert main([*argv, "https://" + _HOST, "--report", "again.json"]) == 1
    assert "Malformed Enterprise token answer" in capsys.readouterr().err
    assert not (tmp_path / "again.json").exists()
