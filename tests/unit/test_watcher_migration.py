import pytest

from src.application.components.watcher_migration import WatcherMigration


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


# --- Skip-reason breakdown ---------------------------------------------------
# Per the live NRS audit (2026-05-06): the watcher migration silently
# dropped 58% of watchers with no per-row logging. These tests pin a
# categorized ``skip_reasons`` breakdown surfaced in
# ``result.details`` so the next live run is diagnosable.


def test_skip_reasons_user_unmapped(
    monkeypatch: pytest.MonkeyPatch,
    _map_store,
    tmp_path,
):
    """Most common skip in practice — watcher is a Jira user not in
    the OP user mapping (locked / disabled / never logged in to OP).
    """
    op = DummyOpClient()

    class DummyJira:
        def get_issue_watchers(self, key: str):
            # alice is in the user map (id=5), bob is NOT.
            return [{"name": "alice"}, {"name": "bob"}]

    wm = WatcherMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    cache_dir = tmp_path / "data"
    cache_dir.mkdir()
    (cache_dir / "jira_issues_cache.json").write_text('{"J1": {"key": "J1"}}')
    wm.data_dir = cache_dir

    res = wm.run()
    breakdown = res.details["skip_reasons"]
    assert breakdown.get("user_unmapped") == 1, breakdown
    assert res.details["created"] == 1


def test_skip_reasons_wp_unmapped(
    monkeypatch: pytest.MonkeyPatch,
    _map_store,
    tmp_path,
):
    """Issue whose key isn't in the WP map → ``wp_unmapped``."""
    op = DummyOpClient()

    class DummyJira:
        def get_issue_watchers(self, key: str):
            return [{"name": "alice"}]

    wm = WatcherMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    cache_dir = tmp_path / "data"
    cache_dir.mkdir()
    # J99 is NOT in the WP mapping; J1 IS (mapped to id=10).
    (cache_dir / "jira_issues_cache.json").write_text(
        '{"J1": {"key": "J1"}, "J99": {"key": "J99"}}',
    )
    wm.data_dir = cache_dir

    res = wm.run()
    breakdown = res.details["skip_reasons"]
    assert breakdown.get("wp_unmapped") == 1, breakdown


def test_unmapped_users_set_is_distinct(
    monkeypatch: pytest.MonkeyPatch,
    _map_store,
    tmp_path,
):
    """Same Jira user watching N issues counts ONCE in the unmapped set.

    Per the live TEST audit (2026-05-06): 38 watcher rows missing,
    but likely only ~10 distinct users. Telling the operator
    "10 users to fix" is far more actionable than "38 watchers
    skipped". Pin the dedup so a future regression that increments
    a list (instead of a set) is caught.
    """
    op = DummyOpClient()

    class DummyJira:
        def get_issue_watchers(self, key: str):
            # Same unmapped user (bob) appears on every issue.
            return [{"name": "bob"}]

    wm = WatcherMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    cache_dir = tmp_path / "data"
    cache_dir.mkdir()
    # 3 issues all mapped to the same WP, all watched by ``bob``.
    _map_store.set_mapping(
        "work_package",
        {
            "J1": {"openproject_id": 10},
            "J2": {"openproject_id": 11},
            "J3": {"openproject_id": 12},
        },
    )
    (cache_dir / "jira_issues_cache.json").write_text(
        '{"J1": {"key": "J1"}, "J2": {"key": "J2"}, "J3": {"key": "J3"}}',
    )
    wm.data_dir = cache_dir

    res = wm.run()
    # 3 watcher rows dropped (one per issue) but only 1 distinct user.
    assert res.details["unmapped_user_count"] == 1, res.details
    assert res.details["unmapped_users"] == ["bob"], res.details
    # Aggregate skip count still matches all the dropped rows.
    assert res.details["skip_reasons"].get("user_unmapped") == 3, res.details


def test_unmapped_users_records_identity_via_probe_order(
    monkeypatch: pytest.MonkeyPatch,
    _map_store,
    tmp_path,
):
    """Recorded identity matches ``_resolve_user_id``'s probe order.

    Operators search the user mapping with the SAME identity the
    resolver tried. Logging ``account_id`` first (then name, email,
    display_name) means the operator can paste the logged value
    straight into the mapping key.
    """
    op = DummyOpClient()

    class DummyJira:
        def get_issue_watchers(self, key: str):
            # Watcher with both account_id AND name; account_id wins.
            return [{"accountId": "557058:abc", "name": "fallback-name"}]

    wm = WatcherMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    cache_dir = tmp_path / "data"
    cache_dir.mkdir()
    (cache_dir / "jira_issues_cache.json").write_text('{"J1": {"key": "J1"}}')
    wm.data_dir = cache_dir

    res = wm.run()
    # account_id wins over name in the probe order.
    assert res.details["unmapped_users"] == ["557058:abc"], res.details


def test_unmapped_users_empty_when_all_mapped(
    monkeypatch: pytest.MonkeyPatch,
    _map_store,
    tmp_path,
):
    """All watchers map cleanly → unmapped_users is empty list."""
    op = DummyOpClient()

    class DummyJira:
        def get_issue_watchers(self, key: str):
            return [{"name": "alice"}]

    wm = WatcherMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    cache_dir = tmp_path / "data"
    cache_dir.mkdir()
    (cache_dir / "jira_issues_cache.json").write_text('{"J1": {"key": "J1"}}')
    wm.data_dir = cache_dir

    res = wm.run()
    assert res.details["unmapped_users"] == [], res.details
    assert res.details["unmapped_user_count"] == 0


def test_skip_reasons_breakdown_sums_to_total_skipped(
    monkeypatch: pytest.MonkeyPatch,
    _map_store,
    tmp_path,
):
    """The breakdown's values must sum to the aggregate ``skipped``.

    Mirror of the relation-side ``test_run_skip_reasons_breakdown_sums_to_total_skipped``.
    Pins the load-bearing invariant: a future refactor that
    introduces a bare ``skipped += 1`` (bypassing the Counter) or
    stops adding ``bulk_dedup_or_invalid`` to the breakdown dict
    would silently desynchronize the breakdown from the aggregate.
    Multiple buckets fire simultaneously here so the sum-check is
    actually exercised.
    """
    op = DummyOpClient()

    class DummyJira:
        def get_issue_watchers(self, key: str):
            # Two unmapped users on J1, plus a parse-failure-shaped object
            return [{"name": "alice"}, {"name": "bob"}, {"name": "charlie"}, None]

    wm = WatcherMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    cache_dir = tmp_path / "data"
    cache_dir.mkdir()
    # J1 is mapped (will see the unmapped-user + parse-fail buckets fire);
    # J99 is NOT (will see the wp_unmapped bucket fire).
    (cache_dir / "jira_issues_cache.json").write_text(
        '{"J1": {"key": "J1"}, "J99": {"key": "J99"}}',
    )
    wm.data_dir = cache_dir

    res = wm.run()
    breakdown = res.details["skip_reasons"]

    # Multiple buckets must have fired (different reasons).
    assert len([k for k, v in breakdown.items() if v]) >= 2, breakdown

    # Sum of breakdown values must equal the aggregate ``skipped``.
    assert sum(breakdown.values()) == res.details["skipped"], (
        breakdown,
        res.details["skipped"],
    )


def test_skip_reasons_empty_when_all_succeed(
    monkeypatch: pytest.MonkeyPatch,
    _map_store,
    tmp_path,
):
    """All watchers map cleanly → ``skip_reasons`` is empty dict."""
    op = DummyOpClient()

    class DummyJira:
        def get_issue_watchers(self, key: str):
            return [{"name": "alice"}]

    wm = WatcherMigration(jira_client=DummyJira(), op_client=op)  # type: ignore[arg-type]
    cache_dir = tmp_path / "data"
    cache_dir.mkdir()
    (cache_dir / "jira_issues_cache.json").write_text('{"J1": {"key": "J1"}}')
    wm.data_dir = cache_dir

    res = wm.run()
    breakdown = res.details["skip_reasons"]
    assert breakdown == {}, breakdown
    assert res.details["created"] == 1
    assert res.details["skipped"] == 0
