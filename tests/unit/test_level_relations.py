# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Relations of level work packages: only inside one sub-project and one audience (ADR-009)."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components.relation_migration import RelationMigration
from tests.unit.test_level_attachments import LIGHT as LEVEL_SEARCH
from tests.unit.test_level_attachments import LIVE
from tests.unit.test_level_work_packages import SUB_PROJECT, level_issues
from tests.unit.test_level_work_packages import setup as level_work_package_setup  # noqa: F401  (fixture)

LINK_FIELDS = "project,security,issuelinks"
LEVEL_OF = {10: "10000", 11: "10000", 12: "10000", 13: "10000", 20: "10001"}
ORDINARY = {
    "1": {"jira_key": "APP-1", "openproject_id": 500, "project_key": "APP"},
    "2": {"jira_key": "APP-2", "openproject_id": 501, "project_key": "APP"},
}
SECOND_LEVEL = {**SUB_PROJECT, "level_id": "10001", "openproject_id": 101, "openproject_identifier": "app_level_10001"}
SECOND_LIVE = {101: {"field": True, "work_packages": [920], "keys": [[920, "APP-20"]]}}
ENTERPRISE_LIVE = {
    "field": True,
    "work_packages": [900, 901, 902],
    "keys": [[900, "APP-10"], [901, "APP-11"], [902, "APP-12"]],
}


def link(name, direction, target):
    """A Jira issue link as the REST answer carries it."""
    return {"type": {"name": name, "inward": name, "outward": name}, f"{direction}Issue": {"key": f"APP-{target}"}}


# APP-10 relates to APP-11 (same sub-project) and links out of its audience to the ordinary
# APP-1, to APP-20 of another level and to the per_issue APP-12; APP-12 relates to APP-11.
LINKS = {
    1: [link("Blocks", "inward", 10), link("Relates", "outward", 2)],
    2: [],
    10: [
        link("Relates", "outward", 11),
        link("Blocks", "outward", 1),
        link("Relates", "outward", 20),
        link("Relates", "outward", 12),
    ],
    11: [],
    12: [link("Relates", "outward", 11)],
    13: [],
    20: [link("Relates", "outward", 1)],
}
INSIDE = {"from_id": 900, "to_id": 901, "relation_type": "relates"}
ORDINARY_RELATION = {"from_id": 500, "to_id": 501, "relation_type": "relates"}


@pytest.fixture
def level_setup(level_work_package_setup):  # noqa: F811 (the imported fixture, requested by name)
    """The reports, mappings and switch of the level work package tests."""
    return level_work_package_setup


def _issue(number, level):
    fields = {"project": {"key": "APP"}, "issuelinks": LINKS[number]} | ({"security": {"id": level}} if level else {})
    return SimpleNamespace(key=f"APP-{number}", id=str(number), raw={"fields": fields}, fields=SimpleNamespace())


class FakeJira:
    """Answer the level searches and the ordinary batch read; record every call."""

    def __init__(self, moved=(), omitted=(), links_fail=False):
        self.moved, self.omitted, self.links_fail = set(moved), set(omitted), links_fail
        self.searches, self.batches = [], []
        self.get_projects = Mock(return_value=[{"key": "APP"}])
        self.jira = Mock()
        self.jira.search_issues.side_effect = self.search

    def search(self, jql, startAt, maxResults, **kwargs):
        assert jql == LEVEL_SEARCH, jql
        self.searches.append(kwargs.get("fields"))
        if kwargs.get("fields") == "security":
            rows = level_issues()
        else:
            assert kwargs.get("fields") == LINK_FIELDS
            if self.links_fail:
                raise OSError("search unavailable")
            rows = [
                _issue(n, "10001" if n in self.moved else level)
                for n, level in LEVEL_OF.items()
                if n not in self.omitted
            ]
        return rows[startAt : startAt + maxResults]

    def batch_get_issues(self, keys):
        self.batches.append(list(keys))
        return {key: {"key": key, "fields": {"issuelinks": LINKS[int(key.split("-")[1])]}} for key in keys}


class FakeOpenProject:
    """Answer the live sub-project read; record every bulk relation call."""

    def __init__(self, live=None, by_project=None):
        self.live, self.by_project = LIVE if live is None else live, by_project or {}
        self.live_reads, self.bulk_calls, self.relations = [], [], set()

    def execute_query_to_json_file(self, script):
        values = json.loads(script.split("\n")[1])
        self.live_reads.append(values)
        return json.loads(json.dumps(self.by_project.get(values["project_id"], self.live)))

    def bulk_create_relations(self, relations):
        self.bulk_calls.append([dict(r) for r in relations])
        new = [r for r in relations if (r["from_id"], r["to_id"], r["relation_type"]) not in self.relations]
        self.relations |= {(r["from_id"], r["to_id"], r["relation_type"]) for r in relations}
        return {"created": len(new), "skipped": len(relations) - len(new), "failed": 0}


def _run(level_setup, *, live=None, moved=(), omitted=(), ordinary=None, level_map=None, cache=None, runs=1, **kw):
    second_level, links_fail = kw.pop("second_level", False), kw.pop("links_fail", False)
    if second_level:
        kw["mapping"] = {"security_level_project": {"APP:10000": SUB_PROJECT, "APP:10001": SECOND_LEVEL}}
    mappings, data = level_setup(**kw)
    mappings.data["work_package"] = dict(ORDINARY if ordinary is None else ordinary)
    if mappings.data["work_package"]:
        # The relation run reads the work package mapping file, as in production.
        (data / "work_package_mapping.json").write_text(json.dumps(mappings.data["work_package"]))
    if level_map is not None:
        mappings.data["security_level_work_package"] = level_map
    if cache is not None:
        (data / "jira_issues_cache.json").write_text(json.dumps(cache))
    jira = FakeJira(moved, omitted, links_fail)
    op = FakeOpenProject(live, SECOND_LIVE if second_level else None)
    migration = RelationMigration(jira_client=jira, op_client=op)
    results = [migration.run() for _ in range(runs)]
    return SimpleNamespace(result=results[-1], results=results, jira=jira, op=op, mappings=mappings)


def _refused(outcome):
    return outcome.result.details["security_level"]["refused"]


def test_switch_off_issues_the_ordinary_calls_only(level_setup):
    outcome = _run(level_setup, on=False)
    assert outcome.result.success, outcome.result.message
    assert outcome.jira.batches == [["APP-1", "APP-2"]]
    assert outcome.jira.searches == []
    assert outcome.op.live_reads == []
    assert outcome.op.bulk_calls == [[ORDINARY_RELATION]]
    assert outcome.result.details["skip_reasons"] == {"target_wp_unmigrated": 1}
    assert not {"security_level_work_package", "security_level_project"} & set(outcome.mappings.reads)
    assert "security_level" not in outcome.result.details


def test_level_relation_inside_one_sub_project_is_written_and_crossing_links_are_withheld(level_setup):
    outcome = _run(level_setup)
    assert outcome.result.success, outcome.result.message
    # One level search per project for the selection, one for the links of the selected issues,
    # each a page and the empty page after it.
    assert outcome.jira.searches == ["security", "security", LINK_FIELDS, LINK_FIELDS]
    assert outcome.jira.batches == [["APP-1", "APP-2"]]
    assert outcome.op.bulk_calls == [[ORDINARY_RELATION, INSIDE]]
    # APP-1 to APP-10, APP-10 to APP-1, to APP-20 (no sub-project) and to APP-12 (not selected).
    assert outcome.result.details["skip_reasons"] == {"level_relation_withheld": 4}
    assert outcome.result.details["level_relations_withheld"] == 4
    assert outcome.result.details["security_level"] == {"work_packages": 2, "refused": 0}


def test_a_link_to_another_sub_project_is_withheld(level_setup):
    outcome = _run(level_setup, second_level=True)
    assert outcome.result.success, outcome.result.message
    assert outcome.op.bulk_calls == [[ORDINARY_RELATION, INSIDE]]
    # As above, plus APP-20 to APP-1; APP-10 to APP-20 now crosses into a second sub-project.
    assert outcome.result.details["skip_reasons"] == {"level_relation_withheld": 5}
    assert outcome.result.details["security_level"] == {"work_packages": 3, "refused": 0}


def test_a_relation_with_a_shared_work_package_is_withheld(level_setup):
    # On Enterprise the per_issue APP-12 is shared with its extra readers, who would see a
    # placeholder row for APP-10 and APP-11 of the same sub-project.
    outcome = _run(level_setup, edition="enterprise", live=ENTERPRISE_LIVE)
    assert outcome.result.success, outcome.result.message
    assert outcome.op.bulk_calls == [[ORDINARY_RELATION, INSIDE]]
    assert outcome.result.details["skip_reasons"] == {"level_relation_withheld": 5}
    assert outcome.result.details["security_level"] == {"work_packages": 3, "refused": 0}


@pytest.mark.parametrize(
    ("options", "refused"),
    [
        pytest.param({"live": {"field": True, "work_packages": [901], "keys": [[901, "APP-11"]]}}, 1, id="moved"),
        pytest.param({"live": {"field": True}}, 2, id="unreadable-sub-project"),
        pytest.param({"moved": (10,)}, 1, id="level-changed"),
        pytest.param({"omitted": (10,)}, 1, id="source-omitted-by-search"),
        pytest.param({"omitted": (11,)}, 1, id="target-omitted-by-search"),
        pytest.param({"links_fail": True}, 2, id="level-search-fails"),
    ],
)
def test_a_refused_level_issue_gets_no_relation(level_setup, options, refused):
    outcome = _run(level_setup, **options)
    assert not outcome.result.success
    assert _refused(outcome) == refused
    assert outcome.op.bulk_calls == [[ORDINARY_RELATION]]
    assert outcome.result.details["failed_count"] == refused


def test_an_unselected_level_issue_gets_no_relation(level_setup):
    # A mapping entry of the unexplained APP-13 is refused; APP-13 is never a source or target.
    entry = {"jira_key": "APP-13", "openproject_id": 903, "security_level_project": "APP:10000"}
    outcome = _run(level_setup, level_map={"13": entry})
    assert not outcome.result.success
    assert _refused(outcome) == 1
    assert outcome.op.bulk_calls == [[ORDINARY_RELATION, INSIDE]]


@pytest.mark.parametrize("cached", [False, True], ids=["batch-read", "cached-snapshot"])
def test_an_ordinary_entry_of_a_level_issue_is_refused(level_setup, cached):
    # APP-10 has an ordinary work package (777), under its key as legacy mappings store it; the
    # cached snapshot, when there is one, carries its links. Neither end gets a relation.
    ordinary = {**ORDINARY, "APP-10": {"jira_key": "APP-10", "openproject_id": 777, "project_key": "APP"}}
    cache = {f"APP-{n}": {"key": f"APP-{n}", "fields": {"issuelinks": LINKS[n]}} for n in (1, 2, 10)}
    outcome = _run(level_setup, ordinary=ordinary, cache=cache if cached else None)
    assert not outcome.result.success
    assert _refused(outcome) == 1
    assert outcome.jira.batches == ([] if cached else [["APP-1", "APP-2"]])
    assert outcome.op.bulk_calls == [[ORDINARY_RELATION]]


def test_a_repeated_run_writes_the_same_relations(level_setup):
    outcome = _run(level_setup, runs=2)
    assert [r.success for r in outcome.results] == [True, True]
    assert outcome.op.bulk_calls == [[ORDINARY_RELATION, INSIDE]] * 2
    assert [r.details["created"] for r in outcome.results] == [2, 0]


def test_refusals_are_reported_on_the_early_return(level_setup):
    outcome = _run(level_setup, live={"field": True}, ordinary={})
    assert not outcome.result.success
    assert outcome.result.message.startswith("Relation migration completed")
    assert outcome.op.bulk_calls == []
    assert _refused(outcome) == 2
    assert outcome.result.details["failed_count"] == 2


def test_a_missing_report_stops_before_any_write(level_setup):
    outcome = _run(level_setup, reports=False)
    assert not outcome.result.success
    assert outcome.result.errors == ["security_level"]
    assert outcome.op.bulk_calls == []
    assert outcome.jira.batches == []
