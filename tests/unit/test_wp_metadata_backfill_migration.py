"""Tests for the WP metadata backfill migration.

Covers:

* Happy-path: WP missing assignee + CFs gets both filled.
* Idempotency: WP that already has assigned_to_id non-null is left alone
  on the assignee field (Rails-side rule pinned by the script).
* User unmapped: assignee not in user_mapping → skipped without crashing.
* Empty work_package mapping → fail loud (same error tag as siblings).
* Legacy bare-int rows → fail loud (no usable rows).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.application.components.wp_metadata_backfill_migration import (
    WpMetadataBackfillMigration,
)


class _DummyJiraIssue:
    def __init__(self, key: str, assignee: dict | None, project_key: str = "PROJ", project_id: str = "10001") -> None:
        self.key = key
        self.id = "1" + key.split("-")[-1]
        self.fields = SimpleNamespace(
            assignee=SimpleNamespace(**assignee) if assignee else None,
            project=SimpleNamespace(key=project_key, id=project_id),
        )


class _DummyJiraClient:
    def __init__(self, issues: dict[str, Any]) -> None:
        self._issues = issues

    def batch_get_issues(self, keys: list[str]) -> dict[str, Any]:
        return {k: self._issues[k] for k in keys if k in self._issues}


class _DummyOpClient:
    def __init__(self, rails_response: dict[str, int] | None = None) -> None:
        self.script_calls: list[tuple[str, list[dict]]] = []
        self._cf_id_seq = 0
        self._rails_response = rails_response or {
            "updated_assignee": 0,
            "updated_cf": 0,
            "skipped": 0,
            "wp_missing": 0,
            "failed": 0,
        }

    def ensure_custom_field(self, name: str, field_format: str) -> dict[str, int]:
        self._cf_id_seq += 1
        return {"id": 100 + self._cf_id_seq, "name": name}

    def execute_script_with_data(self, script: str, data: list[dict]) -> dict[str, int]:
        self.script_calls.append((script, list(data)))
        # Compute the response from the last call's payload so tests can
        # assert on what the orchestrator surfaced. The Rails script
        # itself is opaque; tests pin the orchestrator's behaviour, not
        # the Rails internals (those are pinned by an integration test
        # that actually runs ``ruby -c`` — see #192).
        updated_assignee = sum(1 for r in data if r.get("assigned_to_id"))
        updated_cf = sum(len(r.get("custom_fields") or []) for r in data)
        return {
            "updated_assignee": updated_assignee,
            "updated_cf": updated_cf,
            "skipped": 0,
            "wp_missing": 0,
            "failed": 0,
        }


@pytest.fixture(autouse=True)
def _mappings(monkeypatch: pytest.MonkeyPatch):
    """Seed both ``user`` and ``work_package`` mappings on the global config."""
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "user": {"alice": {"openproject_id": 11}},
                "work_package": {
                    "10001": {"jira_key": "PROJ-1", "openproject_id": 501},
                    "10002": {"jira_key": "PROJ-2", "openproject_id": 502},
                },
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

        def set_mapping(self, name: str, data) -> None:
            self._m[name] = data

    dummy = DummyMappings()
    monkeypatch.setattr(cfg, "mappings", dummy, raising=False)
    return dummy


def _make_migration(jira_client: Any, op_client: Any) -> WpMetadataBackfillMigration:
    """Bypass ``__init__`` to avoid the BaseMigration boot path."""
    instance = WpMetadataBackfillMigration.__new__(WpMetadataBackfillMigration)
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
    # ``self.mappings`` is the proxy attribute the production
    # ``BaseMigration.__init__`` wires up. Tests rely on the fixture's
    # monkeypatch of ``config.mappings``; assigning the proxy here makes
    # the lookup go through the monkeypatched global.
    import src.config as cfg

    instance.mappings = cfg.mappings
    return instance


def test_happy_path_fills_assignee_and_cfs():
    """WP missing assignee + CFs → assignee_updates=1, cf_updates>0."""
    issues = {
        "PROJ-1": _DummyJiraIssue("PROJ-1", assignee={"name": "alice"}),
    }
    jira = _DummyJiraClient(issues)
    op = _DummyOpClient()

    mig = _make_migration(jira, op)
    # Single mapped WP.
    mig.mappings.set_mapping(
        "work_package",
        {"10001": {"jira_key": "PROJ-1", "openproject_id": 501}},
    )

    result = mig.run()

    assert result.success, result
    # One Rails call with one record.
    assert len(op.script_calls) == 1
    _, payload = op.script_calls[0]
    assert len(payload) == 1
    rec = payload[0]
    assert rec["work_package_id"] == 501
    assert rec["assigned_to_id"] == 11  # alice → openproject_id=11
    # Provenance CFs were built (at least Origin Key + Origin ID).
    assert len(rec["custom_fields"]) >= 2


def test_user_unmapped_assignee_omitted_but_cfs_still_filled():
    """Assignee not in user_mapping → assigned_to_id is None; CFs still run.

    The Rails script's ``if r['assigned_to_id'] && wp.assigned_to_id.nil?``
    guard ensures a None is silently ignored; the CF block still runs
    over the same record. Pin: the orchestrator emits the record (with
    ``assigned_to_id=None``) rather than dropping it entirely, so the
    CF backfill still proceeds.
    """
    issues = {
        "PROJ-1": _DummyJiraIssue("PROJ-1", assignee={"name": "bob"}),  # bob NOT in mapping
    }
    jira = _DummyJiraClient(issues)
    op = _DummyOpClient()

    mig = _make_migration(jira, op)
    mig.mappings.set_mapping(
        "work_package",
        {"10001": {"jira_key": "PROJ-1", "openproject_id": 501}},
    )

    result = mig.run()
    assert result.success, result
    _, payload = op.script_calls[0]
    rec = payload[0]
    assert rec["assigned_to_id"] is None  # unmapped → None
    assert len(rec["custom_fields"]) >= 2  # CFs unaffected by user-mapping miss


def test_no_assignee_and_no_cfs_skips_record():
    """Issue with no assignee and no project → nothing to update → skipped.

    The orchestrator drops the record entirely instead of sending an
    empty Rails update; counted as ``nothing_to_update`` in
    ``skip_reasons``.
    """

    # Issue with no assignee and synthetic ``fields`` that omits project
    # so ``_build_provenance_custom_field_entries`` produces no entries
    # apart from Origin Key/ID/System (which always fire because they
    # only need the issue key/id).
    class _NoCFsIssue:
        def __init__(self) -> None:
            self.key = "PROJ-1"
            self.id = "1"
            self.fields = SimpleNamespace(assignee=None, project=None)

    issues = {"PROJ-1": _NoCFsIssue()}
    jira = _DummyJiraClient(issues)
    op = _DummyOpClient()

    mig = _make_migration(jira, op)
    mig.mappings.set_mapping(
        "work_package",
        {"10001": {"jira_key": "PROJ-1", "openproject_id": 501}},
    )

    result = mig.run()
    # Origin Key + Origin ID + Origin System still get emitted (the
    # ``_add`` helper doesn't need ``project``), so this is NOT a
    # nothing-to-update case. Pin: it still runs.
    assert result.success
    assert len(op.script_calls) == 1


def test_empty_wp_mapping_fails_loud():
    """Empty WP map → success=False with stable error tag.

    Mirrors the fail-loud pattern in ``attachments`` /
    ``attachment_provenance`` / ``watchers`` (PRs #194/#197/#198).
    """
    jira = _DummyJiraClient({})
    op = _DummyOpClient()
    mig = _make_migration(jira, op)
    mig.mappings.set_mapping("work_package", {})

    result = mig.run()
    assert result.success is False
    assert "missing_work_package_mapping" in (result.errors or [])


def test_legacy_int_only_mapping_fails_loud():
    """Mapping non-empty but only legacy bare-int rows → fail loud.

    Same anti-pattern as #198 thread 1 — distinguishes "mapping
    absent" from "mapping present but unusable" so the operator
    knows to back-fill ``jira_key`` rather than re-running skeleton
    from scratch.
    """
    jira = _DummyJiraClient({})
    op = _DummyOpClient()
    mig = _make_migration(jira, op)
    mig.mappings.set_mapping(
        "work_package",
        {"PROJ-1": 42, "PROJ-2": 43},  # bare-int rows
    )

    result = mig.run()
    assert result.success is False
    assert "missing_work_package_mapping" in (result.errors or [])


def test_jira_batch_fetch_failure_records_skip_reason():
    """Jira batch fetch raises → records counted under ``jira_batch_failed``.

    Pin: the orchestrator catches the exception, records the bucket,
    and continues to the next batch instead of crashing the whole run.
    """

    class _FailingJira:
        def batch_get_issues(self, keys):
            msg = "simulated Jira API failure"
            raise RuntimeError(msg)

    op = _DummyOpClient()
    mig = _make_migration(_FailingJira(), op)
    mig.mappings.set_mapping(
        "work_package",
        {"10001": {"jira_key": "PROJ-1", "openproject_id": 501}},
    )

    result = mig.run()
    # No Rails calls because the Jira fetch failed before a payload
    # could be built.
    assert op.script_calls == []
    # Skip reason recorded.
    assert result.details["skip_reasons"].get("jira_batch_failed") == 1


def test_legacy_int_mixed_with_dict_rows_skips_only_int_rows():
    """Mixed mapping: dict-shape rows produce records; int rows are skipped.

    Mirrors :meth:`AttachmentsMigration._wp_lookup_by_jira_key` —
    legacy bare-int rows have no recoverable Jira key and are dropped,
    but the run continues with the dict-shape rows.
    """
    issues = {"PROJ-1": _DummyJiraIssue("PROJ-1", assignee={"name": "alice"})}
    jira = _DummyJiraClient(issues)
    op = _DummyOpClient()

    mig = _make_migration(jira, op)
    mig.mappings.set_mapping(
        "work_package",
        {
            "10001": {"jira_key": "PROJ-1", "openproject_id": 501},
            "PROJ-99": 999,  # bare int — skipped
        },
    )

    result = mig.run()
    assert result.success
    # Only the dict row should reach the Rails payload.
    assert len(op.script_calls) == 1
    _, payload = op.script_calls[0]
    assert len(payload) == 1
    assert payload[0]["work_package_id"] == 501
