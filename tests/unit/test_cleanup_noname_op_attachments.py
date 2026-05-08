"""Unit tests for ``tools.cleanup_noname_op_attachments``.

Pinned behaviours:

* ``_build_script`` produces a Ruby script whose marker-fenced
  envelope matches the runner's expected shape.
* The ``apply`` flag is propagated correctly so dry-runs don't
  destroy data.
* Project key sanitization keeps the blast radius bounded.
* CLI surfaces a Ruby-side ``error`` payload as non-zero exit
  + stderr message (PR #220 review).
* Dry-run summary reports ``eligible_for_deletion`` (always
  populated) instead of ``will_delete`` (gated on apply mode) —
  the prior version silently always reported 0.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from tools.cleanup_noname_op_attachments import _PROJECT_KEY_RE, _build_script, main


def test_build_script_dry_run_includes_apply_false() -> None:
    """Dry-run mode must encode ``apply_literal = false`` so the
    generated Ruby never enters the destroy branch.
    """
    script = _build_script(apply=False)
    assert "apply_mode: false" in script
    # Eligibility is independent of apply mode now (PR #220 review fix);
    # the ``will_delete`` gate still requires apply.
    assert "false && eligible" in script
    # Project identifier is read from input_data, not interpolated.
    assert "input_data['identifier']" in script
    assert "Project.find_by(identifier: identifier)" in script


def test_build_script_apply_includes_apply_true() -> None:
    """Apply mode must encode ``apply_literal = true`` so the
    destroy branch runs.
    """
    script = _build_script(apply=True)
    assert "apply_mode: true" in script
    assert "true && eligible" in script
    assert "att.destroy!" in script


def test_build_script_marker_fenced_envelope() -> None:
    """The script must emit JSON between the runner's standard
    start/end markers — same envelope contract recovery uses.
    """
    script = _build_script(apply=False)
    assert "start_marker" in script
    assert "end_marker" in script
    assert "data.to_json" in script


def test_build_script_uses_single_sibling_query() -> None:
    """Per PR #220 review: the Ruby must batch sibling lookups into
    one query keyed by ``container_id`` instead of querying per
    ``noname`` row (the prior N+1 pattern).
    """
    script = _build_script(apply=False)
    assert "siblings_by_wp" in script
    assert "group_by(&:first)" in script


def test_project_key_regex_matches_uppercase_only() -> None:
    """Sanity: the CLI's regex rejects keys that could leak into
    other projects via SQL/JQL.
    """
    assert _PROJECT_KEY_RE.match("NRS")
    assert _PROJECT_KEY_RE.match("NRS_PROD")
    assert not _PROJECT_KEY_RE.match("nrs")  # lowercase
    assert not _PROJECT_KEY_RE.match("N")  # single char (regex requires 2+)
    assert not _PROJECT_KEY_RE.match("NRS PROD")  # space
    assert not _PROJECT_KEY_RE.match("NRS;DROP")  # injection attempt


def test_main_rejects_invalid_project_key(capsys) -> None:
    """``main`` must exit non-zero on a bad project key without
    making any Rails call (no OpenProjectClient required).
    """
    rc = main(["nrs"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "Invalid project key" in captured.err


def test_main_surfaces_ruby_error_payload_as_nonzero_exit(capsys) -> None:
    """Per PR #220 review: a Rails envelope with a Ruby-side
    ``error`` field (e.g. "project not found") must exit non-zero
    and print the error to stderr — the prior version exited 0
    with a misleading "0 candidates" summary.
    """
    fake_envelope: dict[str, Any] = {
        "status": "success",
        "message": "ok",
        "data": {"error": "project not found", "identifier": "nrs"},
        "output": "<dummy>",
    }
    with patch("tools.cleanup_noname_op_attachments.OpenProjectClient") as mock_cls:
        mock_op = MagicMock()
        mock_op.execute_script_with_data.return_value = fake_envelope
        mock_cls.return_value = mock_op

        rc = main(["NRS"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "project not found" in captured.err
    # Stdout still gets the structured payload for consumers that
    # parse it; the human-readable error goes to stderr.
    assert "project not found" in captured.out


def test_main_dry_run_reports_eligible_count(capsys) -> None:
    """Per PR #220 review: dry-run summary must use
    ``eligible_for_deletion`` (independent of apply mode), not
    ``will_delete`` (gated on apply). With the prior version, the
    dry-run always claimed "0 eligible" even when candidates with
    valid siblings were present — a misleading UX that this fixes.
    """
    fake_envelope: dict[str, Any] = {
        "status": "success",
        "message": "ok",
        "data": {
            "candidates": 7,
            "eligible_for_deletion": 5,
            "will_delete": 0,  # gated on apply, so 0 in dry-run
            "skipped_no_sibling": 2,
            "deleted": 0,
            "plan_sample": [],
            "deleted_ids": [],
            "apply_mode": False,
        },
        "output": "<dummy>",
    }
    with patch("tools.cleanup_noname_op_attachments.OpenProjectClient") as mock_cls:
        mock_op = MagicMock()
        mock_op.execute_script_with_data.return_value = fake_envelope
        mock_cls.return_value = mock_op

        rc = main(["NRS"])

    assert rc == 0
    captured = capsys.readouterr()
    # Operator must see "5 eligible" — not "0".
    assert "5 eligible" in captured.err
    assert "7 candidate" in captured.err
