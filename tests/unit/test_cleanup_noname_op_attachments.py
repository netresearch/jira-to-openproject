"""Unit tests for ``tools.cleanup_noname_op_attachments``.

Pinned behaviours:

* ``_build_script`` produces a Ruby script whose marker-fenced
  envelope matches the runner's expected shape.
* The ``apply`` flag is propagated correctly so dry-runs don't
  destroy data.
* Project key sanitization keeps the blast radius bounded.
"""

from __future__ import annotations

from tools.cleanup_noname_op_attachments import _PROJECT_KEY_RE, _build_script, main


def test_build_script_dry_run_includes_apply_false() -> None:
    """Dry-run mode must encode ``apply_literal = false`` so the
    generated Ruby never enters the destroy branch.
    """
    script = _build_script("NRS", apply=False)
    assert "apply_mode: false" in script
    assert "false && siblings.size > 0" in script
    # Project identifier is lowercased for OP's identifier convention.
    assert "Project.find_by(identifier: 'nrs')" in script


def test_build_script_apply_includes_apply_true() -> None:
    """Apply mode must encode ``apply_literal = true`` so the
    destroy branch runs.
    """
    script = _build_script("NRS", apply=True)
    assert "apply_mode: true" in script
    assert "true && siblings.size > 0" in script
    assert "att.destroy!" in script


def test_build_script_marker_fenced_envelope() -> None:
    """The script must emit JSON between the runner's standard
    start/end markers — same envelope contract recovery uses.
    """
    script = _build_script("NRS", apply=False)
    assert "start_marker" in script
    assert "end_marker" in script
    assert "data.to_json" in script


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
