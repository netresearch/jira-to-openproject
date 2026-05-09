"""Unit tests for scripts/cleanup_anonymous_comment_duplicates.py.

Tests the pure-Python deduplication logic without hitting Rails.
Specifically:
- _strip_marker: removes provenance marker from notes for equality matching.
- _select_keeper: picks the right journal to preserve from a duplicate group.
- _plan_deletions: produces correct (kept, deleted) split for a WP's journals.
- run(): mocks the Rails query and asserts the right journal IDs are selected
  for deletion.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import the helpers from the script
# ---------------------------------------------------------------------------


def _import_helpers():
    from scripts.cleanup_anonymous_comment_duplicates import (
        ANONYMOUS_USER_ID,
        _plan_deletions,
        _select_keeper,
        _strip_marker,
    )

    return ANONYMOUS_USER_ID, _strip_marker, _select_keeper, _plan_deletions


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _journal(
    journal_id: int,
    wp_id: int,
    user_id: int,
    notes: str,
    created_at: str = "2026-05-07T10:00:00Z",
) -> dict[str, Any]:
    return {
        "id": journal_id,
        "wp_id": wp_id,
        "user_id": user_id,
        "notes": notes,
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# _strip_marker
# ---------------------------------------------------------------------------


class TestStripMarker:
    def test_strips_provenance_marker(self) -> None:
        _, _strip_marker, _, __ = _import_helpers()
        text = "Comment body\n<!-- j2o:jira-comment-id:597766 -->"
        assert _strip_marker(text) == "Comment body"

    def test_strips_marker_with_spaces(self) -> None:
        _, _strip_marker, _, __ = _import_helpers()
        text = "Comment body\n<!--  j2o:jira-comment-id:597766  -->"
        assert _strip_marker(text) == "Comment body"

    def test_noop_on_text_without_marker(self) -> None:
        _, _strip_marker, _, __ = _import_helpers()
        text = "Plain comment with no marker"
        assert _strip_marker(text) == text

    def test_strips_leading_newline_before_marker(self) -> None:
        _, _strip_marker, _, __ = _import_helpers()
        text = "Body\n<!-- j2o:jira-comment-id:42 -->"
        result = _strip_marker(text)
        assert "j2o:jira-comment-id" not in result
        assert "Body" in result


# ---------------------------------------------------------------------------
# _select_keeper
# ---------------------------------------------------------------------------


class TestSelectKeeper:
    def test_real_author_preferred_over_anonymous(self) -> None:
        ANON, _, _select_keeper, __ = _import_helpers()
        anon = _journal(1, 5040, ANON, "Same note", "2026-05-07T09:00:00Z")
        real = _journal(2, 5040, 100, "Same note\n<!-- j2o:jira-comment-id:1 -->", "2026-05-09T10:00:00Z")
        result = _select_keeper([anon, real])
        assert result["id"] == real["id"]

    def test_marker_bearing_journal_preferred_among_real_authors(self) -> None:
        """When both are real-author journals, prefer the one with a provenance marker."""
        _, _, _select_keeper, __ = _import_helpers()
        no_marker = _journal(1, 5040, 100, "Same note", "2026-05-07T09:00:00Z")
        with_marker = _journal(2, 5040, 100, "Same note\n<!-- j2o:jira-comment-id:1 -->", "2026-05-07T09:00:00Z")
        result = _select_keeper([no_marker, with_marker])
        assert result["id"] == with_marker["id"]

    def test_newest_anonymous_kept_when_all_anonymous(self) -> None:
        ANON, _, _select_keeper, __ = _import_helpers()
        older = _journal(1, 5040, ANON, "Note", "2026-05-07T09:00:00Z")
        newer = _journal(2, 5040, ANON, "Note", "2026-05-09T10:00:00Z")
        result = _select_keeper([older, newer])
        assert result["id"] == newer["id"]

    def test_single_journal_returned_unchanged(self) -> None:
        _, _, _select_keeper, __ = _import_helpers()
        j = _journal(1, 5040, 100, "Only journal")
        assert _select_keeper([j])["id"] == 1


# ---------------------------------------------------------------------------
# _plan_deletions
# ---------------------------------------------------------------------------


class TestPlanDeletions:
    def test_no_duplicates_nothing_deleted(self) -> None:
        _, _, _, _plan_deletions = _import_helpers()
        journals = [
            _journal(1, 5040, 100, "First comment"),
            _journal(2, 5040, 200, "Second comment"),
        ]
        kept, to_delete = _plan_deletions(journals)
        assert len(kept) == 2
        assert len(to_delete) == 0

    def test_anonymous_duplicate_of_real_author_deleted(self) -> None:
        """An anonymous copy of a real-author comment must be deleted."""
        ANON, _, _, _plan_deletions = _import_helpers()
        real = _journal(10, 5040, 100, "Comment body\n<!-- j2o:jira-comment-id:1 -->", "2026-05-09T10:00:00Z")
        anon = _journal(5, 5040, ANON, "Comment body", "2026-05-07T09:00:00Z")
        kept, to_delete = _plan_deletions([real, anon])

        assert len(kept) == 1
        assert len(to_delete) == 1
        assert kept[0]["id"] == real["id"]
        assert to_delete[0]["id"] == anon["id"]

    def test_three_duplicates_two_deleted(self) -> None:
        ANON, _, _, _plan_deletions = _import_helpers()
        j1 = _journal(1, 5040, ANON, "Note", "2026-05-06T00:00:00Z")
        j2 = _journal(2, 5040, ANON, "Note", "2026-05-07T00:00:00Z")
        j3 = _journal(3, 5040, 100, "Note\n<!-- j2o:jira-comment-id:9 -->", "2026-05-09T00:00:00Z")

        kept, to_delete = _plan_deletions([j1, j2, j3])
        assert len(kept) == 1
        assert len(to_delete) == 2
        assert kept[0]["id"] == j3["id"]
        delete_ids = {j["id"] for j in to_delete}
        assert delete_ids == {1, 2}

    def test_different_notes_all_kept(self) -> None:
        ANON, _, _, _plan_deletions = _import_helpers()
        journals = [
            _journal(1, 5040, ANON, "First comment"),
            _journal(2, 5040, ANON, "Second comment"),
            _journal(3, 5040, 100, "Third comment"),
        ]
        kept, to_delete = _plan_deletions(journals)
        assert len(kept) == 3
        assert len(to_delete) == 0

    def test_marker_stripped_for_equality(self) -> None:
        """A comment body WITH and WITHOUT marker should be treated as duplicates."""
        ANON, _, _, _plan_deletions = _import_helpers()
        plain = _journal(1, 5040, ANON, "Comment text", "2026-05-07T00:00:00Z")
        marked = _journal(2, 5040, 100, "Comment text\n<!-- j2o:jira-comment-id:5 -->", "2026-05-09T00:00:00Z")
        kept, to_delete = _plan_deletions([plain, marked])
        assert len(kept) == 1
        assert kept[0]["id"] == marked["id"]
        assert to_delete[0]["id"] == plain["id"]

    def test_wp_5040_live_scenario(self) -> None:
        """Reproduce the WP 5040 scenario: 8 anonymous + 4 real → keep 4 real, delete 8 anon."""
        ANON, _, _, _plan_deletions = _import_helpers()
        # 4 unique comments × 3 duplicates each (2 anon + 1 real)
        journals = []
        jid = 1
        # 8 anonymous copies from broken May-7 runs (two copies of each of 4 comments)
        anon_ids = []
        for comment_idx in range(4):
            for run_n in range(2):
                j = _journal(jid, 5040, ANON, f"Comment {comment_idx}", f"2026-05-0{7 + run_n}T10:00:00Z")
                journals.append(j)
                anon_ids.append(jid)
                jid += 1
        # 4 real journals from correct May-9 run
        real_ids = []
        for comment_idx in range(4):
            author_id = 300 + comment_idx
            j = _journal(
                jid,
                5040,
                author_id,
                f"Comment {comment_idx}\n<!-- j2o:jira-comment-id:{1000 + comment_idx} -->",
                "2026-05-09T10:00:00Z",
            )
            journals.append(j)
            real_ids.append(jid)
            jid += 1

        kept, to_delete = _plan_deletions(journals)
        assert len(kept) == 4
        assert len(to_delete) == 8
        assert {j["id"] for j in kept} == set(real_ids)
        assert {j["id"] for j in to_delete} == set(anon_ids)


# ---------------------------------------------------------------------------
# run() — integration with mocked Rails
# ---------------------------------------------------------------------------


class TestRunFunction:
    def _make_logger(self) -> MagicMock:
        logger = MagicMock()
        logger.info = MagicMock()
        logger.error = MagicMock()
        logger.warning = MagicMock()
        return logger

    def test_dry_run_does_not_call_delete(self) -> None:
        """In dry-run mode, execute_query_to_json_file is called exactly once (fetch only)."""
        from scripts.cleanup_anonymous_comment_duplicates import run

        ANON = 2
        mock_op = MagicMock()
        mock_op.execute_query_to_json_file.return_value = {
            "project_id": 1,
            "wp_ids_count": 1,
            "journals": [
                _journal(1, 5040, ANON, "Body", "2026-05-07T00:00:00Z"),
                _journal(2, 5040, 100, "Body\n<!-- j2o:jira-comment-id:9 -->", "2026-05-09T00:00:00Z"),
            ],
        }

        stats = run("NRS", apply=False, logger=self._make_logger(), op_client=mock_op)

        # Only the fetch call — no delete
        mock_op.execute_query_to_json_file.assert_called_once()
        assert stats["to_delete"] == 1
        assert stats["deleted"] == 0

    def test_apply_calls_delete_with_correct_ids(self) -> None:
        """In apply mode, the delete script must receive exactly the IDs selected for deletion."""
        from scripts.cleanup_anonymous_comment_duplicates import run

        ANON = 2
        mock_op = MagicMock()

        # First call: fetch
        # Second call: delete
        fetch_result = {
            "project_id": 1,
            "wp_ids_count": 1,
            "journals": [
                _journal(1, 5040, ANON, "Body", "2026-05-07T00:00:00Z"),
                _journal(2, 5040, 100, "Body\n<!-- j2o:jira-comment-id:9 -->", "2026-05-09T00:00:00Z"),
            ],
        }
        delete_result = {"deleted": 1}
        mock_op.execute_query_to_json_file.side_effect = [fetch_result, delete_result]

        stats = run("NRS", apply=True, logger=self._make_logger(), op_client=mock_op)

        # Fetch + delete calls
        assert mock_op.execute_query_to_json_file.call_count == 2
        delete_call_script: str = mock_op.execute_query_to_json_file.call_args_list[1][0][0]
        # Journal id=1 (anonymous duplicate) must be in the delete payload
        assert "1" in delete_call_script
        # Journal id=2 (real author, keeper) must NOT be in the delete payload
        # (it should only appear if id=2 is explicitly in the ids list — use full parse)
        import json as _json

        start = delete_call_script.find("[")
        end = delete_call_script.find("]", start) + 1
        ids_to_delete = _json.loads(delete_call_script[start:end])
        assert 1 in ids_to_delete
        assert 2 not in ids_to_delete

        assert stats["deleted"] == 1
        assert stats["to_delete"] == 1

    def test_no_duplicates_returns_zero_counts(self) -> None:
        from scripts.cleanup_anonymous_comment_duplicates import run

        mock_op = MagicMock()
        mock_op.execute_query_to_json_file.return_value = {
            "project_id": 1,
            "wp_ids_count": 2,
            "journals": [
                _journal(1, 5040, 100, "First comment"),
                _journal(2, 5041, 100, "Second comment"),
            ],
        }

        stats = run("NRS", apply=False, logger=self._make_logger(), op_client=mock_op)

        assert stats["to_delete"] == 0
        assert stats["deleted"] == 0
        assert stats["duplicate_groups"] == 0
        # Only the fetch call — no delete even if apply=False
        mock_op.execute_query_to_json_file.assert_called_once()

    def test_rails_error_propagated(self) -> None:
        from scripts.cleanup_anonymous_comment_duplicates import run

        mock_op = MagicMock()
        mock_op.execute_query_to_json_file.return_value = {
            "error": "project not found",
            "identifier": "nrs",
        }

        with pytest.raises(RuntimeError, match="project not found"):
            run("NRS", apply=False, logger=self._make_logger(), op_client=mock_op)

    def test_apply_batches_large_deletion_lists(self) -> None:
        """Large delete lists must be batched (>100 IDs)."""
        from scripts.cleanup_anonymous_comment_duplicates import run

        ANON = 2
        mock_op = MagicMock()

        # 150 anonymous journals for the same comment — 1 real keeper + 149 anon to delete
        journals = []
        for i in range(149):
            journals.append(_journal(i + 1, 5040, ANON, "Same comment", f"2026-05-0{(i % 9) + 1}T00:00:00Z"))
        real = _journal(200, 5040, 100, "Same comment\n<!-- j2o:jira-comment-id:1 -->", "2026-05-10T00:00:00Z")
        journals.append(real)

        fetch_result = {
            "project_id": 1,
            "wp_ids_count": 1,
            "journals": journals,
        }

        # Mock all subsequent calls (delete batches) to return success
        def side_effect(script: str):
            if "delete_all" in script:
                return {"deleted": 100}
            return fetch_result

        mock_op.execute_query_to_json_file.side_effect = side_effect

        stats = run("NRS", apply=True, logger=self._make_logger(), op_client=mock_op)

        # 149 to delete → 2 batches (100 + 49)
        total_calls = mock_op.execute_query_to_json_file.call_count
        assert total_calls == 3, f"Expected 3 calls (1 fetch + 2 batches), got {total_calls}"
        assert stats["to_delete"] == 149
