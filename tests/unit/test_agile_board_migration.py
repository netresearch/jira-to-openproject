"""Unit tests for AgileBoardMigration component.

Covers happy path (board → query, sprint → version), missing
project-mapping skip, and the closed-sprint state mapping branch.
"""

from __future__ import annotations

import pytest

from src.application.components.agile_board_migration import AgileBoardMigration


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
        {"id": 2, "name": "Lonely Board", "type": "kanban", "location": {"projectKey": "MISSING"}},
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
    """If get_boards raises, _extract returns success with empty data (matches source)."""

    class BoomJira:
        def get_boards(self):
            raise RuntimeError("jira down")

    op = DummyOp()
    mig = AgileBoardMigration(jira_client=BoomJira(), op_client=op)  # type: ignore[arg-type]

    extracted = mig._extract()

    # Source swallows in _get_current_entities_for_type → returns []
    # then _extract wraps that in a successful empty payload.
    assert extracted.success is True
    assert extracted.data == {"boards": [], "sprints": []}
