import pytest

from src.migrations.watcher_migration import WatcherMigration


class DummyOpClient:
    def __init__(self) -> None:
        self.added: list[tuple[int, int]] = []
        self.bulk_watchers: list[dict] = []

    def add_watcher(self, wp_id: int, user_id: int) -> bool:
        self.added.append((wp_id, user_id))
        return True

    def bulk_add_watchers(self, watchers: list[dict]):
        self.bulk_watchers.extend(watchers)
        # Track what was added for assertions. Production calls this with
        # {"work_package_id": ..., "user_id": ...} dicts.
        for w in watchers:
            self.added.append((w["work_package_id"], w["user_id"]))
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


def test_watcher_migration_adds_watchers(
    monkeypatch: pytest.MonkeyPatch,
    _map_store,
    tmp_path,
):
    op = DummyOpClient()

    class DummyJira:
        def get_issue_watchers(self, key: str):
            return [{"name": "alice"}]

    jira = DummyJira()
    wm = WatcherMigration(jira_client=jira, op_client=op)  # type: ignore[arg-type]

    # Production iterates a Jira issues cache on disk; seed a minimal one so
    # the loop actually visits J1 instead of skipping on an empty dict.
    cache_dir = tmp_path / "data"
    cache_dir.mkdir()
    (cache_dir / "jira_issues_cache.json").write_text('{"J1": {"key": "J1"}}')
    wm.data_dir = cache_dir

    res = wm.run()
    assert res.success
    assert op.added == [(10, 5)]
