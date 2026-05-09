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

from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest


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
        (they are already idempotent — nothing to do)."""
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
            assert "update_columns" not in script, (
                "dry_run=True must never emit an update_columns Rails call"
            )

        assert stats["updated"] == 0
        assert stats["would_update"] == 4

    def test_apply_mode_fires_update_columns(self) -> None:
        """dry_run=False (apply) must call execute_query_to_json_file with
        update_columns for each WP that needs markers."""
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

        assert stats["skipped"] == 1
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

        assert stats["skipped"] == 1
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
        embed the correct provenance marker strings."""
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
            "Marker backfill must use update_columns, not save/save! "
            "(save creates new journal versions)"
        )
