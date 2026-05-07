"""Tests for ``UserMappingBackfillMigration``.

Pinned behaviours:

* Names sourced from previous ``migration_results_*.json`` files
  (multiple files merged, ``unmapped_users`` from any component).
* Names sourced from ``jira_issues_cache.json`` and per-project
  ``jira_issues_*.json`` dumps (assignee, reporter, watchers,
  comment authors).
* Probe order: ``login`` first, ``email`` fallback.
* Idempotency: already-mapped names skipped on the cheap path AND
  on the post-Jira-lookup second-chance dedup.
* Failure modes: not-found-in-jira, not-found-in-op, missing OP id.
* Persistence: ``set_mapping`` only called when something actually changed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def _make_migration(
    tmp_path: Path,
    jira_client: Any,
    op_client: Any,
    user_map: dict[str, Any] | None = None,
) -> Any:
    """Bypass ``__init__`` to avoid the BaseMigration boot path.

    Mirrors the helper used in ``test_wp_metadata_backfill_migration``.
    Provides a fake ``mappings`` so the test can both read and write.
    """
    from src.application.components.user_mapping_backfill_migration import (
        UserMappingBackfillMigration,
    )

    instance = UserMappingBackfillMigration.__new__(UserMappingBackfillMigration)
    instance.jira_client = jira_client
    instance.op_client = op_client
    instance.logger = SimpleNamespace(
        info=lambda *a, **kw: None,
        warning=lambda *a, **kw: None,
        debug=lambda *a, **kw: None,
        error=lambda *a, **kw: None,
        exception=lambda *a, **kw: None,
        success=lambda *a, **kw: None,
        notice=lambda *a, **kw: None,
    )
    instance.data_dir = tmp_path / "data"
    instance.data_dir.mkdir(parents=True, exist_ok=True)

    class FakeMappings:
        def __init__(self, initial: dict[str, Any]) -> None:
            self._m = {"user": dict(initial)}
            self.set_calls: list[tuple[str, dict[str, Any]]] = []

        def get_mapping(self, name: str) -> dict[str, Any]:
            return self._m.get(name, {})

        def set_mapping(self, name: str, data: dict[str, Any]) -> None:
            self._m[name] = dict(data)
            self.set_calls.append((name, dict(data)))

    instance.mappings = FakeMappings(user_map or {})
    return instance


def _write_results(
    tmp_path: Path,
    *,
    timestamp: str = "2026-05-07_07-20-27",
    components: dict[str, Any] | None = None,
) -> Path:
    """Create a ``migration_results_<timestamp>.json`` under ``tmp_path/results``."""
    results_dir = tmp_path / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"migration_results_{timestamp}.json"
    payload = {"components": components or {}}
    path.write_text(json.dumps(payload))
    return path


# --- name-source helpers ----------------------------------------------------


def test_names_from_previous_results_unions_all_components(tmp_path: Path) -> None:
    """Every component's ``details.unmapped_users`` is unioned."""
    from src.application.components.user_mapping_backfill_migration import (
        UserMappingBackfillMigration,
    )

    _write_results(
        tmp_path,
        components={
            "watchers": {"details": {"unmapped_users": ["alice", "bob", "alice"]}},
            "users": {"details": {}},
            "time_entries": {"details": {"unmapped_users": ["carol"]}},
        },
    )
    out = UserMappingBackfillMigration._names_from_previous_results(tmp_path / "results")
    assert out == {"alice", "bob", "carol"}


def test_names_from_previous_results_missing_dir_is_empty(tmp_path: Path) -> None:
    """Fresh checkout / CI: results dir doesn't exist yet → empty set."""
    from src.application.components.user_mapping_backfill_migration import (
        UserMappingBackfillMigration,
    )

    out = UserMappingBackfillMigration._names_from_previous_results(tmp_path / "missing")
    assert out == set()


def test_names_from_previous_results_corrupt_file_is_skipped(tmp_path: Path) -> None:
    """A half-written / unparseable result file must NOT crash the run.

    Pin: corrupted JSON in one results file is silently skipped
    while other files in the directory still contribute.
    """
    from src.application.components.user_mapping_backfill_migration import (
        UserMappingBackfillMigration,
    )

    results_dir = tmp_path / "results"
    results_dir.mkdir(parents=True)
    (results_dir / "migration_results_corrupt.json").write_text('{"components": {"watchers')
    _write_results(
        tmp_path,
        timestamp="2026-05-07_07-20-27",
        components={"watchers": {"details": {"unmapped_users": ["alice"]}}},
    )
    out = UserMappingBackfillMigration._names_from_previous_results(results_dir)
    assert out == {"alice"}


def test_names_from_issue_cache_unified_dict_shape(tmp_path: Path) -> None:
    """Unified ``jira_issues_cache.json`` (dict shape) yields users from
    assignee, reporter, watchers, comment authors.
    """
    from src.application.components.user_mapping_backfill_migration import (
        UserMappingBackfillMigration,
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "jira_issues_cache.json").write_text(
        json.dumps(
            {
                "NRS-1": {
                    "fields": {
                        "assignee": {"name": "alice"},
                        "reporter": {"name": "bob"},
                        "watches": {"watchers": [{"name": "carol"}, {"name": "dave"}]},
                        "comment": {"comments": [{"author": {"name": "eve"}}]},
                    },
                },
            },
        ),
    )
    out = UserMappingBackfillMigration._names_from_issue_cache(data_dir)
    assert out == {"alice", "bob", "carol", "dave", "eve"}


def test_names_from_issue_cache_per_project_dumps(tmp_path: Path) -> None:
    """Per-project ``jira_issues_NRS.json`` (list shape) is also consumed."""
    from src.application.components.user_mapping_backfill_migration import (
        UserMappingBackfillMigration,
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "jira_issues_NRS.json").write_text(
        json.dumps(
            [
                {
                    "key": "NRS-1",
                    "fields": {"assignee": {"name": "alice"}, "reporter": None},
                },
                {
                    "key": "NRS-2",
                    "fields": {"assignee": None, "reporter": {"name": "bob"}},
                },
            ],
        ),
    )
    out = UserMappingBackfillMigration._names_from_issue_cache(data_dir)
    assert out == {"alice", "bob"}


def test_names_from_issue_cache_no_files_is_empty(tmp_path: Path) -> None:
    """No cache files present → empty set, no crash."""
    from src.application.components.user_mapping_backfill_migration import (
        UserMappingBackfillMigration,
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    out = UserMappingBackfillMigration._names_from_issue_cache(data_dir)
    assert out == set()


# --- run() integration ------------------------------------------------------


class _Jira:
    """Configurable Jira fake.

    ``responses`` maps username → returned profile dict (or ``None``
    to simulate "user doesn't exist in Jira"). Calls outside the map
    return ``None``.
    """

    def __init__(self, responses: dict[str, dict[str, Any] | None] | None = None) -> None:
        self._r = responses or {}

    def get_user_info(self, name: str) -> dict[str, Any] | None:
        return self._r.get(name)


class _Op:
    """Configurable OP fake.

    ``by_login`` and ``by_email`` map identifier → returned user dict.
    Anything else raises ``LookupError`` to mirror the real client's
    "not found" behaviour.
    """

    def __init__(
        self,
        by_login: dict[str, dict[str, Any]] | None = None,
        by_email: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._by_login = by_login or {}
        self._by_email = by_email or {}

    def get_user(self, identifier: int | str) -> dict[str, Any]:
        if identifier in self._by_login:
            return self._by_login[identifier]
        # Mirror the real client's not-found exception so the
        # narrowed ``except RecordNotFoundError`` in production
        # is exercised by the tests.
        from src.infrastructure.exceptions import RecordNotFoundError

        msg = f"no user with login {identifier!r}"
        raise RecordNotFoundError(msg)

    def get_user_by_email(self, email: str) -> dict[str, Any]:
        if email in self._by_email:
            return self._by_email[email]
        from src.infrastructure.exceptions import RecordNotFoundError

        msg = f"no user with email {email!r}"
        raise RecordNotFoundError(msg)


def test_run_no_candidates_returns_success(tmp_path: Path) -> None:
    """No previous results, no cache → noop run, success=True, updated=0."""
    mig = _make_migration(tmp_path, _Jira(), _Op())
    res = mig.run()
    assert res.success
    assert res.updated == 0
    assert "No backfill candidates" in (res.message or "")
    # No persistence — nothing changed.
    assert mig.mappings.set_calls == []


def test_run_resolves_via_previous_results_writes_mapping(tmp_path: Path) -> None:
    """Watcher's ``unmapped_users`` from a previous run is the seed.

    OP user found by login → ``user_map`` updated, ``set_mapping``
    called once at the end of the run.
    """
    _write_results(
        tmp_path,
        components={"watchers": {"details": {"unmapped_users": ["alice"]}}},
    )
    jira = _Jira({"alice": {"name": "alice", "emailAddress": "alice@x.com"}})
    op = _Op(by_login={"alice": {"id": 42, "login": "alice"}})

    mig = _make_migration(tmp_path, jira, op)
    res = mig.run()

    assert res.success
    assert res.updated == 1
    user_map = mig.mappings.get_mapping("user")
    assert user_map["alice"]["openproject_id"] == 42
    assert user_map["alice@x.com"]["openproject_id"] == 42
    # Persisted exactly once.
    assert len(mig.mappings.set_calls) == 1


def test_run_resolves_via_email_fallback(tmp_path: Path) -> None:
    """OP login miss + email match → mapping still updated."""
    _write_results(tmp_path, components={"watchers": {"details": {"unmapped_users": ["bob"]}}})
    jira = _Jira({"bob": {"name": "bob", "emailAddress": "bob@x.com"}})
    op = _Op(by_email={"bob@x.com": {"id": 99, "mail": "bob@x.com"}})

    mig = _make_migration(tmp_path, jira, op)
    res = mig.run()

    assert res.updated == 1
    assert mig.mappings.get_mapping("user")["bob"]["openproject_id"] == 99


def test_run_skips_already_mapped_cheap_path(tmp_path: Path) -> None:
    """Name already a key in user_mapping → never queries Jira."""
    _write_results(tmp_path, components={"watchers": {"details": {"unmapped_users": ["alice"]}}})

    class _BoomJira:
        def get_user_info(self, name: str) -> dict[str, Any] | None:
            msg = "should not be called"
            raise AssertionError(msg)

    mig = _make_migration(
        tmp_path,
        _BoomJira(),
        _Op(),
        user_map={"alice": {"openproject_id": 1}},
    )
    res = mig.run()
    assert res.updated == 0
    assert res.details["already_mapped"] == 1


def test_run_skips_already_mapped_via_alternate_identifier(tmp_path: Path) -> None:
    """Name not directly mapped, but Jira reports an alternate
    identifier (email) that IS mapped → second-chance dedup catches it.

    Pin: prevents adding a duplicate entry under a different key
    when the OP user was already reachable via email or accountId.
    """
    _write_results(tmp_path, components={"watchers": {"details": {"unmapped_users": ["alice"]}}})
    jira = _Jira({"alice": {"name": "alice", "emailAddress": "alice@x.com"}})
    # Jira lookup will be queried, but mapping already has alice@x.com.
    op = _Op(by_login={"alice": {"id": 42}})

    mig = _make_migration(
        tmp_path,
        jira,
        op,
        user_map={"alice@x.com": {"openproject_id": 999, "matched_by": "manual"}},
    )
    res = mig.run()

    assert res.updated == 0
    assert res.details["already_mapped"] == 1
    # Manual entry preserved untouched.
    assert mig.mappings.get_mapping("user")["alice@x.com"]["openproject_id"] == 999
    # No write happened (nothing actually changed).
    assert mig.mappings.set_calls == []


def test_run_no_jira_user_records_not_found_in_jira(tmp_path: Path) -> None:
    """Jira returns ``None`` → user goes to ``not_found_in_jira``."""
    _write_results(tmp_path, components={"watchers": {"details": {"unmapped_users": ["ghost"]}}})

    mig = _make_migration(tmp_path, _Jira(), _Op())
    res = mig.run()
    assert res.details["not_found_in_jira"] == 1
    # Mapping unchanged.
    assert mig.mappings.set_calls == []


def test_run_no_op_user_records_not_found_in_op(tmp_path: Path) -> None:
    """Jira returns user but OP doesn't → ``not_found_in_op``."""
    _write_results(tmp_path, components={"watchers": {"details": {"unmapped_users": ["carol"]}}})
    jira = _Jira(
        {
            "carol": {
                "name": "carol",
                "emailAddress": "carol@x.com",
                "displayName": "Carol C.",
                "active": False,
            },
        },
    )
    op = _Op()  # no matches

    mig = _make_migration(tmp_path, jira, op)
    res = mig.run()
    assert res.details["not_found_in_op_count"] == 1
    item = res.details["not_found_in_op_sample"][0]
    assert item["jira_name"] == "carol"
    assert item["jira_email"] == "carol@x.com"
    assert item["jira_display"] == "Carol C."
    assert item["active"] is False
    # No persistence — nothing matched.
    assert mig.mappings.set_calls == []


def test_run_combines_results_and_cache_sources(tmp_path: Path) -> None:
    """Both sources contribute; the union is processed.

    Pin: the run uses BOTH the previous-results breadcrumb AND the
    issue cache, deduplicated. A name showing up in both counts
    once.
    """
    _write_results(tmp_path, components={"watchers": {"details": {"unmapped_users": ["alice"]}}})
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "jira_issues_cache.json").write_text(
        json.dumps(
            {
                "NRS-1": {
                    "fields": {
                        "assignee": {"name": "alice"},  # also in previous results
                        "reporter": {"name": "bob"},  # only in cache
                    },
                },
            },
        ),
    )
    jira = _Jira(
        {
            "alice": {"name": "alice", "emailAddress": "alice@x.com"},
            "bob": {"name": "bob", "emailAddress": "bob@x.com"},
        },
    )
    op = _Op(by_login={"alice": {"id": 1}, "bob": {"id": 2}})

    mig = _make_migration(tmp_path, jira, op)
    res = mig.run()

    assert res.updated == 2
    assert res.details["from_previous_results"] == 1
    assert res.details["from_issue_cache"] == 2  # alice + bob


def test_run_does_not_clobber_manual_entries(tmp_path: Path) -> None:
    """Operator's manual entry under one identifier is preserved when
    a backfill discovers the same OP user via another identifier.
    """
    _write_results(tmp_path, components={"watchers": {"details": {"unmapped_users": ["alice"]}}})
    jira = _Jira({"alice": {"name": "alice", "emailAddress": "alice@x.com"}})
    # Mapping has alice@x.com → 999 (operator-set).
    op = _Op(by_email={"alice@x.com": {"id": 42, "mail": "alice@x.com"}})

    mig = _make_migration(
        tmp_path,
        jira,
        op,
        user_map={"alice@x.com": {"openproject_id": 999, "matched_by": "manual"}},
    )
    mig.run()
    user_map = mig.mappings.get_mapping("user")
    # Manual entry preserved.
    assert user_map["alice@x.com"]["openproject_id"] == 999
    # Already-mapped via email → second-chance dedup; no new entry under "alice".
    assert "alice" not in user_map
