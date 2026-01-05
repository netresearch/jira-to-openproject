import sys
from unittest.mock import MagicMock

import pytest

from src.migrations.watcher_migration import WatcherMigration

# Skip these tests on Python 3.14 due to a known issue with class definition
# during pytest import mocking
pytestmark = pytest.mark.skipif(
    sys.version_info >= (3, 14),
    reason="Python 3.14 has known issues with class definition during pytest imports",
)


class DummyOpClient:
    def __init__(self) -> None:
        self.added: list[tuple[int, int]] = []
        self.bulk_watchers: list[dict] = []

    def add_watcher(self, wp_id: int, user_id: int) -> bool:
        self.added.append((wp_id, user_id))
        return True

    def bulk_add_watchers(self, watchers: list[dict]):
        self.bulk_watchers.extend(watchers)
        # Track what was added for assertions
        for w in watchers:
            self.added.append((w["wp_id"], w["user_id"]))
        return {"created": len(watchers), "skipped": 0, "failed": 0}


@pytest.fixture(autouse=True)
def _map_store(monkeypatch: pytest.MonkeyPatch):
    class DummyMappings:
        def __init__(self) -> None:
            self._maps: dict[str, dict[str, object]] = {
                "work_package": {"J1": {"openproject_id": 10}},
                "user": {"alice": {"openproject_id": 5}},
            }

        def get_mapping(self, name: str) -> dict[str, object]:
            return self._maps.get(name, {})

        def set_mapping(self, name: str, data: dict[str, object]) -> None:
            self._maps[name] = data

    dummy = DummyMappings()
    import src.config as cfg

    monkeypatch.setattr(cfg, "mappings", dummy, raising=False)
    return dummy


def test_watcher_migration_adds_watchers(monkeypatch: pytest.MonkeyPatch, _map_store):
    op = DummyOpClient()

    class DummyJira:
        def get_issue_watchers(self, key: str):
            return [{"name": "alice"}]

        def batch_get_issues(self, keys):
            return {"J1": object()}

    jira = DummyJira()
    wm = WatcherMigration(jira_client=jira, op_client=op)  # type: ignore[arg-type]

    res = wm.run()
    assert res.success
    assert op.added == [(10, 5)]
