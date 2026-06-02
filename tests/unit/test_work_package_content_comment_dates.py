"""Comment journals must preserve the original Jira comment date (issue #260).

Issue #260 background
---------------------
A user reported that migrated comments do not preserve their original
date/time. The default profile migrates comments in the CONTENT phase
(``WorkPackageContentMigration``), which collected each comment's body,
author and Jira id but **dropped the comment's ``created`` timestamp**. The
Rails helper then created the journal with ``wp.journal_notes = ...; wp.save!``,
so ActiveRecord stamped ``journals.created_at = Time.now`` (the migration run
time) — the value OpenProject shows as the comment date.

These tests pin the corrected contract end-to-end:
  1. ``_collect_content_for_issue`` captures ``created_at`` from ``comment.created``.
  2. ``_bulk_process_collected_content`` forwards ``created_at`` to the op client.
  3. ``bulk_create_work_package_activities`` puts ``created_at`` in the JSON payload
     AND the Ruby script back-dates the new journal (``update_columns`` created_at).
  4. The single-comment path (``_populate_comments`` →
     ``create_work_package_activity``) does the same.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers (mirroring tests/unit/test_work_package_content_comment_idempotency.py)
# ---------------------------------------------------------------------------


def _build_mig(tmp_path: Path):
    from src.application.components.work_package_content_migration import (
        WorkPackageContentMigration,
    )

    jira = MagicMock()
    op = MagicMock()
    mig = WorkPackageContentMigration(jira_client=jira, op_client=op)
    mig.data_dir = tmp_path
    mig.work_package_mapping_file = tmp_path / mig.WORK_PACKAGE_MAPPING_FILE
    mig.attachment_mapping_file = tmp_path / mig.ATTACHMENT_MAPPING_FILE
    return mig


@pytest.fixture
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "project": {"PROJ": {"openproject_id": 11}},
                "user": {"alice": {"openproject_id": 201}},
                "custom_field": {},
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

        def set_mapping(self, name: str, value):
            self._m[name] = value

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def _make_comment(author_name: str, body: str, comment_id: str = "cid-1", created: str | None = None):
    c = MagicMock()
    c.body = body
    c.id = comment_id
    c.created = created
    c.author = SimpleNamespace(
        name=author_name,
        displayName=author_name.capitalize(),
        emailAddress=f"{author_name}@example.com",
        accountId=None,
        key=None,
    )
    return c


def _extract_j2o_payload(script: str) -> list[dict]:
    """Pull the JSON embedded between the ``J2O_DATA`` heredoc markers."""
    start = script.find("\n", script.find("J2O_DATA")) + 1
    end = script.find("J2O_DATA", start)
    return json.loads(script[start:end].strip())


_JIRA_CREATED = "2023-10-15T10:00:00.000+0000"


# ---------------------------------------------------------------------------
# 1. _collect_content_for_issue captures the comment date
# ---------------------------------------------------------------------------


class TestCollectCapturesCommentDate:
    def test_created_at_captured_from_comment(self, tmp_path: Path, _mock_mappings: None) -> None:
        mig = _build_mig(tmp_path)
        issue = MagicMock()
        issue.key = "PROJ-1"
        issue.fields.description = None
        issue.raw = {"fields": {}}
        mig.jira_client.jira.comments.return_value = [
            _make_comment("alice", "First", comment_id="1", created=_JIRA_CREATED),
        ]
        mig.jira_client.get_issue_watchers.return_value = []

        collected = mig._collect_content_for_issue(issue, wp_id=5040)

        assert collected["comments"][0]["created_at"] == _JIRA_CREATED

    def test_created_at_none_when_comment_has_no_created(self, tmp_path: Path, _mock_mappings: None) -> None:
        mig = _build_mig(tmp_path)
        issue = MagicMock()
        issue.key = "PROJ-1"
        issue.fields.description = None
        issue.raw = {"fields": {}}
        comment = MagicMock(spec=["body", "author", "id"])
        comment.body = "No date"
        comment.id = "1"
        comment.author = SimpleNamespace(
            name="alice", displayName="Alice", emailAddress="a@e.com", accountId=None, key=None
        )
        mig.jira_client.jira.comments.return_value = [comment]
        mig.jira_client.get_issue_watchers.return_value = []

        collected = mig._collect_content_for_issue(issue, wp_id=1)

        assert collected["comments"][0]["created_at"] is None

    def test_datetime_created_is_coerced_to_str(self, tmp_path: Path, _mock_mappings: None) -> None:
        """The Jira SDK sometimes returns a ``datetime`` for ``comment.created``.
        It must be coerced to a string so the bulk JSON payload stays
        ``json.dumps``-serialisable (a raw datetime would raise TypeError).
        """
        mig = _build_mig(tmp_path)
        issue = MagicMock()
        issue.key = "PROJ-1"
        issue.fields.description = None
        issue.raw = {"fields": {}}
        dt = datetime(2023, 10, 15, 10, 0, 0, tzinfo=UTC)
        mig.jira_client.jira.comments.return_value = [
            _make_comment("alice", "First", comment_id="1", created=dt),
        ]
        mig.jira_client.get_issue_watchers.return_value = []

        collected = mig._collect_content_for_issue(issue, wp_id=5040)

        created_at = collected["comments"][0]["created_at"]
        assert isinstance(created_at, str), f"datetime must be coerced to str, got {type(created_at)}"
        # The whole comments payload must be JSON-serialisable (the bulk path).
        json.dumps(collected["comments"])


# ---------------------------------------------------------------------------
# 2. _bulk_process_collected_content forwards the comment date to the op client
# ---------------------------------------------------------------------------


class TestBulkProcessForwardsCommentDate:
    def test_created_at_forwarded(self, tmp_path: Path, _mock_mappings: None) -> None:
        mig = _build_mig(tmp_path)
        collected_items = [
            {
                "wp_id": 5040,
                "jira_key": "PROJ-1",
                "description_update": None,
                "custom_field_updates": {},
                "comments": [
                    {"comment": "First", "user_id": 201, "jira_comment_id": "1", "created_at": _JIRA_CREATED},
                ],
                "watchers": [],
            }
        ]
        mig.op_client.bulk_create_work_package_activities.return_value = {"created": 1, "skipped": 0, "failed": 0}

        mig._bulk_process_collected_content(collected_items)

        activities = mig.op_client.bulk_create_work_package_activities.call_args[0][0]
        assert activities[0]["created_at"] == _JIRA_CREATED


# ---------------------------------------------------------------------------
# 3. bulk_create_work_package_activities — date in payload + script back-dates journal
# ---------------------------------------------------------------------------


class TestBulkCreateBackdatesJournal:
    def _svc_capturing_script(self):
        from src.infrastructure.openproject.openproject_work_package_content_service import (
            OpenProjectWorkPackageContentService,
        )

        mock_client = MagicMock()
        mock_client.logger = MagicMock()
        captured: list[str] = []

        def capture(script: str):
            captured.append(script)
            return {"created": 1, "skipped": 0, "failed": 0, "success": True}

        mock_client.execute_query_to_json_file.side_effect = capture
        return OpenProjectWorkPackageContentService(mock_client), captured

    def test_created_at_in_json_payload(self) -> None:
        svc, captured = self._svc_capturing_script()
        svc.bulk_create_work_package_activities(
            [
                {
                    "work_package_id": 5040,
                    "comment": "Hi",
                    "user_id": 100,
                    "jira_comment_id": "1",
                    "created_at": _JIRA_CREATED,
                }
            ]
        )
        payload = _extract_j2o_payload(captured[0])
        assert payload[0]["created_at"] == _JIRA_CREATED

    def test_ruby_script_backdates_journal_created_at(self) -> None:
        source = inspect.getsource(
            __import__(
                "src.infrastructure.openproject.openproject_work_package_content_service",
                fromlist=["OpenProjectWorkPackageContentService"],
            ).OpenProjectWorkPackageContentService.bulk_create_work_package_activities
        )
        # The script must, after save!, set the new journal's created_at from the
        # comment date (guarded by presence of created_at).
        assert "created_at" in source, "bulk script never references created_at"
        assert "update_columns" in source, "bulk script must update_columns the journal's created_at"
        assert "Time.zone.parse" in source, "bulk script must parse the ISO comment date"


# ---------------------------------------------------------------------------
# 4. Single-comment path forwards + back-dates
# ---------------------------------------------------------------------------


class TestSingleCommentPathBackdates:
    def test_populate_comments_forwards_created_at(self, tmp_path: Path, _mock_mappings: None) -> None:
        mig = _build_mig(tmp_path)
        issue = MagicMock()
        issue.key = "PROJ-1"
        mig.jira_client.jira.comments.return_value = [
            _make_comment("alice", "First", comment_id="1", created=_JIRA_CREATED),
        ]
        # Avoid link-resolution surprises: return body unchanged.
        mig._convert_jira_links = lambda body, jira_key=None: body  # type: ignore[method-assign]

        mig._populate_comments(issue, wp_id=5040)

        activity = mig.op_client.create_work_package_activity.call_args[0][1]
        assert activity["created_at"] == _JIRA_CREATED

    def test_single_ruby_script_backdates_journal(self) -> None:
        source = inspect.getsource(
            __import__(
                "src.infrastructure.openproject.openproject_work_package_content_service",
                fromlist=["OpenProjectWorkPackageContentService"],
            ).OpenProjectWorkPackageContentService.create_work_package_activity
        )
        assert "created_at" in source, "single script never references created_at"
        assert "update_columns" in source, "single script must update_columns the journal's created_at"
        assert "Time.zone.parse" in source, "single script must parse the ISO comment date"
