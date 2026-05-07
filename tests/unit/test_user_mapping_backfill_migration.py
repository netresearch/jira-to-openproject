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


def test_run_alias_writes_missing_identifier_to_existing_mapping(tmp_path: Path) -> None:
    """Alias path: an existing entry under one identifier (e.g.
    ``JIRAUSER18400``) doesn't suppress the alias write under a
    different identifier (e.g. ``anne.geissler``) the consumer
    resolver probes first.

    Caught by the live 2026-05-07 NRS run: 18 watcher
    ``unmapped_users`` were already mapped under their JIRAUSER ids
    but the watcher resolver probes ``name`` first, so the watchers
    kept getting dropped. Pin: the alias entry is written under
    every cand_key NOT yet present, reusing the existing OP id.
    No extra OP probe (fast).
    """

    class _BoomOp:
        """OP fake that explodes if any method is called — proves the
        alias path doesn't probe OP.
        """

        def get_user(self, identifier: int | str) -> dict[str, Any]:
            msg = "OP must not be probed on alias path"
            raise AssertionError(msg)

        def get_user_by_email(self, email: str) -> dict[str, Any]:
            msg = "OP must not be probed on alias path"
            raise AssertionError(msg)

    _write_results(tmp_path, components={"watchers": {"details": {"unmapped_users": ["anne.geissler"]}}})
    jira = _Jira(
        {
            "anne.geissler": {
                "name": "anne.geissler",
                "key": "JIRAUSER18400",
                "emailAddress": "anne.geissler@x.com",
            },
        },
    )
    mig = _make_migration(
        tmp_path,
        jira,
        _BoomOp(),
        user_map={"JIRAUSER18400": {"openproject_id": 42, "matched_by": "users"}},
    )
    res = mig.run()

    user_map = mig.mappings.get_mapping("user")
    # Original entry preserved.
    assert user_map["JIRAUSER18400"]["openproject_id"] == 42
    assert user_map["JIRAUSER18400"]["matched_by"] == "users"
    # Alias under the watcher's primary probe identifier.
    assert user_map["anne.geissler"]["openproject_id"] == 42
    assert user_map["anne.geissler"]["matched_by"] == "user_mapping_backfill_alias"
    # And under email so the email-probe consumer (TE) also resolves.
    assert user_map["anne.geissler@x.com"]["openproject_id"] == 42
    # Counted as added (it's an alias addition, not a no-op).
    assert res.details["added"] == 1
    assert res.details["already_mapped"] == 0


def test_run_cheap_path_skips_when_seed_name_already_mapped(tmp_path: Path) -> None:
    """Seed identifier ``alice`` IS a key in ``user_map`` → cheap-path
    skip fires before the Jira lookup or alias logic runs.

    Reworded per PR #207 review: the previous docstring claimed
    "already-mapped under EVERY candidate identifier", but the
    fixture only ensures the seed (``"alice"``) is present. The
    cheap-path skip is a ``name in user_map`` check that doesn't
    even consult the Jira user's other identifiers — that's what
    we're actually pinning here.
    """
    _write_results(tmp_path, components={"watchers": {"details": {"unmapped_users": ["alice"]}}})

    class _BoomJira:
        def get_user_info(self, name: str):
            msg = "Jira must not be queried when cheap-path skip fires"
            raise AssertionError(msg)

    mig = _make_migration(
        tmp_path,
        _BoomJira(),
        _Op(),  # _Op never queried either
        user_map={"alice": {"openproject_id": 999, "matched_by": "manual"}},
    )
    res = mig.run()
    assert res.details["already_mapped"] == 1
    assert res.details["added"] == 0
    assert mig.mappings.set_calls == []


def test_run_alias_conflict_records_not_found_in_op(tmp_path: Path) -> None:
    """Two cand_keys map to DIFFERENT OP users → conflict; refuse alias.

    Pin (PR #207 review): the alias path collects every distinct
    ``openproject_id`` across cand_keys before deciding. When two
    or more distinct ids exist, the resolver returns a conflict
    flag and the caller records ``not_found_in_op`` with reason
    ``alias_op_id_conflict`` instead of silently picking one.
    """

    class _BoomOp:
        def get_user(self, identifier: int | str):
            msg = "OP must not be probed on alias path"
            raise AssertionError(msg)

        def get_user_by_email(self, email: str):
            msg = "OP must not be probed on alias path"
            raise AssertionError(msg)

    _write_results(tmp_path, components={"watchers": {"details": {"unmapped_users": ["dave"]}}})
    jira = _Jira({"dave": {"name": "dave", "key": "JIRAUSER999", "emailAddress": "dave@x.com"}})
    mig = _make_migration(
        tmp_path,
        jira,
        _BoomOp(),
        user_map={
            "JIRAUSER999": {"openproject_id": 42, "matched_by": "users"},
            "dave@x.com": {"openproject_id": 999, "matched_by": "auto"},
        },
    )
    res = mig.run()
    # Conflict surfaced — no alias written.
    assert res.details["not_found_in_op_count"] == 1
    item = res.details["not_found_in_op_sample"][0]
    assert item["reason"] == "alias_op_id_conflict"
    # Original entries untouched.
    assert mig.mappings.set_calls == []


def test_run_alias_conflict_resolved_when_manual_entry_present(tmp_path: Path) -> None:
    """Manual entry wins over auto entry on conflict.

    Pin: when one of the conflicting cand_keys carries
    ``matched_by="manual"``, the resolver picks ITS
    ``openproject_id`` and writes alias entries pointing there —
    honouring the "manual target wins" contract.
    """

    class _BoomOp:
        def get_user(self, identifier: int | str):
            msg = "OP must not be probed"
            raise AssertionError(msg)

        def get_user_by_email(self, email: str):
            msg = "OP must not be probed"
            raise AssertionError(msg)

    _write_results(tmp_path, components={"watchers": {"details": {"unmapped_users": ["eve"]}}})
    jira = _Jira({"eve": {"name": "eve", "key": "JIRAUSER123", "emailAddress": "eve@x.com"}})
    mig = _make_migration(
        tmp_path,
        jira,
        _BoomOp(),
        user_map={
            "JIRAUSER123": {"openproject_id": 42, "matched_by": "users"},  # auto
            "eve@x.com": {"openproject_id": 999, "matched_by": "manual"},  # operator-set
        },
    )
    res = mig.run()
    user_map = mig.mappings.get_mapping("user")
    # Alias entries point at the manual id (999), NOT the auto id (42).
    assert user_map["eve"]["openproject_id"] == 999
    assert user_map["eve"]["matched_by"] == "user_mapping_backfill_alias"
    assert res.details["added"] == 1


def test_run_alias_handles_malformed_historical_op_id(tmp_path: Path) -> None:
    """Malformed historical entry (e.g. ``openproject_id="not-a-number"``)
    is skipped silently — doesn't crash the run.

    Pin (PR #207 review): defensive int parse with
    ``try/except (TypeError, ValueError)`` so a single corrupted
    historical record doesn't take down the whole component.
    """

    class _BoomOp:
        def get_user(self, identifier: int | str):
            msg = "OP must not be probed when alternate identifier is parseable"
            raise AssertionError(msg)

        def get_user_by_email(self, email: str):
            msg = "OP must not be probed"
            raise AssertionError(msg)

    _write_results(tmp_path, components={"watchers": {"details": {"unmapped_users": ["frank"]}}})
    jira = _Jira({"frank": {"name": "frank", "key": "JIRAUSER555", "emailAddress": "frank@x.com"}})
    mig = _make_migration(
        tmp_path,
        jira,
        _BoomOp(),
        user_map={
            "JIRAUSER555": {"openproject_id": "garbage", "matched_by": "users"},  # bad id
            "frank@x.com": {"openproject_id": 77, "matched_by": "users"},  # good id
        },
    )
    res = mig.run()
    user_map = mig.mappings.get_mapping("user")
    # Bad entry skipped, good one used as the alias target.
    assert user_map["frank"]["openproject_id"] == 77
    assert res.details["added"] == 1


def test_run_seed_name_aliased_when_jira_profile_omits_name(tmp_path: Path) -> None:
    """Seed name is always written as an alias even when Jira's profile
    doesn't return it as a string field.

    Caught by the live 2026-05-07 NRS run: 18 watcher
    ``unmapped_users`` were silently marked ``already_mapped``
    because their Jira profiles for locked Server users had
    ``name=None`` on the SDK object. Only ``key`` (e.g.
    ``JIRAUSER18400``) made it into cand_keys; that key was
    already in ``user_map`` so the alias path concluded "no
    missing keys to write". Watchers kept dropping these users.

    Pin: ``cand_keys`` always includes the seed name (the one
    from ``unmapped_users`` that triggered this lookup), so the
    alias gets written under the identifier the consumer
    actually probes.
    """

    class _BoomOp:
        def get_user(self, identifier: int | str):
            msg = "OP must not be probed on alias path"
            raise AssertionError(msg)

        def get_user_by_email(self, email: str):
            msg = "OP must not be probed"
            raise AssertionError(msg)

    _write_results(tmp_path, components={"watchers": {"details": {"unmapped_users": ["anne.geissler"]}}})
    # Jira returns a profile with ``name=None`` (locked / minimal
    # Server profile shape). Only ``key`` is a usable string.
    jira = _Jira({"anne.geissler": {"name": None, "key": "JIRAUSER18400", "emailAddress": ""}})
    mig = _make_migration(
        tmp_path,
        jira,
        _BoomOp(),
        user_map={"JIRAUSER18400": {"openproject_id": 42, "matched_by": "users"}},
    )
    res = mig.run()

    user_map = mig.mappings.get_mapping("user")
    # Alias under the seed name — what the watcher will probe for.
    assert user_map["anne.geissler"]["openproject_id"] == 42
    assert user_map["anne.geissler"]["matched_by"] == "user_mapping_backfill_alias"
    # Existing JIRAUSER entry preserved.
    assert user_map["JIRAUSER18400"]["matched_by"] == "users"
    assert res.details["added"] == 1
    assert res.details["already_mapped"] == 0


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

    Updated expectation: the alias path (added 2026-05-07) DOES write
    a new entry under the missing identifier, but it reuses the
    existing entry's ``openproject_id`` (so the operator's mapping
    target isn't redirected). The manual entry's metadata is
    preserved untouched.
    """
    _write_results(tmp_path, components={"watchers": {"details": {"unmapped_users": ["alice"]}}})
    jira = _Jira({"alice": {"name": "alice", "emailAddress": "alice@x.com"}})
    op = _Op(by_email={"alice@x.com": {"id": 42, "mail": "alice@x.com"}})

    mig = _make_migration(
        tmp_path,
        jira,
        op,
        user_map={"alice@x.com": {"openproject_id": 999, "matched_by": "manual"}},
    )
    mig.run()
    user_map = mig.mappings.get_mapping("user")
    # Manual entry preserved untouched.
    assert user_map["alice@x.com"]["openproject_id"] == 999
    assert user_map["alice@x.com"]["matched_by"] == "manual"
    # Alias under the missing identifier reuses the existing OP id.
    # Operator's manual mapping target wins — backfill never overrides
    # an existing entry's ``openproject_id``.
    assert user_map["alice"]["openproject_id"] == 999
    assert user_map["alice"]["matched_by"] == "user_mapping_backfill_alias"
