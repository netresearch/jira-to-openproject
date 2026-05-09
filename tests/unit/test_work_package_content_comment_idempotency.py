"""Tests for idempotent comment creation via provenance marker.

Root cause: every re-run of work_packages_content added a fresh set of
comments to OpenProject work packages.  WP 5040 accumulated 12 journals
(8 Anonymous from broken May-7 runs + 4 correct ones) because
bulk_create_work_package_activities blindly INSERT-ed without checking
for prior existence.

Fix: embed ``<!-- j2o:jira-comment-id:{id} -->`` at the end of each
migrated comment body.  The Rails script pre-fetches existing markers and
skips any activity whose marker is already present.

Coverage:
1. _build_comment_with_marker — marker appended when id present, no-op otherwise.
2. bulk_create_work_package_activities — jira_comment_id forwarded in JSON payload.
3. bulk_create_work_package_activities — Ruby script contains Set-based marker
   dedup logic (static inspection).
4. _collect_content_for_issue — jira_comment_id captured from Jira comment.
5. _bulk_process_collected_content — jira_comment_id forwarded to bulk helper.
6. Single-issue _populate_comments — jira_comment_id forwarded to single helper.
7. create_work_package_activity — jira_comment_id forwarded to Ruby script.
8. fetch_migrated_comment_ids — returns set of (wp_id, jira_comment_id) pairs.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers shared across tests
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


def _make_comment(author_name: str, body: str, comment_id: str = "cid-1"):
    c = MagicMock()
    c.body = body
    c.id = comment_id
    c.author = SimpleNamespace(
        name=author_name,
        displayName=author_name.capitalize(),
        emailAddress=f"{author_name}@example.com",
        accountId=None,
        key=None,
    )
    return c


# ---------------------------------------------------------------------------
# 1. _build_comment_with_marker unit tests
# ---------------------------------------------------------------------------


class TestBuildCommentWithMarker:
    def test_marker_appended_when_id_present(self) -> None:
        from src.infrastructure.openproject.openproject_work_package_content_service import (
            _build_comment_with_marker,
        )

        result = _build_comment_with_marker("Hello world", "597766")
        assert "<!-- j2o:jira-comment-id:597766 -->" in result
        assert result.startswith("Hello world")

    def test_no_marker_when_id_is_none(self) -> None:
        from src.infrastructure.openproject.openproject_work_package_content_service import (
            _build_comment_with_marker,
        )

        result = _build_comment_with_marker("Hello world", None)
        assert result == "Hello world"
        assert "j2o:" not in result

    def test_no_marker_when_id_is_empty_string(self) -> None:
        from src.infrastructure.openproject.openproject_work_package_content_service import (
            _build_comment_with_marker,
        )

        result = _build_comment_with_marker("Hello world", "")
        assert result == "Hello world"

    def test_marker_on_new_line(self) -> None:
        """Marker must be on its own line, not inline with last word."""
        from src.infrastructure.openproject.openproject_work_package_content_service import (
            _build_comment_with_marker,
        )

        result = _build_comment_with_marker("Line one\nLine two", "42")
        lines = result.split("\n")
        assert any("<!-- j2o:jira-comment-id:42 -->" in line for line in lines)
        # Marker must not be on the same line as text content
        assert lines[-1].strip() == "<!-- j2o:jira-comment-id:42 -->"


# ---------------------------------------------------------------------------
# 2. bulk_create_work_package_activities — jira_comment_id in JSON payload
# ---------------------------------------------------------------------------


class TestBulkCreateActivitiesPayload:
    def test_jira_comment_id_forwarded_in_json_payload(self) -> None:
        """The JSON data sent to Rails must include jira_comment_id for each activity."""
        from unittest.mock import MagicMock

        from src.infrastructure.openproject.openproject_work_package_content_service import (
            OpenProjectWorkPackageContentService,
        )

        mock_client = MagicMock()
        mock_client.logger = MagicMock()
        captured_scripts: list[str] = []

        def capture_script(script: str):
            captured_scripts.append(script)
            return {"created": 1, "skipped": 0, "failed": 0, "success": True}

        mock_client.execute_query_to_json_file.side_effect = capture_script

        svc = OpenProjectWorkPackageContentService(mock_client)
        svc.bulk_create_work_package_activities(
            [
                {
                    "work_package_id": 5040,
                    "comment": "First comment",
                    "user_id": 100,
                    "jira_comment_id": "597766",
                }
            ]
        )

        assert captured_scripts, "No Rails script was generated"
        script = captured_scripts[0]

        # The JSON payload embedded in the script must contain the jira_comment_id
        # Extract JSON from script (between J2O_DATA markers)
        start = script.find("\n", script.find("J2O_DATA")) + 1
        end = script.find("J2O_DATA", start)
        payload_json = script[start:end].strip()
        payload = json.loads(payload_json)

        assert len(payload) == 1
        assert payload[0]["jira_comment_id"] == "597766"

    def test_null_jira_comment_id_when_not_provided(self) -> None:
        """When no jira_comment_id is supplied, null is sent (legacy path still works)."""
        from src.infrastructure.openproject.openproject_work_package_content_service import (
            OpenProjectWorkPackageContentService,
        )

        mock_client = MagicMock()
        mock_client.logger = MagicMock()
        captured_scripts: list[str] = []

        def capture_script(script: str):
            captured_scripts.append(script)
            return {"created": 1, "skipped": 0, "failed": 0, "success": True}

        mock_client.execute_query_to_json_file.side_effect = capture_script

        svc = OpenProjectWorkPackageContentService(mock_client)
        svc.bulk_create_work_package_activities([{"work_package_id": 1, "comment": "Legacy comment", "user_id": 5}])

        script = captured_scripts[0]
        start = script.find("\n", script.find("J2O_DATA")) + 1
        end = script.find("J2O_DATA", start)
        payload = json.loads(script[start:end].strip())
        assert payload[0]["jira_comment_id"] is None


# ---------------------------------------------------------------------------
# 3. Ruby script contains Set-based idempotency dedup logic
# ---------------------------------------------------------------------------


class TestBulkCreateRubyScriptIdempotency:
    def test_ruby_script_fetches_migrated_pairs(self) -> None:
        """The Ruby script must query existing Journal notes for the provenance marker."""
        import inspect

        from src.infrastructure.openproject.openproject_work_package_content_service import (
            OpenProjectWorkPackageContentService,
        )

        source = inspect.getsource(OpenProjectWorkPackageContentService.bulk_create_work_package_activities)
        # Must contain the pre-fetch query for existing markers
        assert "j2o:jira-comment-id" in source, "Ruby script does not search for existing provenance markers"
        # Must contain the idempotency skip check
        assert "migrated_pairs" in source, "Ruby script missing migrated_pairs set for idempotency check"
        assert "skipped" in source, "Ruby script must increment skipped counter for already-migrated comments"

    def test_ruby_script_uses_set_for_dedup(self) -> None:
        """The Ruby script must use a Set (not Array#include?) for O(1) lookups."""
        import inspect

        from src.infrastructure.openproject.openproject_work_package_content_service import (
            OpenProjectWorkPackageContentService,
        )

        source = inspect.getsource(OpenProjectWorkPackageContentService.bulk_create_work_package_activities)
        # Set.new must be used for the migrated_pairs collection
        assert "Set.new" in source, "Ruby script must use Set.new for O(1) idempotency lookups"

    def test_result_dict_includes_skipped_key(self) -> None:
        """bulk_create_work_package_activities must return a 'skipped' count."""
        from src.infrastructure.openproject.openproject_work_package_content_service import (
            OpenProjectWorkPackageContentService,
        )

        mock_client = MagicMock()
        mock_client.logger = MagicMock()
        mock_client.execute_query_to_json_file.return_value = {
            "created": 1,
            "skipped": 1,
            "failed": 0,
            "success": True,
        }

        svc = OpenProjectWorkPackageContentService(mock_client)
        result = svc.bulk_create_work_package_activities(
            [
                {"work_package_id": 1, "comment": "a", "user_id": 1, "jira_comment_id": "111"},
                {"work_package_id": 1, "comment": "a", "user_id": 1, "jira_comment_id": "111"},
            ]
        )
        assert "skipped" in result, f"Expected 'skipped' key in result: {result}"
        assert result["skipped"] == 1


# ---------------------------------------------------------------------------
# 4. _collect_content_for_issue — jira_comment_id captured
# ---------------------------------------------------------------------------


class TestCollectContentCapturesCommentId:
    def test_jira_comment_id_present_in_collected_comments(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        """Each collected comment must carry jira_comment_id from the Jira comment."""
        mig = _build_mig(tmp_path)

        issue = MagicMock()
        issue.key = "PROJ-1"
        issue.fields.description = None
        issue.raw = {"fields": {}}
        mig.jira_client.jira.comments.return_value = [
            _make_comment("alice", "First comment", comment_id="597766"),
            _make_comment("alice", "Second comment", comment_id="597767"),
        ]
        mig.jira_client.get_issue_watchers.return_value = []

        collected = mig._collect_content_for_issue(issue, wp_id=5040)
        comments = collected["comments"]

        assert len(comments) == 2
        assert comments[0]["jira_comment_id"] == "597766"
        assert comments[1]["jira_comment_id"] == "597767"

    def test_none_comment_id_when_jira_comment_has_no_id(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        """When comment has no .id attribute, jira_comment_id should be None."""
        mig = _build_mig(tmp_path)

        issue = MagicMock()
        issue.key = "PROJ-1"
        issue.fields.description = None
        issue.raw = {"fields": {}}

        # Comment without an id attribute
        comment = MagicMock(spec=["body", "author"])
        comment.body = "No id comment"
        comment.author = SimpleNamespace(
            name="alice",
            displayName="Alice",
            emailAddress="alice@example.com",
            accountId=None,
            key=None,
        )
        mig.jira_client.jira.comments.return_value = [comment]
        mig.jira_client.get_issue_watchers.return_value = []

        collected = mig._collect_content_for_issue(issue, wp_id=1)
        assert collected["comments"][0]["jira_comment_id"] is None


# ---------------------------------------------------------------------------
# 5. _bulk_process_collected_content — jira_comment_id forwarded to bulk helper
# ---------------------------------------------------------------------------


class TestBulkProcessForwardsCommentId:
    def test_jira_comment_id_in_activity_payload_sent_to_op(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        """_bulk_process_collected_content must forward jira_comment_id to the op_client call."""
        mig = _build_mig(tmp_path)

        collected_items = [
            {
                "wp_id": 5040,
                "jira_key": "PROJ-1",
                "description_update": None,
                "custom_field_updates": {},
                "comments": [
                    {"comment": "First", "user_id": 201, "jira_comment_id": "597766"},
                    {"comment": "Second", "user_id": 201, "jira_comment_id": "597767"},
                ],
                "watchers": [],
            }
        ]

        mig.op_client.bulk_create_work_package_activities.return_value = {
            "created": 2,
            "skipped": 0,
            "failed": 0,
        }

        mig._bulk_process_collected_content(collected_items)

        call_args = mig.op_client.bulk_create_work_package_activities.call_args
        assert call_args is not None
        activities = call_args[0][0]
        assert len(activities) == 2

        # Both entries must carry jira_comment_id
        assert activities[0]["jira_comment_id"] == "597766"
        assert activities[1]["jira_comment_id"] == "597767"

    def test_none_jira_comment_id_forwarded_when_absent(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        """When jira_comment_id is absent from collected entry, None is forwarded."""
        mig = _build_mig(tmp_path)

        collected_items = [
            {
                "wp_id": 1,
                "jira_key": "PROJ-1",
                "description_update": None,
                "custom_field_updates": {},
                "comments": [
                    # Legacy entry without jira_comment_id key
                    {"comment": "Legacy comment", "user_id": 201},
                ],
                "watchers": [],
            }
        ]

        mig.op_client.bulk_create_work_package_activities.return_value = {
            "created": 1,
            "skipped": 0,
            "failed": 0,
        }

        mig._bulk_process_collected_content(collected_items)

        activities = mig.op_client.bulk_create_work_package_activities.call_args[0][0]
        assert activities[0]["jira_comment_id"] is None


# ---------------------------------------------------------------------------
# 6. Single-issue _populate_comments — jira_comment_id forwarded
# ---------------------------------------------------------------------------


class TestPopulateCommentsForwardsCommentId:
    def test_jira_comment_id_in_single_activity_payload(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        """_populate_comments must include jira_comment_id in every create_work_package_activity call."""
        mig = _build_mig(tmp_path)

        issue = MagicMock()
        issue.key = "PROJ-1"
        mig.jira_client.jira.comments.return_value = [
            _make_comment("alice", "alice's note", comment_id="597766"),
        ]

        mig._populate_comments(issue, wp_id=5040)

        calls = mig.op_client.create_work_package_activity.call_args_list
        assert len(calls) == 1
        payload = calls[0][0][1]
        assert "jira_comment_id" in payload, f"jira_comment_id missing from payload: {payload}"
        assert payload["jira_comment_id"] == "597766"

    def test_multiple_comments_each_have_their_own_id(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        """Each comment gets its own distinct jira_comment_id."""
        mig = _build_mig(tmp_path)

        issue = MagicMock()
        issue.key = "PROJ-1"
        mig.jira_client.jira.comments.return_value = [
            _make_comment("alice", "comment one", comment_id="100"),
            _make_comment("alice", "comment two", comment_id="101"),
            _make_comment("alice", "comment three", comment_id="102"),
        ]

        mig._populate_comments(issue, wp_id=99)

        calls = mig.op_client.create_work_package_activity.call_args_list
        assert len(calls) == 3
        sent_ids = [c[0][1]["jira_comment_id"] for c in calls]
        assert sent_ids == ["100", "101", "102"]


# ---------------------------------------------------------------------------
# 7. create_work_package_activity — jira_comment_id forwarded to Ruby script
# ---------------------------------------------------------------------------


class TestSingleActivityRubyIdempotency:
    def test_marker_embedded_in_single_activity_ruby_script(self) -> None:
        """create_work_package_activity must embed the provenance marker in the Ruby script."""
        from src.infrastructure.openproject.openproject_work_package_content_service import (
            OpenProjectWorkPackageContentService,
        )

        mock_client = MagicMock()
        mock_client.logger = MagicMock()
        captured_scripts: list[str] = []

        def capture_script(script: str):
            captured_scripts.append(script)
            return {"id": 1, "status": "created"}

        mock_client.execute_query_to_json_file.side_effect = capture_script

        svc = OpenProjectWorkPackageContentService(mock_client)
        svc.create_work_package_activity(
            work_package_id=5040,
            activity_data={
                "comment": {"raw": "Hello, comment body"},
                "user_id": 100,
                "jira_comment_id": "597766",
            },
        )

        assert captured_scripts
        script = captured_scripts[0]
        # The provenance marker must appear in the Ruby script
        assert "j2o:jira-comment-id:597766" in script, f"Provenance marker not found in Ruby script:\n{script}"

    def test_idempotency_check_in_single_activity_ruby_script(self) -> None:
        """create_work_package_activity must include a skip-if-exists check in Ruby."""
        from src.infrastructure.openproject.openproject_work_package_content_service import (
            OpenProjectWorkPackageContentService,
        )

        mock_client = MagicMock()
        mock_client.logger = MagicMock()
        captured_scripts: list[str] = []

        def capture_script(script: str):
            captured_scripts.append(script)
            return {"id": 1, "status": "created"}

        mock_client.execute_query_to_json_file.side_effect = capture_script

        svc = OpenProjectWorkPackageContentService(mock_client)
        svc.create_work_package_activity(
            work_package_id=42,
            activity_data={
                "comment": {"raw": "idempotency test"},
                "user_id": 5,
                "jira_comment_id": "999",
            },
        )

        script = captured_scripts[0]
        # The script must contain a skip-if-exists guard
        assert "skipped" in script or "existing_journal" in script, (
            f"No idempotency guard found in single-activity Ruby script:\n{script}"
        )

    def test_no_idempotency_check_without_jira_comment_id(self) -> None:
        """When jira_comment_id is absent no idempotency guard should be injected."""
        from src.infrastructure.openproject.openproject_work_package_content_service import (
            OpenProjectWorkPackageContentService,
        )

        mock_client = MagicMock()
        mock_client.logger = MagicMock()
        captured_scripts: list[str] = []

        def capture_script(script: str):
            captured_scripts.append(script)
            return {"id": 1, "status": "created"}

        mock_client.execute_query_to_json_file.side_effect = capture_script

        svc = OpenProjectWorkPackageContentService(mock_client)
        svc.create_work_package_activity(
            work_package_id=42,
            activity_data={"comment": {"raw": "no id comment"}, "user_id": 5},
        )

        script = captured_scripts[0]
        assert "existing_journal" not in script, (
            "Idempotency guard should NOT be present when jira_comment_id is absent"
        )


# ---------------------------------------------------------------------------
# 8. fetch_migrated_comment_ids — returns set of (wp_id, jira_comment_id) pairs
# ---------------------------------------------------------------------------


class TestFetchMigratedCommentIds:
    def test_returns_empty_set_for_no_wp_ids(self) -> None:
        from src.infrastructure.openproject.openproject_work_package_content_service import (
            OpenProjectWorkPackageContentService,
        )

        mock_client = MagicMock()
        mock_client.logger = MagicMock()
        svc = OpenProjectWorkPackageContentService(mock_client)

        result = svc.fetch_migrated_comment_ids([])
        assert result == set()
        mock_client.execute_query_to_json_file.assert_not_called()

    def test_returns_pairs_from_rails_result(self) -> None:
        """fetch_migrated_comment_ids converts Rails [[wp_id, jira_id], ...] to set of tuples."""
        from src.infrastructure.openproject.openproject_work_package_content_service import (
            OpenProjectWorkPackageContentService,
        )

        mock_client = MagicMock()
        mock_client.logger = MagicMock()
        mock_client.execute_query_to_json_file.return_value = [
            [5040, "597766"],
            [5040, "597767"],
            [5041, "597770"],
        ]

        svc = OpenProjectWorkPackageContentService(mock_client)
        result = svc.fetch_migrated_comment_ids([5040, 5041])

        assert result == {(5040, "597766"), (5040, "597767"), (5041, "597770")}

    def test_returns_empty_set_on_rails_error(self) -> None:
        """On Rails failure, returns empty set (non-fatal; callers treat as 'none migrated yet')."""
        from src.infrastructure.openproject.openproject_work_package_content_service import (
            OpenProjectWorkPackageContentService,
        )

        mock_client = MagicMock()
        mock_client.logger = MagicMock()
        mock_client.execute_query_to_json_file.side_effect = RuntimeError("Rails down")

        svc = OpenProjectWorkPackageContentService(mock_client)
        result = svc.fetch_migrated_comment_ids([1, 2, 3])
        assert result == set()

    def test_ruby_script_queries_notes_column(self) -> None:
        """The Rails query must target the notes column with the j2o marker pattern."""
        import inspect

        from src.infrastructure.openproject.openproject_work_package_content_service import (
            OpenProjectWorkPackageContentService,
        )

        source = inspect.getsource(OpenProjectWorkPackageContentService.fetch_migrated_comment_ids)
        assert "j2o:jira-comment-id:" in source
        assert "notes" in source
        assert "pluck" in source
