# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Watchers of level work packages: none until the importer knows the readers of a level (ADR-009)."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components.watcher_migration import WatcherMigration
from tests.unit.test_level_attachments import LIGHT as LEVEL_SEARCH
from tests.unit.test_level_attachments import LIVE
from tests.unit.test_level_work_packages import level_issues
from tests.unit.test_level_work_packages import setup as level_work_package_setup  # noqa: F401  (fixture)

ORDINARY = {
    "1": {"jira_key": "APP-1", "openproject_id": 500, "project_key": "APP"},
    "2": {"jira_key": "APP-2", "openproject_id": 501, "project_key": "APP"},
}
LEVEL_ENTRY = {"10": {"jira_key": "APP-10", "openproject_id": 777, "project_key": "APP"}}
ORDINARY_WATCHERS = [{"work_package_id": 500, "user_id": 5}, {"work_package_id": 501, "user_id": 5}]


@pytest.fixture
def level_setup(level_work_package_setup):  # noqa: F811 (the imported fixture, requested by name)
    """The reports, mappings and switch of the level work package tests."""
    return level_work_package_setup


class FakeOpenProject:
    """Answer the live sub-project read; record every bulk watcher call."""

    def __init__(self, live=None):
        self.live = LIVE if live is None else live
        self.live_reads, self.bulk_calls = [], []

    def execute_query_to_json_file(self, script):
        values = json.loads(script.split("\n")[1])
        self.live_reads.append(values)
        return json.loads(json.dumps(self.live))

    def bulk_add_watchers(self, watchers):
        self.bulk_calls.append([dict(w) for w in watchers])
        return {"created": len(watchers), "skipped": 0, "failed": 0}


def _run(level_setup, *, live=None, ordinary=None, level_map=None, cache=None, runs=1, **kw):
    mappings, data = level_setup(**kw)
    mappings.data["work_package"] = dict(ORDINARY if ordinary is None else ordinary)
    mappings.data["user"] = {"alice": {"openproject_id": 5}}
    if level_map is not None:
        mappings.data["security_level_work_package"] = level_map
    if cache is not None:
        (data / "jira_issues_cache.json").write_text(json.dumps(cache))
    jira = Mock()
    jira.get_projects.return_value = [{"key": "APP"}]
    jira.jira.search_issues.side_effect = lambda jql, startAt, maxResults, **_: (
        level_issues()[startAt : startAt + maxResults] if jql == LEVEL_SEARCH else pytest.fail(jql)
    )
    jira.get_issue_watchers.return_value = [{"name": "alice"}]
    op = FakeOpenProject(live)
    migration = WatcherMigration(jira_client=jira, op_client=op)
    results = [migration.run() for _ in range(runs)]
    return SimpleNamespace(
        result=results[-1] if results else None, results=results, jira=jira, op=op, mappings=mappings
    )


def _watched(outcome):
    return [call.args[0] for call in outcome.jira.get_issue_watchers.call_args_list]


def test_switch_off_reads_and_writes_the_ordinary_watchers_only(level_setup):
    outcome = _run(level_setup, on=False)
    assert outcome.result.success
    assert _watched(outcome) == ["APP-1", "APP-2"]
    outcome.jira.jira.search_issues.assert_not_called()
    assert outcome.op.live_reads == []
    assert outcome.op.bulk_calls == [ORDINARY_WATCHERS]
    assert not {"level_watchers_withheld", "security_level"} & set(outcome.result.details)


def test_level_work_packages_get_no_watchers(level_setup):
    outcome = _run(level_setup, runs=2)
    assert [r.success for r in outcome.results] == [True, True]
    # The watchers of a level issue are never read; a repeated run writes the same payload.
    assert _watched(outcome) == ["APP-1", "APP-2"] * 2
    assert outcome.op.bulk_calls == [ORDINARY_WATCHERS] * 2
    assert outcome.result.details["level_watchers_withheld"] == 2
    assert outcome.result.details["security_level"] == {"work_packages": 2, "refused": 0}


@pytest.mark.parametrize("cached", [False, True], ids=["watchers-endpoint", "cached-snapshot"])
def test_an_ordinary_entry_of_a_level_issue_is_refused(level_setup, cached):
    cache = {f"APP-{n}": {"key": f"APP-{n}"} for n in (1, 2, 10)}
    outcome = _run(level_setup, ordinary={**ORDINARY, **LEVEL_ENTRY}, cache=cache if cached else None)
    assert not outcome.result.success
    assert outcome.result.details["security_level"] == {"work_packages": 1, "refused": 1}
    assert _watched(outcome) == ["APP-1", "APP-2"]
    assert outcome.op.bulk_calls == [ORDINARY_WATCHERS]


@pytest.mark.parametrize(
    ("options", "refused"),
    [
        pytest.param({"live": {"field": True, "work_packages": [901], "keys": [[901, "APP-11"]]}}, 1, id="moved"),
        pytest.param({"live": {"field": True}}, 2, id="unreadable-sub-project"),
        pytest.param(
            {"level_map": {"13": {"jira_key": "APP-13", "openproject_id": 903, "security_level_project": "APP:10000"}}},
            1,
            id="unselected-issue",
        ),
    ],
)
def test_refused_level_issues_are_reported(level_setup, options, refused):
    outcome = _run(level_setup, **options)
    assert not outcome.result.success
    assert outcome.result.details["security_level"]["refused"] == refused
    assert _watched(outcome) == ["APP-1", "APP-2"]
    assert outcome.op.bulk_calls == [ORDINARY_WATCHERS]


def test_refusals_are_reported_on_the_early_return(level_setup):
    outcome = _run(level_setup, live={"field": True}, ordinary=LEVEL_ENTRY)
    assert not outcome.result.success
    assert outcome.result.errors == ["missing_work_package_mapping"]
    assert _watched(outcome) == []
    assert outcome.op.bulk_calls == []
    assert outcome.result.details["security_level"] == {"work_packages": 0, "refused": 3}


def test_the_component_runner_reaches_the_level_path(level_setup):
    # The migration runner calls run_with_change_detection, which ends in run().
    from src.migration import _invoke_component

    outcome = _run(level_setup, runs=0)
    result = _invoke_component("watchers", WatcherMigration(jira_client=outcome.jira, op_client=outcome.op))
    assert result.success, result.message
    assert outcome.op.bulk_calls == [ORDINARY_WATCHERS]
    assert result.details["level_watchers_withheld"] == 2
    assert result.details["security_level"] == {"work_packages": 2, "refused": 0}


def test_a_missing_report_stops_before_any_read(level_setup):
    outcome = _run(level_setup, reports=False)
    assert not outcome.result.success
    assert outcome.result.errors == ["security_level"]
    assert _watched(outcome) == []
    assert outcome.op.bulk_calls == []


READER_AUDIENCE = {"APP:10000": frozenset({5})}


@pytest.fixture
def readers(monkeypatch):
    """A readers file of the inventory: alice (5) is a static reader of APP:10000, bob (6) is not."""
    import src.config as cfg
    from src.application.components.level_access import Audience

    monkeypatch.setattr(
        "src.application.components.level_issues.staging_audience",
        lambda mappings, report, edition: Audience(members=READER_AUDIENCE),
    )

    def configure():
        cfg.migration_config["security_level_readers"] = "readers.json"

    return configure


def _run_with_readers(level_setup, readers, **kw):
    original = level_setup

    def setup(**options):
        result = original(**options)
        readers()
        return result

    return _run(setup, **kw)


def test_level_work_packages_get_only_the_watchers_who_are_readers(level_setup, readers):
    def watchers(key):
        return [{"name": "alice"}, {"name": "bob"}]

    outcome = _run_with_readers(level_setup, readers, runs=0)
    outcome.mappings.data["user"] = {"alice": {"openproject_id": 5}, "bob": {"openproject_id": 6}}
    outcome.jira.get_issue_watchers.side_effect = watchers
    result = WatcherMigration(jira_client=outcome.jira, op_client=outcome.op).run()
    assert result.success, result.message
    level_rows = [row for row in outcome.op.bulk_calls[0] if row["work_package_id"] in (900, 901)]
    assert level_rows == [{"work_package_id": 900, "user_id": 5}, {"work_package_id": 901, "user_id": 5}]
    assert result.details["watchers_not_readers"] == 2
    assert "level_watchers_withheld" not in result.details
    # The level watchers come from the watchers endpoint; their unmapped names are not listed.
    assert sorted(_watched(outcome)) == ["APP-1", "APP-10", "APP-11", "APP-2"]


def test_an_unmapped_level_watcher_is_not_named_in_the_result(level_setup, readers):
    outcome = _run_with_readers(level_setup, readers, runs=0)
    outcome.jira.get_issue_watchers.side_effect = lambda key: [{"name": "carol"}] if key == "APP-10" else []
    result = WatcherMigration(jira_client=outcome.jira, op_client=outcome.op).run()
    assert result.details["unmapped_users"] == []
    assert result.details["skip_reasons"] == {"user_unmapped": 1}


def test_a_failed_level_watcher_read_refuses_the_work_package(level_setup, readers):
    def watchers(key):
        if key == "APP-10":
            raise RuntimeError("watchers endpoint down")
        return [{"name": "alice"}]

    outcome = _run_with_readers(level_setup, readers, runs=0)
    outcome.mappings.data["user"] = {"alice": {"openproject_id": 5}}
    outcome.jira.get_issue_watchers.side_effect = watchers
    result = WatcherMigration(jira_client=outcome.jira, op_client=outcome.op).run()
    assert not result.success
    level_rows = [row for row in outcome.op.bulk_calls[0] if row["work_package_id"] in (900, 901)]
    assert level_rows == [{"work_package_id": 901, "user_id": 5}]


def test_extra_readers_may_watch_their_shared_work_package_only(monkeypatch):
    from collections import Counter

    import src.config as cfg
    from src.application.components.level_issues import LevelTarget, LevelWorkPackages

    shared = {"members": [5], "shares": {"APP-12": [7]}}
    target = LevelTarget("APP:10000", "10000", 100, 7, "app_level_10000", "per_issue", shared)
    static = LevelTarget("APP:10000", "10000", 100, 7, "app_level_10000", "static", shared)
    migration = WatcherMigration.__new__(WatcherMigration)
    migration._level = LevelWorkPackages(
        work_packages={"APP-12": 902, "APP-10": 900}, targets={"APP-12": target, "APP-10": static}
    )
    migration.jira_client = SimpleNamespace(get_issue_watchers=lambda key: [{"name": "alice"}, {"name": "guest"}])
    migration.mappings = SimpleNamespace(
        get_mapping=lambda name: {"alice": {"openproject_id": 5}, "guest": {"openproject_id": 7}}
    )
    monkeypatch.setitem(cfg.migration_config, "security_level_readers", "readers.json")
    rows = migration._level_watchers(Counter())
    assert rows == [
        {"work_package_id": 900, "user_id": 5},
        {"work_package_id": 902, "user_id": 5},
        {"work_package_id": 902, "user_id": 7},
    ]
    assert migration._not_readers == 1
