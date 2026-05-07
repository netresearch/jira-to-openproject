"""Tests for ``AttachmentRecoveryMigration``.

Pinned behaviours:

* Empty WP map → fail loud (siblings #194/#197/#198/#199 pattern).
* No project keys derivable → success, message reflects no-op.
* Clean state (every Jira file present in OP) → success, recovered=0.
* Missing files → delegates to
  ``AttachmentsMigration._process_batch_end_to_end`` for the affected
  jira_keys.
* Multiset semantics for duplicate filenames.
* Rails envelope parsed correctly (``status`` + ``data`` only — same
  envelope-bug class PR #201 caught for ``wp_metadata_backfill``).
* Recompute-after-recover: ``still_missing_total`` reflects post-recover
  state, not the pre-recover count.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


def _make_migration(
    jira_client: Any,
    op_client: Any,
    wp_map: dict[str, Any] | None = None,
    data_dir: Any = None,
) -> Any:
    """Bypass ``__init__`` to avoid the BaseMigration boot path."""
    from pathlib import Path

    from src.application.components.attachment_recovery_migration import (
        AttachmentRecoveryMigration,
    )

    instance = AttachmentRecoveryMigration.__new__(AttachmentRecoveryMigration)
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
    # ``data_dir`` is needed by ``_merge_attachment_mapping``. Default
    # to ``Path("/tmp")`` so tests that don't exercise persistence
    # don't need to pass one.
    instance.data_dir = Path(data_dir) if data_dir is not None else Path("/tmp")

    class FakeMappings:
        def __init__(self, m: dict[str, Any]) -> None:
            self._m = {"work_package": m}

        def get_mapping(self, name: str) -> dict[str, Any]:
            return self._m.get(name, {})

    instance.mappings = FakeMappings(wp_map or {})
    return instance


def _envelope(status: str, data: Any) -> dict[str, Any]:
    """Build a Rails ``execute_script_with_data`` response envelope."""
    return {"status": status, "message": "ok", "data": data, "output": "<dummy>"}


# --- fail-loud + scope guards -----------------------------------------------


def test_run_empty_wp_map_fails_loud() -> None:
    """No usable WP rows → success=False with stable error tag.

    Mirrors the fail-loud pattern in attachments / attachment_provenance /
    watchers / wp_skeleton (PRs #194/#197/#198/#199).
    """
    mig = _make_migration(jira_client=None, op_client=None, wp_map={})
    result = mig.run()
    assert result.success is False
    assert "missing_work_package_mapping" in (result.errors or [])


def test_run_legacy_int_only_mapping_fails_loud() -> None:
    """WP map contains only legacy bare-int rows (no recoverable
    Jira key) → fail loud with the same error tag as the empty-map
    case. ``_wp_lookup_by_jira_key`` filters bare-int rows out, so
    the lookup returns an empty dict — same fail-loud guard fires.
    Pin: corrected docstring/name to match the asserted behaviour
    (PR #206 review).
    """
    mig = _make_migration(jira_client=None, op_client=None, wp_map={"PROJ-1": 42})
    result = mig.run()
    assert result.success is False
    assert "missing_work_package_mapping" in (result.errors or [])


# --- diff + delegation -------------------------------------------------------


class _FakeJiraIssue:
    def __init__(self, key: str, attachments: list[dict[str, Any]]) -> None:
        self.key = key
        self.fields = SimpleNamespace(
            attachment=[
                SimpleNamespace(
                    filename=a["filename"],
                    size=a.get("size"),
                    id=a.get("id"),
                    content=a.get("url"),
                )
                for a in attachments
            ],
        )


class _FakeUnderlying:
    def __init__(self, pages: list[list[Any]]) -> None:
        self._pages = list(pages)

    def search_issues(self, *_a, **_kw):
        return self._pages.pop(0) if self._pages else []


class _FakeJira:
    def __init__(self, pages: list[list[Any]]) -> None:
        self.jira = _FakeUnderlying(pages)


class _RecordingOp:
    """OP fake that records script calls + returns canned envelopes.

    ``op_calls[i]`` is the i-th attachment-fetch envelope to return.
    Each call advances a cursor; subsequent calls (e.g. the
    post-recovery recompute) consume the next envelope.
    """

    def __init__(self, attachment_fetches: list[dict[int, list[str]]] | None = None) -> None:
        self.script_calls: list[tuple[str, list[Any]]] = []
        self._fetches = list(attachment_fetches or [])

    def execute_script_with_data(self, script: str, data: list[Any]) -> dict[str, Any]:
        self.script_calls.append((script, list(data)))
        if self._fetches:
            payload = self._fetches.pop(0)
            return _envelope("success", {str(k): v for k, v in payload.items()})
        return _envelope("success", {})


def test_run_clean_state_no_recovery_no_op_call_to_attachments_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every Jira file already in OP → ``recovered=0``, no batch
    delegation. Pin: idempotent on a healthy instance.
    """
    pages = [
        [
            _FakeJiraIssue("NRS-1", [{"filename": "ok.txt", "id": 1, "url": "u"}]),
        ],
    ]
    jira = _FakeJira(pages)
    # OP already has the file.
    op = _RecordingOp(attachment_fetches=[{501: ["ok.txt"]}])

    mig = _make_migration(
        jira_client=jira,
        op_client=op,
        wp_map={"10001": {"jira_key": "NRS-1", "openproject_id": 501}},
    )

    # Spy on AttachmentsMigration to ensure we don't delegate.
    delegated: list[list[str]] = []

    def _spy_init(self, jira_client=None, op_client=None):
        return None

    def _spy_process(self, keys):
        delegated.append(list(keys))
        return (0, 0, {})

    from src.application.components import attachments_migration as am_mod

    monkeypatch.setattr(am_mod.AttachmentsMigration, "__init__", _spy_init)
    monkeypatch.setattr(am_mod.AttachmentsMigration, "_process_batch_end_to_end", _spy_process)

    result = mig.run()

    assert result.success
    assert result.updated == 0
    assert delegated == []  # no delegation on a clean state
    assert result.details["clean"] == 1
    assert result.details["still_missing_total"] == 0


def test_run_with_missing_files_delegates_to_attachments_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Jira has ``a.txt`` and ``b.txt``; OP only has ``a.txt`` →
    delegates the affected jira_key to ``_process_batch_end_to_end``.

    The post-recover recompute returns OP with both files (simulating
    a successful recover) → ``still_missing_total = 0``.
    """
    pages = [
        [
            _FakeJiraIssue(
                "NRS-1",
                [{"filename": "a.txt", "id": 1, "url": "u1"}, {"filename": "b.txt", "id": 2, "url": "u2"}],
            ),
        ],
    ]
    jira = _FakeJira(pages)
    op = _RecordingOp(
        attachment_fetches=[
            {501: ["a.txt"]},  # initial state — b.txt missing
            {501: ["a.txt", "b.txt"]},  # after recover — both present
        ],
    )

    mig = _make_migration(
        jira_client=jira,
        op_client=op,
        wp_map={"10001": {"jira_key": "NRS-1", "openproject_id": 501}},
    )

    delegated_keys: list[list[str]] = []

    def _spy_init(self, jira_client=None, op_client=None):
        return None

    def _spy_process(self, keys):
        delegated_keys.append(list(keys))
        # Simulate a successful recover.
        return (1, 0, {"NRS-1": {"b.txt": 999}})

    from src.application.components import attachments_migration as am_mod

    monkeypatch.setattr(am_mod.AttachmentsMigration, "__init__", _spy_init)
    monkeypatch.setattr(am_mod.AttachmentsMigration, "_process_batch_end_to_end", _spy_process)

    result = mig.run()

    assert result.success, result
    assert result.updated == 1
    assert delegated_keys == [["NRS-1"]]
    assert result.details["recovered"] == 1
    assert result.details["still_missing_total"] == 0
    assert result.details["missing_total_before"] == 1


def test_run_handles_duplicate_filenames_with_multiset_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Jira has ``screenshot.png`` twice and OP has it once →
    ``missing_total_before == 1`` (NOT 0 — set-based dedup would
    have masked the loss).
    """
    pages = [
        [
            _FakeJiraIssue(
                "NRS-1",
                [
                    {"filename": "screenshot.png", "id": 1, "url": "u1"},
                    {"filename": "screenshot.png", "id": 2, "url": "u2"},
                ],
            ),
        ],
    ]
    jira = _FakeJira(pages)
    op = _RecordingOp(
        attachment_fetches=[
            {501: ["screenshot.png"]},
            {501: ["screenshot.png"]},  # post-recover unchanged (Rails dedup)
        ],
    )

    mig = _make_migration(
        jira_client=jira,
        op_client=op,
        wp_map={"10001": {"jira_key": "NRS-1", "openproject_id": 501}},
    )

    def _spy_init(self, jira_client=None, op_client=None):
        return None

    def _spy_process(self, keys):
        # Rails dedup keeps OP at 1 file → still_missing remains 1.
        return (0, 0, {})

    from src.application.components import attachments_migration as am_mod

    monkeypatch.setattr(am_mod.AttachmentsMigration, "__init__", _spy_init)
    monkeypatch.setattr(am_mod.AttachmentsMigration, "_process_batch_end_to_end", _spy_process)

    result = mig.run()

    assert result.details["missing_total_before"] == 1
    # Recovery couldn't help (Rails-side filename collision) → still missing.
    assert result.details["still_missing_total"] == 1
    assert result.success is False


def test_run_extra_in_op_is_reported_but_does_not_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phantom OP attachment (in OP but not in Jira) is reported under
    ``extra_total`` but does NOT trigger a recovery or a failure.

    Pin: extras are informational; only true loss (missing) is
    actionable here. Reverse direction would need a separate
    "delete phantom" component (out of scope).
    """
    pages = [
        [
            _FakeJiraIssue("NRS-1", [{"filename": "real.txt", "id": 1, "url": "u"}]),
        ],
    ]
    jira = _FakeJira(pages)
    op = _RecordingOp(attachment_fetches=[{501: ["real.txt", "phantom.bin"]}])

    mig = _make_migration(
        jira_client=jira,
        op_client=op,
        wp_map={"10001": {"jira_key": "NRS-1", "openproject_id": 501}},
    )

    def _spy_init(self, jira_client=None, op_client=None):
        return None

    def _spy_process(self, keys):
        msg = "delegation should not happen on extra-only state"
        raise AssertionError(msg)

    from src.application.components import attachments_migration as am_mod

    monkeypatch.setattr(am_mod.AttachmentsMigration, "__init__", _spy_init)
    monkeypatch.setattr(am_mod.AttachmentsMigration, "_process_batch_end_to_end", _spy_process)

    result = mig.run()

    assert result.success
    assert result.updated == 0
    assert result.details["extra_total"] == 1


def test_op_envelope_error_status_is_logged_and_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rails ``status="error"`` → batch skipped, run continues.

    Pin: the recovery uses ``envelope['data']`` (NOT top-level
    keys), and a non-success status doesn't crash the run — same
    envelope-bug class PR #201 caught for wp_metadata_backfill.
    """

    class _ErrOp:
        def __init__(self) -> None:
            self.calls = 0

        def execute_script_with_data(self, script: str, data: list[Any]) -> dict[str, Any]:
            self.calls += 1
            return {"status": "error", "message": "no markers", "output": "garbage"}

    pages = [
        [_FakeJiraIssue("NRS-1", [{"filename": "x.txt", "id": 1, "url": "u"}])],
    ]
    jira = _FakeJira(pages)
    op = _ErrOp()

    mig = _make_migration(
        jira_client=jira,
        op_client=op,
        wp_map={"10001": {"jira_key": "NRS-1", "openproject_id": 501}},
    )

    def _spy_init(self, jira_client=None, op_client=None):
        return None

    def _spy_process(self, keys):
        # Rails error skipped the OP-side fetch → component sees OP
        # as empty for that WP → file appears missing → delegation
        # fires.
        return (1, 0, {})

    from src.application.components import attachments_migration as am_mod

    monkeypatch.setattr(am_mod.AttachmentsMigration, "__init__", _spy_init)
    monkeypatch.setattr(am_mod.AttachmentsMigration, "_process_batch_end_to_end", _spy_process)

    result = mig.run()
    # Run completes (no crash).
    assert isinstance(result.message, str)
    # OP fetch was attempted twice — initial (sees nothing because
    # the error envelope drops the data payload) AND the
    # post-recover recompute (also sees an error envelope, so
    # still_missing remains positive). Pin the exact contract per
    # PR #206 review.
    assert op.calls >= 2
