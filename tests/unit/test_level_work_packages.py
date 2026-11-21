# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Work packages of Jira issues with a security level in their level sub-project (ADR-009, step 4)."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components.level_issues import LevelScope, issue_level_id
from src.application.components.work_package_content_migration import WorkPackageContentMigration
from src.application.components.work_package_skeleton_migration import WorkPackageSkeletonMigration
from src.restricted_issues import ADMIN_REPORT_FORMAT, REPORT_FORMAT

SHA = "b" * 64
UNRESTRICTED = 'project = "APP" AND level IS EMPTY ORDER BY created ASC, key ASC'
LEVELS = 'project = "APP" AND level IS NOT EMPTY ORDER BY created ASC, key ASC'
SUB_PROJECT = {
    "jira_project_key": "APP",
    "level_id": "10000",
    "level": "Management",
    "openproject_id": 100,
    "openproject_identifier": "app_level_10000",
    "parent_openproject_id": 7,
}
STATE = {"found": True, "id": 100, "identifier": "app_level_10000", "public": False, "parent_id": 7, "members": 0}


def issue(number, level=None):
    raw = {"summary": f"Issue {number}", "description": f"Text {number}"}
    if level is not None:
        raw["security"] = {"id": level}
    fields = SimpleNamespace(project=SimpleNamespace(key="APP"), **raw)
    return SimpleNamespace(id=str(number), key=f"APP-{number}", fields=fields, raw={"fields": raw})


def row(level_id="10000", static=("APP-10", "APP-11"), per_issue=1, unexplained=1):
    return {
        "project": "APP",
        "level_id": level_id,
        "level": "Management",
        "static": len(static),
        "static_keys": list(static),
        "per_issue": per_issue,
        "unexplained": unexplained,
    }


ARCHIVED = [
    {"key": "APP-12", "level_id": "10000", "class": "per_issue"},
    {"key": "APP-13", "level_id": "10000", "class": "unexplained"},
]


def level_issues():
    """Two static issues, one per_issue and one unexplained of 10000, one static of 10001."""
    return [issue(10, "10000"), issue(11, "10000"), issue(12, "10000"), issue(13, "10000"), issue(20, "10001")]


class FakeMappings:
    def __init__(self, data):
        self.data = data
        self.reads = []

    def get_mapping(self, name):
        self.reads.append(name)
        return self.data.get(name, {})

    def set_mapping(self, name, value):
        self.data[name] = json.loads(json.dumps(dict(value)))


NO_WORK_PACKAGES = {"field": True, "work_packages": [], "keys": []}


def FakeOpenProject(state=None, existing=None, fail=False):
    """Record batch payloads; answer the sub-project read with ``state``, the work-package read with ``existing``."""
    op = Mock()
    op.payloads, op.reads, op.existing_reads = [], [], []

    def read(script):
        values = json.loads(script.split("\n")[1])
        if "CustomValue" in script:
            op.existing_reads.append(values)
            return json.loads(json.dumps(NO_WORK_PACKAGES if existing is None else existing))
        op.reads.append(values)
        return dict(STATE if state is None else state)

    def create(payloads):
        if fail:
            raise RuntimeError("batch answer lost")
        start = len(op.payloads)
        op.payloads.extend(payloads)
        rows = [{"status": "created", "id": 500 + start + i} for i in range(len(payloads))]
        return {"created": len(rows), "failed": 0, "results": rows}

    op.execute_query_to_json_file.side_effect = read
    op._create_work_packages_batch.side_effect = create
    return op


def fake_jira(unrestricted, levels):
    jira = Mock()
    jira.get_projects.return_value = [{"key": "APP"}]

    def search(jql, startAt, maxResults, **kwargs):
        if "level IS NOT EMPTY" in jql:
            rows = levels
        elif "level IS EMPTY" in jql:
            rows = unrestricted
        else:
            raise AssertionError(jql)
        return rows[startAt : startAt + maxResults]

    jira.jira.search_issues.side_effect = search
    jira.select_comments_for_migration.return_value = SimpleNamespace(comments=[], held=0)
    jira.get_issue_watchers.return_value = []
    return jira


@pytest.fixture
def setup(monkeypatch, tmp_path):
    """Configure reports, mappings and the switch; return a factory for the settings."""
    import src.config as cfg

    def _setup(*, on=True, edition="community", report_levels=None, archived=None, mapping=None, reports=True):
        settings = {"security_level_work_packages": on, "batch_size": 100, "skeleton_batch_size": 50}
        if reports:
            report = tmp_path / "report.json"
            report.write_text(
                json.dumps(
                    {
                        "format": REPORT_FORMAT,
                        "levels": [row(), row("10001", ("APP-20",), 0, 0)] if report_levels is None else report_levels,
                        "archived": ARCHIVED if archived is None else archived,
                        "archive_sha256": SHA,
                    }
                )
            )
            admin = tmp_path / "admins.json"
            admin.write_text(json.dumps({"format": ADMIN_REPORT_FORMAT, "archive_sha256": SHA, "edition": edition}))
            settings |= {"security_level_report": str(report), "security_level_admin_report": str(admin)}
        else:
            settings |= {"security_level_report": str(tmp_path / "missing.json")}
        mappings = FakeMappings(
            {"project": {"APP": {"openproject_id": 7}}, "security_level_project": {"APP:10000": SUB_PROJECT}}
            | (mapping or {})
        )
        monkeypatch.setattr(cfg, "migration_config", settings)
        monkeypatch.setattr(cfg, "mappings", mappings)
        monkeypatch.setattr(cfg, "selected_jira_projects", list)
        data = tmp_path / "data"
        data.mkdir(exist_ok=True)
        original = cfg.get_path
        monkeypatch.setattr(cfg, "get_path", lambda key: data if key == "data" else original(key))
        monkeypatch.delenv("J2O_MAX_ISSUES", raising=False)
        return mappings, data

    return _setup


def skeleton(setup, *, unrestricted=(), levels=(), state=None, existing=None, fail=False, **kwargs):
    mappings, data = setup(**kwargs)
    jira, op = fake_jira(list(unrestricted), list(levels)), FakeOpenProject(state, existing, fail)
    migration = WorkPackageSkeletonMigration(jira_client=jira, op_client=op)
    migration._get_j2o_origin_key_cf_id = lambda: 5
    migration._build_skeleton_payload = lambda source, project_id, _cf: {
        "subject": source.fields.summary,
        "project_id": project_id,
        "_jira_key": source.key,
        "_jira_id": str(source.id),
    }
    result = migration.run()
    created = {(payload["subject"], payload["project_id"]) for payload in op.payloads}
    on_disk = json.loads((data / "work_package_mapping.json").read_text()) if op.payloads else {}
    return SimpleNamespace(result=result, jira=jira, op=op, mappings=mappings, created=created, on_disk=on_disk)


def searches(jira):
    return [call.args[0] for call in jira.jira.search_issues.call_args_list]


def test_switch_off_issues_only_the_unrestricted_search_and_reads_no_level_input(setup):
    outcome = skeleton(setup, on=False, reports=False, unrestricted=[issue(1)], levels=level_issues())
    assert outcome.result.success, outcome.result.error
    # The same calls as before the level path existed: one page and the empty page after it.
    assert searches(outcome.jira) == [UNRESTRICTED, UNRESTRICTED]
    assert outcome.created == {("Issue 1", 7)}
    assert "security_level_work_package" not in outcome.mappings.reads
    assert "security_level_project" not in outcome.mappings.reads
    assert "security_level" not in outcome.result.details


@pytest.mark.parametrize("value", ["true", 1, "yes"])
def test_only_the_boolean_true_switches_the_level_path_on(setup, value):
    outcome = skeleton(setup, on=value, reports=False, unrestricted=[issue(1)], levels=level_issues())
    assert searches(outcome.jira) == [UNRESTRICTED, UNRESTRICTED]


def test_switch_on_creates_static_issues_in_the_sub_project_only(setup):
    outcome = skeleton(setup, unrestricted=[issue(1)], levels=level_issues())
    assert outcome.result.success, outcome.result.error
    assert searches(outcome.jira) == [UNRESTRICTED, UNRESTRICTED, LEVELS, LEVELS]
    assert outcome.created == {("Issue 1", 7), ("Issue 10", 100), ("Issue 11", 100)}
    assert outcome.op.reads == [{"identifier": "app_level_10000", "parent_id": 7}]
    level_map = outcome.mappings.data["security_level_work_package"]
    assert level_map["10"] == {
        "jira_key": "APP-10",
        "openproject_id": 501,
        "project_key": "APP",
        "openproject_project_id": 100,
        "security_level_id": "10000",
        "security_level_project": "APP:10000",
        "security_class": "static",
    }
    assert set(level_map) == {"10", "11"}
    # No level work package reaches the work_package mapping, on disk or in the facade.
    assert outcome.on_disk == {"1": {"jira_key": "APP-1", "openproject_id": 500, "project_key": "APP"}}
    assert outcome.mappings.data["work_package"] == outcome.on_disk
    counts = outcome.result.details["security_level"]
    assert (counts["created"], counts["withheld"], counts["unmapped"], counts["refused"]) == (2, 2, 1, 0)


@pytest.mark.parametrize(("edition", "created"), [("community", False), ("enterprise", True)])
def test_per_issue_issues_need_enterprise_and_unexplained_never_go(setup, edition, created):
    outcome = skeleton(setup, edition=edition, levels=level_issues())
    assert (("Issue 12", 100) in outcome.created) is created
    assert ("Issue 13", 100) not in outcome.created
    if created:
        assert outcome.mappings.data["security_level_work_package"]["12"]["security_class"] == "per_issue"


def test_level_without_a_sub_project_entry_is_created_nowhere(setup):
    outcome = skeleton(setup, levels=level_issues())
    assert not any(subject == "Issue 20" for subject, _ in outcome.created)
    assert outcome.result.details["security_level"]["unmapped"] == 1


@pytest.mark.parametrize("level", [None, "", "abc", "0"])
def test_search_result_without_a_security_id_is_refused(setup, level):
    rows = [*level_issues(), issue(30)]
    if level is not None:
        rows[-1].raw["fields"]["security"] = {"id": level}
    outcome = skeleton(setup, levels=rows)
    assert not outcome.result.success
    assert ("Issue 30", 100) not in outcome.created
    assert outcome.result.details["security_level"]["refused"] == 1


def test_archived_issue_with_another_security_id_refuses_its_level(setup):
    archived = [{**ARCHIVED[0], "level_id": "10001"}, ARCHIVED[1]]
    outcome = skeleton(setup, archived=archived, levels=level_issues())
    assert not outcome.result.success
    assert not [pid for _, pid in outcome.created if pid == 100]


def test_a_search_that_does_not_match_the_inventory_refuses_the_level(setup):
    # One more issue than the report counts: it could be anything, so nothing of the level goes.
    outcome = skeleton(setup, levels=[*level_issues(), issue(14, "10000")])
    assert not outcome.result.success
    assert not outcome.op.payloads
    assert outcome.result.details["security_level"]["refused"] == 1


def test_same_counts_with_another_static_issue_refuse_the_level(setup):
    # APP-11 was deleted and APP-14 created after the inventory: the counts still match.
    levels = [issue(10, "10000"), issue(14, "10000"), issue(12, "10000"), issue(13, "10000"), issue(20, "10001")]
    outcome = skeleton(setup, levels=levels)
    assert not outcome.result.success
    assert outcome.result.details["security_level"]["refused"] == 1
    assert not [pid for _, pid in outcome.created if pid == 100]


@pytest.mark.parametrize(
    "change",
    [
        {"static_keys": None},
        {"static_keys": ["APP-10"]},
        {"static_keys": ["APP-10", "APP-12"]},
        {"per_issue": 2},
    ],
)
def test_a_report_without_exact_static_keys_stops_before_any_write(setup, change):
    other = row("10001", ("APP-20",), 0, 0)
    outcome = skeleton(setup, levels=level_issues(), report_levels=[{**row(), **change}, other])
    assert not outcome.result.success
    assert not outcome.op.payloads
    assert "Malformed issue keys for level APP:10000" in repr(outcome.result)


def test_a_reported_level_missing_from_the_search_is_refused(setup):
    outcome = skeleton(setup, levels=[issue(20, "10001")])
    assert not outcome.result.success
    assert outcome.result.details["security_level"]["refused"] == 1


def test_sub_project_entry_of_another_level_is_refused(setup):
    entry = {**SUB_PROJECT, "level_id": "10001"}
    outcome = skeleton(setup, mapping={"security_level_project": {"APP:10000": entry}}, levels=level_issues())
    assert not outcome.result.success
    assert not outcome.op.payloads
    assert not outcome.op.reads


@pytest.mark.parametrize(
    "state",
    [
        {**STATE, "members": 1},
        {**STATE, "public": True},
        {**STATE, "parent_id": 8},
        {**STATE, "id": 101},
        {"found": False},
    ],
)
def test_a_sub_project_that_is_no_longer_private_and_memberless_gets_nothing(setup, state):
    outcome = skeleton(setup, levels=level_issues(), state=state)
    assert not outcome.result.success
    assert not outcome.op.payloads


def test_level_issue_already_in_the_work_package_mapping_is_not_created_again(setup, tmp_path):
    _, data = setup()
    (data / "work_package_mapping.json").write_text(json.dumps({"10": {"jira_key": "APP-10", "openproject_id": 9}}))
    outcome = skeleton(setup, levels=level_issues())
    assert ("Issue 10", 100) not in outcome.created
    assert ("Issue 11", 100) in outcome.created
    assert outcome.result.details["security_level"]["refused"] == 1


def test_rerun_creates_nothing_and_a_moved_entry_is_refused(setup):
    first = skeleton(setup, levels=level_issues())
    level_map = first.mappings.data["security_level_work_package"]
    moved = {**level_map, "11": {**level_map["11"], "security_level_id": "10001"}}
    second = skeleton(setup, mapping={"security_level_work_package": moved}, levels=level_issues())
    assert not second.op.payloads
    counts = second.result.details["security_level"]
    assert (counts["skipped"], counts["refused"]) == (1, 1)


def test_issue_that_lost_its_level_is_not_created_in_the_parent(setup):
    first = skeleton(setup, levels=level_issues())
    level_map = first.mappings.data["security_level_work_package"]
    second = skeleton(setup, mapping={"security_level_work_package": level_map}, unrestricted=[issue(10)])
    assert ("Issue 10", 7) not in second.created


def test_switched_on_level_path_without_reports_stops_before_any_write(setup):
    outcome = skeleton(setup, reports=False, unrestricted=[issue(1)], levels=level_issues())
    assert not outcome.result.success
    assert not outcome.op.payloads
    assert not outcome.jira.jira.search_issues.called


def test_level_id_is_read_from_the_raw_search_result():
    assert issue_level_id(issue(1, "10000")) == "10000"
    assert issue_level_id(issue(1, 10000)) == "10000"
    assert issue_level_id(issue(1)) is None
    assert issue_level_id({"fields": {"security": {"id": True}}}) is None
    for malformed in ("abc", "0", "", "10000 ", "-1"):
        assert issue_level_id(issue(1, malformed)) is None


def test_scope_rejects_malformed_archived_rows():
    report = {"levels": [row()], "archived": [{"key": "APP-1", "level_id": "1", "class": "static"}]}
    with pytest.raises(ValueError, match="archived"):
        LevelScope(report, "community", {})


def test_level_mapping_round_trips_through_the_real_mapping_facade(setup, monkeypatch):
    import src.config as cfg
    from src.mappings.mappings import Mappings

    _, data = setup()
    (data / "project_mapping.json").write_text(json.dumps({"APP": {"openproject_id": 7}}))
    (data / "security_level_project_mapping.json").write_text(json.dumps({"APP:10000": SUB_PROJECT}))
    monkeypatch.setattr(cfg, "mappings", Mappings(data_dir=data))
    jira, op = fake_jira([issue(1)], level_issues()), FakeOpenProject()
    migration = WorkPackageSkeletonMigration(jira_client=jira, op_client=op)
    migration._get_j2o_origin_key_cf_id = lambda: 5
    migration._build_skeleton_payload = lambda source, project_id, _cf: {
        "subject": source.fields.summary,
        "project_id": project_id,
        "_jira_key": source.key,
        "_jira_id": str(source.id),
    }
    assert migration.run().success
    stored = json.loads((data / "security_level_work_package_mapping.json").read_text())
    assert set(stored) == {"10", "11"}
    assert Mappings(data_dir=data).get_mapping("security_level_work_package") == stored
    assert set(json.loads((data / "work_package_mapping.json").read_text())) == {"1"}
    # A second run in a new process reads the entries back and creates nothing.
    monkeypatch.setattr(cfg, "mappings", Mappings(data_dir=data))
    op = FakeOpenProject()
    again = WorkPackageSkeletonMigration(jira_client=fake_jira([issue(1)], level_issues()), op_client=op)
    again._get_j2o_origin_key_cf_id = lambda: 5
    assert again.run().details["security_level"]["skipped"] == 2
    assert not op.payloads


def test_lost_mapping_adopts_the_work_package_the_sub_project_already_holds(setup):
    existing = {"field": True, "work_packages": [900], "keys": [[900, "APP-10"]]}
    outcome = skeleton(setup, levels=level_issues(), existing=existing)
    assert outcome.result.success
    assert outcome.created == {("Issue 11", 100)}
    entry = outcome.mappings.data["security_level_work_package"]["10"]
    assert (entry["openproject_id"], entry["security_level_project"]) == (900, "APP:10000")
    assert outcome.result.details["security_level"]["adopted"] == 1
    assert outcome.op.existing_reads == [{"project_id": 100, "field": "J2O Origin Key"}]


@pytest.mark.parametrize(
    "existing",
    [
        {"field": True, "work_packages": [900], "keys": []},
        {"field": False, "work_packages": [900], "keys": []},
        {"field": True, "work_packages": [900, 901], "keys": [[900, "APP-10"], [901, "APP-10"]]},
        {"field": True, "work_packages": [900], "keys": [[900, "APP-10"], [900, "APP-11"]]},
        {"field": True, "work_packages": [900], "keys": [[900, "APP-99"]]},
        {"field": True, "work_packages": [900]},
        {"field": True, "work_packages": [900, 901], "keys": [[901, "APP-10"], [901, "APP-11"]]},
    ],
)
def test_a_sub_project_with_unclear_work_packages_gets_nothing(setup, existing):
    outcome = skeleton(setup, levels=level_issues(), existing=existing)
    assert not outcome.result.success
    assert not outcome.op.payloads
    assert outcome.result.details["security_level"]["refused"] == 2


def test_adopted_work_packages_are_saved_without_a_new_batch(setup):
    existing = {"field": True, "work_packages": [900, 901], "keys": [[900, "APP-10"], [901, "APP-11"]]}
    outcome = skeleton(setup, levels=level_issues(), existing=existing)
    assert not outcome.op.payloads
    saved = outcome.mappings.data["security_level_work_package"]
    assert {key: entry["openproject_id"] for key, entry in saved.items()} == {"10": 900, "11": 901}


def test_rerun_after_a_lost_batch_answer_adopts_instead_of_creating_again(setup):
    mappings, _ = setup()
    saved = []

    def lost_answer(payloads):
        start = len(saved)
        saved.extend((900 + start + i, "APP-" + payload["subject"].split()[1]) for i, payload in enumerate(payloads))
        raise RuntimeError("batch answer lost")

    def run(existing):
        jira, op = fake_jira([], level_issues()), FakeOpenProject(existing=existing)
        if existing is None:
            op._create_work_packages_batch.side_effect = lost_answer
        migration = WorkPackageSkeletonMigration(jira_client=jira, op_client=op)
        migration._get_j2o_origin_key_cf_id = lambda: 5
        migration._build_skeleton_payload = lambda source, project_id, _cf: {
            "subject": source.fields.summary,
            "project_id": project_id,
            "_jira_key": source.key,
            "_jira_id": str(source.id),
        }
        return migration.run(), op

    first, _ = run(None)
    assert not first.success
    assert [key for _, key in saved] == ["APP-10", "APP-11"]
    held = {"field": True, "work_packages": [wp for wp, _ in saved], "keys": [list(pair) for pair in saved]}
    second, op = run(held)
    assert second.success
    assert not op.payloads
    assert second.details["security_level"]["adopted"] == 2
    entries = mappings.data["security_level_work_package"]
    assert {key: entry["openproject_id"] for key, entry in entries.items()} == {"10": 900, "11": 901}


def test_a_failed_level_batch_makes_the_component_unsuccessful(setup):
    outcome = skeleton(setup, levels=level_issues(), fail=True)
    assert not outcome.result.success
    assert outcome.result.details["security_level"]["failed"] == 2


def test_level_path_runs_when_the_parent_has_no_project_mapping(setup):
    outcome = skeleton(setup, mapping={"project": {}}, unrestricted=[issue(1)], levels=level_issues())
    assert outcome.created == {("Issue 10", 100), ("Issue 11", 100)}


@pytest.mark.parametrize(
    ("report_levels", "archived"),
    [
        ([row(), row(), row("10001", ("APP-20",), 0, 0)], None),
        ([row(), row("10001", ("APP-10", "APP-20"), 0, 0)], None),
        (None, [*ARCHIVED, {"key": "APP-30", "level_id": "10009", "class": "per_issue"}]),
        (None, [*ARCHIVED, {"key": "APP-10", "level_id": "10009", "class": "per_issue"}]),
    ],
)
def test_a_report_with_a_key_or_level_listed_twice_stops_before_any_write(setup, report_levels, archived):
    outcome = skeleton(setup, levels=level_issues(), report_levels=report_levels, archived=archived)
    assert not outcome.result.success
    assert not outcome.op.payloads
    assert "inventory report" in repr(outcome.result)


LIVE = {"field": True, "work_packages": [900, 901], "keys": [[900, "APP-10"], [901, "APP-11"]]}


def live_op(live=None):
    """An OpenProject double whose sub-project read answers ``live`` and whose updates succeed."""
    op = Mock()
    op.live_reads = []

    def read(script):
        op.live_reads.append(json.loads(script.split("\n")[1]))
        return json.loads(json.dumps(LIVE if live is None else live))

    op.execute_query_to_json_file.side_effect = read
    op.batch_update_work_packages.side_effect = lambda rows: {"updated": len(rows)}
    return op


def content(setup, *, unrestricted=(), levels=(), level_map=None, wp_map=None, live=None, **kwargs):
    setup(**kwargs)
    import src.config as cfg

    if level_map is not None:
        cfg.mappings.data["security_level_work_package"] = level_map
    jira, op = fake_jira(list(unrestricted), list(levels)), live_op(live)
    migration = WorkPackageContentMigration(jira_client=jira, op_client=op)
    migration.work_package_mapping = wp_map or {"1": {"jira_key": "APP-1", "openproject_id": 500}}
    results = migration._migrate_content()
    written = [row for call in op.batch_update_work_packages.call_args_list for row in call.args[0]]
    return SimpleNamespace(results=results, jira=jira, op=op, written=written)


LEVEL_MAP = {
    "10": {
        "jira_key": "APP-10",
        "openproject_id": 900,
        "openproject_project_id": 100,
        "security_level_id": "10000",
        "security_level_project": "APP:10000",
    },
    "11": {
        "jira_key": "APP-11",
        "openproject_id": 901,
        "openproject_project_id": 100,
        "security_level_id": "10000",
        "security_level_project": "APP:10000",
    },
}


def test_content_switch_off_reads_only_unrestricted_issues(setup):
    outcome = content(
        setup, on=False, reports=False, unrestricted=[issue(1)], levels=level_issues(), level_map=LEVEL_MAP
    )
    assert set(searches(outcome.jira)) == {UNRESTRICTED, 'project = "APP" AND level IS EMPTY ORDER BY id'}
    assert outcome.written == [{"id": 500, "description": "Text 1"}]


def test_content_of_a_level_issue_goes_to_its_work_package_without_watchers(setup):
    outcome = content(setup, unrestricted=[issue(1)], levels=level_issues(), level_map=LEVEL_MAP)
    assert {"id": 900, "description": "Text 10"} in outcome.written
    assert {"id": 901, "description": "Text 11"} in outcome.written
    assert {"id": 500, "description": "Text 1"} in outcome.written
    assert len(outcome.written) == 3
    level_calls = [call for call in outcome.jira.jira.search_issues.call_args_list if call.args[0] == LEVELS]
    fields = [(call.kwargs["fields"], call.kwargs.get("expand")) for call in level_calls]
    assert list(dict.fromkeys(fields)) == [("security", None), ("*all", "renderedFields,changelog")]
    assert [call.args[0] for call in outcome.jira.get_issue_watchers.call_args_list] == ["APP-1"]
    commented = [call.args[0] for call in outcome.jira.select_comments_for_migration.call_args_list]
    assert commented == ["APP-1", "APP-10", "APP-11"]
    assert outcome.results["security_level"] == {"processed": 2, "updated": 2, "skipped": 3, "refused": 0}
    assert outcome.results["total_updated"] == 3
    assert outcome.op.live_reads == [{"project_id": 100, "field": "J2O Origin Key"}]


@pytest.mark.parametrize(
    "entry",
    [
        {"security_level_id": "10001"},
        {"openproject_project_id": 101},
        {"security_level_project": "APP:10001"},
        {"openproject_id": "900"},
    ],
)
def test_content_refuses_an_entry_that_does_not_match_the_current_level(setup, entry):
    level_map = {**LEVEL_MAP, "10": {**LEVEL_MAP["10"], **entry}}
    outcome = content(setup, levels=level_issues(), level_map=level_map)
    assert not [row for row in outcome.written if row["id"] == 900]
    assert outcome.results["security_level"]["refused"] == 1
    assert outcome.results["total_failed"] == 1


def test_content_refuses_an_issue_the_selection_no_longer_keeps(setup):
    # On Community a per_issue issue is not selected, even if a mapping entry exists.
    level_map = {**LEVEL_MAP, "12": {**LEVEL_MAP["10"], "jira_key": "APP-12", "openproject_id": 902}}
    outcome = content(setup, levels=level_issues(), level_map=level_map)
    assert all(row["id"] != 902 for row in outcome.written)
    assert outcome.results["security_level"]["refused"] == 1


def test_content_refuses_an_issue_whose_level_changed_between_the_two_searches(setup):
    setup()
    import src.config as cfg

    cfg.mappings.data["security_level_work_package"] = LEVEL_MAP
    light, full = level_issues(), level_issues()
    full[0].raw["fields"]["security"] = {"id": "10001"}
    jira, op = fake_jira([], []), live_op()

    def search(jql, startAt, maxResults, **kwargs):
        rows = (light if kwargs["fields"] == "security" else full) if "level IS NOT EMPTY" in jql else []
        return rows[startAt : startAt + maxResults]

    jira.jira.search_issues.side_effect = search
    migration = WorkPackageContentMigration(jira_client=jira, op_client=op)
    migration.work_package_mapping = {"1": {"jira_key": "APP-1", "openproject_id": 500}}
    results = migration._migrate_content()
    written = [row for call in op.batch_update_work_packages.call_args_list for row in call.args[0]]
    assert [row["id"] for row in written] == [901]
    assert results["security_level"]["refused"] == 1


def test_content_recovers_a_lost_level_mapping_from_the_sub_project(setup):
    outcome = content(setup, levels=level_issues(), level_map={})
    assert sorted(row["id"] for row in outcome.written) == [900, 901]
    assert outcome.results["security_level"]["refused"] == 0


@pytest.mark.parametrize(
    "live",
    [
        # APP-10's work package moved out of the sub-project.
        {"field": True, "work_packages": [901], "keys": [[901, "APP-11"]]},
        # The skeleton never created it.
        {"field": True, "work_packages": [], "keys": []},
    ],
)
def test_content_refuses_a_selected_issue_without_a_work_package_in_its_sub_project(setup, live):
    outcome = content(setup, levels=level_issues(), level_map=LEVEL_MAP, live=live)
    assert all(row["id"] != 900 for row in outcome.written)
    assert outcome.results["security_level"]["refused"] >= 1
    assert outcome.results["total_failed"] >= 1


def test_content_refuses_an_entry_that_names_another_work_package(setup):
    live = {"field": True, "work_packages": [901, 950], "keys": [[950, "APP-10"], [901, "APP-11"]]}
    outcome = content(setup, levels=level_issues(), level_map=LEVEL_MAP, live=live)
    assert {row["id"] for row in outcome.written} == {901}
    assert outcome.results["security_level"]["refused"] == 1


def test_content_refuses_every_issue_of_an_unreadable_sub_project(setup):
    outcome = content(setup, levels=level_issues(), level_map=LEVEL_MAP, live={"field": True})
    assert not outcome.written
    assert outcome.results["security_level"]["refused"] == 2
