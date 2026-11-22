# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Version copies per audience for shared level work packages (ADR-010, update 2026-11-22)."""

import hashlib
from unittest.mock import Mock

import pytest

from src.application.components.level_access import Audience
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


def _copy(outcome, sid, users=()):
    return outcome.op.copies[100, level_copy_id(sid, users)]


def test_shared_work_packages_get_the_copies_of_their_audience(level_setup, shared):
    outcome = versions._run(level_setup, "versions", live=LIVE, runs=2, **REPORT)
    first, second = outcome.results
    assert first.success, first
    assert second.success, second
    pair, single = audience_token(PAIR), audience_token(SINGLE)
    rows = [(row["id"], row["attributes"]["name"], row.get("audience")) for row in outcome.op.copy_syncs[0]["versions"]]
    assert rows == [
        ("41", "R1", None),
        (f"41:audience:{pair}", f"R1 [shared {pair}]", [43, 44]),
        (f"41:audience:{single}", f"R1 [shared {single}]", [45]),
        ("42", "R2", None),
        (f"42:audience:{single}", f"R2 [shared {single}]", [45]),
    ]
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


@pytest.mark.parametrize("users", [frozenset(), None], ids=["no-user", "no-readers-entry"])
def test_a_shared_work_package_without_a_known_audience_keeps_being_withheld(level_setup, shared, users):
    shares = {key: value for key, value in SHARED.items() if key != "APP-15"}
    shared(shares if users is None else shares | {"APP-15": users})
    outcome = versions._run(level_setup, "versions", live=LIVE, **REPORT)
    assert outcome.result.success, outcome.result
    assert outcome.result.details["level_versions_withheld"] == 1
    assert all(u["id"] != 905 for u in outcome.op.updates)
    assert all(row.get("audience") != [45] for row in outcome.op.copy_syncs[0]["versions"])


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
    pair, single = audience_token(PAIR), audience_token(SINGLE)
    assert sorted(outcome.op.copy_syncs[0]["sprints"], key=lambda row: row["id"]) == sorted(
        [
            {"id": "42", "version": 9},
            {"id": f"42:audience:{pair}", "version": 9, "audience": [43, 44], "suffix": f" [shared {pair}]"},
            {"id": f"42:audience:{single}", "version": 9, "audience": [45], "suffix": f" [shared {single}]"},
        ],
        key=lambda row: row["id"],
    )
    copy = {users: outcome.op.copies[100, level_copy_id("42", users)] for users in ((), PAIR, SINGLE)}
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
    monkeypatch.setattr("src.infrastructure.openproject.version_catalog.audience_token", lambda users: "c0ffee000000")


def test_two_audiences_with_one_token_never_share_a_version_copy(level_setup, shared, one_token):
    outcome = versions._run(level_setup, "versions", live=LIVE, **REPORT)
    assert not outcome.result.success
    assert not outcome.op.copies
    assert not versions._level_writes(outcome, "versions")
    assert outcome.result.details["security_level"]["refused"] == 5


def test_two_audiences_with_one_token_never_share_a_sprint_copy(sprint_level_setup, shared, one_token):  # noqa: F811
    rows = [sprints.level_sprint_row(n, "10000") for n in (10, 11, 12, 14, 15)]
    outcome = sprints.level_sprint_run(sprint_level_setup, rows=rows, live=LIVE, **REPORT)
    copy = {users: outcome.op.copies[100, level_copy_id("42", users)] for users in ((), PAIR)}
    # APP-15 has another audience than APP-12 but the same copy ID: it is refused, not merged.
    assert sprints.level_assignments(outcome.op) == {
        (100, 900, (copy[()],)),
        (100, 901, (copy[()],)),
        (100, 902, (copy[PAIR],)),
        (100, 904, (copy[PAIR],)),
    }
    assert outcome.result.details["security_level"]["refused"] == 1
    assert [row.get("audience") for row in outcome.op.copy_syncs[0]["sprints"]] == [None, [43, 44]]


def test_the_audience_token_names_the_sorted_user_ids():
    assert audience_token([44, 43]) == audience_token((43, 44)) == hashlib.sha256(b"43,44").hexdigest()[:12]
    assert audience_token([43]) != audience_token([43, 44])
    assert level_copy_id("41") == "41"
    assert level_copy_id("41", (43, 44)) == f"41:audience:{audience_token((43, 44))}"
    for invalid in ([], [0], ["43"]):
        with pytest.raises(ValueError, match="audience"):
            audience_token(invalid)


def test_the_version_writer_adopts_only_the_copy_of_the_same_audience_in_a_readers_sub_project():
    client = versions._Answer({"versions": {"41": 1000, f"41:audience:{audience_token(PAIR)}": 1001}, "changed": 0})
    audience = {"members": [41], "shares": {"APP-12": [43, 44]}}
    copies = sync_level_versions(
        client, (100, 7, audience), "https://jira.local", "APP", [versions.CATALOGUE[0]], audiences={"41": {(), PAIR}}
    )
    assert copies == {"41": 1000, f"41:audience:{audience_token(PAIR)}": 1001}
    (script,) = client.scripts
    static, copy = versions._payload(script)["versions"]
    assert "audience" not in static
    assert copy["audience"] == [43, 44]
    assert copy["attributes"] == static["attributes"] | {"name": f"R1 [shared {audience_token(PAIR)}]"}
    assert "origin['audience'] = audience if audience" in script
    assert "raise 'Use audience version copies only in a sub-project of its readers' if audience && !staging" in script
    assert "audiences[[source, row.fetch('id')]] != audience" in script


def test_the_version_writer_refuses_copy_names_that_collide():
    row = {**versions.CATALOGUE[1], "name": f"R1 [shared {audience_token(PAIR)}]"}
    client = versions._Answer({})
    with pytest.raises(ValueError, match="Ambiguous"):
        sync_level_versions(
            client,
            (100, 7, {"members": [], "shares": {}}),
            "https://jira.local",
            "APP",
            [versions.CATALOGUE[0], row],
            audiences={"41": {PAIR}, "42": {()}},
        )
    assert not client.scripts


def test_the_sprint_writer_checks_each_audience_copy_id():
    client = Mock()
    nobody = {"members": [], "shares": {}}
    cid = level_copy_id("42", PAIR)
    client.execute_json_query.return_value = {"versions": {f"sprint:{cid}": 2001}, "changed": 1}
    origin = ("https://jira.local", "APP", "10")
    assert sync_level_sprints(client, (100, 7, nobody), origin, {cid: 9}, {cid: PAIR}) == {cid: 2001}
    script = client.execute_json_query.call_args.args[0]
    assert "attrs['name'] = attrs.fetch('name') + row.fetch('suffix')" in script
    other = level_copy_id("42", SINGLE)
    # An audience copy ID without its users, or with the users of another audience, is refused.
    for wrong, audiences in (({cid: 9}, {}), ({other: 9}, {other: PAIR})):
        with pytest.raises(ValueError, match="Invalid"):
            sync_level_sprints(client, (100, 7, nobody), origin, wrong, audiences)
