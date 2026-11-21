# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components.registry import DEFAULT_COMPONENT_SEQUENCE, PREDEFINED_PROFILES
from src.application.components.sprint_epic_migration import SprintEpicMigration
from tests.unit.test_level_attachments import LEVEL_MAP, LEVEL_REFUSALS, LIVE
from tests.unit.test_level_work_packages import level_issues
from tests.unit.test_level_work_packages import setup as level_work_package_setup  # noqa: F401  (fixture)


@pytest.fixture
def migration(monkeypatch):
    mappings = {
        "work_package": {"111": {"jira_key": "DEMO-1", "openproject_id": 10}, "DEMO-2": 20},
        "project": {"DEMO": {"openproject_id": 4}},
        "sprint": {
            "42": {"openproject_id": 9, "project_id": 4},
            "Closed, sprint": {"openproject_id": 999, "project_id": 5},
        },
    }
    monkeypatch.setattr("src.config.mappings", SimpleNamespace(get_mapping=mappings.get))
    jira = Mock()
    jira.get_custom_fields.return_value = [
        {"id": "customfield_123", "schema": {"custom": "com.pyxis.greenhopper.jira:gh-sprint"}},
        {"id": "customfield_456", "schema": {"custom": "com.pyxis.greenhopper.jira:gh-epic-link"}},
    ]
    jira.batch_get_issues.return_value = {
        "DEMO-1": SimpleNamespace(fields=SimpleNamespace()),
        "DEMO-2": {
            "fields": {
                "customfield_456": "DEMO-1",
                "customfield_123": [
                    {"id": 42, "name": "Closed, sprint"},
                    {"id": 43, "name": "Next sprint"},
                ],
                "customfield_10008": "WRONG-1",
            }
        },
    }
    op = Mock()
    op.ensure_wp_custom_field_id.return_value = 101
    op.batch_update_work_packages.side_effect = lambda rows: {"updated": len(rows), "failed": 0}
    op.bulk_set_wp_custom_field_values.side_effect = lambda rows: {"updated": len(rows), "failed": 0}
    return SprintEpicMigration(jira_client=jira, op_client=op), mappings


@pytest.mark.parametrize("sequence", [DEFAULT_COMPONENT_SEQUENCE, PREDEFINED_PROFILES["full"]])
def test_cold_run_dependencies(sequence):
    assert sequence.index("work_packages_skeleton") < sequence.index("agile_boards") < sequence.index("sprint_epic")
    assert (
        sequence.index("work_packages_skeleton")
        < sequence.index("work_packages_content")
        < sequence.index("sprint_epic")
    )


def test_real_field_ids_and_confirmed_writes(migration):
    mig, _ = migration
    result = mig.run_with_change_detection("sprint_epic")
    assert result.success, result
    assert result.updated == 4
    assert [call.args[0] for call in mig.op_client.batch_update_work_packages.call_args_list] == [
        [{"id": 20, "parent_id": 10}],
        [{"id": 20, "version_id": 9}],
    ]
    fields = mig.op_client.bulk_set_wp_custom_field_values.call_args_list
    assert fields[0].args[0] == [
        {"work_package_id": 20, "custom_field_id": 101, "value": "Closed, sprint, Next sprint"}
    ]
    assert json.loads(fields[1].args[0][0]["value"]) == [
        {"id": "42", "name": "Closed, sprint"},
        {"id": "43", "name": "Next sprint"},
    ]


def test_missing_issue_parent_and_unconfirmed_writes(migration):
    mig, _ = migration
    del mig.jira_client.batch_get_issues.return_value["DEMO-1"]
    mig.jira_client.batch_get_issues.return_value["DEMO-2"]["fields"]["customfield_456"] = "OTHER-7"
    mig.op_client.batch_update_work_packages.side_effect = lambda rows: {"updated": 0, "failed": len(rows)}
    mig.op_client.bulk_set_wp_custom_field_values.side_effect = lambda rows: {}
    result = mig.run()
    assert not result.success
    assert result.failed == 5
    assert result.updated == 0
    assert (
        result.details["parent_links"]
        == result.details["version_assignments"]
        == result.details["sprint_cf_updates"]
        == 0
    )
    assert "DEMO-1" in result.details["missing_issues"]
    assert "OTHER-7" in result.details["unresolved_epics"].values()


def test_release_and_sprint_history_coexist(migration):
    mig, _ = migration
    fields = mig.jira_client.batch_get_issues.return_value["DEMO-2"]["fields"]
    fields["fixVersions"] = [{"name": "Release 1"}]
    result = mig.run()
    assert result.success
    assert result.details["version_assignments"] == 0
    assert result.details["sprint_cf_updates"] == 1


def test_unavailable_field_and_missing_version(migration):
    mig, mappings = migration
    mappings["sprint"].clear()
    mig.op_client.ensure_wp_custom_field_id.return_value = 0
    result = mig.run()
    assert not result.success
    assert result.updated == 1
    assert result.failed == 3
    assert "DEMO-2" in result.details["unresolved_sprints"]
    assert result.details["sprint_cf_updates"] == 0


def test_unavailable_mapping(migration):
    mig, mappings = migration
    mappings["work_package"].clear()
    result = mig.run()
    assert not result.success
    mig.jira_client.batch_get_issues.assert_not_called()
    mig.op_client.bulk_set_wp_custom_field_values.assert_not_called()


def test_serialized_server_sprint_and_sdk_epic(migration):
    mig, _ = migration
    mig.jira_client.batch_get_issues.return_value["DEMO-2"] = SimpleNamespace(
        fields=SimpleNamespace(
            customfield_456="DEMO-1",
            customfield_123=[
                "com.atlassian.greenhopper.service.sprint.Sprint@10ab[id=42,rapidViewId=7,state=CLOSED,name=Closed, sprint,startDate=2026-04-01,endDate=2026-04-15]",
            ],
        )
    )
    result = mig.run()
    assert result.updated == 4
    assert mig.op_client.bulk_set_wp_custom_field_values.call_args_list[0].args[0][0]["value"] == "Closed, sprint"


def test_subtask_keeps_immediate_parent_instead_of_inherited_epic(migration):
    mig, mappings = migration
    mappings["work_package"]["DEMO-3"] = 30
    mig.jira_client.batch_get_issues.return_value["DEMO-3"] = {"fields": {}}
    mig.jira_client.batch_get_issues.return_value["DEMO-2"]["fields"]["parent"] = {"key": "DEMO-3"}
    result = mig.run()
    assert result.success
    assert mig.op_client.batch_update_work_packages.call_args_list[0].args[0] == [{"id": 20, "parent_id": 30}]


def test_missing_subtask_parent_is_actionable(migration):
    mig, _ = migration
    mig.jira_client.batch_get_issues.return_value["DEMO-2"]["fields"]["parent"] = {"key": "DEMO-99"}
    result = mig.run()
    assert not result.success
    assert result.details["unresolved_parents"] == {"DEMO-2": "DEMO-99"}
    assert result.details["parent_links"] == 0


def test_recovers_missing_work_package_mapping_from_project_origin_keys(migration):
    mig, mappings = migration
    mappings["work_package"].clear()
    mig.op_client.get_project_wp_cf_snapshot.return_value = [
        {"id": 10, "jira_issue_key": "DEMO-1"},
        {"id": 20, "jira_issue_key": "DEMO-2"},
        {"id": 90, "jira_issue_key": None},
    ]
    result = mig.run()
    assert result.success
    assert result.updated == 4
    assert not mappings["work_package"]  # A key-only recovery cannot replace the shared Jira ID mapping.
    mig.op_client.get_project_wp_cf_snapshot.assert_called_once_with(4)
    mig.op_client.get_project_wp_cf_snapshot.return_value.append({"id": 11, "jira_issue_key": "DEMO-1"})
    assert not mig.run().success


def test_same_named_sprints_without_versions_keep_both_ids(migration):
    mig, mappings = migration
    mappings["sprint"].clear()
    fields = mig.jira_client.batch_get_issues.return_value["DEMO-2"]["fields"]
    fields["fixVersions"] = [{"id": 75}]
    fields["customfield_123"] = [{"name": "Sprint, one", "id": 21}, {"name": "Sprint, one", "id": 22}]
    result = mig.run()
    assert result.success
    assert result.details["version_assignments"] == 0
    assert result.details["sprint_history_updates"] == 1
    history = mig.op_client.bulk_set_wp_custom_field_values.call_args.args[0][0]["value"]
    assert json.loads(history) == [{"id": "21", "name": "Sprint, one"}, {"id": "22", "name": "Sprint, one"}]


def test_cached_keys_of_unselected_projects_are_not_read(migration, monkeypatch):
    mig, mappings = migration
    mappings["work_package"]["OTHER-1"] = 30
    monkeypatch.setattr("src.config.selected_jira_projects", lambda: ["DEMO"])
    mig.run_with_change_detection("sprint_epic")
    read = [key for call in mig.jira_client.batch_get_issues.call_args_list for key in call.args[0]]
    assert "OTHER-1" not in read
    assert {"DEMO-1", "DEMO-2"} <= set(read)


# --- Level work packages (ADR-009, step 5c) ---------------------------------------------------

LEVELS = 'project = "APP" AND level IS NOT EMPTY ORDER BY created ASC, key ASC'
SPRINT_FIELDS = [
    {"id": "customfield_123", "schema": {"custom": "com.pyxis.greenhopper.jira:gh-sprint"}},
    {"id": "customfield_456", "schema": {"custom": "com.pyxis.greenhopper.jira:gh-epic-link"}},
]
STALE = {"11": {"jira_key": "APP-11", "openproject_id": 510, "project_key": "APP"}}


@pytest.fixture
def level_setup(level_work_package_setup):  # noqa: F811 (the imported fixture, requested by name)
    """The level reports and mappings, one ordinary work package (APP-1) and one sprint version."""

    def _setup(*, level_map=None, ordinary=None, work_packages=None, **kwargs):
        mappings, _ = level_work_package_setup(**kwargs)
        if work_packages is None:
            work_packages = {"1": {"jira_key": "APP-1", "openproject_id": 500, "project_key": "APP"}}
        mappings.data["work_package"] = {**work_packages, **(ordinary or {})}
        mappings.data["sprint"] = {"42": {"openproject_id": 9, "project_id": 7}}
        mappings.data["security_level_work_package"] = dict(LEVEL_MAP if level_map is None else level_map)
        return mappings

    return _setup


def sprint_issue(epic=None):
    fields = {"customfield_123": [{"id": 42, "name": "Sprint 42"}]}
    if epic:
        fields["customfield_456"] = epic
    return {"fields": fields}


def level_sprint_jira(level_rows=None):
    jira = Mock()
    jira.get_projects.return_value = [{"key": "APP"}]
    jira.get_custom_fields.return_value = SPRINT_FIELDS
    jira.searches = []
    rows = level_issues() if level_rows is None else level_rows

    def search(jql, startAt, maxResults, **kwargs):
        jira.searches.append((jql, kwargs.get("fields")))
        assert jql == LEVELS
        return rows[startAt : startAt + maxResults]

    jira.jira.search_issues.side_effect = search
    # APP-1 names the level issue APP-10 as its epic; every issue is in sprint 42.
    jira.batch_get_issues.side_effect = lambda keys: {
        key: sprint_issue("APP-10" if key == "APP-1" else None) for key in keys
    }
    return jira


def level_op(live=None, snapshot=()):
    op = Mock()
    op.live_reads = []

    def read(script):
        op.live_reads.append(json.loads(script.split("\n")[1]))
        return json.loads(json.dumps(LIVE if live is None else live))

    op.execute_query_to_json_file.side_effect = read
    op.get_project_wp_cf_snapshot.return_value = [dict(row) for row in snapshot]
    op.ensure_wp_custom_field_id.return_value = 101
    op.batch_update_work_packages.side_effect = lambda rows: {"updated": len(rows), "failed": 0}
    op.bulk_set_wp_custom_field_values.side_effect = lambda rows: {"updated": len(rows), "failed": 0}
    return op


def level_sprints(level_setup, *, live=None, level_rows=None, snapshot=(), runs=1, **kwargs):
    level_setup(**kwargs)
    jira, op = level_sprint_jira(level_rows), level_op(live, snapshot)
    migration = SprintEpicMigration(jira_client=jira, op_client=op)
    results = [migration.run() for _ in range(runs)]
    return SimpleNamespace(result=results[-1], results=results, jira=jira, op=op)


def written(op):
    """Every work package any write names, with the kind of write."""
    rows = [("wp", r) for c in op.batch_update_work_packages.call_args_list for r in c.args[0]]
    rows += [("field", r) for c in op.bulk_set_wp_custom_field_values.call_args_list for r in c.args[0]]
    return {(kind, r.get("id", r.get("work_package_id")), r.get("parent_id")) for kind, r in rows}


def read_keys(jira):
    return [key for call in jira.batch_get_issues.call_args_list for key in call.args[0]]


def test_level_switch_off_reads_and_writes_as_before(level_setup):
    outcome = level_sprints(level_setup, on=False, reports=False, ordinary=STALE)
    # Before the switch, the ordinary entry of APP-11 is read and written like any other.
    assert read_keys(outcome.jira) == ["APP-1", "APP-11"]
    assert {wp for _, wp, _ in written(outcome.op)} == {500, 510}
    assert outcome.jira.searches == []
    outcome.jira.get_projects.assert_not_called()
    assert outcome.op.live_reads == []
    assert "security_level" not in outcome.result.details


def test_level_switch_on_gives_level_issues_nothing_and_links_nothing_to_them(level_setup):
    outcome = level_sprints(level_setup)
    assert read_keys(outcome.jira) == ["APP-1"]
    # APP-1 gets its sprint; its epic is a level issue, so it gets no parent link.
    assert written(outcome.op) == {("wp", 500, None), ("field", 500, None)}
    assert outcome.result.details["unresolved_epics"] == {"APP-1": "APP-10"}
    # The selection, then the sprint search of the level issues (here without sprints): both pages each.
    assert outcome.jira.searches == [(LEVELS, "security")] * 2 + [(LEVELS, SPRINT_SEARCH)] * 2
    assert outcome.result.details["security_level"] == {"work_packages": 2, "refused": 0}


def test_level_issue_with_an_ordinary_entry_is_refused_and_not_read(level_setup):
    outcome = level_sprints(level_setup, ordinary=STALE)
    assert not outcome.result.success
    assert read_keys(outcome.jira) == ["APP-1"]
    assert {wp for _, wp, _ in written(outcome.op)} == {500}
    assert outcome.result.details["security_level"] == {"work_packages": 1, "refused": 1}


def test_level_issue_recovered_from_the_parent_origin_scan_is_refused(level_setup):
    # Without a work-package mapping the keys come from the origin scan of the parent, which
    # also names a parent work package of the level issue APP-10.
    snapshot = [{"id": 500, "jira_issue_key": "APP-1"}, {"id": 510, "jira_issue_key": "APP-10"}]
    outcome = level_sprints(level_setup, snapshot=snapshot, work_packages={})
    outcome.op.get_project_wp_cf_snapshot.assert_called_once_with(7)
    assert read_keys(outcome.jira) == ["APP-1"]
    # APP-1 names APP-10 as its epic; the recovered parent work package of APP-10 is no target.
    assert written(outcome.op) == {("wp", 500, None), ("field", 500, None)}
    assert outcome.result.details["security_level"]["refused"] == 1


@pytest.mark.parametrize("options", LEVEL_REFUSALS)
def test_level_refusals_fail_the_component_and_level_work_packages_get_nothing(level_setup, options):
    outcome = level_sprints(level_setup, **options)
    assert not outcome.result.success
    assert read_keys(outcome.jira) == ["APP-1"]
    assert {wp for _, wp, _ in written(outcome.op)} == {500}
    assert outcome.result.details["security_level"]["refused"] >= 1


def test_level_repeated_run_gives_the_same_result(level_setup):
    outcome = level_sprints(level_setup, ordinary=STALE, runs=2)
    first, second = outcome.results
    assert first.details["security_level"] == second.details["security_level"] == {"work_packages": 1, "refused": 1}
    assert read_keys(outcome.jira) == ["APP-1", "APP-1"]
    assert len(outcome.op.live_reads) == 2


def test_level_refusals_are_reported_when_no_ordinary_work_package_is_left(level_setup):
    outcome = level_sprints(level_setup, work_packages=dict(STALE))
    assert not outcome.result.success
    assert "No mapped work packages" in outcome.result.message
    outcome.jira.batch_get_issues.assert_not_called()
    assert written(outcome.op) == set()
    assert outcome.result.details["security_level"] == {"work_packages": 1, "refused": 1}


def test_level_switch_on_without_reports_stops_before_any_read_or_write(level_setup):
    outcome = level_sprints(level_setup, reports=False)
    assert not outcome.result.success
    assert outcome.result.errors == ["security_level"]
    outcome.jira.batch_get_issues.assert_not_called()
    assert written(outcome.op) == set()


# --- Sprint versions of level work packages as sub-project copies (ADR-010) -------------------

SPRINT_SEARCH = "project,security,fixVersions,customfield_123"
SECOND_SUB = {
    "jira_project_key": "APP",
    "level_id": "10001",
    "level": "Management",
    "openproject_id": 101,
    "openproject_identifier": "app_level_10001",
    "parent_openproject_id": 7,
}


def level_sprint_row(number, level, sprints=("42",), fix=False):
    fields = {
        "project": {"id": "10", "key": "APP"},
        "security": {"id": level},
        "customfield_123": [{"id": int(sid), "name": f"Sprint {sid}"} for sid in sprints],
        "fixVersions": [{"id": "41", "name": "R1"}] if fix else [],
    }
    return SimpleNamespace(key=f"APP-{number}", id=str(number), raw={"fields": fields})


def copy_op(op, members=()):
    """Answer the sprint copy writer of ``op`` with copies kept per sub-project; record each payload."""
    import base64

    op.copy_syncs, op.copies = [], {}

    def sync(script):
        payload = json.loads(base64.b64decode(script.split("strict_decode64('", 1)[1].split("')", 1)[0]))
        op.copy_syncs.append(payload)
        project = payload["project"]
        if project in members and any((project, row["id"]) not in op.copies for row in payload["sprints"]):
            raise RuntimeError("Create or change level versions before granting project access")
        for row in payload["sprints"]:
            op.copies.setdefault((project, row["id"]), 2000 + len(op.copies))
        return {
            "versions": {f"sprint:{r['id']}": op.copies[project, r["id"]] for r in payload["sprints"]},
            "changed": 0,
        }

    op.execute_json_query.side_effect = sync
    return op


def level_sprint_run(level_setup, *, rows=None, live=None, members=(), runs=1, second=False, **kwargs):
    if second:
        from tests.unit.test_level_work_packages import SUB_PROJECT

        kwargs["mapping"] = {"security_level_project": {"APP:10000": SUB_PROJECT, "APP:10001": SECOND_SUB}}
    mappings = level_setup(**kwargs)
    mappings.data["sprint"]["43"] = {"openproject_id": 19, "project_id": 8}  # a sprint version of another project
    writes = []
    original = mappings.set_mapping
    mappings.set_mapping = lambda name, value: (writes.append(name), original(name, value))
    jira = level_sprint_jira()
    jira.base_url = "https://jira.local"
    sprint_rows = [level_sprint_row(n, "10000") for n in (10, 11)] if rows is None else rows
    selection = jira.jira.search_issues.side_effect

    def search(jql, startAt, maxResults, **kw):
        if kw.get("fields") == "security":
            return selection(jql, startAt, maxResults, **kw)
        jira.searches.append((jql, kw.get("fields")))
        return sprint_rows[startAt : startAt + maxResults]

    jira.jira.search_issues.side_effect = search
    by_project = {101: {"field": True, "work_packages": [920], "keys": [[920, "APP-20"]]}} if second else {}
    op = copy_op(level_op(live), members)
    if by_project:
        default = op.execute_query_to_json_file.side_effect

        def read(script):
            project = json.loads(script.split("\n")[1])["project_id"]
            return dict(by_project[project]) if project in by_project else default(script)

        op.execute_query_to_json_file.side_effect = read
    migration = SprintEpicMigration(jira_client=jira, op_client=op)
    results = [migration.run() for _ in range(runs)]
    return SimpleNamespace(result=results[-1], results=results, jira=jira, op=op, writes=writes, mappings=mappings)


def level_assignments(op):
    """(sub-project, work package, version IDs) of every level version assignment."""
    rows = [r for c in op.batch_update_work_packages.call_args_list for r in c.args[0]]
    return {
        (r["jira_version_issue_origin"]["project"], r["id"], tuple(r["target_version_ids"]))
        for r in rows
        if "jira_version_issue_origin" in r
    }


def test_level_switch_off_writes_no_sprint_copy(level_setup):
    outcome = level_sprint_run(level_setup, on=False, reports=False)
    assert [c.args[0] for c in outcome.op.batch_update_work_packages.call_args_list] == [[{"id": 500, "version_id": 9}]]
    assert not outcome.jira.searches
    outcome.op.execute_json_query.assert_not_called()
    assert not level_assignments(outcome.op)
    assert "security_level_version" not in outcome.writes


def test_level_issues_get_the_sprint_copy_of_their_sub_project(level_setup):
    outcome = level_sprint_run(level_setup)
    # One copy of the parent's sprint version 9, shared by both issues of the sub-project.
    assert [
        (p["project"], p["parent_id"], p["key"], p["jira_project_id"], p["sprints"]) for p in outcome.op.copy_syncs
    ] == [(100, 7, "APP", "10", [{"id": "42", "version": 9}])]
    copy = outcome.op.copies[100, "42"]
    assert level_assignments(outcome.op) == {(100, 900, (copy,)), (100, 901, (copy,))}
    # The ordinary work package keeps the parent's sprint version; no level work package gets the
    # sprint field or the parent version.
    ordinary = [r for c in outcome.op.batch_update_work_packages.call_args_list for r in c.args[0] if "version_id" in r]
    assert ordinary == [{"id": 500, "version_id": 9}]
    fields = {
        r["work_package_id"] for c in outcome.op.bulk_set_wp_custom_field_values.call_args_list for r in c.args[0]
    }
    assert fields == {500}
    assert outcome.mappings.data["security_level_version"] == {
        "APP:10000:sprint:42": {
            "openproject_id": copy,
            "openproject_project_id": 100,
            "security_level_project": "APP:10000",
            "jira_project_key": "APP",
            "jira_sprint_id": "42",
            "parent_version_id": 9,
        }
    }
    assert outcome.jira.searches.count((LEVELS, SPRINT_SEARCH)) == 2


def test_level_repeat_run_adopts_the_sprint_copy(level_setup):
    outcome = level_sprint_run(level_setup, runs=2)
    assert len(outcome.op.copy_syncs) == 2
    assert list(outcome.op.copies) == [(100, "42")]
    assert outcome.writes.count("security_level_version") == 1


def test_level_two_sub_projects_get_two_sprint_copies(level_setup):
    rows = [level_sprint_row(10, "10000"), level_sprint_row(11, "10000"), level_sprint_row(20, "10001")]
    outcome = level_sprint_run(level_setup, rows=rows, second=True)
    first, second = outcome.op.copies[100, "42"], outcome.op.copies[101, "42"]
    assert first != second
    assert level_assignments(outcome.op) == {(100, 900, (first,)), (100, 901, (first,)), (101, 920, (second,))}


@pytest.mark.parametrize(
    ("options", "refused"),
    [
        pytest.param(
            {"rows": [level_sprint_row(10, "10000", fix=True), level_sprint_row(11, "10000")]}, 0, id="fix-version"
        ),
        pytest.param(
            {"rows": [level_sprint_row(10, "10000", ("77",)), level_sprint_row(11, "10000")]}, 1, id="unmapped"
        ),
        pytest.param(
            {"rows": [level_sprint_row(10, "10000", ("43",)), level_sprint_row(11, "10000")]}, 1, id="foreign-project"
        ),
        pytest.param({"rows": [level_sprint_row(10, "10001"), level_sprint_row(11, "10000")]}, 1, id="level-changed"),
        pytest.param({"rows": [level_sprint_row(11, "10000")]}, 1, id="omitted"),
        pytest.param({"live": {"field": True, "work_packages": [901], "keys": [[901, "APP-11"]]}}, 1, id="moved"),
        pytest.param(
            {"ordinary": {"10": {"jira_key": "APP-10", "openproject_id": 510, "project_key": "APP"}}}, 1, id="ordinary"
        ),
    ],
)
def test_level_issue_without_a_confirmed_sprint_gets_no_copy(level_setup, options, refused):
    outcome = level_sprint_run(level_setup, **options)
    copy = outcome.op.copies.get((100, "42"))
    assert level_assignments(outcome.op) == {(100, 901, (copy,))}
    assert outcome.result.details["security_level"]["refused"] == refused


def test_level_sub_project_with_a_member_refuses_the_sprint_copy(level_setup):
    outcome = level_sprint_run(level_setup, members={100})
    assert not outcome.op.copies
    assert not level_assignments(outcome.op)
    assert "security_level_version" not in outcome.writes
    assert outcome.result.details["security_level"] == {"work_packages": 2, "refused": 2}


def test_level_sprint_writer_copies_the_parent_version():
    from src.infrastructure.openproject.version_catalog import sync_level_sprints

    client = Mock()
    client.execute_json_query.return_value = {"versions": {"sprint:42": 2000}, "changed": 1}
    assert sync_level_sprints(client, (100, 7), ("https://jira.local/", "APP", "10"), {"42": 9}) == {"42": 2000}
    script = client.execute_json_query.call_args.args[0]
    assert "sprint = owner.versions.find(row.fetch('version'))" in script
    assert "owner.custom_values.where(custom_field: field).sole.value == value" in script
    assert "!Member.where(project_id: project.id).exists?" in script
    client.execute_json_query.return_value = {"versions": {"sprint:42": "2000"}}
    with pytest.raises(ValueError, match="Incomplete"):
        sync_level_sprints(client, (100, 7), ("https://jira.local", "APP", "10"), {"42": 9})
    with pytest.raises(ValueError, match="Invalid"):
        sync_level_sprints(client, (100, 7), ("https://jira.local", "APP", "10"), {"x": 9})
