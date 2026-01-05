import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.migrations.relation_migration import RelationMigration

# Tests that run() and import EnhancedJiraClient are skipped on Python 3.14
# due to a known issue with class definition during pytest imports
_py314_skip = pytest.mark.skipif(
    sys.version_info >= (3, 14),
    reason="Python 3.14 has known issues with class definition during pytest imports",
)


class DummyOpClient:
    def __init__(self) -> None:
        self.created: list[tuple[int, int, str]] = []
        self.relations: set[tuple[int, int]] = set()

    def find_relation(self, a: int, b: int):
        return (a, b) in self.relations

    def create_relation(self, a: int, b: int, rel_type: str) -> bool:
        self.created.append((a, b, rel_type))
        return True

    def bulk_create_relations(self, relations: list[dict]):
        created = 0
        skipped = 0
        for r in relations:
            from_id = r["from_id"]
            to_id = r["to_id"]
            rel_type = r["type"]
            if (from_id, to_id) not in self.relations:
                self.created.append((from_id, to_id, rel_type))
                self.relations.add((from_id, to_id))
                created += 1
            else:
                skipped += 1
        return {"created": created, "skipped": skipped, "failed": 0}


@pytest.fixture(autouse=True)
def _map_store(monkeypatch: pytest.MonkeyPatch):
    # Minimal in-memory mapping store
    class DummyMappings:
        def __init__(self) -> None:
            self._maps: dict[str, dict[str, object]] = {
                "work_package": {"J1": {"openproject_id": 10}, "J2": 20},
                "link_type": {
                    "relates": {},
                    "duplicates": {},
                    "blocks": {},
                    "precedes": {},
                },
            }

        def get_mapping(self, name: str) -> dict[str, object]:
            return self._maps.get(name, {})

        def set_mapping(self, name: str, data: dict[str, object]) -> None:
            self._maps[name] = data

    dummy = DummyMappings()
    import src.config as cfg

    monkeypatch.setattr(cfg, "mappings", dummy, raising=False)
    return dummy


def test_map_type_and_direction():
    rm = RelationMigration(jira_client=MagicMock(), op_client=MagicMock())
    assert rm._map_type_and_direction("duplicates", "inward") == ("duplicates", True)
    assert rm._map_type_and_direction("relates", "outward") == ("relates", False)
    assert rm._map_type_and_direction("foobar", "outward") is None


def test_resolve_wp_id_various_shapes(_map_store):
    rm = RelationMigration(jira_client=MagicMock(), op_client=MagicMock())
    # direct int entry
    _map_store.set_mapping("work_package", {"A": 5})
    assert rm._resolve_wp_id("A") == 5
    # dict with int
    _map_store.set_mapping("work_package", {"B": {"openproject_id": 7}})
    assert rm._resolve_wp_id("B") == 7
    # dict with numeric string
    _map_store.set_mapping("work_package", {"C": {"openproject_id": "8"}})
    assert rm._resolve_wp_id("C") == 8
    # missing / invalid
    _map_store.set_mapping("work_package", {"D": {"openproject_id": "x"}})
    assert rm._resolve_wp_id("D") is None


def _make_issue(key: str, link_type_name: str, direction: str, target_key: str):
    outward = SimpleNamespace(key=target_key) if direction == "outward" else None
    inward = SimpleNamespace(key=target_key) if direction == "inward" else None
    link = SimpleNamespace(
        type=SimpleNamespace(name=link_type_name, outward=link_type_name),
        outwardIssue=outward,
        inwardIssue=inward,
    )
    return key, SimpleNamespace(fields=SimpleNamespace(issuelinks=[link]))


@_py314_skip
def test_run_skips_when_already_exists(monkeypatch: pytest.MonkeyPatch, _map_store):
    op = DummyOpClient()
    # Existing relation 10->20
    op.relations.add((10, 20))

    issues = dict([_make_issue("J1", "relates", "outward", "J2")])

    class DummyJira:
        def batch_get_issues(self, keys):
            return issues

    rm = RelationMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]

    res = rm.run()
    assert res.details["created"] == 0
    assert res.details["skipped"] >= 1
    assert res.success


@_py314_skip
def test_run_creates_with_swap(monkeypatch: pytest.MonkeyPatch, _map_store):
    op = DummyOpClient()

    # blocks + inward => swap to (20,10)
    issues = dict([_make_issue("J1", "blocks", "inward", "J2")])

    class DummyJira:
        def batch_get_issues(self, keys):
            return issues

    rm = RelationMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]

    res = rm.run()
    assert op.created == [(20, 10, "blocks")]
    assert res.details["created"] == 1
    assert res.success
