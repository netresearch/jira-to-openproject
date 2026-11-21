# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Attachments, their provenance and their recovery for level work packages (ADR-009, step 5a)."""

import json
import re
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components.attachment_provenance_migration import AttachmentProvenanceMigration
from src.application.components.attachment_recovery_migration import AttachmentRecoveryMigration
from src.application.components.attachments_migration import AttachmentsMigration
from tests.unit.test_level_work_packages import level_issues
from tests.unit.test_level_work_packages import setup as level_work_package_setup  # noqa: F401  (fixture)


@pytest.fixture
def setup(level_work_package_setup):  # noqa: F811 (the imported fixture, requested by name)
    """The reports, mappings and switch of the level work package tests."""
    return level_work_package_setup


LIGHT = 'project = "APP" AND level IS NOT EMPTY ORDER BY created ASC, key ASC'
ORDINARY = {"1": {"jira_key": "APP-1", "openproject_id": 500, "project_key": "APP"}}
LIVE = {"field": True, "work_packages": [900, 901], "keys": [[900, "APP-10"], [901, "APP-11"]]}
LEVEL_MAP = {
    str(n): {
        "jira_key": f"APP-{n}",
        "openproject_id": wp,
        "project_key": "APP",
        "openproject_project_id": 100,
        "security_level_id": "10000",
        "security_level_project": "APP:10000",
        "security_class": "static",
    }
    for n, wp in ((10, 900), (11, 901))
}
# Every issue of the project has one attachment; APP-12 (per_issue), APP-13 (unexplained) stay in
# the operator archive on Community, APP-20 belongs to a level without a sub-project.
NUMBERS = (1, 10, 11, 12, 13, 20)


def url(number):
    return f"https://jira.local/secure/attachment/{number}/file-{number}.txt"


LEVEL_OF = {10: "10000", 11: "10000", 12: "10000", 13: "10000", 20: "10001"}


def attachment_issue(number, level=None):
    level = LEVEL_OF.get(number) if level is None else level
    raw = {"fields": {"security": {"id": level}} if level else {}}
    att = SimpleNamespace(
        id=str(number),
        filename=f"file-{number}.txt",
        size=4,
        content=url(number),
        created="2026-01-01T00:00:00.000+0000",
        author={"name": "alice"},
    )
    return SimpleNamespace(id=str(number), key=f"APP-{number}", fields=SimpleNamespace(attachment=[att]), raw=raw)


class FakeJira:
    """Answer the level search, ``key in`` searches, the project search and downloads; record each."""

    def __init__(self, moved=()):
        self.searches, self.downloads, self.batches, self.fields = [], [], [], []
        # Issues that have another level in the attachment search than in the selection search.
        self.moved = set(moved)
        self.jira = Mock()
        self.jira.search_issues.side_effect = self.search
        self.jira._session.get.side_effect = self.download
        self.get_projects = Mock(return_value=[{"key": "APP"}])

    def search(self, jql, startAt, maxResults, **kwargs):
        self.searches.append(jql)
        if "level IS NOT EMPTY" in jql:
            rows = level_issues()
        elif jql.startswith("key in ("):
            self.fields.append(kwargs.get("fields"))
            keys = jql.removeprefix("key in (").split(")")[0].split(",")
            numbers = [int(key.split("-")[1]) for key in keys]
            rows = [attachment_issue(n, "10001" if n in self.moved else None) for n in numbers]
        elif jql == 'project = "APP"':
            rows = [attachment_issue(n) for n in NUMBERS]
        else:
            raise AssertionError(jql)
        return rows[startAt : startAt + maxResults]

    @contextmanager
    def download(self, target, stream):
        self.downloads.append(target)
        yield SimpleNamespace(raise_for_status=lambda: None, iter_content=lambda chunk_size: [b"data"])

    def batch_get_issues(self, keys):
        self.batches.append(list(keys))
        return {key: attachment_issue(int(key.split("-")[1])) for key in keys}


class FakeOpenProject:
    """Answer the live sub-project read, the size cap, the attach and inventory scripts; record each."""

    def __init__(self, live=None):
        self.live = LIVE if live is None else live
        self.live_reads, self.transfers, self.attached, self.inventories, self.provenance = [], [], [], [], []
        self.docker_client = self

    def execute_command(self, command):
        return ("/tmp/j2o-stage.0123456789", "", 0) if "mktemp -d " in command else ("", "", 0)

    def transfer_file_to_container(self, local_path, container_path):
        self.transfers.append(container_path.as_posix())

    def execute_query_to_json_file(self, script):
        self.live_reads.append(json.loads(script.split("\n")[1]))
        return json.loads(json.dumps(self.live))

    def execute_script_with_data(self, script, data):
        if "Setting.attachment_max_size" in script:
            match = re.search(r"Setting\.attachment_max_size = (\d+)", script)
            return {"status": "success", "data": {"value": int(match[1]) if match else 5120}}
        if "Attachment.where(container_type" in script:
            ids = json.loads(re.search(r"ids = (\[[^\]]*\])", script)[1])
            self.inventories.append(ids)
            names = {op["work_package_id"]: [op["filename"]] for op in self.attached}
            return {"status": "success", "data": {str(wp): names[wp] for wp in ids if wp in names}}
        if "a.author = User.find" in script:
            self.provenance.extend(data)
            return {"updated": len(data), "failed": 0}
        self.attached.extend(data)
        rows = [
            {"jira_key": op["jira_key"], "filename": op["filename"], "attachment_id": 7000 + i, "existed": False}
            for i, op in enumerate(data)
        ]
        return {"status": "success", "data": {"results": rows, "errors": []}}


def prepare(setup, *, level_map=None, wp_map=None, **kwargs):
    mappings, data = setup(**kwargs)
    mappings.data["work_package"] = dict(ORDINARY if wp_map is None else wp_map)
    mappings.data["security_level_work_package"] = dict(LEVEL_MAP if level_map is None else level_map)
    mappings.writes = []
    original = mappings.set_mapping
    mappings.set_mapping = lambda name, value: (mappings.writes.append(name), original(name, value))
    return mappings, data


def attachments(setup, *, live=None, moved=(), **kwargs):
    mappings, data = prepare(setup, **kwargs)
    jira, op = FakeJira(moved), FakeOpenProject(live)
    result = AttachmentsMigration(jira_client=jira, op_client=op).run()
    return SimpleNamespace(result=result, jira=jira, op=op, mappings=mappings, data=data)


def targets(op):
    return {(row["jira_key"], row["work_package_id"]) for row in op.attached}


def test_switch_off_attaches_exactly_as_before(setup):
    outcome = attachments(setup, on=False, reports=False)
    assert outcome.result.success, outcome.result
    assert outcome.jira.searches == ["key in (APP-1) ORDER BY key ASC"]
    assert outcome.jira.fields == ["attachment"]
    assert outcome.jira.downloads == [url(1)]
    assert targets(outcome.op) == {("APP-1", 500)}
    assert len(outcome.op.transfers) == 1
    assert not outcome.op.live_reads
    assert not outcome.jira.get_projects.called
    assert not [name for name in outcome.mappings.reads if name.startswith("security_level")]
    assert "security_level" not in outcome.result.details


def test_switch_on_attaches_selected_level_issues_to_the_work_package_their_sub_project_holds(setup):
    outcome = attachments(setup)
    assert outcome.result.success, outcome.result
    assert targets(outcome.op) == {("APP-1", 500), ("APP-10", 900), ("APP-11", 901)}
    # Archived (APP-12, APP-13) and unmapped-level (APP-20) issues are neither searched nor downloaded.
    assert outcome.jira.searches == [LIGHT, LIGHT, "key in (APP-1,APP-10,APP-11) ORDER BY key ASC"]
    assert sorted(outcome.jira.downloads) == sorted([url(1), url(10), url(11)])
    assert outcome.op.live_reads == [{"project_id": 100, "field": "J2O Origin Key"}]
    assert outcome.result.details["security_level"] == {"work_packages": 2, "refused": 0}
    # Level entries are never written into the work_package mapping (or any mapping).
    assert outcome.mappings.writes == []
    assert outcome.mappings.data["work_package"] == ORDINARY
    saved = json.loads((outcome.data / "attachment_mapping.json").read_text())
    assert set(saved) == {"APP-1", "APP-10", "APP-11"}


@pytest.mark.parametrize(
    ("live", "level_map", "refused"),
    [
        # APP-10's work package moved out of the sub-project.
        ({"field": True, "work_packages": [901], "keys": [[901, "APP-11"]]}, None, 1),
        # The entry names another work package than the sub-project holds.
        ({"field": True, "work_packages": [950, 901], "keys": [[950, "APP-10"], [901, "APP-11"]]}, None, 1),
        # The entry names another sub-project.
        (None, {**LEVEL_MAP, "10": {**LEVEL_MAP["10"], "openproject_project_id": 101}}, 1),
    ],
)
def test_a_level_issue_whose_work_package_is_not_where_its_entry_says_gets_nothing(setup, live, level_map, refused):
    outcome = attachments(setup, live=live, level_map=level_map)
    assert not outcome.result.success
    assert targets(outcome.op) == {("APP-1", 500), ("APP-11", 901)}
    assert url(10) not in outcome.jira.downloads
    assert outcome.result.details["security_level"]["refused"] == refused
    assert outcome.result.failed == refused


@pytest.mark.parametrize(
    ("moved", "wp_map", "attached"),
    [
        ({10}, None, {("APP-1", 500), ("APP-11", 901)}),
        # Every issue of the batch changed its level: nothing is attached at all.
        ({10, 11}, {}, set()),
    ],
)
def test_an_issue_whose_level_changed_after_the_selection_gets_nothing(setup, moved, wp_map, attached):
    outcome = attachments(setup, moved=moved, wp_map=wp_map)
    assert not outcome.result.success
    assert outcome.jira.fields == ["attachment,security"]
    assert targets(outcome.op) == attached
    assert not {url(n) for n in moved} & set(outcome.jira.downloads)
    assert outcome.result.details["loss_counters"]["extract_level_changed"] == len(moved)
    assert outcome.result.failed == len(moved)


def test_a_moved_work_package_without_a_mapping_entry_is_refused(setup):
    outcome = attachments(setup, level_map={}, live={"field": True, "work_packages": [901], "keys": [[901, "APP-11"]]})
    assert not outcome.result.success
    assert targets(outcome.op) == {("APP-1", 500), ("APP-11", 901)}
    assert outcome.result.details["security_level"] == {"work_packages": 1, "refused": 1}


def test_an_unreadable_sub_project_refuses_all_its_issues(setup):
    outcome = attachments(setup, live={"field": True})
    assert not outcome.result.success
    assert targets(outcome.op) == {("APP-1", 500)}
    assert outcome.jira.downloads == [url(1)]
    assert outcome.result.details["security_level"]["refused"] == 2


def test_an_issue_the_selection_does_not_keep_gets_nothing_even_with_an_entry(setup):
    # On Community APP-12 is per_issue and stays in the archive; a stray entry is refused.
    level_map = {**LEVEL_MAP, "12": {**LEVEL_MAP["10"], "jira_key": "APP-12", "openproject_id": 902}}
    outcome = attachments(setup, level_map=level_map)
    assert not outcome.result.success
    assert all(key != "APP-12" for key, _ in targets(outcome.op))
    assert url(12) not in outcome.jira.downloads
    assert outcome.result.details["security_level"]["refused"] == 1


def test_a_lost_level_mapping_is_recovered_from_the_live_read_and_not_written(setup):
    outcome = attachments(setup, level_map={})
    assert outcome.result.success, outcome.result
    assert targets(outcome.op) == {("APP-1", 500), ("APP-10", 900), ("APP-11", 901)}
    assert outcome.mappings.writes == []


def test_a_level_issue_with_an_ordinary_work_package_gets_nothing_on_either_side(setup):
    wp_map = {**ORDINARY, "10": {"jira_key": "APP-10", "openproject_id": 510, "project_key": "APP"}}
    outcome = attachments(setup, wp_map=wp_map)
    assert not outcome.result.success
    assert targets(outcome.op) == {("APP-1", 500), ("APP-11", 901)}
    assert url(10) not in outcome.jira.downloads
    assert outcome.result.details["security_level"]["refused"] == 1


def test_an_archived_issue_in_the_ordinary_mapping_is_not_downloaded(setup):
    wp_map = {**ORDINARY, "12": {"jira_key": "APP-12", "openproject_id": 512, "project_key": "APP"}}
    outcome = attachments(setup, wp_map=wp_map)
    assert url(12) not in outcome.jira.downloads
    assert all(key != "APP-12" for key, _ in targets(outcome.op))


@pytest.mark.parametrize(
    "component", [AttachmentsMigration, AttachmentProvenanceMigration, AttachmentRecoveryMigration]
)
def test_switch_on_without_reports_stops_before_any_write(setup, component):
    prepare(setup, reports=False)
    jira, op = FakeJira(), FakeOpenProject()
    result = component(jira_client=jira, op_client=op).run()
    assert not result.success
    assert not op.attached
    assert not op.provenance
    assert not jira.downloads
    assert not jira.searches
    assert not jira.batches


def test_a_second_run_on_the_same_instance_resolves_the_level_again(setup):
    prepare(setup)
    jira, op = FakeJira(), FakeOpenProject()
    migration = AttachmentsMigration(jira_client=jira, op_client=op)
    first, second = migration.run(), migration.run()
    assert first.success, first
    assert second.success, second
    assert second.details["security_level"] == {"work_packages": 2, "refused": 0}
    assert [row["work_package_id"] for row in op.attached].count(900) == 2


def provenance(setup, *, live=None, **kwargs):
    mappings, _ = prepare(setup, **kwargs)
    mappings.data["user"] = {"alice": {"openproject_id": 301}}
    jira, op = FakeJira(), FakeOpenProject(live)
    result = AttachmentProvenanceMigration(jira_client=jira, op_client=op).run()
    return SimpleNamespace(result=result, jira=jira, op=op, mappings=mappings)


def test_provenance_switch_off_updates_exactly_as_before(setup):
    outcome = provenance(setup, on=False, reports=False)
    assert outcome.jira.batches == [["APP-1"]]
    assert [row["work_package_id"] for row in outcome.op.provenance] == [500]
    assert not outcome.op.live_reads
    assert not outcome.jira.searches
    assert not [name for name in outcome.mappings.reads if name.startswith("security_level")]


def test_provenance_of_level_attachments_goes_to_the_held_work_package(setup):
    level_map = {**LEVEL_MAP, "12": {**LEVEL_MAP["10"], "jira_key": "APP-12", "openproject_id": 902}}
    outcome = provenance(
        setup, level_map=level_map, live={"field": True, "work_packages": [900], "keys": [[900, "APP-10"]]}
    )
    assert outcome.jira.batches == [["APP-1", "APP-10"]]
    assert {(row["work_package_id"], row["filename"]) for row in outcome.op.provenance} == {
        (500, "file-1.txt"),
        (900, "file-10.txt"),
    }
    # APP-11 has no work package in the sub-project, APP-12 is not selected.
    assert not outcome.result.success
    assert outcome.result.details["security_level"]["refused"] == 2
    assert outcome.mappings.writes == []


def test_provenance_skips_an_ordinary_entry_of_an_archived_issue(setup):
    wp_map = {**ORDINARY, "12": {"jira_key": "APP-12", "openproject_id": 512, "project_key": "APP"}}
    outcome = provenance(setup, wp_map=wp_map)
    assert outcome.jira.batches == [["APP-1", "APP-10", "APP-11"]]
    assert all(row["work_package_id"] != 512 for row in outcome.op.provenance)
    assert outcome.result.details["security_level"]["refused"] == 1


def recovery(setup, **kwargs):
    mappings, _ = prepare(setup, **kwargs)
    jira, op = FakeJira(), FakeOpenProject()
    result = AttachmentRecoveryMigration(jira_client=jira, op_client=op).run()
    return SimpleNamespace(result=result, jira=jira, op=op, mappings=mappings)


def test_recovery_switch_off_leaves_level_issues_unmapped(setup):
    outcome = recovery(setup, on=False, reports=False)
    assert outcome.jira.searches == ['project = "APP"', 'project = "APP"', "key in (APP-1) ORDER BY key ASC"]
    assert outcome.op.inventories == [[500], [500]]
    assert targets(outcome.op) == {("APP-1", 500)}
    assert outcome.result.details["wp_unmapped"] == 5
    assert not outcome.op.live_reads


def test_recovery_with_an_unreadable_sub_project_fails_and_downloads_nothing_of_the_level(setup):
    prepare(setup)
    jira, op = FakeJira(), FakeOpenProject(live={"field": True})
    # OpenProject already holds the ordinary attachment, so nothing ordinary is missing.
    op.attached.append({"jira_key": "APP-1", "work_package_id": 500, "filename": "file-1.txt"})
    result = AttachmentRecoveryMigration(jira_client=jira, op_client=op).run()
    assert not result.success
    assert result.details["security_level"] == {"work_packages": 0, "refused": 2}
    assert not jira.downloads
    assert op.inventories == [[500]]


def test_recovery_refuses_an_issue_whose_level_changed_after_the_selection(setup):
    prepare(setup)
    jira, op = FakeJira(moved={10}), FakeOpenProject()
    result = AttachmentRecoveryMigration(jira_client=jira, op_client=op).run()
    assert not result.success
    assert targets(op) == {("APP-1", 500), ("APP-11", 901)}
    assert url(10) not in jira.downloads
    assert result.failed >= 1


def test_recovery_restores_level_attachments_and_never_downloads_archived_ones(setup):
    outcome = recovery(setup, level_map={})
    assert outcome.result.success, outcome.result
    assert outcome.op.inventories == [[500, 900, 901], [500, 900, 901]]
    assert targets(outcome.op) == {("APP-1", 500), ("APP-10", 900), ("APP-11", 901)}
    assert sorted(outcome.jira.downloads) == sorted([url(1), url(10), url(11)])
    assert outcome.result.details["wp_unmapped"] == 3
    assert outcome.result.details["security_level"] == {"work_packages": 2, "refused": 0}
    assert outcome.mappings.writes == []
