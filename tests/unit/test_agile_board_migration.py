# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Unit tests for AgileBoardMigration component.

Covers happy path (board → query, sprint → version), missing
project-mapping skip, and the closed-sprint state mapping branch.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.application.components.agile_board_migration import AgileBoardMigration
from src.utils.change_detector import ChangeDetector
from tests.unit.test_level_attachments import LEVEL_MAP, LEVEL_REFUSALS, LIVE
from tests.unit.test_level_work_packages import level_issues
from tests.unit.test_level_work_packages import setup as level_work_package_setup  # noqa: F401  (fixture)


class DummyJira:
    def __init__(
        self,
        boards: list[dict] | None = None,
        sprints_by_board: dict[int, list[dict]] | None = None,
        configurations_by_board: dict[int, dict] | None = None,
    ) -> None:
        self._boards = boards if boards is not None else []
        self._sprints = sprints_by_board or {}
        self._configs = configurations_by_board or {}

    def get_boards(self):
        return self._boards

    def get_board_projects(self, board_id):
        board = next(row for row in self._boards if row["id"] == board_id)
        return [{"key": board["location"]["projectKey"]}] if board.get("location") else []

    def get_board_issue_keys(self, board_id, project_key):
        return []

    def get_board_configuration(self, board_id):
        return self._configs.get(board_id, {})

    def get_board_sprints(self, board_id):
        return self._sprints.get(board_id, [])


class DummyOp:
    def __init__(self) -> None:
        self.created_queries: list[dict] = []
        self.created_versions: list[dict] = []

    def create_or_update_query(self, **payload):
        self.created_queries.append(payload)
        return {"success": True, "created": True, "id": 700 + len(self.created_queries)}

    def ensure_project_version(self, **payload):
        self.created_versions.append(payload)
        return {"success": True, "created": True, "id": 800 + len(self.created_versions)}


@pytest.fixture
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "project": {"PROJ": {"openproject_id": 11}},
                "sprint": {},
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

        def set_mapping(self, name: str, value):
            self._m[name] = value

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def test_agile_board_migration_end_to_end_creates_query_and_version(
    _mock_mappings: None,
) -> None:
    """One mapped board → one query; one open sprint → one open version."""
    boards = [
        {
            "id": 1,
            "name": "Sprint Board",
            "type": "scrum",
            "location": {"projectKey": "PROJ"},
        },
    ]
    configs = {
        1: {
            "columnConfig": {"columns": [{"statuses": [{"id": "10001"}]}]},
            "filter": {"query": "project = PROJ"},
        },
    }
    sprints = {
        1: [
            {
                "id": 42,
                "name": "Sprint 1",
                "state": "active",
                "startDate": "2025-01-01",
                "endDate": "2025-01-14",
                "goal": "ship it",
            },
        ],
    }
    op = DummyOp()
    mig = AgileBoardMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board=sprints, configurations_by_board=configs),
        op_client=op,
    )  # type: ignore[arg-type]

    extracted = mig._extract()
    mapped = mig._map(extracted)
    result = mig._load(mapped)

    assert extracted.success is True
    assert mapped.success is True
    assert mapped.details["queries"] == 1
    assert mapped.details["versions"] == 1
    assert result.success is True
    # One query + one version created.
    assert result.details["queries_created"] == 1
    assert result.details["versions_created"] == 1
    # Closed status only on `state == 'closed'`; an active sprint must remain "open".
    assert op.created_versions[0]["status"] == "open"


def test_agile_board_migration_skips_unmapped_project_boards_and_sprints(
    _mock_mappings: None,
) -> None:
    """A board / sprint whose projectKey isn't in project_mapping is skipped."""
    boards = [
        {"id": 2, "name": "Lonely Board", "type": "scrum", "location": {"projectKey": "MISSING"}},
    ]
    sprints = {2: [{"id": 99, "name": "Orphan Sprint", "state": "active"}]}
    op = DummyOp()
    mig = AgileBoardMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board=sprints),
        op_client=op,
    )  # type: ignore[arg-type]

    extracted = mig._extract()
    mapped = mig._map(extracted)

    assert mapped.success is True
    assert mapped.details["queries"] == 0
    assert mapped.details["versions"] == 0
    assert mapped.details["skipped_boards"] == 1
    assert mapped.details["skipped_sprints"] == 1


def test_agile_board_migration_closed_sprint_maps_to_closed_version(
    _mock_mappings: None,
) -> None:
    """state='closed' (case-insensitive) → status='closed' on the version payload."""
    boards = [{"id": 1, "name": "B", "type": "scrum", "location": {"projectKey": "PROJ"}}]
    sprints = {1: [{"id": 50, "name": "Done Sprint", "state": "CLOSED"}]}
    op = DummyOp()
    mig = AgileBoardMigration(
        jira_client=DummyJira(boards=boards, sprints_by_board=sprints),
        op_client=op,
    )  # type: ignore[arg-type]

    extracted = mig._extract()
    mapped = mig._map(extracted)
    result = mig._load(mapped)

    assert result.success is True
    assert op.created_versions[0]["status"] == "closed"


def test_agile_board_migration_handles_jira_failure_gracefully(
    _mock_mappings: None,
) -> None:
    """A Jira outage must be distinguishable from an empty inventory."""

    class BoomJira:
        def get_boards(self):
            raise RuntimeError("jira down")

    op = DummyOp()
    mig = AgileBoardMigration(jira_client=BoomJira(), op_client=op)  # type: ignore[arg-type]

    extracted = mig._extract()

    assert not extracted.success
    assert "jira down" in extracted.message


def test_server_board_without_location_uses_projects_endpoint(_mock_mappings, monkeypatch, tmp_path):
    jira = DummyJira(boards=[{"id": 1, "name": "Server Board", "type": "scrum"}])
    monkeypatch.setattr(jira, "get_board_projects", lambda board: [{"key": "PROJ"}], raising=False)
    op = DummyOp()
    mig = AgileBoardMigration(jira_client=jira, op_client=op)
    mig.change_detector = ChangeDetector(tmp_path)
    result = mig.run_with_change_detection("agile_boards")
    assert result.success
    assert op.created_queries[0]["project_id"] == 11
    assert op.created_queries[0]["is_public"] is False


def test_ambiguous_board_projects_are_reported(_mock_mappings, monkeypatch):
    jira = DummyJira(boards=[{"id": 1, "name": "Shared Board", "type": "scrum"}])
    monkeypatch.setattr(jira, "get_board_projects", lambda board: [{"key": "A"}, {"key": "B"}], raising=False)
    op = DummyOp()
    result = AgileBoardMigration(jira_client=jira, op_client=op).run()
    assert not result.success
    assert result.details["unresolved_projects"][0]["board_id"] == 1
    assert result.details["unresolved_projects"][0]["candidate_projects"] == ["A", "B"]
    assert not op.created_queries


def test_next_run_sees_sprint_edit_during_load(_mock_mappings, monkeypatch, tmp_path):
    sprint = {"id": 42, "name": "April", "goal": "First goal"}
    jira = DummyJira(boards=[{"id": 2, "location": {"projectKey": "PROJ"}}], sprints_by_board={2: [sprint]})
    op = DummyOp()

    def write_version(**payload):
        op.created_versions.append(payload)
        sprint["goal"] = "Edited during load"
        return {"success": True, "id": 42}

    monkeypatch.setattr(op, "ensure_project_version", write_version)
    mig = AgileBoardMigration(jira_client=jira, op_client=op)
    mig.change_detector = ChangeDetector(tmp_path)
    for _ in range(3):
        assert mig.run_with_change_detection("agile_boards").success
    assert [v["description"] for v in op.created_versions] == ["First goal", "Edited during load"]


def test_filtered_board_does_not_claim_the_origin_boards_sprint(_mock_mappings):
    boards = [{"id": n, "name": f"Board {n}", "type": "scrum", "location": {"projectKey": "PROJ"}} for n in (10, 20)]
    sprint = {"id": 8, "name": "Sprint, eight", "originBoardId": "20", "state": "closed"}
    op = DummyOp()
    mig = AgileBoardMigration(DummyJira(boards=boards, sprints_by_board={10: [sprint], 20: [sprint]}), op)
    extracted = mig._extract()
    assert [(row["id"], row["board_id"]) for row in extracted.data["sprints"]] == [(8, 20)]
    assert mig._load(mig._map(extracted)).success
    assert len(op.created_versions) == 1


def test_shared_board_is_split_into_exact_mapped_project_snapshots(_mock_mappings, monkeypatch):
    from src import config

    config.mappings.set_mapping("project", {"A": {"openproject_id": 11}, "B": {"openproject_id": 12}})
    config.mappings.set_mapping("work_package", {"91": {"jira_key": "A-1", "openproject_id": 25}, "A-2": 26})
    jira = DummyJira(boards=[{"id": 23, "name": "Shared", "type": "kanban"}])
    monkeypatch.setattr(jira, "get_board_projects", lambda _: [{"key": key} for key in ("A", "B", "C")], raising=False)
    monkeypatch.setattr(jira, "get_board_issue_keys", lambda _, project: {"A": ["A-1"], "B": []}[project])
    op = DummyOp()
    assert AgileBoardMigration(jira, op).run().success
    assert [(p["project_id"], p["options"]["board_work_package_ids"]) for p in op.created_queries] == [
        (11, [25]),
        (12, []),
    ]
    monkeypatch.setattr(jira, "get_board_issue_keys", lambda _, project: ["A-999"])
    monkeypatch.setattr("src.application.components.agile_board_migration.fetch_issue_page_with_retry", _no_issues)
    assert not AgileBoardMigration(jira, op).run().success
    assert len(op.created_queries) == 2


def _no_issues(*_args, **_kwargs):
    return []


def test_board_omits_issues_withheld_for_their_security_level(_mock_mappings, monkeypatch):
    from types import SimpleNamespace

    from src import config

    config.mappings.set_mapping("project", {"A": {"openproject_id": 11}})
    config.mappings.set_mapping("work_package", {"91": {"jira_key": "A-1", "openproject_id": 25}})
    jira = DummyJira(boards=[{"id": 23, "name": "Secure", "type": "kanban"}])
    monkeypatch.setattr(jira, "get_board_projects", lambda _: [{"key": "A"}], raising=False)
    monkeypatch.setattr(jira, "get_board_issue_keys", lambda _, project: ["A-1", "A-2", "A-3"])
    queries = []

    def search(_client, *, jql, start_at, **_kwargs):
        queries.append(jql)
        secured = SimpleNamespace(id="2", key="A-2", raw={"fields": {"security": {"id": "1", "name": "Staff"}}})
        return [secured] if start_at == 0 else []

    monkeypatch.setattr("src.application.components.agile_board_migration.fetch_issue_page_with_retry", search)
    op = DummyOp()
    # A-3 is neither mapped nor reported with a security level, so the board stays unverified.
    assert not AgileBoardMigration(jira, op).run().success
    assert queries[0] == 'project = "A" AND key in ("A-2", "A-3") AND level IS NOT EMPTY ORDER BY id'

    monkeypatch.setattr(jira, "get_board_issue_keys", lambda _, project: ["A-1", "A-2"])
    op = DummyOp()
    assert AgileBoardMigration(jira, op).run().success
    assert [p["options"]["board_work_package_ids"] for p in op.created_queries] == [[25]]


def test_board_pagination_requires_all_issue_keys_in_the_requested_project():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from src.infrastructure.jira.jira_agile_service import JiraAgileService
    from src.infrastructure.jira.jira_client import JiraApiError

    session = Mock()
    service = JiraAgileService(SimpleNamespace(base_url="https://jira.invalid", jira=SimpleNamespace(_session=session)))

    def page(offset, key):
        return {"startAt": offset, "total": 2, "issues": [{"key": key, "fields": {"project": {"key": "A"}}}]}

    session.get.return_value.json.side_effect = [page(0, "A-1"), page(1, "A-2")]
    assert service.get_board_issue_keys(12, "A") == ["A-1", "A-2"]
    assert [c.kwargs["params"]["startAt"] for c in session.get.call_args_list] == [0, 1]
    session.get.return_value.json.side_effect = [page(0, "A-1"), page(1, "A-1")]
    with pytest.raises(JiraApiError, match="duplicate"):
        service.get_board_issue_keys(12, "A")
    session.get.return_value.json.side_effect = [page(0, "B-1")]
    with pytest.raises(JiraApiError, match="outside"):
        service.get_board_issue_keys(12, "A")


def test_project_filter_can_select_a_shared_board_with_a_different_home(_mock_mappings, monkeypatch):
    from src import config

    board = {"id": 31, "name": "Shared", "type": "scrum", "location": {"projectKey": "OTHER"}}
    jira = DummyJira(boards=[board], sprints_by_board={31: [{"id": 12, "name": "Other project sprint"}]})
    monkeypatch.setitem(config.migration_config, "jira_project_filter", ["PROJ"])
    monkeypatch.setattr(jira, "get_boards", lambda: pytest.fail("Unscoped board listing"))
    monkeypatch.setattr(jira, "get_boards_for_project", lambda key: [board] if key == "PROJ" else [], raising=False)
    monkeypatch.setattr(jira, "get_board_projects", lambda _: [{"key": "PROJ"}, {"key": "OTHER"}])
    op = DummyOp()
    assert AgileBoardMigration(jira, op).run().success
    assert [q["project_id"] for q in op.created_queries] == [11]
    assert not op.created_versions


def test_cli_project_filter_limits_the_boards(_mock_mappings, monkeypatch):
    from tests.helpers.cli_project_filter import apply_cli_project_filter

    board = {"id": 31, "name": "Shared", "type": "scrum", "location": {"projectKey": "OTHER"}}
    jira = DummyJira(boards=[board], sprints_by_board={31: [{"id": 12, "name": "Other project sprint"}]})
    apply_cli_project_filter(monkeypatch, "PROJ")
    monkeypatch.setattr(jira, "get_boards", lambda: pytest.fail("Unscoped board listing"))
    monkeypatch.setattr(jira, "get_boards_for_project", lambda key: [board] if key == "PROJ" else [], raising=False)
    monkeypatch.setattr(jira, "get_board_projects", lambda _: [{"key": "PROJ"}, {"key": "OTHER"}])
    op = DummyOp()
    assert AgileBoardMigration(jira, op).run().success
    assert [q["project_id"] for q in op.created_queries] == [11]


# --- Level work packages (ADR-009, step 5c) ---------------------------------------------------

LEVELS = 'project = "APP" AND level IS NOT EMPTY ORDER BY created ASC, key ASC'
BOARD_KEYS = ["APP-1", "APP-10", "APP-11", "APP-12", "APP-13", "APP-20"]


@pytest.fixture
def level_setup(level_work_package_setup):  # noqa: F811 (the imported fixture, requested by name)
    """The level reports and mappings with one ordinary work package (APP-1)."""

    def _setup(*, level_map=None, ordinary=None, **kwargs):
        mappings, _ = level_work_package_setup(**kwargs)
        mappings.data["work_package"] = {"1": {"jira_key": "APP-1", "openproject_id": 500, "project_key": "APP"}}
        mappings.data["work_package"].update(ordinary or {})
        mappings.data["security_level_work_package"] = dict(LEVEL_MAP if level_map is None else level_map)
        return mappings

    return _setup


class LevelJira(DummyJira):
    """One kanban board of APP holding every issue; answer and record the level search."""

    def __init__(self, level_rows=None):
        super().__init__(boards=[{"id": 23, "name": "Team", "type": "kanban", "location": {"projectKey": "APP"}}])
        self.get_projects = Mock(return_value=[{"key": "APP"}])
        self.searches = []
        rows = level_issues() if level_rows is None else level_rows

        def search(jql, startAt, maxResults, **kwargs):
            self.searches.append((jql, kwargs.get("fields")))
            assert jql == LEVELS
            return rows[startAt : startAt + maxResults]

        self.jira = Mock()
        self.jira.search_issues.side_effect = search

    def get_board_issue_keys(self, board_id, project_key):
        return list(BOARD_KEYS)


class LevelOp(DummyOp):
    def __init__(self, live=None):
        super().__init__()
        self.live = LIVE if live is None else live
        self.live_reads = []

    def execute_query_to_json_file(self, script):
        self.live_reads.append(json.loads(script.split("\n")[1]))
        return json.loads(json.dumps(self.live))


def level_board(level_setup, monkeypatch, *, live=None, level_rows=None, runs=1, detect=False, **kwargs):
    level_setup(**kwargs)
    withheld_queries = []

    def search(_client, *, jql, start_at, **_kwargs):
        # Jira reports every listed board issue except APP-1 with a security level.
        withheld_queries.append(jql)
        keys = jql.split("key in (")[1].split(")")[0].replace('"', "").split(", ")
        rows = [SimpleNamespace(key=key, raw={"fields": {"security": {"id": "10000"}}}) for key in keys]
        return rows if start_at == 0 else []

    monkeypatch.setattr("src.application.components.agile_board_migration.fetch_issue_page_with_retry", search)
    jira, op = LevelJira(level_rows), LevelOp(live)
    migration = AgileBoardMigration(jira, op)
    run = migration.run
    if detect:
        migration.change_detector = ChangeDetector(detect)
        run = lambda: migration.run_with_change_detection("agile_boards")  # noqa: E731
    results = [run() for _ in range(runs)]
    return SimpleNamespace(result=results[-1], results=results, jira=jira, op=op, withheld=withheld_queries)


def board_ids(op):
    return [q["options"]["board_work_package_ids"] for q in op.created_queries]


def withheld_query(*keys):
    listed = ", ".join(f'"{key}"' for key in keys)
    return f'project = "APP" AND key in ({listed}) AND level IS NOT EMPTY ORDER BY id'


def test_level_switch_off_builds_the_board_query_as_before(level_setup, monkeypatch):
    outcome = level_board(level_setup, monkeypatch, on=False, reports=False)
    assert outcome.result.success, outcome.result
    assert board_ids(outcome.op) == [[500]]
    # The first page and the empty page that ends the search.
    assert outcome.withheld == [withheld_query(*BOARD_KEYS[1:])] * 2
    # No level search, no project listing, no sub-project read.
    assert outcome.jira.searches == []
    outcome.jira.get_projects.assert_not_called()
    assert outcome.op.live_reads == []
    assert "security_level" not in outcome.result.details


def test_level_switch_on_keeps_level_cards_out_of_the_parent_board(level_setup, monkeypatch):
    outcome = level_board(level_setup, monkeypatch)
    assert outcome.result.success, outcome.result
    # One private query in the parent; the level sub-project gets none.
    assert [(q["project_id"], q["is_public"]) for q in outcome.op.created_queries] == [(7, False)]
    assert board_ids(outcome.op) == [[500]]
    assert outcome.jira.searches == [(LEVELS, "security"), (LEVELS, "security")]
    assert outcome.op.live_reads == [{"project_id": 100, "field": "J2O Origin Key"}]
    assert outcome.result.details["security_level"] == {"work_packages": 2, "refused": 0}


def test_level_issue_with_an_ordinary_entry_is_refused_and_stays_off_the_board(level_setup, monkeypatch):
    ordinary = {"11": {"jira_key": "APP-11", "openproject_id": 510, "project_key": "APP"}}
    off = level_board(level_setup, monkeypatch, on=False, reports=False, ordinary=ordinary)
    # Before the switch, the ordinary entry put the card on the parent board.
    assert board_ids(off.op) == [[500, 510]]
    outcome = level_board(level_setup, monkeypatch, ordinary=ordinary)
    assert not outcome.result.success
    assert board_ids(outcome.op) == [[500]]
    # The first page and the empty page that ends the search.
    assert outcome.withheld == [withheld_query(*BOARD_KEYS[1:])] * 2
    assert outcome.result.details["security_level"] == {"work_packages": 1, "refused": 1}
    assert outcome.result.failed_count == 1


@pytest.mark.parametrize("options", LEVEL_REFUSALS)
def test_level_refusals_fail_the_component_and_no_level_card_reaches_a_board(level_setup, monkeypatch, options):
    outcome = level_board(level_setup, monkeypatch, **options)
    assert not outcome.result.success
    assert board_ids(outcome.op) == [[500]]
    refused = outcome.result.details["security_level"]["refused"]
    assert refused >= 1
    assert outcome.result.failed_count == refused


def test_level_repeated_runs_with_change_detection_resolve_and_report_each_time(level_setup, monkeypatch, tmp_path):
    # An unchanged board would let change detection skip the second pass without any report.
    outcome = level_board(level_setup, monkeypatch, runs=2, detect=tmp_path)
    first, second = outcome.results
    assert first.success, first
    assert first.details["security_level"] == second.details["security_level"] == {"work_packages": 2, "refused": 0}
    assert board_ids(outcome.op) == [[500], [500]]
    assert len(outcome.op.live_reads) == 2


def test_level_ordinary_entries_of_unselected_projects_are_not_refused(level_setup, monkeypatch):
    # The archived APP-12 has an ordinary entry, but only OTHER is selected for this run.
    ordinary = {"12": {"jira_key": "APP-12", "openproject_id": 512, "project_key": "APP"}}
    level_setup(ordinary=ordinary)
    monkeypatch.setattr("src.config.selected_jira_projects", lambda: ["OTHER"])
    jira, op = LevelJira(), LevelOp()
    jira.get_boards_for_project = Mock(return_value=[])
    result = AgileBoardMigration(jira, op).run()
    assert result.success, result
    assert jira.searches == []
    assert result.details["security_level"] == {"work_packages": 0, "refused": 0}


def test_level_refusals_are_reported_when_the_board_read_fails(level_setup, monkeypatch):
    ordinary = {"11": {"jira_key": "APP-11", "openproject_id": 510, "project_key": "APP"}}
    level_setup(ordinary=ordinary)
    jira, op = LevelJira(), LevelOp()
    jira.get_boards = Mock(side_effect=RuntimeError("jira down"))
    result = AgileBoardMigration(jira, op).run()
    assert not result.success
    assert op.created_queries == []
    assert result.details["security_level"] == {"work_packages": 1, "refused": 1}


def test_level_switch_on_without_reports_stops_before_any_board_read(level_setup, monkeypatch):
    level_setup(reports=False)
    jira, op = LevelJira(), LevelOp()
    jira.get_boards = Mock(side_effect=AssertionError("board read"))
    result = AgileBoardMigration(jira, op).run()
    assert not result.success
    assert result.errors == ["security_level"]
    assert op.created_queries == []
