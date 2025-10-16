from unittest.mock import MagicMock

import pytest

from src.migrations.watcher_migration import WatcherMigration


class DummyOpClient:
    def __init__(self) -> None:
        self.added: list[tuple[int, int]] = []

    def add_watcher(self, wp_id: int, user_id: int) -> bool:
        self.added.append((wp_id, user_id))
        return True


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
    wm = WatcherMigration(jira_client=MagicMock(), op_client=op)

    # Simulate EJ batch_get_issues and JiraClient.get_issue_watchers
    from src.clients.enhanced_jira_client import EnhancedJiraClient

    monkeypatch.setattr(EnhancedJiraClient, "batch_get_issues", lambda self, keys: {"J1": object()})

    class DummyJira:
        def get_issue_watchers(self, key: str):
            return [{"name": "alice"}]

    wm.jira_client = DummyJira()  # type: ignore[assignment]

    res = wm.run()
    assert res.success
    assert op.added == [(10, 5)]


