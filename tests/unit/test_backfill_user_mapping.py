"""Unit tests for ``tools.backfill_user_mapping``.

The tool spans Jira + OP, so the tests stub both clients through
small fakes. Pinned behaviours:

* Mapping load / atomic save (tmp + rename).
* Read names from CLI args, file, and migration_results JSON.
* Probe order: ``login`` first, ``email`` fallback.
* Idempotency: already-mapped names are skipped.
* Failure modes: not-found-in-jira, not-found-in-op, missing op id.
* Multi-key insertion: every probe identifier is reachable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _patch_clients(monkeypatch: pytest.MonkeyPatch):
    """Default fakes — individual tests override the methods they need.

    The Jira/OP clients are imported *inside* ``backfill()`` (lazy) and
    inside ``_find_op_user`` so monkeypatching at module level catches
    them.
    """

    class _DefaultJira:
        def get_user_info(self, name: str) -> dict[str, Any] | None:
            return None

    class _DefaultOp:
        def get_user(self, identifier: int | str) -> dict[str, Any]:
            msg = "not found"
            raise LookupError(msg)

        def get_user_by_email(self, email: str) -> dict[str, Any]:
            msg = "not found"
            raise LookupError(msg)

    monkeypatch.setattr("src.infrastructure.jira.jira_client.JiraClient", _DefaultJira)

    from tools import backfill_user_mapping as mod

    monkeypatch.setattr(mod, "OpenProjectClient", _DefaultOp)
    return mod


def _write_mapping(path: Path, data: dict[str, Any]) -> None:
    with path.open("w") as f:
        json.dump(data, f)


def test_load_user_mapping_missing_file_is_empty(tmp_path: Path) -> None:
    """Missing file returns ``{}`` rather than raising."""
    from tools.backfill_user_mapping import _load_user_mapping

    out = _load_user_mapping(tmp_path / "nope.json")
    assert out == {}


def test_load_user_mapping_non_dict_raises(tmp_path: Path) -> None:
    from tools.backfill_user_mapping import _load_user_mapping

    f = tmp_path / "wpm.json"
    f.write_text("[]")
    with pytest.raises(TypeError, match="not a dict"):
        _load_user_mapping(f)


def test_save_user_mapping_atomic(tmp_path: Path) -> None:
    """``_save_user_mapping`` writes through a tmp + rename so a crash
    mid-dump doesn't leave a half-written mapping (the very class
    PR #197 caught for ``work_package_mapping``).
    """
    from tools.backfill_user_mapping import _save_user_mapping

    target = tmp_path / "user_mapping.json"
    _save_user_mapping(target, {"alice": {"openproject_id": 11}})
    assert target.exists()
    assert json.loads(target.read_text()) == {"alice": {"openproject_id": 11}}
    # No leftover .tmp.
    assert list(tmp_path.glob("*.tmp")) == []


def test_read_names_from_file_strips_blanks_and_comments(tmp_path: Path) -> None:
    from tools.backfill_user_mapping import _read_names_from_file

    f = tmp_path / "names.txt"
    f.write_text("# header\nalice\n\n  bob  \n# trailer\ncarol\n")
    assert _read_names_from_file(f) == ["alice", "bob", "carol"]


def test_read_names_from_migration_results_extracts_unmapped_users(tmp_path: Path) -> None:
    from tools.backfill_user_mapping import _read_names_from_migration_results

    f = tmp_path / "results.json"
    f.write_text(
        json.dumps(
            {
                "components": {
                    "watchers": {"details": {"unmapped_users": ["alice", "bob", "alice"]}},
                    "users": {"details": {}},  # no unmapped_users
                    "time_entries": {"details": {"unmapped_users": ["carol"]}},
                },
            },
        ),
    )
    out = _read_names_from_migration_results(f)
    # Sorted + deduped.
    assert out == ["alice", "bob", "carol"]


def test_backfill_already_mapped_is_skipped(tmp_path: Path, _patch_clients) -> None:
    """If ``alice`` already exists in the mapping, the backfill must skip her."""
    from tools.backfill_user_mapping import backfill

    f = tmp_path / "user_mapping.json"
    _write_mapping(f, {"alice": {"openproject_id": 11}})

    report = backfill(["alice"], f)
    assert report["summary"]["added"] == 0
    assert report["summary"]["already_mapped"] == 1


def test_backfill_match_via_login_and_writes_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OP user found by ``login=jira.name`` → mapping updated, file written."""
    from tools import backfill_user_mapping as mod

    class _Jira:
        def get_user_info(self, name: str):
            return {"name": name, "emailAddress": f"{name}@example.com", "displayName": name.title(), "key": name}

    class _Op:
        def get_user(self, identifier: int | str):
            return {"id": 42, "login": identifier, "mail": f"{identifier}@example.com"}

        def get_user_by_email(self, email: str):
            msg = "not used"
            raise LookupError(msg)

    monkeypatch.setattr("src.infrastructure.jira.jira_client.JiraClient", _Jira)
    monkeypatch.setattr(mod, "OpenProjectClient", _Op)

    f = tmp_path / "user_mapping.json"
    _write_mapping(f, {})

    report = mod.backfill(["alice"], f)
    assert report["summary"]["added"] == 1

    mapping = json.loads(f.read_text())
    # Inserted under every probe identifier so future migrations can
    # resolve via name OR email.
    assert mapping["alice"]["openproject_id"] == 42
    assert mapping["alice@example.com"]["openproject_id"] == 42
    assert mapping["alice"]["matched_by"] == "backfill_unmapped_users"


def test_backfill_match_via_email_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OP login miss + email match → mapping still updated."""
    from tools import backfill_user_mapping as mod

    class _Jira:
        def get_user_info(self, name: str):
            return {"name": name, "emailAddress": f"{name}@example.com"}

    class _Op:
        def get_user(self, identifier: int | str):
            msg = "no login match"
            raise LookupError(msg)

        def get_user_by_email(self, email: str):
            return {"id": 99, "mail": email}

    monkeypatch.setattr("src.infrastructure.jira.jira_client.JiraClient", _Jira)
    monkeypatch.setattr(mod, "OpenProjectClient", _Op)

    f = tmp_path / "user_mapping.json"
    _write_mapping(f, {})

    report = mod.backfill(["bob"], f)
    assert report["summary"]["added"] == 1
    mapping = json.loads(f.read_text())
    assert mapping["bob"]["openproject_id"] == 99


def test_backfill_no_jira_user_records_not_found_in_jira(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Jira returns ``None`` → user goes to ``not_found_in_jira``,
    file is NOT written.
    """
    from tools import backfill_user_mapping as mod

    f = tmp_path / "user_mapping.json"
    _write_mapping(f, {})
    initial_mtime = f.stat().st_mtime

    report = mod.backfill(["ghost"], f)
    assert report["summary"]["not_found_in_jira"] == 1
    assert report["not_found_in_jira"] == ["ghost"]
    # File untouched (no add → no save call → mtime unchanged).
    assert f.stat().st_mtime == initial_mtime


def test_backfill_no_op_user_records_not_found_in_op(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Jira returns user but OP doesn't → ``not_found_in_op`` with
    ``jira_email`` / ``jira_display`` so an operator can act.
    """
    from tools import backfill_user_mapping as mod

    class _Jira:
        def get_user_info(self, name: str):
            return {"name": name, "emailAddress": f"{name}@example.com", "displayName": "Carol C.", "active": False}

    monkeypatch.setattr("src.infrastructure.jira.jira_client.JiraClient", _Jira)
    # Default _Op stub from autouse fixture raises LookupError for both.

    f = tmp_path / "user_mapping.json"
    _write_mapping(f, {})

    report = mod.backfill(["carol"], f)
    assert report["summary"]["not_found_in_op"] == 1
    item = report["not_found_in_op"][0]
    assert item["jira_name"] == "carol"
    assert item["jira_email"] == "carol@example.com"
    assert item["jira_display"] == "Carol C."
    assert item["active"] is False


def test_backfill_dry_run_does_not_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``dry_run=True`` returns the additions but doesn't touch the file."""
    from tools import backfill_user_mapping as mod

    class _Jira:
        def get_user_info(self, name: str):
            return {"name": name, "emailAddress": f"{name}@example.com"}

    class _Op:
        def get_user(self, identifier: int | str):
            return {"id": 42, "login": identifier}

    monkeypatch.setattr("src.infrastructure.jira.jira_client.JiraClient", _Jira)
    monkeypatch.setattr(mod, "OpenProjectClient", _Op)

    f = tmp_path / "user_mapping.json"
    _write_mapping(f, {})
    initial_text = f.read_text()

    report = mod.backfill(["dave"], f, dry_run=True)
    assert report["summary"]["added"] == 1
    # File untouched.
    assert f.read_text() == initial_text


def test_backfill_existing_mapping_for_email_is_not_clobbered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin: backfill must not clobber an entry an operator manually
    set on an alternate identifier (e.g. email).

    Insert ``alice@example.com`` manually with ``openproject_id=999``;
    backfill ``alice`` (the login). The new entry only writes the
    keys that aren't already present, so the manual fix is preserved.
    """
    from tools import backfill_user_mapping as mod

    class _Jira:
        def get_user_info(self, name: str):
            return {"name": name, "emailAddress": f"{name}@example.com"}

    class _Op:
        def get_user(self, identifier: int | str):
            return {"id": 42, "login": identifier}

    monkeypatch.setattr("src.infrastructure.jira.jira_client.JiraClient", _Jira)
    monkeypatch.setattr(mod, "OpenProjectClient", _Op)

    f = tmp_path / "user_mapping.json"
    _write_mapping(f, {"alice@example.com": {"openproject_id": 999, "matched_by": "manual"}})

    mod.backfill(["alice"], f)
    mapping = json.loads(f.read_text())
    # Manual entry preserved.
    assert mapping["alice@example.com"]["openproject_id"] == 999
    # New entry added under the login.
    assert mapping["alice"]["openproject_id"] == 42
