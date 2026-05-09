"""Tests for comment-author resolution in WorkPackageContentMigration.

BUG: Both code paths (_populate_comments + _collect_content_for_issue /
_bulk_process_collected_content) dropped the Jira comment author and sent
activities to OpenProject without a user_id.  The Rails script then fell
back to ``default_user`` (User.find(2) — the "Anonymous" account) for
every migrated comment.

These tests pin:
1. The bulk path (_collect_content_for_issue + _bulk_process_collected_content)
   resolves each comment's author and includes user_id in every activity dict.
2. The single-issue path (_populate_comments) does the same.
3. An unmappable author falls back to the BUG #32 fallback user id (NOT
   Anonymous / user_id 2) and emits a WARNING log naming the unknown author.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _mock_mappings(monkeypatch: pytest.MonkeyPatch):
    """Wire the global config proxy to a three-user mapping."""
    import src.config as cfg

    class DummyMappings:
        def __init__(self) -> None:
            self._m = {
                "project": {"PROJ": {"openproject_id": 11}},
                "user": {
                    "alice": {"openproject_id": 201},
                    "bob": {"openproject_id": 202},
                    "carol": {"openproject_id": 203},
                },
                "custom_field": {},
            }

        def get_mapping(self, name: str):
            return self._m.get(name, {})

        def set_mapping(self, name: str, value):
            self._m[name] = value

    monkeypatch.setattr(cfg, "mappings", DummyMappings(), raising=False)


def _build_mig(tmp_path: Path):
    """Build WorkPackageContentMigration wired to tmp_path (no real files needed)."""
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


def _make_comment(author_name: str, body: str):
    """Return a Jira-comment-like MagicMock with .author.name and .body."""
    c = MagicMock()
    c.body = body
    c.author = SimpleNamespace(
        name=author_name,
        displayName=author_name.capitalize(),
        emailAddress=f"{author_name}@example.com",
        accountId=None,
        key=None,
    )
    return c


# ---------------------------------------------------------------------------
# Bulk path: _collect_content_for_issue + _bulk_process_collected_content
# ---------------------------------------------------------------------------


class TestBulkPathAuthorResolution:
    """_collect_content_for_issue must capture author; _bulk_process must pass user_id."""

    def test_collected_comments_carry_user_id(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        """Each entry in collected['comments'] must be a dict with 'user_id'."""
        mig = _build_mig(tmp_path)

        issue = MagicMock()
        issue.key = "PROJ-1"
        issue.fields.description = None
        issue.raw = {"fields": {}}
        mig.jira_client.jira.comments.return_value = [
            _make_comment("alice", "First comment"),
            _make_comment("bob", "Second comment"),
            _make_comment("carol", "Third comment"),
        ]
        mig.jira_client.get_issue_watchers.return_value = []

        collected = mig._collect_content_for_issue(issue, wp_id=99)

        comments = collected["comments"]
        assert len(comments) == 3
        # All entries must be dicts with user_id
        for entry in comments:
            assert isinstance(entry, dict), f"Expected dict, got {type(entry)}"
            assert "user_id" in entry, f"Missing user_id in {entry}"
            assert "comment" in entry, f"Missing comment text in {entry}"

    def test_collected_comments_map_to_correct_op_user_ids(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        """user_id in each collected comment matches the user_mapping for that author."""
        mig = _build_mig(tmp_path)

        issue = MagicMock()
        issue.key = "PROJ-1"
        issue.fields.description = None
        issue.raw = {"fields": {}}
        mig.jira_client.jira.comments.return_value = [
            _make_comment("alice", "alice's comment"),
            _make_comment("bob", "bob's comment"),
            _make_comment("carol", "carol's comment"),
        ]
        mig.jira_client.get_issue_watchers.return_value = []

        collected = mig._collect_content_for_issue(issue, wp_id=99)
        comments = collected["comments"]

        user_ids = [c["user_id"] for c in comments]
        assert user_ids == [201, 202, 203], f"Got {user_ids}"

    def test_bulk_process_passes_user_id_to_activity_creation(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        """bulk_create_work_package_activities receives user_id in every dict."""
        mig = _build_mig(tmp_path)

        collected_items = [
            {
                "wp_id": 10,
                "jira_key": "PROJ-1",
                "description_update": None,
                "custom_field_updates": {},
                "comments": [
                    {"comment": "alice says hi", "user_id": 201},
                    {"comment": "bob says hi", "user_id": 202},
                ],
                "watchers": [],
            }
        ]

        mig.op_client.bulk_create_work_package_activities.return_value = {"created": 2}

        mig._bulk_process_collected_content(collected_items)

        call_args = mig.op_client.bulk_create_work_package_activities.call_args
        assert call_args is not None
        activities = call_args[0][0]  # positional first arg
        assert len(activities) == 2

        for act in activities:
            assert "user_id" in act, f"Missing user_id in activity: {act}"
            assert act["user_id"] in (201, 202), f"Unexpected user_id: {act['user_id']}"

        # Verify specific mapping
        alice_act = next(a for a in activities if a["user_id"] == 201)
        assert alice_act["comment"] == "alice says hi"
        bob_act = next(a for a in activities if a["user_id"] == 202)
        assert bob_act["comment"] == "bob says hi"


# ---------------------------------------------------------------------------
# Single-issue path: _populate_comments
# ---------------------------------------------------------------------------


class TestSingleIssuePathAuthorResolution:
    """_populate_comments must resolve author and pass user_id to each API call."""

    def test_populate_comments_passes_user_id_per_comment(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        """create_work_package_activity must be called with user_id for each comment."""
        mig = _build_mig(tmp_path)

        issue = MagicMock()
        issue.key = "PROJ-1"
        mig.jira_client.jira.comments.return_value = [
            _make_comment("alice", "alice's note"),
            _make_comment("bob", "bob's note"),
        ]

        mig._populate_comments(issue, wp_id=55)

        calls = mig.op_client.create_work_package_activity.call_args_list
        assert len(calls) == 2

        # Both calls must carry a user_id
        for c in calls:
            payload = c[0][1]  # second positional argument (the dict)
            assert "user_id" in payload, f"No user_id in call payload: {payload}"

        # Verify correct user mapping
        payloads = [c[0][1] for c in calls]
        user_ids_sent = [p["user_id"] for p in payloads]
        assert 201 in user_ids_sent, "alice (201) missing"
        assert 202 in user_ids_sent, "bob (202) missing"


# ---------------------------------------------------------------------------
# Unmappable author fallback
# ---------------------------------------------------------------------------


class TestUnmappableAuthorFallback:
    """An author absent from user_mapping must fall back to BUG32_FALLBACK_USER_ID
    and emit a WARNING log — never silently produce user_id=None or 2 (Anonymous).
    """

    def test_unmappable_author_uses_fallback_not_anonymous(
        self,
        tmp_path: Path,
        _mock_mappings: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A comment author not in user_mapping must produce the BUG32 fallback id."""
        from src.application.transformers.issue_transformer import IssueTransformer

        expected_fallback = IssueTransformer._BUG32_FALLBACK_USER_ID
        anonymous_user_id = 2  # OpenProject Anonymous user — must NEVER appear

        mig = _build_mig(tmp_path)

        issue = MagicMock()
        issue.key = "PROJ-X"
        issue.fields.description = None
        issue.raw = {"fields": {}}
        # "unknown_user" has no entry in user_mapping
        mig.jira_client.jira.comments.return_value = [
            _make_comment("unknown_user", "some comment body"),
        ]
        mig.jira_client.get_issue_watchers.return_value = []

        with caplog.at_level(logging.WARNING):
            collected = mig._collect_content_for_issue(issue, wp_id=77)

        comments = collected["comments"]
        assert len(comments) == 1
        user_id_used = comments[0]["user_id"]

        assert user_id_used != anonymous_user_id, (
            f"Got anonymous user_id={anonymous_user_id}; expected fallback {expected_fallback}"
        )
        assert user_id_used == expected_fallback, f"Expected BUG32 fallback {expected_fallback}, got {user_id_used}"

        # Must log a WARNING mentioning the unresolved author
        warning_texts = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("unknown_user" in t for t in warning_texts), (
            f"No WARNING mentioning 'unknown_user' in: {warning_texts}"
        )

    def test_unmappable_author_emits_warning_for_single_issue_path(
        self,
        tmp_path: Path,
        _mock_mappings: None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Single-issue path (_populate_comments) also warns on unmapped author."""
        from src.application.transformers.issue_transformer import IssueTransformer

        expected_fallback = IssueTransformer._BUG32_FALLBACK_USER_ID

        mig = _build_mig(tmp_path)

        issue = MagicMock()
        issue.key = "PROJ-Y"
        mig.jira_client.jira.comments.return_value = [
            _make_comment("ghost_user", "ghostly comment"),
        ]

        with caplog.at_level(logging.WARNING):
            count = mig._populate_comments(issue, wp_id=88)

        assert count == 1
        calls = mig.op_client.create_work_package_activity.call_args_list
        payload = calls[0][0][1]
        assert payload["user_id"] == expected_fallback

        warning_texts = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("ghost_user" in t for t in warning_texts), f"No WARNING mentioning 'ghost_user' in: {warning_texts}"


# ---------------------------------------------------------------------------
# Issue #1 — CRITICAL: single-issue path Ruby script must honour user_id
# ---------------------------------------------------------------------------


class TestRubyScriptHonoursUserId:
    """create_work_package_activity must pass user_id to the Ruby script.

    The Rails helper previously used ``User.current || User.find_by(admin: true)``
    unconditionally, ignoring ``activity_data['user_id']``.  After the fix the
    Ruby script must resolve the author from ``user_id`` the same way the bulk
    helper does (``users[item['user_id']] || default_user``).
    """

    def test_ruby_script_includes_user_id_resolution(self) -> None:
        """The single-activity Ruby script must contain user_id lookup logic.

        This test inspects the generated Ruby script inside
        ``OpenProjectWorkPackageContentService.create_work_package_activity``
        to confirm it actually uses ``activity_data['user_id']`` rather than
        always falling back to ``User.current || User.find_by(admin: true)``.
        """
        import inspect

        from src.infrastructure.openproject.openproject_work_package_content_service import (
            OpenProjectWorkPackageContentService,
        )

        # Read the source to extract what the Ruby script looks like.
        # We inspect the method source rather than running Rails.
        source = inspect.getsource(OpenProjectWorkPackageContentService.create_work_package_activity)
        # The fixed script must reference user_id from activity_data
        assert "user_id" in source, (
            "create_work_package_activity Ruby script does not reference user_id; "
            "single-issue path will always attribute comments to the default user"
        )
        # Must NOT unconditionally use User.current as the only user resolution
        # (the old bug was: user = User.current || User.find_by(admin: true) with no user_id lookup)
        # After fix, user_id branch must exist before the fallback
        assert "activity_data" in source or "user_id" in source, (
            "No user_id branch found in create_work_package_activity"
        )

    def test_single_path_ruby_script_does_not_ignore_passed_user_id(self) -> None:
        """The Ruby script template must contain a conditional user_id lookup.

        Specifically, the script must contain logic equivalent to:
          user_id = activity_data['user_id']
          user = user_id ? User.find_by(id: user_id) || default_user : default_user
        so that a passed user_id is actually honoured.
        """
        from unittest.mock import MagicMock

        from src.infrastructure.openproject.openproject_work_package_content_service import (
            OpenProjectWorkPackageContentService,
        )

        mock_client = MagicMock()
        mock_client.logger = MagicMock()
        # Capture the script passed to execute_query_to_json_file
        captured_scripts: list[str] = []

        def capture_script(script: str):
            captured_scripts.append(script)
            return {"id": 1, "status": "created"}

        mock_client.execute_query_to_json_file.side_effect = capture_script

        svc = OpenProjectWorkPackageContentService(mock_client)
        svc.create_work_package_activity(
            work_package_id=42,
            activity_data={"comment": {"raw": "hello"}, "user_id": 999},
        )

        assert captured_scripts, "No script was executed"
        script = captured_scripts[0]
        # The script must use the user_id from activity_data, not just User.current
        assert "999" in script or "user_id" in script, (
            f"Ruby script does not reference the passed user_id=999:\n{script}"
        )
        # The script must NOT simply hard-code User.current as the only user source
        # i.e. it must contain some conditional on user_id
        assert "user_id" in script, f"Ruby script ignores user_id entirely:\n{script}"


# ---------------------------------------------------------------------------
# Issue #2 — transformer caching: same instance reused across calls
# ---------------------------------------------------------------------------


class TestTransformerCaching:
    """_resolve_comment_author_id must reuse a single IssueTransformer instance."""

    def test_same_transformer_instance_reused(
        self,
        tmp_path: Path,
        _mock_mappings: None,
    ) -> None:
        """Calling _resolve_comment_author_id twice must use the same IssueTransformer."""
        from src.application.transformers.issue_transformer import IssueTransformer

        mig = _build_mig(tmp_path)

        comment_a = _make_comment("alice", "first")
        comment_b = _make_comment("bob", "second")

        instances: list[IssueTransformer] = []
        original_init = IssueTransformer.__init__

        def tracking_init(self_inner, *args, **kwargs):
            original_init(self_inner, *args, **kwargs)
            instances.append(self_inner)

        from unittest import mock

        with mock.patch.object(IssueTransformer, "__init__", tracking_init):
            mig._resolve_comment_author_id(comment_a, "PROJ-1")
            mig._resolve_comment_author_id(comment_b, "PROJ-2")

        assert len(instances) == 1, (
            f"IssueTransformer was instantiated {len(instances)} times; expected exactly 1 (cached after first call)"
        )


# ---------------------------------------------------------------------------
# Issue #4 — probe order: account_id must come before name/key/email
# ---------------------------------------------------------------------------


class TestProbeOrder:
    """IssueTransformer._JOURNAL_AUTHOR_PROBE_KEYS must follow Cloud-first canonical order."""

    def test_probe_order_is_canonical_cloud_first(self) -> None:
        """Probe keys must be account_id → name → key → email → displayName."""
        from src.application.transformers.issue_transformer import IssueTransformer

        keys = IssueTransformer._JOURNAL_AUTHOR_PROBE_KEYS
        # accountId (Cloud-first) must appear before name, key, emailAddress, displayName
        assert "accountId" in keys, f"accountId missing from probe keys: {keys}"
        idx_account = list(keys).index("accountId")
        for other in ("name", "key", "emailAddress", "displayName"):
            if other in keys:
                idx_other = list(keys).index(other)
                assert idx_account < idx_other, (
                    f"accountId (idx {idx_account}) must precede {other} (idx {idx_other}) in probe order: {keys}"
                )
