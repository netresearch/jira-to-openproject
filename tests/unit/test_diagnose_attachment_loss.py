"""Unit tests for ``tools.diagnose_attachment_loss``.

The tool fans out to live Jira + OP, so the tests stub both paths
through small fakes. Pinned behaviours:

* WP-mapping load: dict-shape rows, legacy-int rows skipped, inner
  ``jira_key`` preferred over outer numeric key.
* Per-issue diff: clean match, missing-in-op, extra-in-op, duplicate
  filenames (multiset semantics), unmapped Jira issue.
* Aggregate summary counts agree with per-issue results.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def _write_mapping(path: Path, data: dict[str, Any]) -> None:
    with path.open("w") as f:
        json.dump(data, f)


def test_load_wp_mapping_prefers_inner_jira_key(tmp_path: Path) -> None:
    """Outer key is numeric, inner ``jira_key`` is the human-readable one.

    The diagnostic must use the inner key so it matches what
    ``AttachmentsMigration._wp_lookup_by_jira_key`` produces — any
    divergence here would silently miscount.
    """
    from tools.diagnose_attachment_loss import _load_wp_mapping

    f = tmp_path / "wpm.json"
    _write_mapping(
        f,
        {
            "10001": {"jira_key": "PROJ-1", "openproject_id": 501},
            "10002": {"jira_key": "PROJ-2", "openproject_id": 502},
        },
    )
    out = _load_wp_mapping(f)
    assert out == {"PROJ-1": 501, "PROJ-2": 502}


def test_load_wp_mapping_skips_legacy_int_rows(tmp_path: Path) -> None:
    """Bare-int rows have no recoverable ``jira_key`` — skipped silently."""
    from tools.diagnose_attachment_loss import _load_wp_mapping

    f = tmp_path / "wpm.json"
    _write_mapping(
        f,
        {
            "10001": {"jira_key": "PROJ-1", "openproject_id": 501},
            "PROJ-99": 999,  # legacy int — skip
        },
    )
    out = _load_wp_mapping(f)
    assert out == {"PROJ-1": 501}


def test_load_wp_mapping_falls_back_to_outer_when_inner_missing(tmp_path: Path) -> None:
    """If inner ``jira_key`` is absent, the outer key is taken as the human-readable form.

    Mirrors the legacy/test fixture shape some prior runs left on
    disk; matches the fallback in
    ``AttachmentsMigration._wp_lookup_by_jira_key``.
    """
    from tools.diagnose_attachment_loss import _load_wp_mapping

    f = tmp_path / "wpm.json"
    _write_mapping(f, {"PROJ-1": {"openproject_id": 501}})
    out = _load_wp_mapping(f)
    assert out == {"PROJ-1": 501}


def test_load_wp_mapping_missing_file_raises(tmp_path: Path) -> None:
    from tools.diagnose_attachment_loss import _load_wp_mapping

    with pytest.raises(FileNotFoundError, match="not found"):
        _load_wp_mapping(tmp_path / "nope.json")


def test_diagnose_clean_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-issue Jira+OP filenames match → ``clean`` count incremented."""
    from tools import diagnose_attachment_loss as mod

    f = tmp_path / "wpm.json"
    _write_mapping(f, {"10001": {"jira_key": "NRS-1", "openproject_id": 501}})

    monkeypatch.setattr(
        mod,
        "_iter_jira_issues_with_attachments",
        lambda _k: {"NRS-1": [{"filename": "a.txt", "size": 10, "id": "1"}]},
    )

    class _StubOp:
        def execute_json_query(self, *_a, **_kw) -> dict[str, list[dict[str, Any]]]:
            return {"501": [{"filename": "a.txt", "id": 100, "size": 10}]}

    monkeypatch.setattr(mod, "OpenProjectClient", _StubOp)

    report = mod.diagnose("NRS", f)
    assert report["summary"]["clean"] == 1
    assert report["summary"]["missing_attachments_total"] == 0
    assert "NRS-1" not in report["per_issue_diffs"]


def test_diagnose_missing_in_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """File present in Jira but absent from OP → counted as missing."""
    from tools import diagnose_attachment_loss as mod

    f = tmp_path / "wpm.json"
    _write_mapping(f, {"10001": {"jira_key": "NRS-1", "openproject_id": 501}})

    monkeypatch.setattr(
        mod,
        "_iter_jira_issues_with_attachments",
        lambda _k: {
            "NRS-1": [{"filename": "a.txt", "size": 10, "id": "1"}, {"filename": "b.txt", "size": 5, "id": "2"}]
        },
    )

    class _StubOp:
        def execute_json_query(self, *_a, **_kw) -> dict[str, list[dict[str, Any]]]:
            return {"501": [{"filename": "a.txt", "id": 100, "size": 10}]}

    monkeypatch.setattr(mod, "OpenProjectClient", _StubOp)

    report = mod.diagnose("NRS", f)
    assert report["summary"]["missing_attachments_total"] == 1
    diff = report["per_issue_diffs"]["NRS-1"]
    assert diff["missing_in_op"] == ["b.txt"]
    assert diff["extra_in_op"] == []


def test_diagnose_extra_in_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """File present in OP but absent from Jira → counted as extra (phantom)."""
    from tools import diagnose_attachment_loss as mod

    f = tmp_path / "wpm.json"
    _write_mapping(f, {"10001": {"jira_key": "NRS-1", "openproject_id": 501}})

    monkeypatch.setattr(
        mod,
        "_iter_jira_issues_with_attachments",
        lambda _k: {"NRS-1": [{"filename": "a.txt", "size": 10, "id": "1"}]},
    )

    class _StubOp:
        def execute_json_query(self, *_a, **_kw) -> dict[str, list[dict[str, Any]]]:
            return {
                "501": [
                    {"filename": "a.txt", "id": 100, "size": 10},
                    {"filename": "phantom.bin", "id": 101, "size": 99},
                ],
            }

    monkeypatch.setattr(mod, "OpenProjectClient", _StubOp)

    report = mod.diagnose("NRS", f)
    assert report["summary"]["extra_attachments_total"] == 1
    diff = report["per_issue_diffs"]["NRS-1"]
    assert diff["missing_in_op"] == []
    assert diff["extra_in_op"] == ["phantom.bin"]


def test_diagnose_handles_duplicate_filenames(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Duplicate filenames are matched as a multiset.

    Pin: 2 ``screenshot.png`` in Jira and 1 in OP → 1 missing
    (NOT 0 — set-based dedup would have masked the loss).
    """
    from tools import diagnose_attachment_loss as mod

    f = tmp_path / "wpm.json"
    _write_mapping(f, {"10001": {"jira_key": "NRS-1", "openproject_id": 501}})

    monkeypatch.setattr(
        mod,
        "_iter_jira_issues_with_attachments",
        lambda _k: {
            "NRS-1": [
                {"filename": "screenshot.png", "size": 10, "id": "1"},
                {"filename": "screenshot.png", "size": 11, "id": "2"},
            ],
        },
    )

    class _StubOp:
        def execute_json_query(self, *_a, **_kw) -> dict[str, list[dict[str, Any]]]:
            return {"501": [{"filename": "screenshot.png", "id": 100, "size": 10}]}

    monkeypatch.setattr(mod, "OpenProjectClient", _StubOp)

    report = mod.diagnose("NRS", f)
    assert report["summary"]["missing_attachments_total"] == 1
    assert report["per_issue_diffs"]["NRS-1"]["missing_in_op"] == ["screenshot.png"]


def test_diagnose_unmapped_jira_issue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Jira issue has attachments but no entry in WP mapping → ``wp_unmapped``."""
    from tools import diagnose_attachment_loss as mod

    f = tmp_path / "wpm.json"
    _write_mapping(f, {"10001": {"jira_key": "NRS-1", "openproject_id": 501}})

    monkeypatch.setattr(
        mod,
        "_iter_jira_issues_with_attachments",
        lambda _k: {
            "NRS-1": [{"filename": "a.txt", "size": 10, "id": "1"}],
            "NRS-99": [{"filename": "ghost.bin", "size": 5, "id": "2"}],  # not in mapping
        },
    )

    class _StubOp:
        def execute_json_query(self, *_a, **_kw) -> dict[str, list[dict[str, Any]]]:
            return {"501": [{"filename": "a.txt", "id": 100, "size": 10}]}

    monkeypatch.setattr(mod, "OpenProjectClient", _StubOp)

    report = mod.diagnose("NRS", f)
    assert report["summary"]["wp_unmapped"] == 1
    assert report["per_issue_diffs"]["NRS-99"]["status"] == "wp_unmapped"
    assert report["per_issue_diffs"]["NRS-99"]["wp_id"] is None
    # The ghost file shows up under missing_in_op for the unmapped issue.
    assert report["per_issue_diffs"]["NRS-99"]["missing_in_op"] == ["ghost.bin"]


def test_diagnose_invalid_project_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same project-key validation contract as the audit tool."""
    from tools import diagnose_attachment_loss as mod

    f = tmp_path / "wpm.json"
    _write_mapping(f, {})

    # Stub OpenProjectClient so the test doesn't try to connect.
    def _no_op_client() -> SimpleNamespace:
        return SimpleNamespace()

    monkeypatch.setattr(mod, "OpenProjectClient", _no_op_client)

    with pytest.raises(ValueError, match="invalid project key"):
        mod.diagnose("nrs lower-case", f)


def test_diagnose_summary_counts_agree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Aggregate counts must equal the per-issue diff sums.

    Pin: a future regression that double-counts (or under-counts)
    one of the buckets shows up here as a sum mismatch.
    """
    from tools import diagnose_attachment_loss as mod

    f = tmp_path / "wpm.json"
    _write_mapping(
        f,
        {
            "10001": {"jira_key": "NRS-1", "openproject_id": 501},
            "10002": {"jira_key": "NRS-2", "openproject_id": 502},
            "10003": {"jira_key": "NRS-3", "openproject_id": 503},
        },
    )

    monkeypatch.setattr(
        mod,
        "_iter_jira_issues_with_attachments",
        lambda _k: {
            # Clean
            "NRS-1": [{"filename": "ok.txt", "size": 1, "id": "1"}],
            # Missing
            "NRS-2": [
                {"filename": "lost.txt", "size": 1, "id": "2"},
                {"filename": "also_lost.txt", "size": 1, "id": "3"},
            ],
            # Extra
            "NRS-3": [{"filename": "kept.txt", "size": 1, "id": "4"}],
        },
    )

    class _StubOp:
        def execute_json_query(self, *_a, **_kw) -> dict[str, list[dict[str, Any]]]:
            return {
                "501": [{"filename": "ok.txt", "id": 100, "size": 1}],
                "502": [],
                "503": [
                    {"filename": "kept.txt", "id": 102, "size": 1},
                    {"filename": "phantom.txt", "id": 103, "size": 7},
                ],
            }

    monkeypatch.setattr(mod, "OpenProjectClient", _StubOp)

    report = mod.diagnose("NRS", f)
    s = report["summary"]
    assert s["clean"] == 1, s
    assert s["issues_with_missing"] == 1, s
    assert s["issues_with_extra"] == 1, s
    assert s["missing_attachments_total"] == 2, s
    assert s["extra_attachments_total"] == 1, s
    assert s["issues_examined"] == 3, s
