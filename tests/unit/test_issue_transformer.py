"""Unit tests for :class:`IssueTransformer`.

These tests exercise the 16 pure-mapping methods extracted from
``WorkPackageMigration`` in Phase 1 of the decomposition (see
``claudedocs/refactoring/work-package-migration-decomposition-plan.md``).

The transformer is constructed against a lightweight stand-in owner
(``types.SimpleNamespace``) so that no Jira/OpenProject clients are
instantiated.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from src.application.transformers.issue_transformer import IssueTransformer
from src.domain.enums import JournalEntryType


class _RecordingLogger:
    """Drop-in logger replacement that records calls for inspection."""

    def __init__(self) -> None:
        self.debug_calls: list[str] = []
        self.info_calls: list[str] = []
        self.warning_calls: list[str] = []

    def debug(self, msg: object, *args: object, **kwargs: object) -> None:
        self.debug_calls.append(str(msg))

    def info(self, msg: object, *args: object, **kwargs: object) -> None:
        self.info_calls.append(str(msg))

    def warning(self, msg: object, *args: object, **kwargs: object) -> None:
        self.warning_calls.append(str(msg))


class _IdentityNormalizer:
    """Stand-in for ``EnhancedTimestampMigrator``: returns input unchanged."""

    def _normalize_timestamp(self, value: str | None) -> str | None:
        return value


class _RecordingMarkdownConverter:
    """Stand-in for ``MarkdownConverter`` returning configured mention ids."""

    def __init__(self, mention_ids: list[int] | None = None) -> None:
        self._mention_ids = mention_ids or []
        self.last_text: str | None = None

    def extract_mentioned_user_ids(self, text: str) -> list[int]:
        self.last_text = text
        return list(self._mention_ids)


def _make_owner(
    *,
    user_mapping: dict[str, Any] | None = None,
    issue_type_id_mapping: dict[str, Any] | None = None,
    issue_type_mapping: dict[str, Any] | None = None,
    status_mapping: dict[str, Any] | None = None,
    status_category_by_id: dict[str, dict[str, Any]] | None = None,
    status_category_by_name: dict[str, dict[str, Any]] | None = None,
    start_date_fields: list[str] | None = None,
    enhanced_timestamp_migrator: Any | None = None,
    markdown_converter: Any | None = None,
    mentioned_users_by_project: dict[int, set[int]] | None = None,
    current_project_id: int | None = None,
    version_resolver: Any | None = None,
    logger: Any | None = None,
) -> SimpleNamespace:
    """Build a minimal owner namespace exposing the live attributes."""
    owner = SimpleNamespace(
        user_mapping=user_mapping if user_mapping is not None else {},
        issue_type_id_mapping=issue_type_id_mapping if issue_type_id_mapping is not None else {},
        issue_type_mapping=issue_type_mapping if issue_type_mapping is not None else {},
        status_mapping=status_mapping if status_mapping is not None else {},
        status_category_by_id=status_category_by_id if status_category_by_id is not None else {},
        status_category_by_name=status_category_by_name if status_category_by_name is not None else {},
        start_date_fields=start_date_fields if start_date_fields is not None else [],
        enhanced_timestamp_migrator=enhanced_timestamp_migrator or _IdentityNormalizer(),
        markdown_converter=markdown_converter,
        _mentioned_users_by_project=mentioned_users_by_project if mentioned_users_by_project is not None else {},
        _current_project_id=current_project_id,
        _get_or_create_version=version_resolver or (lambda name, project_id: None),
        logger=logger or _RecordingLogger(),
    )
    return owner


# ---------------------------------------------------------------------- #
# resolve_journal_author_id (3 tests)
# ---------------------------------------------------------------------- #


@pytest.mark.unit
class TestResolveJournalAuthorId:
    def test_resolves_via_name_probe(self) -> None:
        owner = _make_owner(
            user_mapping={"jdoe": {"openproject_id": 42}},
        )
        transformer = IssueTransformer(owner=owner)

        result = transformer.resolve_journal_author_id(
            {"name": "jdoe", "displayName": "John Doe"},
            "ABC-1",
            JournalEntryType.COMMENT,
        )

        assert result == 42

    def test_resolves_via_email_probe_when_name_missing(self) -> None:
        owner = _make_owner(
            user_mapping={"john@example.com": {"openproject_id": 99}},
        )
        transformer = IssueTransformer(owner=owner)

        result = transformer.resolve_journal_author_id(
            {"emailAddress": "john@example.com"},
            "XYZ-9",
            JournalEntryType.CHANGELOG,
        )

        assert result == 99

    def test_falls_back_to_bug32_user_when_unresolved(self) -> None:
        recorded = _RecordingLogger()
        owner = _make_owner(user_mapping={}, logger=recorded)
        transformer = IssueTransformer(owner=owner)

        result = transformer.resolve_journal_author_id(
            {"name": "ghost"},
            "ABC-2",
            JournalEntryType.COMMENT,
        )

        assert result == IssueTransformer._BUG32_FALLBACK_USER_ID
        assert any("[BUG32]" in msg for msg in recorded.warning_calls)

    def test_tolerates_non_dict_author_payload(self) -> None:
        owner = _make_owner(user_mapping={})
        transformer = IssueTransformer(owner=owner)

        result = transformer.resolve_journal_author_id(
            None,  # type: ignore[arg-type]
            "ABC-3",
            JournalEntryType.COMMENT,
        )

        assert result == IssueTransformer._BUG32_FALLBACK_USER_ID


# ---------------------------------------------------------------------- #
# track_mentioned_users (3 tests)
# ---------------------------------------------------------------------- #


@pytest.mark.unit
class TestTrackMentionedUsers:
    def test_records_mentioned_ids_on_owner(self) -> None:
        converter = _RecordingMarkdownConverter(mention_ids=[7, 8])
        owner = _make_owner(markdown_converter=converter)
        transformer = IssueTransformer(owner=owner)

        transformer.track_mentioned_users("Hi [~jdoe]!", project_id=11)

        assert owner._mentioned_users_by_project == {11: {7, 8}}

    def test_noop_when_text_empty(self) -> None:
        converter = _RecordingMarkdownConverter(mention_ids=[1])
        owner = _make_owner(markdown_converter=converter)
        transformer = IssueTransformer(owner=owner)

        transformer.track_mentioned_users("", project_id=5)

        assert owner._mentioned_users_by_project == {}
        assert converter.last_text is None

    def test_noop_when_project_id_zero(self) -> None:
        converter = _RecordingMarkdownConverter(mention_ids=[1])
        owner = _make_owner(markdown_converter=converter)
        transformer = IssueTransformer(owner=owner)

        transformer.track_mentioned_users("hello", project_id=0)

        assert owner._mentioned_users_by_project == {}
        assert converter.last_text is None

    def test_swallows_extractor_exceptions(self) -> None:
        class _Boom:
            def extract_mentioned_user_ids(self, text: str) -> list[int]:
                msg = "kaboom"
                raise RuntimeError(msg)

        recorded = _RecordingLogger()
        owner = _make_owner(markdown_converter=_Boom(), logger=recorded)
        transformer = IssueTransformer(owner=owner)

        transformer.track_mentioned_users("anything", project_id=1)

        assert owner._mentioned_users_by_project == {}
        assert any("Error extracting" in m for m in recorded.debug_calls)


# ---------------------------------------------------------------------- #
# extract_final_workflow (3 tests)
# ---------------------------------------------------------------------- #


@pytest.mark.unit
class TestExtractFinalWorkflow:
    def test_returns_latest_workflow_to_string(self) -> None:
        owner = _make_owner()
        transformer = IssueTransformer(owner=owner)
        history_old = SimpleNamespace(
            created="2024-01-01T00:00:00",
            items=[SimpleNamespace(field="Workflow", toString="OldFlow")],
        )
        history_new = SimpleNamespace(
            created="2024-06-01T00:00:00",
            items=[SimpleNamespace(field="Workflow", toString="NewFlow")],
        )
        issue = SimpleNamespace(
            changelog=SimpleNamespace(histories=[history_old, history_new]),
        )

        assert transformer.extract_final_workflow(issue) == "NewFlow"

    def test_returns_none_when_no_changelog(self) -> None:
        owner = _make_owner()
        transformer = IssueTransformer(owner=owner)
        issue = SimpleNamespace(changelog=None)

        assert transformer.extract_final_workflow(issue) is None

    def test_ignores_non_workflow_items(self) -> None:
        owner = _make_owner()
        transformer = IssueTransformer(owner=owner)
        history = SimpleNamespace(
            created="2024-06-01T00:00:00",
            items=[SimpleNamespace(field="status", toString="Done")],
        )
        issue = SimpleNamespace(changelog=SimpleNamespace(histories=[history]))

        assert transformer.extract_final_workflow(issue) is None


# ---------------------------------------------------------------------- #
# parse_datetime (3 tests)
# ---------------------------------------------------------------------- #


@pytest.mark.unit
class TestParseDatetime:
    def test_parses_isoformat_with_z_suffix(self) -> None:
        result = IssueTransformer.parse_datetime("2024-07-01T12:34:56Z")

        assert result == datetime(2024, 7, 1, 12, 34, 56, tzinfo=UTC)

    def test_returns_naive_datetime_with_utc_tzinfo(self) -> None:
        naive = datetime(2024, 7, 1, 12, 0, 0)
        result = IssueTransformer.parse_datetime(naive)

        assert result is not None
        assert result.tzinfo is UTC

    def test_returns_none_for_garbage_input(self) -> None:
        assert IssueTransformer.parse_datetime("not a date") is None
        assert IssueTransformer.parse_datetime(None) is None
        assert IssueTransformer.parse_datetime(12345) is None

    def test_parses_jira_pattern(self) -> None:
        # Jira-style timestamp like ``2024-09-10T08:00:00.000+0200``
        result = IssueTransformer.parse_datetime("2024-09-10T08:00:00.000+0200")

        assert result is not None
        assert result.tzinfo is UTC


# ---------------------------------------------------------------------- #
# derive_snapshot_timestamp (3 tests)
# ---------------------------------------------------------------------- #


@pytest.mark.unit
class TestDeriveSnapshotTimestamp:
    def test_returns_latest_across_keys(self) -> None:
        owner = _make_owner()
        transformer = IssueTransformer(owner=owner)
        snapshot = [
            {"jira_migration_date": "2024-01-01T00:00:00Z"},
            {"updated_at": "2024-06-15T00:00:00Z"},
            {"updated_at_utc": "2024-03-01T00:00:00Z"},
        ]

        result = transformer.derive_snapshot_timestamp(snapshot)

        assert result == datetime(2024, 6, 15, 0, 0, 0, tzinfo=UTC)

    def test_returns_none_for_empty_snapshot(self) -> None:
        owner = _make_owner()
        transformer = IssueTransformer(owner=owner)

        assert transformer.derive_snapshot_timestamp([]) is None
        assert transformer.derive_snapshot_timestamp(None) is None  # type: ignore[arg-type]

    def test_skips_non_dict_entries(self) -> None:
        owner = _make_owner()
        transformer = IssueTransformer(owner=owner)
        snapshot: list[Any] = [
            "garbage",
            123,
            {"jira_migration_date": "2024-05-05T00:00:00Z"},
        ]

        result = transformer.derive_snapshot_timestamp(snapshot)

        assert result == datetime(2024, 5, 5, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------- #
# build_key_exclusion_clause (3 tests)
# ---------------------------------------------------------------------- #


@pytest.mark.unit
class TestBuildKeyExclusionClause:
    def test_returns_jql_clause_for_keys(self) -> None:
        result = IssueTransformer.build_key_exclusion_clause({"ABC-1", "ABC-2"})

        assert result == "key NOT IN (ABC-1,ABC-2)"

    def test_returns_none_for_empty_input(self) -> None:
        assert IssueTransformer.build_key_exclusion_clause(set()) is None

    def test_caps_at_200_keys(self) -> None:
        keys = {f"PROJ-{i}" for i in range(500)}

        result = IssueTransformer.build_key_exclusion_clause(keys)

        assert result is not None
        assert result.startswith("key NOT IN (")
        # 200 keys joined by commas
        assert result.count(",") == 199

    def test_strips_whitespace_and_dedups(self) -> None:
        result = IssueTransformer.build_key_exclusion_clause({"  ABC-1 ", "ABC-1", " "})

        assert result == "key NOT IN (ABC-1)"


# ---------------------------------------------------------------------- #
# resolve_start_date (3 tests)
# ---------------------------------------------------------------------- #


@pytest.mark.unit
class TestResolveStartDate:
    def test_uses_first_configured_field(self) -> None:
        owner = _make_owner(start_date_fields=["customfield_18690", "customfield_12590"])
        transformer = IssueTransformer(owner=owner)
        issue = SimpleNamespace(
            fields=SimpleNamespace(
                customfield_18690="2024-07-01T08:00:00+00:00",
                customfield_12590="2024-07-02T08:00:00+00:00",
            ),
            raw={
                "fields": {
                    "customfield_18690": "2024-07-01T08:00:00+00:00",
                    "customfield_12590": "2024-07-02T08:00:00+00:00",
                },
            },
        )

        assert transformer.resolve_start_date(issue) == "2024-07-01"

    def test_falls_back_to_secondary_field(self) -> None:
        owner = _make_owner(start_date_fields=["customfield_18690", "customfield_12590"])
        transformer = IssueTransformer(owner=owner)
        issue = SimpleNamespace(
            fields=SimpleNamespace(customfield_18690=None, customfield_12590="2024-08-05T08:00:00+00:00"),
            raw={"fields": {"customfield_18690": None, "customfield_12590": "2024-08-05T08:00:00+00:00"}},
        )

        assert transformer.resolve_start_date(issue) == "2024-08-05"

    def test_returns_none_when_no_data_anywhere(self) -> None:
        owner = _make_owner(start_date_fields=["customfield_18690"])
        transformer = IssueTransformer(owner=owner)
        issue: dict[str, Any] = {"fields": {"customfield_18690": None}}

        assert transformer.resolve_start_date(issue) is None


# ---------------------------------------------------------------------- #
# resolve_start_date_from_history (3 tests)
# ---------------------------------------------------------------------- #


@pytest.mark.unit
class TestResolveStartDateFromHistory:
    def test_returns_first_in_progress_transition(self) -> None:
        owner = _make_owner(
            status_category_by_id={"3": {"id": 4, "key": "indeterminate", "name": "In Progress"}},
            status_category_by_name={"in progress": {"id": 4, "key": "indeterminate", "name": "In Progress"}},
        )
        transformer = IssueTransformer(owner=owner)
        issue = {
            "changelog": {
                "histories": [
                    {
                        "created": "2024-09-10T08:00:00.000+0200",
                        "items": [{"field": "status", "to": "3", "toString": "In Progress"}],
                    },
                ],
            },
        }

        assert transformer.resolve_start_date_from_history(issue) == "2024-09-10"

    def test_returns_none_when_only_done_transitions(self) -> None:
        owner = _make_owner(
            status_category_by_id={"5": {"id": 5, "key": "done", "name": "Done"}},
            status_category_by_name={"done": {"id": 5, "key": "done", "name": "Done"}},
        )
        transformer = IssueTransformer(owner=owner)
        issue = {
            "changelog": {
                "histories": [
                    {
                        "created": "2024-09-10T08:00:00.000+0200",
                        "items": [{"field": "status", "to": "5", "toString": "Done"}],
                    },
                ],
            },
        }

        assert transformer.resolve_start_date_from_history(issue) is None

    def test_returns_none_when_no_changelog(self) -> None:
        owner = _make_owner()
        transformer = IssueTransformer(owner=owner)

        assert transformer.resolve_start_date_from_history({"changelog": {"histories": []}}) is None


# ---------------------------------------------------------------------- #
# get_attr (3 tests)
# ---------------------------------------------------------------------- #


@pytest.mark.unit
class TestGetAttr:
    def test_reads_dict_key(self) -> None:
        assert IssueTransformer.get_attr({"foo": "bar"}, "foo") == "bar"

    def test_reads_object_attribute(self) -> None:
        obj = SimpleNamespace(foo="baz")

        assert IssueTransformer.get_attr(obj, "foo") == "baz"

    def test_returns_none_for_missing_key(self) -> None:
        assert IssueTransformer.get_attr({"foo": 1}, "missing") is None
        assert IssueTransformer.get_attr(SimpleNamespace(foo=1), "missing") is None


# ---------------------------------------------------------------------- #
# process_changelog_item (3+ tests)
# ---------------------------------------------------------------------- #


@pytest.mark.unit
class TestProcessChangelogItem:
    def test_assignee_change_resolves_user_ids(self) -> None:
        owner = _make_owner(
            user_mapping={
                "alice": {"openproject_id": 1},
                "bob": {"openproject_id": 2},
            },
        )
        transformer = IssueTransformer(owner=owner)

        result = transformer.process_changelog_item(
            {
                "field": "assignee",
                "from": "alice",
                "to": "bob",
                "fromString": "Alice",
                "toString": "Bob",
            },
        )

        assert result == {"assigned_to_id": [1, 2]}

    def test_status_change_resolves_via_status_mapping(self) -> None:
        owner = _make_owner(
            status_mapping={
                "1": {"openproject_id": 100},
                "6": {"openproject_id": 600},
            },
        )
        transformer = IssueTransformer(owner=owner)

        result = transformer.process_changelog_item(
            {"field": "status", "from": "1", "to": "6"},
        )

        assert result == {"status_id": [100, 600]}

    def test_skips_unmapped_field(self) -> None:
        owner = _make_owner()
        transformer = IssueTransformer(owner=owner)

        # Sprint is in IGNORED_CHANGELOG_FIELDS at the service layer; here we
        # just confirm any field absent from the transformer's mapping table
        # returns None.
        assert transformer.process_changelog_item({"field": "Sprint", "from": "1", "to": "2"}) is None

    def test_skips_no_change_value(self) -> None:
        owner = _make_owner()
        transformer = IssueTransformer(owner=owner)

        result = transformer.process_changelog_item(
            {"field": "summary", "fromString": "Same", "toString": "Same"},
        )

        assert result is None

    def test_time_estimate_converts_seconds_to_hours(self) -> None:
        owner = _make_owner()
        transformer = IssueTransformer(owner=owner)

        result = transformer.process_changelog_item(
            {"field": "timeestimate", "from": "3600", "to": "7200"},
        )

        assert result == {"remaining_hours": [1.0, 2.0]}


# ---------------------------------------------------------------------- #
# extract_changelog_histories (3 tests)
# ---------------------------------------------------------------------- #


@pytest.mark.unit
class TestExtractChangelogHistories:
    def test_extracts_from_jira_issue_object(self) -> None:
        h = SimpleNamespace(items=[])
        issue = SimpleNamespace(changelog=SimpleNamespace(histories=[h]))

        assert IssueTransformer.extract_changelog_histories(issue) == [h]

    def test_extracts_from_raw_dict(self) -> None:
        issue = SimpleNamespace(
            changelog=None,
            raw={"changelog": {"histories": [{"items": []}]}},
        )

        result = IssueTransformer.extract_changelog_histories(issue)

        assert result == [{"items": []}]

    def test_extracts_from_dict_payload(self) -> None:
        issue = {"changelog": {"histories": [{"items": []}]}}

        assert IssueTransformer.extract_changelog_histories(issue) == [{"items": []}]

    def test_returns_empty_when_no_changelog(self) -> None:
        assert IssueTransformer.extract_changelog_histories({}) == []
        assert IssueTransformer.extract_changelog_histories(SimpleNamespace(changelog=None, raw=None)) == []


# ---------------------------------------------------------------------- #
# is_in_progress_category (3 tests)
# ---------------------------------------------------------------------- #


@pytest.mark.unit
class TestIsInProgressCategory:
    def test_matches_indeterminate_key(self) -> None:
        assert IssueTransformer.is_in_progress_category({"key": "indeterminate"}) is True

    def test_matches_in_progress_name_case_insensitive(self) -> None:
        assert IssueTransformer.is_in_progress_category({"name": "IN PROGRESS"}) is True

    def test_matches_jira_default_id_4(self) -> None:
        assert IssueTransformer.is_in_progress_category({"id": 4}) is True

    def test_does_not_match_done(self) -> None:
        assert IssueTransformer.is_in_progress_category({"key": "done", "name": "Done"}) is False

    def test_returns_false_for_empty_dict(self) -> None:
        assert IssueTransformer.is_in_progress_category({}) is False


# ---------------------------------------------------------------------- #
# extract_issue_meta (3 tests)
# ---------------------------------------------------------------------- #


@pytest.mark.unit
class TestExtractIssueMeta:
    def test_extracts_meta_from_jira_issue_object(self) -> None:
        owner = _make_owner()
        transformer = IssueTransformer(owner=owner)
        fields = SimpleNamespace(
            issuetype=SimpleNamespace(id="10001", name="Task"),
            status=SimpleNamespace(id="3", name="In Progress"),
            priority=SimpleNamespace(id="2", name="High"),
            reporter=SimpleNamespace(name="alice"),
            assignee=SimpleNamespace(name="bob"),
            created="2024-01-01T00:00:00Z",
            updated="2024-02-01T00:00:00Z",
            duedate=None,
            labels=["urgent", "security"],
            components=[SimpleNamespace(name="auth"), SimpleNamespace(name="api")],
            parent=SimpleNamespace(key="ABC-100"),
        )
        issue = SimpleNamespace(key="ABC-1", id="10001", fields=fields)

        meta = transformer.extract_issue_meta(issue)

        assert meta["jira_key"] == "ABC-1"
        assert meta["jira_id"] == "10001"
        assert meta["issuetype_name"] == "Task"
        assert meta["status_name"] == "In Progress"
        assert meta["labels"] == ["urgent", "security"]
        assert meta["components"] == ["auth", "api"]
        assert meta["parent_key"] == "ABC-100"

    def test_extracts_meta_from_dict_payload(self) -> None:
        owner = _make_owner()
        transformer = IssueTransformer(owner=owner)
        issue = {
            "key": "DEF-2",
            "id": "20002",
            "fields": {
                "issuetype": {"id": "1", "name": "Bug"},
                "status": {"id": "5", "name": "Closed"},
                "priority": {"id": "3", "name": "Medium"},
                "reporter": {"name": "carol"},
                "assignee": {"displayName": "Dan"},
                "created": "2024-01-01",
                "labels": ["x"],
                "components": [{"name": "core"}],
                "parent": {"key": "DEF-1"},
            },
        }

        meta = transformer.extract_issue_meta(issue)

        assert meta["jira_key"] == "DEF-2"
        assert meta["assignee"] == "Dan"
        assert meta["components"] == ["core"]
        assert meta["parent_key"] == "DEF-1"

    def test_never_raises_on_garbage(self) -> None:
        owner = _make_owner()
        transformer = IssueTransformer(owner=owner)

        # Both of these would historically blow up — meta extraction must
        # always succeed (returns whatever it could gather, possibly empty).
        result_int = transformer.extract_issue_meta(12345)
        result_str = transformer.extract_issue_meta("not an issue")

        assert isinstance(result_int, dict)
        assert isinstance(result_str, dict)


# ---------------------------------------------------------------------- #
# map_issue_type (3 tests)
# ---------------------------------------------------------------------- #


@pytest.mark.unit
class TestMapIssueType:
    def test_maps_via_id_mapping(self) -> None:
        owner = _make_owner(issue_type_id_mapping={"10001": 5})
        transformer = IssueTransformer(owner=owner)

        assert transformer.map_issue_type(type_id="10001") == 5

    def test_falls_back_to_issue_type_mapping_by_id(self) -> None:
        owner = _make_owner(
            issue_type_id_mapping={},
            issue_type_mapping={"10001": {"openproject_id": 7}},
        )
        transformer = IssueTransformer(owner=owner)

        assert transformer.map_issue_type(type_id="10001") == 7

    def test_defaults_to_one_when_unmapped_and_warns(self) -> None:
        recorded = _RecordingLogger()
        owner = _make_owner(logger=recorded)
        transformer = IssueTransformer(owner=owner)

        result = transformer.map_issue_type(type_id="999", type_name="Mystery")

        assert result == 1
        assert any("No mapping found for issue type Mystery" in m for m in recorded.warning_calls)

    def test_raises_when_neither_id_nor_name_provided(self) -> None:
        owner = _make_owner()
        transformer = IssueTransformer(owner=owner)

        with pytest.raises(ValueError, match="Either type_id or type_name"):
            transformer.map_issue_type()


# ---------------------------------------------------------------------- #
# map_status (3 tests)
# ---------------------------------------------------------------------- #


@pytest.mark.unit
class TestMapStatus:
    def test_maps_via_status_mapping(self) -> None:
        owner = _make_owner(status_mapping={"3": {"openproject_id": 13}})
        transformer = IssueTransformer(owner=owner)

        assert transformer.map_status(status_id="3") == 13

    def test_defaults_to_one_when_unmapped(self) -> None:
        recorded = _RecordingLogger()
        owner = _make_owner(logger=recorded)
        transformer = IssueTransformer(owner=owner)

        result = transformer.map_status(status_id="x", status_name="Mystery")

        assert result == 1
        assert any("No mapping found for status Mystery" in m for m in recorded.warning_calls)

    def test_raises_when_neither_id_nor_name_provided(self) -> None:
        owner = _make_owner()
        transformer = IssueTransformer(owner=owner)

        with pytest.raises(ValueError, match="Either status_id or status_name"):
            transformer.map_status()


# ---------------------------------------------------------------------- #
# sanitize_wp_dict (3 tests)
# ---------------------------------------------------------------------- #


@pytest.mark.unit
class TestSanitizeWpDict:
    def test_extracts_type_and_status_from_links(self) -> None:
        wp: dict[str, Any] = {
            "subject": "S",
            "_links": {
                "type": {"href": "/api/v3/types/7"},
                "status": {"href": "/api/v3/statuses/9"},
            },
        }

        IssueTransformer.sanitize_wp_dict(wp)

        assert wp["type_id"] == 7
        assert wp["status_id"] == 9
        assert "_links" not in wp

    def test_strips_non_ar_keys(self) -> None:
        wp: dict[str, Any] = {
            "subject": "S",
            "watcher_ids": [1, 2],
            "jira_id": "10001",
            "jira_key": "ABC-1",
            "type_name": "Task",
        }

        IssueTransformer.sanitize_wp_dict(wp)

        for k in ("watcher_ids", "jira_id", "jira_key", "type_name"):
            assert k not in wp

    def test_escapes_subject_and_description(self) -> None:
        wp: dict[str, Any] = {
            "subject": "with \"quote\" and 'apostrophe'",
            "description": 'desc with "x"',
        }

        IssueTransformer.sanitize_wp_dict(wp)

        assert '\\"' in wp["subject"]
        assert "\\'" in wp["subject"]
        assert '\\"' in wp["description"]


# ---------------------------------------------------------------------- #
# Lazy property smoke test on the migration class
# ---------------------------------------------------------------------- #


@pytest.mark.unit
def test_work_package_migration_delegates_via_lazy_transformer() -> None:
    """Tests using ``WorkPackageMigration.__new__`` (no ``__init__``) still work.

    Several existing test files instantiate the migration via ``__new__`` to
    bypass the heavyweight ``__init__``. The transformer must be created
    lazily on first access so those tests continue to call delegate methods
    like ``_sanitize_wp_dict`` and ``_resolve_start_date`` directly.
    """
    from src.application.components.work_package_migration import WorkPackageMigration

    migration = WorkPackageMigration.__new__(WorkPackageMigration)
    migration.logger = logging.getLogger("test")
    migration.start_date_fields = ["customfield_18690"]
    migration.enhanced_timestamp_migrator = _IdentityNormalizer()
    migration.status_category_by_id = {}
    migration.status_category_by_name = {}

    # Static delegate
    wp = {"subject": "S", "_links": {"type": {"href": "/api/v3/types/3"}}}
    migration._sanitize_wp_dict(wp)
    assert wp.get("type_id") == 3

    # Instance-bound delegate (accesses ``self.start_date_fields``)
    issue = {"fields": {"customfield_18690": "2024-07-01T00:00:00+00:00"}}
    assert migration._resolve_start_date(issue) == "2024-07-01"
