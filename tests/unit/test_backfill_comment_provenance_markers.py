"""Unit tests for scripts/backfill_comment_provenance_markers.py.

Tests the pure-Python pairing and validation logic without hitting Rails or Jira.
Specifically:
- Happy path: N OP journals + N Jira comments, author matches → N marker updates.
- Count mismatch: 3 OP journals vs 4 Jira comments → SKIP + WARNING.
- Author mismatch: one pair has wrong author → SKIP + WARNING.
- Idempotent: OP journals already carry markers → no-op.
- Dry-run: mutate=False → no Rails write calls.
- Apply mode: mutate=True → Rails update_columns calls fired.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Helpers – minimal fake objects that mimic the data shapes the script uses.
# ---------------------------------------------------------------------------


def _jira_comment(
    comment_id: str,
    account_id: str,
    body: str = "Some comment body",
) -> dict[str, Any]:
    """Fake Jira comment dict (post-fetch, fields already extracted)."""
    return {
        "id": comment_id,
        "author_account_id": account_id,
        "body": body,
    }


def _op_journal(
    journal_id: int,
    wp_id: int,
    user_id: int,
    notes: str,
    created_at: str = "2026-05-09T10:00:00Z",
) -> dict[str, Any]:
    """Fake OP journal dict as returned by the Rails query."""
    return {
        "id": journal_id,
        "wp_id": wp_id,
        "user_id": user_id,
        "notes": notes,
        "created_at": created_at,
    }


def _make_logger() -> MagicMock:
    logger = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    logger.debug = MagicMock()
    return logger


# ---------------------------------------------------------------------------
# Import helper (deferred to avoid import-time side effects)
# ---------------------------------------------------------------------------


def _import_module():
    from scripts import backfill_comment_provenance_markers as m

    return m


# ---------------------------------------------------------------------------
# _pair_journals_with_comments
# ---------------------------------------------------------------------------


class TestPairJournalsWithComments:
    """Unit tests for the pure pairing logic."""

    def test_happy_path_4x4_all_match(self) -> None:
        m = _import_module()
        # user_mapping: jira_account_id → op_user_id
        user_mapping = {
            "acc-bjoern": 61,
            "acc-mikhail": 281,
            "acc-anna": 300,
            "acc-peter": 400,
        }
        jira_comments = [
            _jira_comment("c1", "acc-bjoern"),
            _jira_comment("c2", "acc-mikhail"),
            _jira_comment("c3", "acc-anna"),
            _jira_comment("c4", "acc-peter"),
        ]
        op_journals = [
            _op_journal(101, 5040, 61, "Comment 1", "2026-05-09T10:01:00Z"),
            _op_journal(102, 5040, 281, "Comment 2", "2026-05-09T10:02:00Z"),
            _op_journal(103, 5040, 300, "Comment 3", "2026-05-09T10:03:00Z"),
            _op_journal(104, 5040, 400, "Comment 4", "2026-05-09T10:04:00Z"),
        ]
        pairs, skip_reason = m._pair_journals_with_comments(
            op_journals=op_journals,
            jira_comments=jira_comments,
            user_mapping=user_mapping,
        )
        assert skip_reason is None
        assert len(pairs) == 4
        # Verify pairings are in order
        assert pairs[0] == (op_journals[0], jira_comments[0])
        assert pairs[3] == (op_journals[3], jira_comments[3])

    def test_count_mismatch_3_op_4_jira_skips(self) -> None:
        m = _import_module()
        user_mapping: dict[str, int] = {"acc-bjoern": 61}
        jira_comments = [
            _jira_comment("c1", "acc-bjoern"),
            _jira_comment("c2", "acc-bjoern"),
            _jira_comment("c3", "acc-bjoern"),
            _jira_comment("c4", "acc-bjoern"),
        ]
        op_journals = [
            _op_journal(101, 5040, 61, "Comment 1"),
            _op_journal(102, 5040, 61, "Comment 2"),
            _op_journal(103, 5040, 61, "Comment 3"),
        ]
        pairs, skip_reason = m._pair_journals_with_comments(
            op_journals=op_journals,
            jira_comments=jira_comments,
            user_mapping=user_mapping,
        )
        assert pairs == []
        assert skip_reason is not None
        assert "count" in skip_reason.lower() or "mismatch" in skip_reason.lower()

    def test_author_mismatch_one_pair_skips_whole_wp(self) -> None:
        """If any author pair mismatches the whole WP is skipped (safe > throughput)."""
        m = _import_module()
        user_mapping = {"acc-bjoern": 61, "acc-mikhail": 281}
        jira_comments = [
            _jira_comment("c1", "acc-bjoern"),
            _jira_comment("c2", "acc-mikhail"),
        ]
        op_journals = [
            _op_journal(101, 5040, 61, "Comment 1"),
            # Wrong user: journal says user_id=999 but Jira says acc-mikhail → op 281
            _op_journal(102, 5040, 999, "Comment 2"),
        ]
        pairs, skip_reason = m._pair_journals_with_comments(
            op_journals=op_journals,
            jira_comments=jira_comments,
            user_mapping=user_mapping,
        )
        assert pairs == []
        assert skip_reason is not None
        assert "author" in skip_reason.lower() or "mismatch" in skip_reason.lower()

    def test_already_marked_journals_excluded(self) -> None:
        """Journals already carrying a provenance marker are excluded from pairing
        (they are already idempotent — nothing to do).
        """
        m = _import_module()
        user_mapping = {"acc-bjoern": 61}
        jira_comments = [
            _jira_comment("c1", "acc-bjoern"),
        ]
        op_journals = [
            # Already has a marker
            _op_journal(
                101,
                5040,
                61,
                "Comment body\n<!-- j2o:jira-comment-id:c1 -->",
            ),
        ]
        pairs, skip_reason = m._pair_journals_with_comments(
            op_journals=op_journals,
            jira_comments=jira_comments,
            user_mapping=user_mapping,
        )
        # All journals already marked → nothing to pair, but it's not an error
        assert pairs == []
        assert skip_reason is None


# ---------------------------------------------------------------------------
# run() — integration with mocked Rails + Jira
# ---------------------------------------------------------------------------


class TestBackfillRunFunction:
    """Integration tests for the run() function with mocked external calls."""

    def _make_wp_mapping(self) -> list[dict[str, Any]]:
        """Return a minimal WP mapping list (project_key NRS, one WP)."""
        return [
            {
                "jira_key": "NRS-4391",
                "openproject_id": 5040,
                "project_key": "NRS",
            }
        ]

    def test_dry_run_no_rails_writes(self) -> None:
        """dry_run=True must not call the Rails update_columns script."""
        m = _import_module()
        mock_op = MagicMock()
        mock_jira = MagicMock()

        # Rails returns 4 journals (no markers), Jira returns 4 comments
        mock_op.execute_query_to_json_file.return_value = {
            "journals": [
                _op_journal(101, 5040, 61, "B1", "2026-05-09T10:01:00Z"),
                _op_journal(102, 5040, 281, "B2", "2026-05-09T10:02:00Z"),
                _op_journal(103, 5040, 61, "B3", "2026-05-09T10:03:00Z"),
                _op_journal(104, 5040, 281, "B4", "2026-05-09T10:04:00Z"),
            ]
        }
        mock_jira.return_value = [
            _jira_comment("c1", "acc-bjoern"),
            _jira_comment("c2", "acc-mikhail"),
            _jira_comment("c3", "acc-bjoern"),
            _jira_comment("c4", "acc-mikhail"),
        ]

        user_mapping = {"acc-bjoern": 61, "acc-mikhail": 281}

        stats = m.run(
            wp_mapping=self._make_wp_mapping(),
            user_mapping=user_mapping,
            fetch_jira_comments=mock_jira,
            op_client=mock_op,
            dry_run=True,
            logger=_make_logger(),
        )

        # The fetch query may be called, but the update query must NOT be called
        for call_args in mock_op.execute_query_to_json_file.call_args_list:
            script: str = call_args[0][0]
            assert "update_columns" not in script, "dry_run=True must never emit an update_columns Rails call"

        assert stats["updated"] == 0
        assert stats["would_update"] == 4

    def test_apply_mode_fires_update_columns(self) -> None:
        """dry_run=False (apply) must call execute_query_to_json_file with
        update_columns for each WP that needs markers.
        """
        m = _import_module()
        mock_op = MagicMock()
        mock_jira = MagicMock()

        fetch_result = {
            "journals": [
                _op_journal(101, 5040, 61, "Björn comment 1", "2026-05-09T10:01:00Z"),
                _op_journal(102, 5040, 281, "Mikhail comment", "2026-05-09T10:02:00Z"),
                _op_journal(103, 5040, 61, "Björn comment 2", "2026-05-09T10:03:00Z"),
                _op_journal(104, 5040, 61, "Björn comment 3", "2026-05-09T10:04:00Z"),
            ]
        }
        update_result = {"updated": 4}

        def side_effect(script: str):
            if "update_columns" in script:
                return update_result
            return fetch_result

        mock_op.execute_query_to_json_file.side_effect = side_effect

        mock_jira.return_value = [
            _jira_comment("jc1", "acc-bjoern"),
            _jira_comment("jc2", "acc-mikhail"),
            _jira_comment("jc3", "acc-bjoern"),
            _jira_comment("jc4", "acc-bjoern"),
        ]

        user_mapping = {"acc-bjoern": 61, "acc-mikhail": 281}

        stats = m.run(
            wp_mapping=self._make_wp_mapping(),
            user_mapping=user_mapping,
            fetch_jira_comments=mock_jira,
            op_client=mock_op,
            dry_run=False,
            logger=_make_logger(),
        )

        assert stats["updated"] > 0
        # At least one call must contain update_columns
        scripts = [c[0][0] for c in mock_op.execute_query_to_json_file.call_args_list]
        assert any("update_columns" in s for s in scripts), (
            "Expected at least one update_columns Rails call in apply mode"
        )

    def test_count_mismatch_skipped_logged(self) -> None:
        """WP with count mismatch is skipped and a WARNING is emitted."""
        m = _import_module()
        mock_op = MagicMock()
        mock_jira = MagicMock()

        mock_op.execute_query_to_json_file.return_value = {
            "journals": [
                _op_journal(101, 5040, 61, "Comment 1"),
                _op_journal(102, 5040, 61, "Comment 2"),
                _op_journal(103, 5040, 61, "Comment 3"),
            ]
        }
        # Jira has 4 but OP has 3
        mock_jira.return_value = [
            _jira_comment("c1", "acc-bjoern"),
            _jira_comment("c2", "acc-bjoern"),
            _jira_comment("c3", "acc-bjoern"),
            _jira_comment("c4", "acc-bjoern"),
        ]

        logger = _make_logger()
        stats = m.run(
            wp_mapping=self._make_wp_mapping(),
            user_mapping={"acc-bjoern": 61},
            fetch_jira_comments=mock_jira,
            op_client=mock_op,
            dry_run=True,
            logger=logger,
        )

        assert stats["wps_skipped"] == 1
        assert stats["updated"] == 0
        assert stats["would_update"] == 0
        warning_calls = [str(c) for c in logger.warning.call_args_list]
        assert any("NRS-4391" in c or "5040" in c or "count" in c.lower() for c in warning_calls), (
            "Expected a warning mentioning the WP or issue key with count mismatch"
        )

    def test_author_mismatch_skipped_logged(self) -> None:
        """WP where one author pair mismatches is skipped with a WARNING."""
        m = _import_module()
        mock_op = MagicMock()
        mock_jira = MagicMock()

        mock_op.execute_query_to_json_file.return_value = {
            "journals": [
                _op_journal(101, 5040, 61, "Comment 1"),
                # user_id=999 but Jira comment is by acc-mikhail → op 281
                _op_journal(102, 5040, 999, "Comment 2"),
            ]
        }
        mock_jira.return_value = [
            _jira_comment("c1", "acc-bjoern"),
            _jira_comment("c2", "acc-mikhail"),
        ]

        logger = _make_logger()
        stats = m.run(
            wp_mapping=self._make_wp_mapping(),
            user_mapping={"acc-bjoern": 61, "acc-mikhail": 281},
            fetch_jira_comments=mock_jira,
            op_client=mock_op,
            dry_run=True,
            logger=logger,
        )

        assert stats["wps_skipped"] == 1
        assert stats["would_update"] == 0
        warning_calls = [str(c) for c in logger.warning.call_args_list]
        assert any("author" in c.lower() or "mismatch" in c.lower() for c in warning_calls), (
            "Expected a warning mentioning author mismatch"
        )

    def test_already_marked_journals_are_noop(self) -> None:
        """WP whose OP journals already carry provenance markers → no updates, no warnings."""
        m = _import_module()
        mock_op = MagicMock()
        mock_jira = MagicMock()

        mock_op.execute_query_to_json_file.return_value = {
            # All journals already carry markers — returned as empty by the fetch
            # (the Rails query filters out already-marked journals)
            "journals": []
        }
        mock_jira.return_value = [
            _jira_comment("c1", "acc-bjoern"),
        ]

        logger = _make_logger()
        stats = m.run(
            wp_mapping=self._make_wp_mapping(),
            user_mapping={"acc-bjoern": 61},
            fetch_jira_comments=mock_jira,
            op_client=mock_op,
            dry_run=True,
            logger=logger,
        )

        assert stats["would_update"] == 0
        assert stats["updated"] == 0
        # No warnings for already-clean WPs
        assert logger.warning.call_count == 0

    def test_update_script_contains_journal_ids_and_markers(self) -> None:
        """The update_columns Rails script must reference the journal IDs and
        embed the correct provenance marker strings.
        """
        m = _import_module()
        mock_op = MagicMock()
        mock_jira = MagicMock()

        fetch_result = {
            "journals": [
                _op_journal(101, 5040, 61, "Björn note", "2026-05-09T10:01:00Z"),
            ]
        }

        captured_scripts: list[str] = []

        def capture_side_effect(script: str):
            captured_scripts.append(script)
            if "update_columns" in script:
                return {"updated": 1}
            return fetch_result

        mock_op.execute_query_to_json_file.side_effect = capture_side_effect
        mock_jira.return_value = [_jira_comment("jc42", "acc-bjoern")]

        m.run(
            wp_mapping=self._make_wp_mapping(),
            user_mapping={"acc-bjoern": 61},
            fetch_jira_comments=mock_jira,
            op_client=mock_op,
            dry_run=False,
            logger=_make_logger(),
        )

        update_scripts = [s for s in captured_scripts if "update_columns" in s]
        assert len(update_scripts) == 1
        script = update_scripts[0]
        # Must reference the journal id
        assert "101" in script
        # Must embed the Jira comment id in the marker
        assert "jc42" in script
        # Must NOT use .save or wp.save (would create a new journal version)
        assert ".save" not in script.replace("update_columns", ""), (
            "Marker backfill must use update_columns, not save/save! (save creates new journal versions)"
        )


# ---------------------------------------------------------------------------
# Issue #1: Partial backfill — all-or-nothing semantics
# ---------------------------------------------------------------------------


class TestPartialBackfillAllOrNothing:
    """Regression for review comment #1.

    If some OP journals already have markers but others do not (partial
    backfill), the old positional-zip logic silently pairs the wrong journals.
    The correct behaviour is to SKIP the WP with a clear log line
    "partially backfilled" rather than producing incorrect pairings.
    """

    def test_partial_backfill_skipped_not_count_mismatch(self) -> None:
        """WP with 4 Jira comments + 4 OP journals where 1 is already marked.

        The already-marked journal must NOT be included in the pairing pool.
        After excluding it, 3 unmarked journals remain but Jira has 4 comments.
        Old code reports "count mismatch 3 vs 4" which is misleading.
        New code must report "partially backfilled, skipping".
        """
        m = _import_module()
        user_mapping = {"acc-a": 61, "acc-b": 281, "acc-c": 300, "acc-d": 400}
        jira_comments = [
            _jira_comment("c1", "acc-a"),
            _jira_comment("c2", "acc-b"),
            _jira_comment("c3", "acc-c"),
            _jira_comment("c4", "acc-d"),
        ]
        # Journal 101 already has a marker (was backfilled in a prior run).
        op_journals = [
            _op_journal(101, 5040, 61, "Comment 1\n<!-- j2o:jira-comment-id:c1 -->", "2026-05-09T10:01:00Z"),
            _op_journal(102, 5040, 281, "Comment 2", "2026-05-09T10:02:00Z"),
            _op_journal(103, 5040, 300, "Comment 3", "2026-05-09T10:03:00Z"),
            _op_journal(104, 5040, 400, "Comment 4", "2026-05-09T10:04:00Z"),
        ]
        pairs, skip_reason = m._pair_journals_with_comments(
            op_journals=op_journals,
            jira_comments=jira_comments,
            user_mapping=user_mapping,
        )
        # Must be skipped (not a silent mis-pairing)
        assert pairs == []
        assert skip_reason is not None
        # The skip reason must say "partially" — not "count mismatch"
        assert "partial" in skip_reason.lower(), f"Expected 'partial' in skip_reason, got: {skip_reason!r}"

    def test_all_marked_is_noop_not_partial(self) -> None:
        """If ALL journals are already marked, the WP is clean — not a partial backfill."""
        m = _import_module()
        user_mapping = {"acc-a": 61}
        jira_comments = [_jira_comment("c1", "acc-a")]
        op_journals = [
            _op_journal(101, 5040, 61, "Comment 1\n<!-- j2o:jira-comment-id:c1 -->"),
        ]
        pairs, skip_reason = m._pair_journals_with_comments(
            op_journals=op_journals,
            jira_comments=jira_comments,
            user_mapping=user_mapping,
        )
        # All marked → no-op, not a skip
        assert pairs == []
        assert skip_reason is None

    def test_run_logs_partial_backfill_warning(self) -> None:
        """run() must emit a WARNING (not ERROR) for partially-backfilled WPs."""
        m = _import_module()
        mock_op = MagicMock()
        mock_jira = MagicMock()

        # 1 of 4 journals already has a marker
        mock_op.execute_query_to_json_file.return_value = {
            "journals": [
                _op_journal(101, 5040, 61, "C1\n<!-- j2o:jira-comment-id:c1 -->", "2026-05-09T10:01:00Z"),
                _op_journal(102, 5040, 281, "C2", "2026-05-09T10:02:00Z"),
                _op_journal(103, 5040, 300, "C3", "2026-05-09T10:03:00Z"),
                _op_journal(104, 5040, 400, "C4", "2026-05-09T10:04:00Z"),
            ]
        }
        mock_jira.return_value = [
            _jira_comment("c1", "acc-a"),
            _jira_comment("c2", "acc-b"),
            _jira_comment("c3", "acc-c"),
            _jira_comment("c4", "acc-d"),
        ]

        logger = _make_logger()
        stats = m.run(
            wp_mapping=[{"jira_key": "NRS-4391", "openproject_id": 5040, "project_key": "NRS"}],
            user_mapping={"acc-a": 61, "acc-b": 281, "acc-c": 300, "acc-d": 400},
            fetch_jira_comments=mock_jira,
            op_client=mock_op,
            dry_run=True,
            logger=logger,
        )

        assert stats["wps_skipped"] == 1
        assert stats["would_update"] == 0
        warning_calls = [str(c) for c in logger.warning.call_args_list]
        assert any("partial" in c.lower() for c in warning_calls), (
            f"Expected warning mentioning 'partial', got: {warning_calls}"
        )


# ---------------------------------------------------------------------------
# Issue #2: _load_user_mapping schema — index by entry fields
# ---------------------------------------------------------------------------


class TestLoadUserMappingSchema:
    """Regression for review comment #2.

    user_mapping.json is keyed by display name (outer key), but comments
    carry jira_key (e.g. JIRAUSER12345) as their author identifier.
    _load_user_mapping must index by jira_key, jira_name, jira_display_name,
    and jira_email from the entry values — not by the outer key alone.
    """

    def _write_user_mapping(self, path: Path, data: dict) -> None:
        import json

        path.write_text(json.dumps(data), encoding="utf-8")

    def test_lookup_by_jira_key(self) -> None:
        """A Jira comment whose author_account_id == jira_key resolves correctly."""
        m = _import_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            self._write_user_mapping(
                p / "user_mapping.json",
                {
                    "Anne Sophie Geißler": {
                        "jira_display_name": "Anne Sophie Geißler",
                        "jira_email": "anne.geissler@netresearch.de",
                        "jira_key": "JIRAUSER18400",
                        "jira_name": "anne.geissler",
                        "matched_by": "user_mapping_backfill_alias",
                        "openproject_id": 48,
                    }
                },
            )
            result = m._load_user_mapping(p)
        # Must be findable by jira_key
        assert "JIRAUSER18400" in result, (
            f"Expected 'JIRAUSER18400' in user_mapping lookup, got keys: {list(result.keys())[:10]}"
        )
        assert result["JIRAUSER18400"] == 48

    def test_lookup_by_jira_name(self) -> None:
        """A Jira comment whose author is identified by jira_name resolves correctly."""
        m = _import_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            self._write_user_mapping(
                p / "user_mapping.json",
                {
                    "Some User": {
                        "jira_display_name": "Some User",
                        "jira_email": "some.user@example.com",
                        "jira_key": "JIRAUSER99999",
                        "jira_name": "some.user",
                        "openproject_id": 77,
                    }
                },
            )
            result = m._load_user_mapping(p)
        assert "some.user" in result, f"Expected 'some.user' in user_mapping lookup, got keys: {list(result.keys())}"
        assert result["some.user"] == 77

    def test_lookup_by_display_name(self) -> None:
        """A Jira comment whose author is identified by display name resolves correctly."""
        m = _import_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            self._write_user_mapping(
                p / "user_mapping.json",
                {
                    "Display Name User": {
                        "jira_display_name": "Display Name User",
                        "jira_email": "display@example.com",
                        "jira_key": "JIRAUSER77777",
                        "jira_name": "display.user",
                        "openproject_id": 55,
                    }
                },
            )
            result = m._load_user_mapping(p)
        assert "Display Name User" in result, (
            f"Expected 'Display Name User' in user_mapping lookup, got keys: {list(result.keys())}"
        )
        assert result["Display Name User"] == 55

    def test_outer_key_also_indexed(self) -> None:
        """When the outer key is the jira_key (e.g. 'JIRAUSER13400'), it must still resolve."""
        m = _import_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            self._write_user_mapping(
                p / "user_mapping.json",
                {
                    "JIRAUSER13400": {
                        "jira_key": "JIRAUSER13400",
                        "jira_name": "maria.haeglsperger",
                        "jira_display_name": "Maria Haeglsperger",
                        "jira_email": "maria.haeglsperger@axalta.com",
                        "openproject_id": 232,
                    }
                },
            )
            result = m._load_user_mapping(p)
        assert "JIRAUSER13400" in result
        assert result["JIRAUSER13400"] == 232

    def test_author_account_id_resolves_via_jira_key(self) -> None:
        """End-to-end: _pair_journals_with_comments resolves author via jira_key lookup."""
        m = _import_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            self._write_user_mapping(
                p / "user_mapping.json",
                {
                    "Anne Sophie Geißler": {
                        "jira_display_name": "Anne Sophie Geißler",
                        "jira_email": "anne.geissler@netresearch.de",
                        "jira_key": "JIRAUSER18400",
                        "jira_name": "anne.geissler",
                        "openproject_id": 48,
                    }
                },
            )
            user_mapping = m._load_user_mapping(p)

        # The Jira comment has author_account_id == jira_key value
        jira_comments = [_jira_comment("c1", "JIRAUSER18400")]
        op_journals = [_op_journal(101, 5040, 48, "Comment body")]

        pairs, skip_reason = m._pair_journals_with_comments(
            op_journals=op_journals,
            jira_comments=jira_comments,
            user_mapping=user_mapping,
        )
        assert skip_reason is None, (
            f"Expected successful pairing, got skip_reason={skip_reason!r}. "
            f"user_mapping keys: {list(user_mapping.keys())[:10]}"
        )
        assert len(pairs) == 1


# ---------------------------------------------------------------------------
# Issue #6: stats dict key consistency — docstring vs code
# ---------------------------------------------------------------------------


class TestStatsKeyConsistency:
    """Regression for review comment #6.

    run() docstring lists 'wps_skipped' but code uses 'skipped'.
    Either the code or docstring must be aligned.
    """

    def test_returned_stats_has_documented_keys(self) -> None:
        """run() must return all keys documented in its docstring."""
        m = _import_module()
        mock_op = MagicMock()
        mock_op.execute_query_to_json_file.return_value = {"journals": []}
        mock_jira = MagicMock()
        mock_jira.return_value = []

        stats = m.run(
            wp_mapping=[{"jira_key": "NRS-1", "openproject_id": 1, "project_key": "NRS"}],
            user_mapping={},
            fetch_jira_comments=mock_jira,
            op_client=mock_op,
            dry_run=True,
            logger=_make_logger(),
        )

        # Docstring says: wps_processed, wps_skipped, would_update, updated, errors
        # After fix, code and docstring must agree. Test that the documented key exists.
        expected_keys = {"wps_processed", "wps_skipped", "would_update", "updated", "errors"}
        actual_keys = set(stats.keys())
        assert expected_keys == actual_keys, f"Stats keys mismatch. Expected: {expected_keys}, got: {actual_keys}"


# ---------------------------------------------------------------------------
# Issue #7: _setup_logging idempotency (handler duplication)
# ---------------------------------------------------------------------------


class TestSetupLoggingIdempotent:
    """Regression for review comment #7.

    Calling _setup_logging twice on the same logger must NOT add duplicate
    handlers. The handler count must stay at 2 (stream + file) after
    the second call.
    """

    def test_double_invocation_no_handler_duplication(self) -> None:
        m = _import_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "test.log"
            logger1 = m._setup_logging(log_path)
            handler_count_after_first = len(logger1.handlers)

            logger2 = m._setup_logging(log_path)
            handler_count_after_second = len(logger2.handlers)

        # Same logger object returned both times
        assert logger1 is logger2
        assert handler_count_after_second == handler_count_after_first, (
            f"Handler count grew from {handler_count_after_first} to "
            f"{handler_count_after_second} on second call — duplicate handlers added"
        )

    def teardown_method(self, _method) -> None:
        # Close and remove all handlers to avoid cross-test pollution and
        # to prevent ResourceWarning from unclosed FileHandler file objects.
        logger = logging.getLogger("backfill_comment_provenance")
        for h in list(logger.handlers):
            h.close()
            logger.removeHandler(h)


# ---------------------------------------------------------------------------
# Issue: Jira Server uses name/key — not accountId — as canonical identifier
# ---------------------------------------------------------------------------


class TestJiraServerProbeOrder:
    """Regression for the 98%-SKIP rate on NRS (Jira Server 9.12.3).

    Live evidence (2026-05-09 dry-run on NRS):
        WARNING  WP#806 (NRS-207): SKIP — author mismatch at position 0:
                 OP journal #128549 has user_id=110 but Jira comment 39281
                 (account '') maps to op_user_id=None

    Root cause: Jira Server stores the canonical identifier in ``name`` /
    ``key`` (e.g. ``bmarten``, ``JIRAUSER12345``), never in ``accountId``
    (which is a Cloud-only field and is always empty/absent on Server).
    The pairing logic must probe ``name`` and ``key`` as fallbacks when
    ``accountId`` is empty, mirroring
    ``IssueTransformer._JOURNAL_AUTHOR_PROBE_KEYS``.
    """

    def _write_user_mapping(self, path: Path, data: dict) -> None:
        import json

        path.write_text(json.dumps(data), encoding="utf-8")

    def test_server_name_field_resolves_to_op_user(self) -> None:
        """Comment with only name='bmarten' (no accountId) must pair successfully.

        This is the core regression: on Jira Server the comment author dict
        has ``name`` but NOT ``accountId``.  The current code reads only
        ``author_account_id`` (extracted from ``accountId``) and gets an
        empty string, causing every comment to resolve to None → SKIP.
        """
        import tempfile

        m = _import_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            self._write_user_mapping(
                p / "user_mapping.json",
                {
                    "Björn Marten": {
                        "jira_key": "JIRAUSER12345",
                        "jira_name": "bmarten",
                        "jira_email": "bjoern@netresearch.de",
                        "jira_display_name": "Björn Marten",
                        "openproject_id": 61,
                    }
                },
            )
            user_mapping = m._load_user_mapping(p)

        # Jira Server comment: author has name but NOT accountId
        jira_comments = [
            {
                "id": "39281",
                "author_account_id": "",  # always empty on Jira Server
                "author_name": "bmarten",  # Server login name
                "author_key": "",
                "author_email": "",
                "author_display_name": "Björn Marten",
                "body": "Some comment body",
            }
        ]
        op_journals = [_op_journal(128549, 806, 61, "Some comment body")]

        pairs, skip_reason = m._pair_journals_with_comments(
            op_journals=op_journals,
            jira_comments=jira_comments,
            user_mapping=user_mapping,
        )

        assert skip_reason is None, (
            f"Expected successful pairing for Jira Server comment (name='bmarten'), "
            f"got skip_reason={skip_reason!r}. "
            f"user_mapping keys: {list(user_mapping.keys())[:15]}"
        )
        assert len(pairs) == 1
        assert pairs[0][0]["id"] == 128549
        assert pairs[0][1]["id"] == "39281"

    def test_server_key_field_resolves_to_op_user(self) -> None:
        """Comment with only key='JIRAUSER12345' (no accountId) must pair successfully."""
        import tempfile

        m = _import_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            p = Path(tmpdir)
            self._write_user_mapping(
                p / "user_mapping.json",
                {
                    "Björn Marten": {
                        "jira_key": "JIRAUSER12345",
                        "jira_name": "bmarten",
                        "jira_email": "bjoern@netresearch.de",
                        "jira_display_name": "Björn Marten",
                        "openproject_id": 61,
                    }
                },
            )
            user_mapping = m._load_user_mapping(p)

        # Jira Server comment: author has key but NOT accountId
        jira_comments = [
            {
                "id": "39281",
                "author_account_id": "",
                "author_name": "",
                "author_key": "JIRAUSER12345",
                "author_email": "",
                "author_display_name": "",
                "body": "Some comment body",
            }
        ]
        op_journals = [_op_journal(128549, 806, 61, "Some comment body")]

        pairs, skip_reason = m._pair_journals_with_comments(
            op_journals=op_journals,
            jira_comments=jira_comments,
            user_mapping=user_mapping,
        )

        assert skip_reason is None, (
            f"Expected successful pairing for Jira Server comment (key='JIRAUSER12345'), "
            f"got skip_reason={skip_reason!r}."
        )
        assert len(pairs) == 1

    def test_make_jira_fetcher_captures_server_fields(self) -> None:
        """_make_jira_fetcher must extract name/key/emailAddress/displayName from
        the Jira comment author — not only accountId.

        This ensures that when _pair_journals_with_comments probes those fields
        they are actually present in the comment dict.
        """
        m = _import_module()

        # Simulate a Jira Server comment author object
        author = type(
            "Author",
            (),
            {
                "accountId": "",
                "name": "bmarten",
                "key": "JIRAUSER12345",
                "emailAddress": "bjoern@netresearch.de",
                "displayName": "Björn Marten",
            },
        )()
        comment = type(
            "Comment",
            (),
            {
                "id": "39281",
                "author": author,
                "body": "some body",
            },
        )()

        class FakeJiraClient:
            class jira:
                @staticmethod
                def comments(_key: str) -> list[Any]:
                    return [comment]

        result = m._make_jira_fetcher(FakeJiraClient())("NRS-207")
        assert len(result) == 1
        c = result[0]
        assert c["author_name"] == "bmarten", (
            f"Expected author_name='bmarten', got {c!r}. "
            "_make_jira_fetcher must capture 'name' from Jira Server author objects."
        )
        assert c["author_key"] == "JIRAUSER12345"
        assert c["author_email"] == "bjoern@netresearch.de"
        assert c["author_display_name"] == "Björn Marten"
