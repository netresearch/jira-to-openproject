# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Version copies per audience for shared level work packages (ADR-010, update 2026-11-22)."""

import hashlib
import hmac
import os
import stat
from unittest.mock import Mock

import pytest

import src.config as cfg
from src.application.components.level_access import Audience
from src.application.components.level_versions import AUDIENCE_KEY_FILE
from src.infrastructure.openproject.version_catalog import (
    audience_token,
    level_copy_id,
    sync_level_sprints,
    sync_level_versions,
)
from tests.unit import test_level_versions as versions
from tests.unit import test_level_work_packages as wp
from tests.unit import test_sprint_epic_migration as sprints
from tests.unit.test_level_work_packages import setup as level_work_package_setup  # noqa: F401  (fixture)
from tests.unit.test_sprint_epic_migration import level_setup as sprint_level_setup  # noqa: F401  (fixture)

# APP-12 and APP-14 are shared with the same two extra readers, APP-15 with another one.
SHARED = {"APP-12": frozenset({43, 44}), "APP-14": frozenset({44, 43}), "APP-15": frozenset({45})}
PAIR, SINGLE = (43, 44), (45,)
KEY, OTHER_KEY = bytes(range(32)), bytes(range(1, 33))
TOKEN = audience_token(PAIR, KEY)
LIVE = {
    "field": True,
    "work_packages": [900, 901, 902, 904, 905],
    "keys": [[900, "APP-10"], [901, "APP-11"], [902, "APP-12"], [904, "APP-14"], [905, "APP-15"]],
}
REPORT = {
    "edition": "enterprise",
    "report_levels": [wp.row(per_issue=3), wp.row("10001", ("APP-20",), 0, 0)],
    "archived": [
        *wp.ARCHIVED,
        {"key": "APP-14", "level_id": "10000", "class": "per_issue"},
        {"key": "APP-15", "level_id": "10000", "class": "per_issue"},
    ],
}


def _level_issues():
    return [*wp.level_issues(), wp.issue(14, "10000"), wp.issue(15, "10000")]


@pytest.fixture
def level_setup(level_work_package_setup):  # noqa: F811 (the imported fixture, requested by name)
    return level_work_package_setup


@pytest.fixture
def shared(monkeypatch):
    """Three per_issue issues in the level searches and a readers file that shares them."""

    def configure(shares=SHARED):
        audience = Audience(
            members={"APP:10000": frozenset({41})}, shares=dict(shares), share_levels=dict.fromkeys(shares, "APP:10000")
        )
        monkeypatch.setattr(
            "src.application.components.level_issues.staging_audience", lambda mappings, report, edition: audience
        )

    monkeypatch.setattr(versions, "level_issues", _level_issues)
    monkeypatch.setattr(sprints, "level_issues", _level_issues)
    for number, refs in ((14, ["41"]), (15, ["41", "42"])):
        monkeypatch.setitem(versions.LEVEL_OF, number, "10000")
        monkeypatch.setitem(versions.REFS, number, refs)
    configure()
    return configure


def _key_file():
    return cfg.get_path("data") / AUDIENCE_KEY_FILE


def _token(users):
    """The token of the users under the key the run generated in the data directory."""
    return audience_token(users, bytes.fromhex(_key_file().read_text().strip()))


def _copy(outcome, sid, users=()):
    return outcome.op.copies[100, level_copy_id(sid, _token(users) if users else None)]


def test_shared_work_packages_get_the_copies_of_their_audience(level_setup, shared):
    outcome = versions._run(level_setup, "versions", live=LIVE, runs=2, **REPORT)
    first, second = outcome.results
    assert first.success, first
    assert second.success, second
    pair, single = _token(PAIR), _token(SINGLE)
    rows = [(row["id"], row["attributes"]["name"], row.get("audience")) for row in outcome.op.copy_syncs[0]["versions"]]
    # OpenProject gets the token, never a user ID.
    assert sorted(rows, key=str) == sorted(
        [
            ("41", "R1", None),
            (f"41:audience:{pair}", f"R1 [shared {pair}]", pair),
            (f"41:audience:{single}", f"R1 [shared {single}]", single),
            ("42", "R2", None),
            (f"42:audience:{single}", f"R2 [shared {single}]", single),
        ],
        key=str,
    )
    # APP-14 reuses the copy of APP-12; the repeat run adopts all five copies.
    assert versions._level_writes(outcome, "versions") == {
        (100, 900, (_copy(outcome, "41"),)),
        (100, 901, (_copy(outcome, "41"), _copy(outcome, "42"))),
        (100, 902, (_copy(outcome, "41", PAIR),)),
        (100, 904, (_copy(outcome, "41", PAIR),)),
        (100, 905, (_copy(outcome, "41", SINGLE), _copy(outcome, "42", SINGLE))),
    }
    assert len(outcome.op.copy_syncs) == 2
    assert outcome.op.copy_syncs[0] == outcome.op.copy_syncs[1]
    assert len(outcome.op.copies) == 5
    assert outcome.writes.count("security_level_version") == 1
    assert second.details["level_versions_withheld"] == 0
    mapping = outcome.mappings.data["security_level_version"]
    assert mapping["APP:10000:41"] == {
        "openproject_id": _copy(outcome, "41"),
        "openproject_project_id": 100,
        "security_level_project": "APP:10000",
        "jira_project_key": "APP",
        "jira_version_id": "41",
        "name": "R1",
    }
    assert mapping[f"APP:10000:41:audience:{pair}"] == {
        "openproject_id": _copy(outcome, "41", PAIR),
        "openproject_project_id": 100,
        "security_level_project": "APP:10000",
        "jira_project_key": "APP",
        "jira_version_id": "41",
        "name": "R1",
        "audience": [43, 44],
    }
    assert sorted(mapping) == sorted(
        ["APP:10000:41", "APP:10000:42", f"APP:10000:41:audience:{pair}"]
        + [f"APP:10000:{sid}:audience:{single}" for sid in ("41", "42")]
    )


def test_the_audience_key_is_generated_once_with_mode_0600_and_reused(level_setup, shared):
    outcome = versions._run(level_setup, "versions", live=LIVE, **REPORT)
    assert outcome.result.success, outcome.result
    assert stat.S_IMODE(os.stat(_key_file()).st_mode) == 0o600
    key = bytes.fromhex(_key_file().read_text().strip())
    assert len(key) == 32
    again = versions._run(level_setup, "versions", live=LIVE, **REPORT)
    assert bytes.fromhex(_key_file().read_text().strip()) == key
    assert again.op.copy_syncs == outcome.op.copy_syncs


def test_a_lost_audience_key_gives_new_copies_and_never_reuses_the_old_ones(level_setup, shared):
    first = versions._run(level_setup, "versions", live=LIVE, **REPORT)
    old = {row["id"] for row in first.op.copy_syncs[0]["versions"] if row.get("audience")}
    _key_file().unlink()
    second = versions._run(level_setup, "versions", live=LIVE, **REPORT)
    new = {row["id"] for row in second.op.copy_syncs[0]["versions"] if row.get("audience")}
    assert len(old) == len(new) == 3
    assert old.isdisjoint(new)


@pytest.mark.parametrize(
    "content", ["", "zz" * 32, "ab" * 31, "not a key\n"], ids=["empty", "not-hex", "short", "text"]
)
def test_a_malformed_audience_key_stops_the_run_before_any_write(level_setup, shared, content):
    level_setup(**REPORT)
    _key_file().write_text(content)
    outcome = versions._run(level_setup, "versions", live=LIVE, **REPORT)
    assert not outcome.result.success
    assert "audience key" in outcome.result.message
    assert not outcome.op.copy_syncs
    assert not outcome.op.parent_syncs
    assert not outcome.op.updates
    assert _key_file().read_text() == content


def test_an_unreadable_audience_key_stops_the_sprint_run(sprint_level_setup, shared):  # noqa: F811
    sprint_level_setup(**REPORT)
    _key_file().mkdir()
    rows = [sprints.level_sprint_row(n, "10000") for n in (10, 11, 12, 14, 15)]
    outcome = sprints.level_sprint_run(sprint_level_setup, rows=rows, live=LIVE, **REPORT)
    assert not outcome.result.success
    assert "audience key" in outcome.result.message
    outcome.op.execute_json_query.assert_not_called()


@pytest.mark.parametrize("users", [frozenset(), None], ids=["no-user", "no-readers-entry"])
def test_a_shared_work_package_without_a_known_audience_keeps_being_withheld(level_setup, shared, users):
    shares = {key: value for key, value in SHARED.items() if key != "APP-15"}
    shared(shares if users is None else shares | {"APP-15": users})
    outcome = versions._run(level_setup, "versions", live=LIVE, **REPORT)
    assert outcome.result.success, outcome.result
    assert outcome.result.details["level_versions_withheld"] == 1
    assert all(u["id"] != 905 for u in outcome.op.updates)
    assert all("audience" not in row or row["id"].startswith("41:") for row in outcome.op.copy_syncs[0]["versions"])
    assert len([row for row in outcome.op.copy_syncs[0]["versions"] if "audience" in row]) == 1


def test_without_a_known_audience_no_audience_key_is_created(level_setup, shared):
    shared({})
    outcome = versions._run(level_setup, "versions", live=LIVE, **REPORT)
    assert outcome.result.details["level_versions_withheld"] == 3
    assert not _key_file().exists()


def test_affected_versions_of_shared_work_packages_link_the_static_copy(level_setup, shared):
    outcome = versions._run(level_setup, "affects", live=LIVE, **REPORT)
    assert outcome.result.success, outcome.result
    assert [row["id"] for row in outcome.op.copy_syncs[0]["versions"]] == ["41", "42"]
    assert all("audience" not in row for row in outcome.op.copy_syncs[0]["versions"])
    assert {(wp_id, ids) for _, wp_id, ids in versions._level_writes(outcome, "affects")} >= {
        (902, ("41",)),
        (905, ("41", "42")),
    }


def test_shared_work_packages_get_the_sprint_copy_of_their_audience(sprint_level_setup, shared):  # noqa: F811
    rows = [sprints.level_sprint_row(n, "10000") for n in (10, 11, 12, 14, 15)]
    outcome = sprints.level_sprint_run(sprint_level_setup, rows=rows, live=LIVE, runs=2, **REPORT)
    # The fixture's ordinary APP-1 names the level issue APP-10 as its epic: that one unresolved
    # epic is the only failure of each run.
    for result in outcome.results:
        assert result.details["unresolved_epics"] == {"APP-1": "APP-10"}
        assert result.failed == 1
        assert result.details["security_level"]["refused"] == 0
        assert result.details["level_versions_withheld"] == 0
    pair, single = _token(PAIR), _token(SINGLE)
    assert sorted(outcome.op.copy_syncs[0]["sprints"], key=lambda row: row["id"]) == sorted(
        [
            {"id": "42", "version": 9},
            {"id": f"42:audience:{pair}", "version": 9, "audience": pair, "suffix": f" [shared {pair}]"},
            {"id": f"42:audience:{single}", "version": 9, "audience": single, "suffix": f" [shared {single}]"},
        ],
        key=lambda row: row["id"],
    )
    copy = {users: _copy(outcome, "42", users) for users in ((), PAIR, SINGLE)}
    assert sprints.level_assignments(outcome.op) == {
        (100, 900, (copy[()],)),
        (100, 901, (copy[()],)),
        (100, 902, (copy[PAIR],)),
        (100, 904, (copy[PAIR],)),
        (100, 905, (copy[SINGLE],)),
    }
    assert len(outcome.op.copies) == 3
    assert outcome.writes.count("security_level_version") == 1
    mapping = outcome.mappings.data["security_level_version"]
    assert mapping[f"APP:10000:sprint:42:audience:{pair}"] == {
        "openproject_id": copy[PAIR],
        "openproject_project_id": 100,
        "security_level_project": "APP:10000",
        "jira_project_key": "APP",
        "jira_sprint_id": "42",
        "parent_version_id": 9,
        "audience": [43, 44],
    }
    assert "audience" not in mapping["APP:10000:sprint:42"]


@pytest.fixture
def one_token(monkeypatch):
    """Every audience gets the same token, as two audiences whose hashes collide would."""
    monkeypatch.setattr("src.application.components.level_versions.audience_token", lambda users, key: "c0ffee000000")


def test_two_audiences_with_one_token_never_share_a_version_copy(level_setup, shared, one_token):
    outcome = versions._run(level_setup, "versions", live=LIVE, **REPORT)
    assert not outcome.result.success
    assert not outcome.op.copies
    assert not versions._level_writes(outcome, "versions")
    assert outcome.result.details["security_level"]["refused"] == 5


def test_two_audiences_with_one_token_never_share_a_sprint_copy(sprint_level_setup, shared, one_token):  # noqa: F811
    rows = [sprints.level_sprint_row(n, "10000") for n in (10, 11, 12, 14, 15)]
    outcome = sprints.level_sprint_run(sprint_level_setup, rows=rows, live=LIVE, **REPORT)
    copy = {
        users: outcome.op.copies[100, level_copy_id("42", "c0ffee000000" if users else None)] for users in ((), PAIR)
    }
    # APP-15 has another audience than APP-12 but the same copy ID: it is refused, not merged.
    assert sprints.level_assignments(outcome.op) == {
        (100, 900, (copy[()],)),
        (100, 901, (copy[()],)),
        (100, 902, (copy[PAIR],)),
        (100, 904, (copy[PAIR],)),
    }
    assert outcome.result.details["security_level"]["refused"] == 1
    assert [row.get("audience") for row in outcome.op.copy_syncs[0]["sprints"]] == [None, "c0ffee000000"]
    entry = outcome.mappings.data["security_level_version"]["APP:10000:sprint:42:audience:c0ffee000000"]
    assert entry["audience"] == [43, 44]


def test_the_audience_token_is_keyed_and_stable():
    expected = hmac.new(KEY, b"43,44", hashlib.sha256).hexdigest()[:12]
    assert audience_token([44, 43], KEY) == audience_token((43, 44), KEY) == expected
    assert audience_token(PAIR, KEY) != audience_token(PAIR, OTHER_KEY)
    # Not the unkeyed hash of the IDs, which anyone could recompute from a few small integers.
    assert expected != hashlib.sha256(b"43,44").hexdigest()[:12]
    assert audience_token([43], KEY) != audience_token([43, 44], KEY)
    assert level_copy_id("41") == "41"
    assert level_copy_id("41", TOKEN) == f"41:audience:{TOKEN}"
    for invalid in ([], [0], ["43"]):
        with pytest.raises(ValueError, match="audience"):
            audience_token(invalid, KEY)
    for key in (b"", bytes(31), "0" * 32):
        with pytest.raises(ValueError, match="key"):
            audience_token(PAIR, key)


def test_the_version_writer_sends_only_the_token_and_adopts_only_the_same_one():
    client = versions._Answer({"versions": {"41": 1000, f"41:audience:{TOKEN}": 1001}, "changed": 0})
    audience = {"members": [41], "shares": {"APP-12": [43, 44]}}
    copies = sync_level_versions(
        client,
        (100, 7, audience),
        "https://jira.local",
        "APP",
        [versions.CATALOGUE[0]],
        audiences={"41": {None, TOKEN}},
    )
    assert copies == {"41": 1000, f"41:audience:{TOKEN}": 1001}
    (script,) = client.scripts
    static, copy = versions._payload(script)["versions"]
    assert "audience" not in static
    # The origin is built from these fields: the copy ID and the token, no user ID.
    assert copy == static | {
        "id": f"41:audience:{TOKEN}",
        "attributes": static["attributes"] | {"name": f"R1 [shared {TOKEN}]"},
        "audience": TOKEN,
    }
    assert "origin['audience'] = audience if audience" in script
    assert "raise 'Use audience version copies only in a sub-project of its readers' if audience && !staging" in script
    assert "audiences[[source, row.fetch('id')]] != audience" in script


def test_the_version_writer_refuses_copy_names_that_collide():
    row = {**versions.CATALOGUE[1], "name": f"R1 [shared {TOKEN}]"}
    client = versions._Answer({})
    with pytest.raises(ValueError, match="Ambiguous"):
        sync_level_versions(
            client,
            (100, 7, {"members": [], "shares": {}}),
            "https://jira.local",
            "APP",
            [versions.CATALOGUE[0], row],
            audiences={"41": {TOKEN}, "42": {None}},
        )
    assert not client.scripts


def test_the_sprint_writer_checks_each_audience_copy_id():
    client = Mock()
    nobody = {"members": [], "shares": {}}
    cid = level_copy_id("42", TOKEN)
    client.execute_json_query.return_value = {"versions": {f"sprint:{cid}": 2001}, "changed": 1}
    origin = ("https://jira.local", "APP", "10")
    assert sync_level_sprints(client, (100, 7, nobody), origin, {cid: 9}, {cid: TOKEN}) == {cid: 2001}
    script = client.execute_json_query.call_args.args[0]
    assert "attrs['name'] = attrs.fetch('name') + row.fetch('suffix')" in script
    assert versions._payload(script)["sprints"] == [
        {"id": cid, "version": 9, "audience": TOKEN, "suffix": f" [shared {TOKEN}]"}
    ]
    other = level_copy_id("42", audience_token(SINGLE, KEY))
    # An audience copy ID without its token, or with the token of another audience, is refused.
    for wrong, audiences in (({cid: 9}, {}), ({other: 9}, {other: TOKEN})):
        with pytest.raises(ValueError, match="Invalid"):
            sync_level_sprints(client, (100, 7, nobody), origin, wrong, audiences)
