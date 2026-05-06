from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.application.components.relation_migration import RelationMigration


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
            rel_type = r.get("relation_type") or r.get("type")
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


def test_run_result_includes_runner_count_keys(_map_store):
    """The migration runner reports per-component counts via
    ``details.success_count`` / ``failed_count`` / ``total_count``
    (see ``base_migration._extract_counts``). Without these keys the
    summary line shows '0/0 items migrated, 0 failed' even when the
    migration actually created hundreds of relations — exactly what
    happened on the live NRS run on 2026-05-04.
    """
    op = DummyOpClient()
    issues = dict([_make_issue("J1", "relates", "outward", "J2")])

    class DummyJira:
        def batch_get_issues(self, keys):
            return issues

    rm = RelationMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    res = rm.run()

    # Required keys for the runner to report real numbers
    assert "success_count" in res.details, res.details
    assert "failed_count" in res.details
    assert "total_count" in res.details
    # And they must agree with the per-flow counters
    assert res.details["success_count"] == res.details["created"]
    assert res.details["failed_count"] == res.details["errors"]
    assert res.details["total_count"] == res.details["created"] + res.details["skipped"] + res.details["errors"]


# --- Skip-reason breakdown ---------------------------------------------------
# Per the live NRS audit (2026-05-06): the relation migration silently
# dropped ~75% of links with no per-row logging, so an operator
# couldn't tell "real loss" from "expected cross-project asymmetry".
# These tests pin a categorized ``skip_reasons`` breakdown surfaced
# in ``result.details`` so the next live run is diagnosable.


def test_run_skip_reasons_target_wp_unmigrated(_map_store):
    """The biggest bucket in practice — link's other end isn't in WP map."""
    op = DummyOpClient()
    # J1 is mapped (id=10), J2 is mapped (id=20), but the link points to
    # J99 which is NOT in the mapping (mimics cross-project / unmigrated).
    issues = dict([_make_issue("J1", "relates", "outward", "J99")])

    class DummyJira:
        def batch_get_issues(self, keys):
            return issues

    rm = RelationMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    res = rm.run()

    breakdown = res.details["skip_reasons"]
    assert breakdown.get("target_wp_unmigrated") == 1, breakdown
    assert res.details["created"] == 0


def test_run_skip_reasons_link_type_unmapped(_map_store):
    """Link of an unknown type maps to ``link_type_unmapped`` bucket."""
    op = DummyOpClient()
    # J1→J2 with a Jira link type the migrator's direction_map doesn't
    # know about (e.g. some custom Netresearch link type).
    issues = dict([_make_issue("J1", "totally-bogus-link", "outward", "J2")])

    class DummyJira:
        def batch_get_issues(self, keys):
            return issues

    rm = RelationMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    res = rm.run()

    breakdown = res.details["skip_reasons"]
    assert breakdown.get("link_type_unmapped") == 1, breakdown
    assert res.details["created"] == 0


def test_run_skip_reasons_breakdown_sums_to_total_skipped(_map_store):
    """The breakdown dict's values must sum to the aggregate ``skipped``."""
    op = DummyOpClient()
    # 1 unmigrated target + 1 unknown link type + 1 healthy
    issues = dict(
        [
            _make_issue("J1", "relates", "outward", "J99"),  # target unmigrated
            _make_issue("J2", "totally-bogus-link", "outward", "J1"),  # type unmapped
        ],
    )

    class DummyJira:
        def batch_get_issues(self, keys):
            return issues

    rm = RelationMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    res = rm.run()

    breakdown = res.details["skip_reasons"]
    # Total skipped = pre-bulk + bulk_skipped. The Counter from pre-bulk
    # plus the bulk_dedup_or_invalid bucket must equal the aggregate.
    assert sum(breakdown.values()) == res.details["skipped"], (
        breakdown,
        res.details["skipped"],
    )
    assert breakdown.get("target_wp_unmigrated") == 1
    assert breakdown.get("link_type_unmapped") == 1


def test_run_skip_reasons_empty_dict_when_all_succeed(_map_store):
    """When every relation migrates, the breakdown is empty — no
    ``skip_reasons`` entries fire.
    """
    op = DummyOpClient()
    issues = dict([_make_issue("J1", "relates", "outward", "J2")])

    class DummyJira:
        def batch_get_issues(self, keys):
            return issues

    rm = RelationMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    res = rm.run()

    breakdown = res.details["skip_reasons"]
    assert breakdown == {}, breakdown
    assert res.details["created"] == 1
    assert res.details["skipped"] == 0
