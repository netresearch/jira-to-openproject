import pytest

from src.application.components.priority_migration import PriorityMigration


class DummyJira:
    def get_priorities(self):
        return [
            {"name": "High"},
            {"name": "Normal"},
        ]

    def batch_get_issues(self, keys):
        class F:
            def __init__(self, name: str) -> None:
                class FF:
                    def __init__(self, n: str) -> None:
                        self.priority = type("P", (), {"name": n})

                self.fields = FF(name)

        return {"TEST-1": F("High")}


class DummyOp:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.updated: list[dict] = []

    def get_issue_priorities(self):
        return [{"id": 1, "name": "Normal"}]

    def create_issue_priority(self, name: str, position=None, is_default=False):
        self.created.append(name)
        return {"id": 2 if name == "High" else 3, "name": name}

    def batch_update_work_packages(self, updates):
        self.updated.extend(updates)
        return {"updated": len(updates), "failed": 0, "results": []}


@pytest.fixture(autouse=True)
def _map_store(monkeypatch: pytest.MonkeyPatch):
    class DummyMappings:
        def __init__(self) -> None:
            self._maps: dict[str, dict] = {
                "work_package": {"TEST-1": {"openproject_id": 10}},
            }

        def get_mapping(self, name: str):
            return self._maps.get(name, {})

        def set_mapping(self, name: str, mapping):
            self._maps[name] = mapping

    from src import config
    from src import mappings as _m  # type: ignore

    # Patch both the Mappings class and the config.mappings instance
    monkeypatch.setattr(_m, "Mappings", DummyMappings)
    monkeypatch.setattr(config, "mappings", DummyMappings())


def test_priority_migration_creates_and_updates(monkeypatch: pytest.MonkeyPatch):
    jira = DummyJira()
    op = DummyOp()

    # No enhanced client patching needed; DummyJira provides batch_get_issues

    mig = PriorityMigration(jira_client=jira, op_client=op)  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)

    assert mp.created_types == 1  # High created
    assert any(u["priority_id"] == 2 for u in op.updated)
    assert ld.success is True


def test_priority_migration_load_with_numeric_outer_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """_load must update WPs even when wp_map has numeric outer keys.

    Production mappings look like:
        {"144952": {"jira_key": "TEST-1", "openproject_id": 10}, ...}

    Before the fix, _load iterated wp_map.items() and called iss_map.get(key)
    with the numeric outer key ("144952"). Since iss_map is keyed by human-
    readable Jira keys ("TEST-1"), every lookup returned None and zero work
    packages were updated — silent data loss.
    """
    from src import config

    numeric_wp_map = {
        "144952": {"jira_key": "TEST-1", "openproject_id": 10},
        "144953": {"jira_key": "TEST-2", "openproject_id": 11},
    }

    class NumericMappings:
        def __init__(self) -> None:
            self._maps: dict[str, dict] = {"work_package": numeric_wp_map}

        def get_mapping(self, name: str):
            return self._maps.get(name, {})

        def set_mapping(self, name: str, mapping):
            self._maps[name] = mapping

    monkeypatch.setattr(config, "mappings", NumericMappings())

    class NumericJira:
        def get_priorities(self):
            return [{"name": "High"}, {"name": "Normal"}]

        def batch_get_issues(self, keys):
            # Jira returns issues keyed by human-readable key
            issues = {
                "TEST-1": _make_priority_issue("TEST-1", "High"),
                "TEST-2": _make_priority_issue("TEST-2", "High"),
            }
            return {k: v for k, v in issues.items() if k in keys}

    op = DummyOp()
    mig = PriorityMigration(jira_client=NumericJira(), op_client=op)  # type: ignore[arg-type]
    ex = mig._extract()
    mp = mig._map(ex)
    ld = mig._load(mp)

    # Both WPs must have been updated with a priority_id.
    # A zero updated count means every iss_map.get(numeric_key) returned None.
    assert ld.updated == 2, (
        f"Expected 2 WPs updated but got {ld.updated}. "
        "Likely cause: _load looked up iss_map with numeric outer key instead of jira_key."
    )
    assert ld.success is True
    wp_ids = {u["id"] for u in op.updated}
    assert wp_ids == {10, 11}, f"Expected WP ids {{10, 11}} but got {wp_ids}"


def _make_priority_issue(key: str, priority_name: str):
    """Build a minimal Jira issue stub with a priority field."""

    class _Priority:
        def __init__(self, name: str) -> None:
            self.name = name

    class _Fields:
        def __init__(self, name: str) -> None:
            self.priority = _Priority(name)

    class _Issue:
        def __init__(self, k: str, pname: str) -> None:
            self.key = k
            self.fields = _Fields(pname)

    return _Issue(key, priority_name)
