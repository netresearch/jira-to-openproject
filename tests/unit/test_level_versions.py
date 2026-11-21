# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Fix and affected versions of level work packages as sub-project copies (ADR-010)."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components.affects_versions_migration import AffectsVersionsMigration
from src.application.components.versions_migration import VersionsMigration
from tests.unit.test_level_attachments import LEVEL_MAP, LIVE
from tests.unit.test_level_attachments import LIGHT as LEVEL_SEARCH
from tests.unit.test_level_work_packages import SUB_PROJECT, level_issues
from tests.unit.test_level_work_packages import setup as level_work_package_setup  # noqa: F401  (fixture)

COMPONENTS = {"versions": VersionsMigration, "affects": AffectsVersionsMigration}
FIELDS = {"versions": "project,security,fixVersions", "affects": "project,security,versions"}
CATALOGUE = [
    {"id": str(sid), "projectId": "10", "name": name, "released": False, "archived": False}
    for sid, name in ((41, "R1"), (42, "R2"), (43, "R3"))
]
# The versions each issue uses; APP-1 is the ordinary issue of the parent project.
REFS = {1: ["41"], 10: ["41"], 11: ["41", "42"], 12: ["41"], 13: ["41"], 20: ["41"]}
LEVEL_OF = {10: "10000", 11: "10000", 12: "10000", 13: "10000", 20: "10001"}
SECOND_LEVEL = {**SUB_PROJECT, "level_id": "10001", "openproject_id": 101, "openproject_identifier": "app_level_10001"}
SECOND_LIVE = {101: {"field": True, "work_packages": [920], "keys": [[920, "APP-20"]]}}
ORDINARY = {"1": {"jira_key": "APP-1", "openproject_id": 500, "project_key": "APP"}}


@pytest.fixture
def level_setup(level_work_package_setup):  # noqa: F811 (the imported fixture, requested by name)
    """The reports, mappings and switch of the level work package tests."""
    return level_work_package_setup


def _refs(number):
    return [{"id": sid, "name": f"R{int(sid) - 40}"} for sid in REFS[number]]


def _fields(number, level):
    fields = {"project": {"id": "10", "key": "APP"}, "fixVersions": _refs(number), "versions": _refs(number)}
    return fields | ({"security": {"id": level}} if level else {})


class FakeJira:
    """Answer the level searches, the unrestricted search and the catalogue; record every call."""

    def __init__(self, moved=(), omitted=(), unrestricted_fails=False):
        self.base_url = "https://jira.local"
        self.moved, self.omitted = set(moved), set(omitted)
        self.searches, self.unrestricted, self.catalogues = [], [], []
        self.unrestricted_fails = unrestricted_fails
        self.get_projects = Mock(return_value=[{"key": "APP"}])
        self.jira = Mock()
        self.jira.search_issues.side_effect = self.search
        self.projects = self

    def search(self, jql, startAt, maxResults, **kwargs):
        assert jql == LEVEL_SEARCH, jql
        self.searches.append(kwargs.get("fields"))
        if kwargs.get("fields") == "security":
            rows = level_issues()
        else:
            rows = [
                SimpleNamespace(
                    key=f"APP-{n}", id=str(n), raw={"fields": _fields(n, "10001" if n in self.moved else lvl)}
                )
                for n, lvl in LEVEL_OF.items()
                if n not in self.omitted
            ]
        return rows[startAt : startAt + maxResults]

    def get_versions(self, key):
        self.catalogues.append(key)
        return [dict(row) for row in CATALOGUE]

    def unrestricted_version_issues(self, key, keys):
        self.unrestricted.append((key, list(keys)))
        if self.unrestricted_fails:
            raise OSError("search unavailable")
        return {k: {"id": k.split("-")[1], "key": k, "fields": _fields(int(k.split("-")[1]), None)} for k in keys}


class FakeOpenProject:
    """Answer the live sub-project read and the Rails writers; keep the version copies per sub-project."""

    def __init__(self, live=None, by_project=None, members=()):
        self.live, self.by_project, self.members = LIVE if live is None else live, by_project or {}, set(members)
        self.live_reads, self.parent_syncs, self.copy_syncs, self.observed, self.updates = [], [], [], [], []
        self.copies: dict[tuple[int, str], int] = {}

    def execute_query_to_json_file(self, script):
        values = json.loads(script.split("\n")[1])
        self.live_reads.append(values)
        return json.loads(json.dumps(self.by_project.get(values["project_id"], self.live)))

    def execute_json_query(self, script):
        payload = json.loads(base64.b64decode(script.split("strict_decode64('", 1)[1].split("')", 1)[0]))
        level = "Unexpected target sub-project" in script
        if "Affects Versions" in script:
            self.observed.append((level, payload))
            return {"updated": len(payload["issues"]), "native": True}
        if not level:
            self.parent_syncs.append(payload)
            return {
                "versions": {row["attributes"]["name"]: int(row["id"]) for row in payload["versions"]},
                "changed": 0,
            }
        self.copy_syncs.append(payload)
        return {"versions": self._copies(payload), "changed": 0}

    def _copies(self, payload):
        project = payload["project"]
        new = [row["id"] for row in payload["versions"] if (project, row["id"]) not in self.copies]
        if new and project in self.members:
            raise RuntimeError("Create or change level versions before granting project access")
        for sid in new:
            self.copies[project, sid] = 1000 + len(self.copies)
        return {row["id"]: self.copies[project, row["id"]] for row in payload["versions"]}

    def ensure_wp_custom_field_id(self, name, field_format):
        return 555

    def batch_update_work_packages(self, updates):
        self.updates.extend(updates)
        return {"updated": len(updates), "failed": 0}


def _run(level_setup, component, *, live=None, moved=(), omitted=(), ordinary=None, level_map=None, runs=1, **kw):
    second_level = kw.pop("second_level", False)
    members, unrestricted_fails = kw.pop("members", ()), kw.pop("unrestricted_fails", False)
    if second_level:
        kw["mapping"] = {"security_level_project": {"APP:10000": SUB_PROJECT, "APP:10001": SECOND_LEVEL}}
    mappings, _ = level_setup(**kw)
    mappings.data["work_package"] = {**ORDINARY, **(ordinary or {})}
    mappings.data["security_level_work_package"] = dict(LEVEL_MAP if level_map is None else level_map)
    writes = []
    original = mappings.set_mapping
    mappings.set_mapping = lambda name, value: (writes.append(name), original(name, value))
    jira = FakeJira(moved, omitted, unrestricted_fails)
    op = FakeOpenProject(live, SECOND_LIVE if second_level else None, members)
    migration = COMPONENTS[component](jira_client=jira, op_client=op)
    results = [migration.run() for _ in range(runs)]
    return SimpleNamespace(result=results[-1], results=results, jira=jira, op=op, writes=writes, mappings=mappings)


def _level_writes(outcome, component):
    """(sub-project, work package, version IDs) of every level write; the IDs are copies for versions."""
    if component == "versions":
        return {
            (u["jira_version_issue_origin"]["project"], u["id"], tuple(u["target_version_ids"]))
            for u in outcome.op.updates
            if u["jira_version_issue_origin"]["project"] != 7
        }
    return {
        (p["project"], row["wp"], tuple(ref["id"] for ref in row["versions"]))
        for level, p in outcome.op.observed
        if level
        for row in p["issues"]
    }


def _ordinary_writes(outcome, component):
    if component == "versions":
        return [
            (u["id"], u["target_version_ids"])
            for u in outcome.op.updates
            if u["jira_version_issue_origin"]["project"] == 7
        ]
    return [(p["project"], [r["wp"] for r in p["issues"]]) for level, p in outcome.op.observed if not level]


def _expected(component, copies, *rows):
    """The level writes of ``rows`` (sub-project, work package, Jira version IDs)."""
    if component == "versions":
        return {(project, wp, tuple(copies[project, sid] for sid in ids)) for project, wp, ids in rows}
    return {(project, wp, tuple(ids)) for project, wp, ids in rows}


BOTH = pytest.mark.parametrize("component", ["versions", "affects"])
APP_10, APP_11 = (100, 900, ("41",)), (100, 901, ("41", "42"))


@BOTH
def test_level_switch_off_issues_exactly_the_calls_of_the_ordinary_path(level_setup, component):
    outcome = _run(level_setup, component, on=False, reports=False)
    assert outcome.result.success, outcome.result
    assert outcome.jira.unrestricted == [("APP", ["APP-1"])]
    assert not outcome.jira.searches
    assert not outcome.jira.get_projects.called
    assert not outcome.op.live_reads
    assert not outcome.op.copy_syncs
    if component == "versions":
        assert outcome.jira.catalogues == ["APP"]
        assert [p["project"] for p in outcome.op.parent_syncs] == [7]
        assert outcome.op.updates == [
            {
                "id": 500,
                "target_version_ids": [41],
                "jira_version_issue_origin": {
                    "project": 7,
                    "id": "1",
                    "key": "APP-1",
                    "url": "https://jira.local/browse/APP-1",
                },
            }
        ]
        assert outcome.writes == ["version"]
    else:
        assert not outcome.jira.catalogues
        assert [(level, p["project"], p.get("parent_id")) for level, p in outcome.op.observed] == [(False, 7, None)]
        assert outcome.writes == []
    assert "security_level" not in outcome.result.details


@BOTH
def test_level_issues_get_the_copies_of_their_own_sub_project(level_setup, component):
    outcome = _run(level_setup, component)
    assert outcome.result.success, outcome.result
    # One copy per used version, shared by both issues of the sub-project; the parent is not changed.
    assert [(p["project"], p["parent_id"], [r["id"] for r in p["versions"]]) for p in outcome.op.copy_syncs] == [
        (100, 7, ["41", "42"])
    ]
    assert sorted(outcome.op.copies) == [(100, "41"), (100, "42")]
    assert _level_writes(outcome, component) == _expected(component, outcome.op.copies, APP_10, APP_11)
    assert outcome.jira.searches.count(FIELDS[component]) == 2  # the first page and the empty page
    assert outcome.result.details["security_level"] == {"work_packages": 2, "refused": 0}
    assert outcome.mappings.data["security_level_version"] == {
        f"APP:10000:{sid}": {
            "openproject_id": outcome.op.copies[100, sid],
            "openproject_project_id": 100,
            "security_level_project": "APP:10000",
            "jira_project_key": "APP",
            "jira_version_id": sid,
            "name": name,
        }
        for sid, name in (("41", "R1"), ("42", "R2"))
    }
    if component == "versions":
        # The parent catalogue is synced as with the switch off, and only its ID is in the version mapping.
        assert [p["project"] for p in outcome.op.parent_syncs] == [7]
        assert _ordinary_writes(outcome, component) == [(500, [41])]
        assert sorted(outcome.mappings.data["version"]) == ["7"]
        assert outcome.writes == ["version", "security_level_version"]
    else:
        assert outcome.jira.catalogues == ["APP"]
        assert _ordinary_writes(outcome, component) == [(7, [500])]
        assert [p["parent_id"] for level, p in outcome.op.observed if level] == [7]
        assert outcome.writes == ["security_level_version"]


@BOTH
def test_level_repeat_run_adopts_the_copies(level_setup, component):
    outcome = _run(level_setup, component, runs=2)
    first, second = outcome.results
    assert first.success and second.success
    assert len(outcome.op.copy_syncs) == 2
    assert sorted(outcome.op.copies) == [(100, "41"), (100, "42")]
    assert outcome.writes.count("security_level_version") == 1


@BOTH
def test_level_two_sub_projects_get_two_copies(level_setup, component):
    outcome = _run(level_setup, component, second_level=True)
    assert outcome.result.success, outcome.result
    assert sorted(outcome.op.copies) == [(100, "41"), (100, "42"), (101, "41")]
    assert outcome.op.copies[100, "41"] != outcome.op.copies[101, "41"]
    expected = _expected(component, outcome.op.copies, APP_10, APP_11, (101, 920, ("41",)))
    assert _level_writes(outcome, component) == expected


@BOTH
@pytest.mark.parametrize(
    "options",
    [
        {"live": {"field": True, "work_packages": [901], "keys": [[901, "APP-11"]]}},
        {"moved": {10}},
        {"omitted": {10}},
        {"ordinary": {"10": {"jira_key": "APP-10", "openproject_id": 510, "project_key": "APP"}}},
    ],
    ids=["moved-work-package", "level-changed", "omitted", "ordinary-entry"],
)
def test_level_issue_that_cannot_be_confirmed_gets_nothing(level_setup, component, options):
    outcome = _run(level_setup, component, **options)
    assert not outcome.result.success
    assert _level_writes(outcome, component) == _expected(component, outcome.op.copies, APP_11)
    assert outcome.result.details["security_level"]["refused"] == 1
    assert outcome.result.failed == 1
    # The unrestricted search never sees a level issue, also not one with an ordinary entry.
    assert outcome.jira.unrestricted == [("APP", ["APP-1"])]
    assert all(wp != 510 for _, wp, _ in _level_writes(outcome, component))


@BOTH
def test_level_unreadable_sub_project_refuses_all_its_issues(level_setup, component):
    outcome = _run(level_setup, component, live={"field": True})
    assert not outcome.result.success
    assert not outcome.op.copy_syncs
    assert not _level_writes(outcome, component)
    assert outcome.result.details["security_level"]["refused"] == 2


@BOTH
def test_level_unselected_issue_gets_nothing_even_with_an_entry(level_setup, component):
    level_map = {**LEVEL_MAP, "12": {**LEVEL_MAP["10"], "jira_key": "APP-12", "openproject_id": 902}}
    outcome = _run(level_setup, component, level_map=level_map)
    assert not outcome.result.success
    assert _level_writes(outcome, component) == _expected(component, outcome.op.copies, APP_10, APP_11)
    assert outcome.result.details["security_level"]["refused"] == 1


@BOTH
def test_level_sub_project_with_a_member_refuses_the_copy(level_setup, component):
    outcome = _run(level_setup, component, members={100})
    assert not outcome.result.success
    assert not outcome.op.copies
    assert not _level_writes(outcome, component)
    assert "security_level_version" not in outcome.writes
    assert outcome.result.details["security_level"] == {"work_packages": 2, "refused": 2}
    assert _ordinary_writes(outcome, component)


@BOTH
def test_level_refusals_are_reported_when_the_component_stops_early(level_setup, component):
    # The unrestricted search fails before any write.
    outcome = _run(level_setup, component, live={"field": True}, unrestricted_fails=True)
    assert not outcome.result.success
    assert outcome.jira.unrestricted
    assert not outcome.op.copy_syncs
    assert not _level_writes(outcome, component)
    assert outcome.result.details["security_level"] == {"work_packages": 0, "refused": 2}


@BOTH
def test_level_switch_on_without_reports_stops_before_any_search_or_write(level_setup, component):
    outcome = _run(level_setup, component, reports=False)
    assert not outcome.result.success
    assert not outcome.jira.searches
    assert not outcome.jira.unrestricted
    assert not outcome.op.parent_syncs
    assert not outcome.op.copy_syncs
    assert not outcome.op.updates
    assert not outcome.op.observed


class _Answer:
    def __init__(self, answer):
        self.answer, self.scripts = answer, []

    def execute_json_query(self, script):
        self.scripts.append(script)
        return self.answer


def _payload(script):
    return json.loads(base64.b64decode(script.split("strict_decode64('", 1)[1].split("')", 1)[0]))


def test_level_version_writer_checks_the_parent_and_the_memberless_sub_project():
    from src.infrastructure.openproject.version_catalog import sync_level_versions

    client = _Answer({"versions": {"41": 1000}, "changed": 1})
    assert sync_level_versions(client, (100, 7), "https://jira.local/", "APP", [CATALOGUE[0]]) == {"41": 1000}
    (script,) = client.scripts
    assert _payload(script) == {
        "project": 100,
        "parent_id": 7,
        "source": "https://jira.local",
        "key": "APP",
        "jira_project_id": "10",
        "versions": [
            {
                "id": "41",
                "archived": False,
                "released": False,
                "attributes": {
                    "name": "R1",
                    "description": "",
                    "status": "open",
                    "start_date": None,
                    "effective_date": None,
                },
            }
        ],
    }
    # The parent carries the origin; every membership row of the sub-project stops a change.
    assert "owner.custom_values.where(custom_field: field).sole.value == value" in script
    assert "project.parent_id == owner.id && !project.public?" in script
    assert "staging = !project.members.exists? && !Member.where(project_id: project.id).exists?" in script
    assert "merge('sharing' => 'none')" in script
    assert "project.versions.new" in script and "Version.new" not in script


@pytest.mark.parametrize("answer", [None, {"versions": {}}, {"versions": {"41": "1000"}}])
def test_level_version_writer_rejects_an_incomplete_answer(answer):
    from src.infrastructure.openproject.version_catalog import sync_level_versions

    with pytest.raises(ValueError, match="Incomplete"):
        sync_level_versions(_Answer(answer), (100, 7), "https://jira.local", "APP", [CATALOGUE[0]])


def test_level_affected_versions_check_the_parent_origin():
    from src.infrastructure.openproject.observed_versions import write_observed_versions

    client = _Answer({"updated": 1, "native": True})
    payload = {"project": 100, "parent_id": 7, "key": "APP", "jira_project_id": "10", "source": "https://jira.local"}
    write_observed_versions(client, {**payload, "field": 555, "issues": []}, level=True)
    write_observed_versions(client, {**payload, "field": 555, "issues": []})
    level, ordinary = client.scripts
    assert "owner.custom_values" in level and "owner.custom_values" not in ordinary


@BOTH
def test_level_copies_through_the_component_runner_reach_their_mapping_file(
    level_setup, component, tmp_path, monkeypatch
):
    import src.config as cfg
    from src.mappings.mappings import Mappings
    from src.migration import _invoke_component

    fake, _ = level_setup()
    store = tmp_path / "mappings"
    store.mkdir()
    real = Mappings(store)
    for name, value in {**fake.data, "work_package": ORDINARY, "security_level_work_package": LEVEL_MAP}.items():
        real.set_mapping(name, value)
    monkeypatch.setattr(cfg, "mappings", real)
    jira, op = FakeJira(), FakeOpenProject()
    result = _invoke_component(component, COMPONENTS[component](jira_client=jira, op_client=op))
    assert result.success, result
    assert result.details["security_level"] == {"work_packages": 2, "refused": 0}
    saved = json.loads((store / "security_level_version_mapping.json").read_text())
    assert {key: entry["openproject_id"] for key, entry in saved.items()} == {
        "APP:10000:41": op.copies[100, "41"],
        "APP:10000:42": op.copies[100, "42"],
    }
