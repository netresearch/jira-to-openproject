# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Work Package Content Migration - Phase 2 of two-phase migration.

This migration populates work package content using the complete Jira→OpenProject
mapping established in Phase 1 (work_packages_skeleton). With the complete mapping
available, all cross-references in descriptions and comments can be properly
resolved to OpenProject work package links.

Phase 2 populates:
- Descriptions (with full link resolution)
- Custom field values
- Journals/comments (with full link resolution)
- Watchers

Prerequisites:
- Phase 1 (work_packages_skeleton) must be complete
- work_package_mapping.json must exist with complete mapping

Usage:
    python -m src.main migrate --components work_packages_content

Phase 7g notes
--------------
The on-disk ``work_package_mapping.json`` produced by Phase 1 is the
sole source of truth for Jira→OpenProject WP id resolution here. Each
row is normalised through :meth:`WorkPackageMappingEntry.from_legacy`
when building the ``jira_key → openproject_id`` lookup used for link
rewriting. The mapping file is keyed by ``str(jira_id)`` outer with
the human-readable ``jira_key`` stored inside each value; legacy
bare-``int`` rows (which carry no recoverable ``jira_key``) are SKIPPED
to avoid emitting invalid downstream link targets — see PR #162 for
the equivalent fix in :class:`AttachmentsMigration`.

Watcher resolution probes ``user_mapping`` using the canonical
multi-identifier order — ``account_id`` → ``name`` → ``key`` →
``email_address`` → ``display_name`` — by parsing each Jira watcher
payload through :class:`JiraUser` at the boundary. This matches the
order used by :meth:`work_package_skeleton_migration._map_user` and
:meth:`category_defaults_migration._resolve_user_id`.

Custom-field id resolution (`mapping.get("openproject_id") if
isinstance(mapping, dict) else mapping`) is a *different*, orthogonal
``dict | int`` polymorphism on ``custom_field_mapping`` rows — not a
``wp_map`` ladder. Phase 7's scope is the ``wp_map`` ladder; the
custom_field_mapping shape is shared with several status / issue-type
mappings across the codebase and is deferred.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from src import config
from src.application.components import level_issues, list_field_options, written_values
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.application.components.custom_field_migration import TEMPO_ACCOUNT_CUSTOM_TYPE
from src.application.components.issue_security import (
    mapped_project_issues,
    permits_unrestricted_result,
    unrestricted_project_jql,
    warn_if_restricted_mappings,
)
from src.application.components.jira_field_values import without_empty_values
from src.application.components.work_package_migration import WorkPackageMigration
from src.application.transformers.issue_transformer import IssueTransformer
from src.domain.enums import JournalEntryType
from src.infrastructure.jira.issue_pagination import iter_issue_pages
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.models import ComponentResult, JiraUser, WorkPackageMappingEntry
from src.utils.markdown_converter import MarkdownConverter

if TYPE_CHECKING:
    from collections.abc import Iterator

    from jira.resources import Issue


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _iso_date(value: Any) -> str | None:
    """The calendar date of a Jira date or date-time value, as the source wrote it."""
    if value is None or value == "":
        return None
    text = value.isoformat() if isinstance(value, date) else str(value)
    match = _ISO_DATE.match(text.strip())
    return match.group(0) if match else None


def _coerce_comment_timestamp(value: Any) -> str | None:
    """Coerce a Jira comment timestamp to ``str`` while preserving ``None``.

    The Jira SDK sometimes returns ``datetime`` objects for ``comment.created``
    instead of ISO strings; a raw ``datetime`` would break ``json.dumps`` of the
    bulk activity payload.  Mirrors
    ``work_package_skeleton_migration._stringify_optional_timestamp``: ``None``
    and empty strings stay ``None`` (never the literal ``"None"``).
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return str(value)


def _target_field_id(mapping: Any) -> Any:
    """The OpenProject field a mapped Jira field writes to, or None when the content step writes none.

    The sprint and epic component owns sprint data (ADR-017).
    """
    if mapping is None or list_field_options.is_sprint_field(mapping):
        return None
    return mapping.get("openproject_id") if isinstance(mapping, dict) else mapping


@register_entity_types("work_packages_content")
class WorkPackageContentMigration(BaseMigration):
    """Phase 2: Populate work package content with link resolution.

    This migration requires the complete work_package_mapping.json from Phase 1
    to resolve all Jira issue references to OpenProject work package links.

    Additionally, if attachment_mapping.json exists (from attachments migration),
    attachment references in descriptions and comments will be converted to
    OpenProject API URLs (/api/v3/attachments/{id}/content).
    """

    _JIRA_KEY_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")

    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
    ) -> None:
        """Initialize the content migration."""
        super().__init__(jira_client, op_client)

        # File paths
        self.work_package_mapping_file = self.data_dir / self.WORK_PACKAGE_MAPPING_FILENAME
        self.attachment_mapping_file = self.data_dir / self.ATTACHMENT_MAPPING_FILENAME

        # Data storage
        self.work_package_mapping: dict[str, dict[str, Any]] = {}
        self.jira_key_to_wp_id: dict[str, int] = {}  # For quick link resolution
        self.attachment_mapping: dict[str, dict[str, int]] = {}  # {jira_key: {filename: op_attachment_id}}

        # Load mappings
        self._load_mappings()

        # Validate mapping exists
        if not self._load_work_package_mapping():
            self.logger.error(
                "Work package mapping not found. Run work_packages_skeleton first!",
            )

        # Load attachment mapping if available (from attachments migration)
        self._load_attachment_mapping()

        # Initialize markdown converter with complete mapping
        self._init_markdown_converter()

        self.logger.debug(
            "WorkPackageContentMigration initialized with %d work package mappings, %d attachment mappings",
            len(self.work_package_mapping),
            len(self.attachment_mapping),
        )

    def _load_mappings(self) -> None:
        """Load required mappings from config."""
        try:
            self.project_mapping = config.mappings.get_mapping("project") or {}
            self.user_mapping = config.mappings.get_mapping("user") or {}
            self.custom_field_mapping = config.mappings.get_mapping("custom_field") or {}
        except Exception as e:
            self.logger.warning("Failed to load mappings via config: %s", e)
            self.project_mapping = {}
            self.user_mapping = {}
            self.custom_field_mapping = {}

    def _load_work_package_mapping(self) -> bool:
        """Load the work package mapping from Phase 1.

        Returns:
            True if mapping was loaded successfully

        """
        if not self.work_package_mapping_file.exists():
            self.logger.error(
                "Mapping file not found: %s",
                self.work_package_mapping_file,
            )
            return False

        try:
            with self.work_package_mapping_file.open("r", encoding="utf-8") as f:
                self.work_package_mapping = json.load(f)

            # Build the canonical jira_key → openproject_id lookup by
            # normalising every row through WorkPackageMappingEntry.
            # Production rows are dict-shaped (jira_key + openproject_id);
            # bare-int legacy rows have no recoverable jira_key and are
            # SKIPPED here — emitting them as ``int → int`` link targets
            # would corrupt the rewritten descriptions / comments.
            # Mirrors the rule applied in :class:`AttachmentsMigration`
            # (see PR #162).
            self.jira_key_to_wp_id = {}
            for outer_key, raw_entry in self.work_package_mapping.items():
                if not isinstance(raw_entry, dict):
                    # Bare-int legacy rows have no recoverable Jira key.
                    continue
                inner_jira_key = raw_entry.get("jira_key")
                jira_key = str(inner_jira_key or outer_key)
                try:
                    entry = WorkPackageMappingEntry.from_legacy(jira_key, raw_entry)
                except ValueError:
                    continue
                self.jira_key_to_wp_id[jira_key] = int(entry.openproject_id)

            self.logger.info(
                "Loaded work package mapping: %d entries, %d key lookups",
                len(self.work_package_mapping),
                len(self.jira_key_to_wp_id),
            )
            return True

        except Exception as e:
            self.logger.error("Failed to load mapping: %s", e)
            return False

    def _load_attachment_mapping(self) -> bool:
        """Load the attachment mapping from attachments migration.

        The attachment mapping enables proper conversion of Jira attachment
        references to OpenProject API URLs in descriptions and comments.

        Returns:
            True if mapping was loaded successfully

        """
        if not self.attachment_mapping_file.exists():
            self.logger.info(
                "Attachment mapping file not found: %s (attachments may not have been migrated yet)",
                self.attachment_mapping_file,
            )
            return False

        try:
            with self.attachment_mapping_file.open("r") as f:
                self.attachment_mapping = json.load(f)

            total_attachments = sum(len(v) for v in self.attachment_mapping.values())
            self.logger.info(
                "Loaded attachment mapping: %d issues with %d attachments",
                len(self.attachment_mapping),
                total_attachments,
            )
            return True

        except Exception as e:
            self.logger.warning("Failed to load attachment mapping: %s", e)
            return False

    def _init_markdown_converter(self) -> None:
        """Initialize markdown converter with user, WP, and attachment mappings."""
        # Build user mapping for @mentions
        user_mapping = {}
        account_id_mapping = {}
        for username, user_dict in self.user_mapping.items():
            if not user_dict:
                continue
            op_login = user_dict.get("openproject_login")
            op_id = user_dict.get("openproject_id")
            op_user = op_login or (str(op_id) if op_id else None)
            if op_user:
                user_mapping[username] = op_user
                jira_account_id = user_dict.get("jira_account_id") or user_dict.get("jira_id")
                if jira_account_id:
                    account_id_mapping[jira_account_id] = op_user

        self.markdown_converter = MarkdownConverter(
            user_mapping=user_mapping,
            work_package_mapping=self.jira_key_to_wp_id,
            account_id_mapping=account_id_mapping,
            attachment_mapping=self.attachment_mapping,
        )

    def _get_projects_to_migrate(self) -> list[dict[str, Any]]:
        """Get list of Jira projects to migrate based on filter."""
        projects = self.jira_client.get_projects()

        project_filter = config.selected_jira_projects()
        if project_filter:
            projects = [p for p in projects if p.get("key", "").upper() in project_filter]
            self.logger.info(
                "Filtered to %d projects based on filter: %s",
                len(projects),
                project_filter,
            )

        return projects

    def iter_project_issues(self, project_key: str) -> Iterator[Issue]:
        """Generate issues for a project with full field expansion.

        Args:
            project_key: The Jira project key

        Yields:
            Individual Jira Issue objects with all fields

        """
        batch_size = config.migration_config.get("batch_size", 100)
        try:
            limit = int(os.environ["J2O_MAX_ISSUES"])
        except KeyError, ValueError:
            limit = None
        jql = f"{unrestricted_project_jql(project_key)} ORDER BY created ASC, key ASC"

        def fetch_page(offset: int, size: int) -> list[Issue]:
            return self.jira_client.jira.search_issues(
                jql,
                startAt=offset,
                maxResults=size,
                fields="*all",
                expand="renderedFields,changelog",
            )

        yield from filter(permits_unrestricted_result, iter_issue_pages(project_key, fetch_page, batch_size, limit))

    def _convert_jira_links(self, text: str | None, jira_key: str | None = None) -> str:
        """Convert Jira issue references to OpenProject WP links.

        Patterns converted:
        - PROJ-123 → WP#456 or [WP#456](../work_packages/456)
        - [PROJ-123] → [WP#456]
        - {PROJ-123} → WP#456
        - !image.png! → ![](/api/v3/attachments/{id}/content)

        Args:
            text: Text containing Jira references
            jira_key: Jira issue key for attachment resolution context

        Returns:
            Text with converted references

        """
        if not text:
            return ""

        # Use markdown converter for comprehensive conversion (including attachments)
        converted = self.markdown_converter.convert(text, jira_key=jira_key)

        # Additional pattern: bare Jira keys not caught by converter
        # Pattern: PROJECT-123 where PROJECT is uppercase letters
        jira_key_pattern = self._JIRA_KEY_PATTERN

        def replace_key(match: re.Match) -> str:
            jira_key = match.group(1)
            if jira_key in self.jira_key_to_wp_id:
                wp_id = self.jira_key_to_wp_id[jira_key]
                return f"WP#{wp_id}"
            return jira_key  # Keep original if not found

        return jira_key_pattern.sub(replace_key, converted)

    def _get_wp_id_for_issue(self, jira_issue: Issue) -> int | None:
        """Get OpenProject WP ID for a Jira issue.

        Args:
            jira_issue: The Jira issue

        Returns:
            OpenProject WP ID or None

        """
        jira_id = str(jira_issue.id)
        if jira_id in self.work_package_mapping:
            return self.work_package_mapping[jira_id].get("openproject_id")
        return None

    def _get_openproject_project_id(self, jira_project_key: str) -> int | None:
        """Get OpenProject project ID for a Jira project."""
        if jira_project_key in self.project_mapping:
            mapping = self.project_mapping[jira_project_key]
            if isinstance(mapping, dict):
                return mapping.get("openproject_id")
            return mapping
        return None

    def _populate_description(
        self,
        jira_issue: Issue,
        wp_id: int,
    ) -> bool:
        """Populate work package description with link resolution.

        Args:
            jira_issue: The Jira issue
            wp_id: OpenProject work package ID

        Returns:
            True if successful

        """
        description = getattr(jira_issue.fields, "description", None)
        if not description:
            return True  # Nothing to do

        # Convert description with link resolution and attachment URLs
        converted_description = self._convert_jira_links(description, jira_key=jira_issue.key)

        try:
            self.op_client.update_work_package(
                wp_id,
                {"description": {"raw": converted_description}},
            )
            return True
        except Exception as e:
            self.logger.debug("Failed to update description for WP#%d: %s", wp_id, e)
            return False

    def _mapped_field_value(self, mapping: Any, value: Any, issue_key: str) -> Any:
        """Prepare values according to their target format without changing selection labels."""
        field_format = mapping.get("openproject_type") if isinstance(mapping, dict) else None
        if isinstance(mapping, dict) and mapping.get("jira_custom_type") == TEMPO_ACCOUNT_CUSTOM_TYPE:
            if not isinstance(value, dict):
                return value
            # The account key is unique; account names are not.
            key = value.get("key")
            return key if isinstance(key, str) and key else {"unmapped_tempo_account": True}
        if field_format == "user":
            return self._mapped_user_field(value)
        if isinstance(value, str) and field_format in (None, "text", "string"):
            return self._convert_jira_links(value, jira_key=issue_key)
        return value

    def _populate_custom_fields(
        self,
        jira_issue: Issue,
        wp_id: int,
    ) -> bool:
        """Populate custom field values.

        Args:
            jira_issue: The Jira issue
            wp_id: OpenProject work package ID

        Returns:
            True if successful

        """
        # Get custom field values from Jira issue
        updates = self._custom_field_updates(jira_issue)

        if updates:
            try:
                self.op_client.update_work_package(wp_id, updates)
                return True
            except Exception as e:
                self.logger.debug("Failed to update custom fields for WP#%d: %s", wp_id, e)
                return False

        return True

    @property
    def _transformer(self) -> IssueTransformer:
        """Lazily-created :class:`IssueTransformer` bound to this owner.

        Mirrors :attr:`WorkPackageMigration._transformer` — the instance is
        created on first access and cached so the hot path inside
        ``_resolve_comment_author_id`` does not pay construction cost on
        every comment.
        """
        cached = getattr(self, "_transformer_cache", None)
        if cached is None:
            cached = IssueTransformer(owner=self)
            self._transformer_cache = cached
        return cached

    def _resolve_comment_author_id(
        self,
        comment: object,
        jira_key: str,
    ) -> int:
        """Resolve a Jira comment's author to an OpenProject user id.

        Delegates to :meth:`IssueTransformer.resolve_journal_author_id` using
        the canonical BUG #32 probe order (accountId → name → key →
        emailAddress → displayName).  Returns the BUG #32 fallback user id
        and emits a WARNING when the author cannot be mapped so operators can
        audit unresolved authors from production logs.

        Args:
            comment: A Jira comment object (from jira.comments()).
            jira_key: The Jira issue key — used in the warning message only.

        Returns:
            Resolved OpenProject user id (int).

        """
        author = getattr(comment, "author", None)
        if author is None:
            author_data: dict[str, object] = {}
        elif isinstance(author, dict):
            author_data = author
        else:
            author_data = {
                "accountId": getattr(author, "accountId", None),
                "name": getattr(author, "name", None),
                "key": getattr(author, "key", None),
                "emailAddress": getattr(author, "emailAddress", None),
                "displayName": getattr(author, "displayName", None),
            }
        return self._transformer.resolve_journal_author_id(
            author_data,
            jira_key,
            JournalEntryType.COMMENT,
        )

    def _collect_approved_comments(self, issue_key: str) -> tuple[list[dict[str, Any]], int]:
        selection = self.jira_client.select_comments_for_migration(issue_key)
        entries = [
            {
                "comment": self._convert_jira_links(comment.body, jira_key=issue_key),
                "user_id": self._resolve_comment_author_id(comment, issue_key),
                "jira_comment_id": getattr(comment, "id", None),
                "created_at": _coerce_comment_timestamp(getattr(comment, "created", None)),
            }
            for comment in selection.comments
            if getattr(comment, "body", None)
        ]
        return entries, selection.held

    def _populate_comments(self, jira_issue: Issue, wp_id: int) -> int:
        """Read and verify existing journals before adding an approved comment."""
        comments, held = self._collect_approved_comments(jira_issue.key)
        self.op_client.wp_content.verify_imported_comments({wp_id: comments})
        if held:
            self.logger.info("%s: %d comments retained at source", jira_issue.key, held)
        migrated = 0
        for row in comments:
            try:
                self.op_client.create_work_package_activity(wp_id, {**row, "comment": {"raw": row["comment"]}})
                migrated += 1
            except Exception as exc:
                self.logger.warning("Failed to create comment for WP#%d (%s)", wp_id, type(exc).__name__)
        return migrated

    def _populate_watchers(
        self,
        jira_issue: Issue,
        wp_id: int,
    ) -> int:
        """Populate work package watchers.

        Args:
            jira_issue: The Jira issue
            wp_id: OpenProject work package ID

        Returns:
            Number of watchers added

        """
        added = 0

        try:
            watchers = self.jira_client.get_issue_watchers(jira_issue.key)
        except Exception as exc:
            self.logger.warning("Could not read the watchers of %s (%s)", jira_issue.key, type(exc).__name__)
            raise

        for watcher in watchers:
            op_user_id = self._resolve_jira_user_id(watcher)
            if not op_user_id:
                continue
            try:
                self.op_client.add_watcher(wp_id, op_user_id)
                added += 1
            except Exception:
                pass

        return added

    def _resolve_jira_user_id(self, watcher: Any) -> int | None:
        """Resolve a Jira user from a watcher or custom field to an OpenProject user id.

        Phase 7g: parse the watcher payload at the boundary via
        :class:`JiraUser` and probe ``user_mapping`` using the canonical
        multi-identifier order — ``account_id`` (Cloud-first per
        repository convention) → ``name`` → ``key`` → ``email_address``
        → ``display_name``. Same probe order as
        :meth:`work_package_skeleton_migration._map_user` and
        :meth:`category_defaults_migration._resolve_user_id`.

        Args:
            watcher: The Jira watcher payload (dict from
                :meth:`JiraClient.get_issue_watchers`, or any
                attribute-bearing SDK object).

        Returns:
            OpenProject user ID or ``None`` if no probe matched.

        """
        if not watcher:
            return None
        try:
            user = JiraUser.from_dict(watcher) if isinstance(watcher, dict) else JiraUser.from_jira_obj(watcher)
        except ValidationError, TypeError, AttributeError:
            return None
        for probe in (
            user.account_id,
            user.name,
            user.key,
            user.email_address,
            user.display_name,
        ):
            if not probe:
                continue
            user_entry = self.user_mapping.get(probe)
            if not isinstance(user_entry, dict):
                continue
            raw_id = user_entry.get("openproject_id")
            if raw_id is None:
                continue
            try:
                return int(raw_id)
            except TypeError, ValueError:
                continue
        return None

    def _collect_content_for_issue(
        self,
        jira_issue: Issue,
        wp_id: int,
        *,
        watchers: bool = True,
    ) -> dict[str, Any]:
        """Collect all content updates for a work package without executing.

        Args:
            jira_issue: The Jira issue
            wp_id: OpenProject work package ID
            watchers: False for a level work package: a watcher need not be a reader (ADR-009)

        Returns:
            Dict with collected data for bulk operations

        """
        collected: dict[str, Any] = {
            "wp_id": wp_id,
            "jira_key": jira_issue.key,
            "description_update": None,
            "custom_field_updates": {},
            "comments": [],
            "watchers": [],
        }

        # Collect description
        # Store plain string - the batch_update_work_packages method passes directly to Ruby
        # which expects a plain string, not the {"raw": ...} format used by the API
        description = getattr(jira_issue.fields, "description", None)
        if description:
            converted_description = self._convert_jira_links(description, jira_key=jira_issue.key)
            collected["description_update"] = converted_description

        collected["custom_field_updates"] = self._custom_field_updates(jira_issue)
        collected["date_updates"] = self._date_updates(jira_issue)

        try:
            collected["comments"], collected["comments_held"] = self._collect_approved_comments(jira_issue.key)
        except Exception as exc:
            collected["comment_read_failed"] = True
            self.logger.warning("Incomplete Jira comment read for %s (%s)", jira_issue.key, type(exc).__name__)

        # Collect watchers; with J2O_SKIP_WATCHERS set they are neither read nor written.
        if not watchers or os.environ.get("J2O_SKIP_WATCHERS"):
            return collected
        try:
            for watcher in self.jira_client.get_issue_watchers(jira_issue.key):
                op_user_id = self._resolve_jira_user_id(watcher)
                if op_user_id:
                    collected["watchers"].append(op_user_id)
        except Exception as exc:
            # A failed read is no empty watcher list; it counts as failed.
            collected["watchers"] = []
            collected["watcher_read_failed"] = True
            self.logger.warning("Could not read the watchers of %s (%s)", jira_issue.key, type(exc).__name__)

        return collected

    def _bulk_process_collected_content(
        self,
        collected_items: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Process collected content using bulk operations.

        Args:
            collected_items: List of collected content from _collect_content_for_issue

        Returns:
            Dict with counts: descriptions_updated, custom_fields_updated, comments_migrated, watchers_added

        """
        results = {
            "descriptions_updated": 0,
            "descriptions_failed": 0,
            "descriptions_unchanged": 0,
            "descriptions_target_changed": 0,
            "custom_fields_unchanged": 0,
            "custom_fields_target_changed": 0,
            "dates_updated": 0,
            "dates_unchanged": 0,
            "dates_target_changed": 0,
            "dates_failed": 0,
            "target_changed": [],
            "custom_fields_updated": 0,
            "custom_fields_failed": 0,
            "comments_migrated": 0,
            "comments_failed": 0,
            "comments_held": sum(item.get("comments_held", 0) for item in collected_items),
            "comment_reads_failed": sum(bool(item.get("comment_read_failed")) for item in collected_items),
            "watcher_reads_failed": sum(bool(item.get("watcher_read_failed")) for item in collected_items),
            "watchers_added": 0,
        }

        self.op_client.wp_content.verify_imported_comments(
            {item["wp_id"]: item["comments"] for item in collected_items}
        )

        self._write_work_package_fields(collected_items, results)

        # Batch 3: Comments (using bulk_create_work_package_activities).
        # Each entry in item["comments"] carries comment text, user_id,
        # jira_comment_id and created_at. We forward all four: user_id preserves
        # authorship; jira_comment_id enables provenance-based idempotency so
        # re-runs skip already-migrated comments instead of duplicating them;
        # created_at back-dates the journal to the original Jira comment time (#260).
        self._write_approved_comments(collected_items, results)

        # Batch 4: Watchers (using bulk_add_watchers)
        self._write_watchers(collected_items, results)

        return results

    def _populate_content(
        self,
        jira_issue: Issue,
        wp_id: int,
    ) -> dict[str, Any]:
        """Populate all content for a work package (legacy single-issue method).

        Args:
            jira_issue: The Jira issue
            wp_id: OpenProject work package ID

        Returns:
            Results dictionary

        """
        results = {
            "description": False,
            "custom_fields": False,
            "comments_migrated": 0,
            "watchers_added": 0,
        }

        results["description"] = self._populate_description(jira_issue, wp_id)
        results["custom_fields"] = self._populate_custom_fields(jira_issue, wp_id)
        results["comments_migrated"] = self._populate_comments(jira_issue, wp_id)
        results["watchers_added"] = self._populate_watchers(jira_issue, wp_id)

        return results

    def _migrate_content(self) -> dict[str, Any]:
        """Migrate content for all work packages using bulk operations.

        Collects content updates in batches and executes bulk operations
        to minimize SSH/Rails overhead.

        Returns:
            Migration results dictionary

        """
        if not self.work_package_mapping:
            return {
                "status": "error",
                "error": "No work package mapping found. Run work_packages_skeleton first.",
                "total_processed": 0,
            }

        results = {
            "total_processed": 0,
            "total_updated": 0,
            "total_skipped": 0,
            "total_failed": 0,
            "descriptions_updated": 0,
            "descriptions_failed": 0,
            "descriptions_unchanged": 0,
            "descriptions_target_changed": 0,
            "custom_fields_unchanged": 0,
            "custom_fields_target_changed": 0,
            "dates_updated": 0,
            "dates_unchanged": 0,
            "dates_target_changed": 0,
            "dates_failed": 0,
            "target_changed": [],
            "custom_fields_failed": 0,
            "comment_reads_failed": 0,
            "watcher_reads_failed": 0,
            "comments_held": 0,
            "custom_fields_updated": 0,
            "comments_migrated": 0,
            "comments_failed": 0,
            "watchers_added": 0,
            "projects": {},
        }

        scope = None
        if level_issues.enabled():
            scope = level_issues.LevelScope.load(config.mappings)
            level_map = config.mappings.get_mapping(level_issues.WORK_PACKAGE_MAPPING) or {}
            results["security_level"] = dict.fromkeys(("processed", "updated", "skipped", "refused"), 0)

        projects = self._get_projects_to_migrate()
        self.logger.info("Migrating content for %d projects (bulk mode)", len(projects))

        batch_size = config.migration_config.get("batch_size", 100)

        for project in projects:
            project_key = project.get("key")
            project_results = self._process_project_content(project_key, batch_size, results)
            if scope is not None:
                level_results = self._process_level_content(project_key, batch_size, results, scope, level_map)
                project_results["security_level"] = level_results
                for name, count in level_results.items():
                    results["security_level"][name] += count
                # Refusals count once, as failures; the other level counts join the project totals.
                for name in ("processed", "updated", "skipped"):
                    project_results[name] += level_results[name]
                results["total_failed"] += level_results["refused"]

            # Aggregate
            results["projects"][project_key] = project_results
            results["total_processed"] += project_results["processed"]
            results["total_updated"] += project_results["updated"]
            results["total_skipped"] += project_results["skipped"]
            results["total_failed"] += project_results["failed"]

            self.logger.info(
                "Project %s: %d processed, %d updated, %d skipped, %d failed",
                project_key,
                project_results["processed"],
                project_results["updated"],
                project_results["skipped"],
                project_results["failed"],
            )

        self.logger.success(
            "Content migration complete: %d updated, %d skipped, %d failed",
            results["total_updated"],
            results["total_skipped"],
            results["total_failed"],
        )
        self.logger.info(
            "Details: %d descriptions, %d custom fields, %d comments, %d watchers",
            results["descriptions_updated"],
            results["custom_fields_updated"],
            results["comments_migrated"],
            results["watchers_added"],
        )
        if results["comments_held"]:
            self.logger.warning(
                "%d Jira comments retained at source to preserve their audience", results["comments_held"]
            )
        if results["comments_failed"]:
            self.logger.warning(
                "Content migration dropped %d comment(s) across all projects — these were NOT "
                "migrated (#260). Review the per-batch bulk-comment warnings above and re-run.",
                results["comments_failed"],
            )

        return results

    def run(self) -> ComponentResult:
        """Run the content migration.

        Returns:
            ComponentResult with migration status

        """
        start_time = datetime.now(tz=UTC)

        # Validate prerequisites
        if not self.work_package_mapping:
            return ComponentResult(
                status="error",
                success=False,
                error="Work package mapping not found. Run work_packages_skeleton first!",
                timestamp=datetime.now(tz=UTC).isoformat(),
            )

        try:
            migration_results = self._migrate_content()
            end_time = datetime.now(tz=UTC)
            duration_seconds = (end_time - start_time).total_seconds()

            failures = sum(
                migration_results.get(key, 0)
                for key in (
                    "total_failed",
                    "descriptions_failed",
                    "custom_fields_failed",
                    "dates_failed",
                    "comments_failed",
                    "comment_reads_failed",
                    "watcher_reads_failed",
                )
            )
            return ComponentResult(
                status="error" if failures else "success",
                success=failures == 0,
                failed=failures,
                error=f"{failures} content reads or writes failed. Review the batch diagnostics." if failures else None,
                timestamp=end_time.isoformat(),
                start_time=start_time.isoformat(),
                duration_seconds=duration_seconds,
                data=migration_results,
                details={
                    "total_updated": migration_results["total_updated"],
                    "total_skipped": migration_results["total_skipped"],
                    "total_failed": migration_results["total_failed"],
                    "descriptions_updated": migration_results["descriptions_updated"],
                    "comments_migrated": migration_results["comments_migrated"],
                    "comments_held": migration_results.get("comments_held", 0),
                    "comments_failed": migration_results.get("comments_failed", 0),
                    "comment_reads_failed": migration_results.get("comment_reads_failed", 0),
                    "watcher_reads_failed": migration_results.get("watcher_reads_failed", 0),
                    "descriptions_failed": migration_results.get("descriptions_failed", 0),
                    "custom_fields_failed": migration_results.get("custom_fields_failed", 0),
                    "custom_fields_updated": migration_results.get("custom_fields_updated", 0),
                    "dates_updated": migration_results.get("dates_updated", 0),
                    "dates_failed": migration_results.get("dates_failed", 0),
                    # Targets changed in OpenProject since J2O wrote them; not written (ADR-013).
                    "target_changed": migration_results.get("target_changed", []),
                }
                | (
                    {"security_level": migration_results["security_level"]}
                    if "security_level" in migration_results
                    else {}
                ),
            )
        except Exception as e:
            end_time = datetime.now(tz=UTC)
            duration_seconds = (end_time - start_time).total_seconds()
            self.logger.exception("Content migration failed: %s", e)
            return ComponentResult(
                status="error",
                success=False,
                error=str(e),
                timestamp=end_time.isoformat(),
                start_time=start_time.isoformat(),
                duration_seconds=duration_seconds,
            )

    def _mapped_user_field(self, value: Any) -> Any:
        """Map individual Jira identities before writing an OpenProject user field."""
        # Never use a numeric Jira user ID as an OpenProject user ID.
        users = value if isinstance(value, list) else [value]
        ids = []
        for user in users:
            payload = {"name": user, "key": user} if isinstance(user, str) else user
            op_id = self._resolve_jira_user_id(payload)
            if op_id is None:
                return {"unmapped_jira_user": True}
            ids.append(op_id)
        return ids if isinstance(value, list) else ids[0]

    def _custom_field_updates(self, jira_issue: Issue) -> dict:
        """Project the issue values that have a target custom field."""
        raw_fields = getattr(jira_issue, "raw", {}).get("fields", {})
        updates = {}

        for jira_field_id, raw_value in raw_fields.items():
            if not jira_field_id.startswith("customfield_"):
                continue
            value = without_empty_values(raw_value)
            if value is None:
                continue

            mapping = self.custom_field_mapping.get(jira_field_id)
            op_cf_id = _target_field_id(mapping)
            if op_cf_id:
                updates[f"customField{op_cf_id}"] = self._mapped_field_value(mapping, value, jira_issue.key)
        return updates

    def _date_updates(self, jira_issue: Issue) -> dict[str, str]:
        """The Jira due date and the first configured start date field, as OpenProject dates.

        A due date before the start date is left out, because OpenProject's
        ``work_packages_due_larger_start_date`` constraint refuses it.
        """
        fields = getattr(jira_issue, "fields", None)
        raw = getattr(jira_issue, "raw", {}).get("fields", {})
        dates: dict[str, str] = {}
        if due := _iso_date(getattr(fields, "duedate", None) or raw.get("duedate")):
            dates["due_date"] = due
        for field_id in WorkPackageMigration.start_date_field_ids():
            if start := _iso_date(getattr(fields, field_id, None) or raw.get(field_id)):
                dates["start_date"] = start
                break
        if "start_date" in dates and dates.get("due_date", "9999") < dates["start_date"]:
            del dates["due_date"]
        return dates

    def _write_work_package_fields(self, collected_items: list, results: dict) -> None:
        """Write descriptions and field changes, counting each acknowledged row."""
        descriptions = [
            {"id": item["wp_id"], "description": item["description_update"]}
            for item in collected_items
            if item["description_update"]
        ]
        # An unmapped selection or reference must not roll back the other
        # fields on the same issue. All rows still travel in one Rails call.
        fields = [
            {"id": item["wp_id"], key: value}
            for item in collected_items
            for key, value in item["custom_field_updates"].items()
        ]
        # A list field gets the options its rows name before they are written (ADR-017).
        fields, held_back = self._ensure_list_options(fields)
        # Each row is written only while its target holds what J2O wrote last (ADR-013).
        store = written_values.store()
        overwrite = written_values.overwrite_allowed()
        dates = [
            {"id": item["wp_id"], key: value}
            for item in collected_items
            for key, value in item.get("date_updates", {}).items()
        ]
        for kind, updates in (("descriptions", descriptions), ("custom_fields", fields), ("dates", dates)):
            if not updates:
                continue
            try:
                rows = written_values.guarded(updates, store, overwrite=overwrite)
                result = self.op_client.batch_update_work_packages(rows)
                outcome = written_values.settle(store, rows, result)
                results[f"{kind}_updated"] = outcome.updated
                results[f"{kind}_unchanged"] = outcome.unchanged
                results[f"{kind}_target_changed"] = len(outcome.changed)
                results["target_changed"] = [*results.get("target_changed", []), *outcome.changed]
                results[f"{kind}_failed"] = len(updates) - outcome.acknowledged
                if outcome.changed:
                    self.logger.warning(
                        "Skipped %d %s changed in OpenProject since J2O wrote them (work package:attribute): %s",
                        len(outcome.changed),
                        kind,
                        ", ".join(outcome.changed),
                    )
                if results[f"{kind}_failed"]:
                    errors = [row for row in result.get("results", []) if row.get("status") == "failed"]
                    self.logger.warning(
                        "Incomplete %s batch: %d failed. First failures: %s",
                        kind,
                        results[f"{kind}_failed"],
                        errors[:3],
                    )
            except Exception as exc:
                results[f"{kind}_failed"] = len(updates)
                self.logger.warning("Could not confirm %s writes: %s", kind, exc)
        results["custom_fields_failed"] = results.get("custom_fields_failed", 0) + held_back

    def _ensure_list_options(self, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        """Create the options the rows name; hold back the rows of every field not confirmed.

        Returns the rows to write and the number held back, which count as failed.
        """
        requests = list_field_options.ensure_requests(rows, list_field_options.list_fields(self.custom_field_mapping))
        if not requests:
            return rows, 0
        confirmed: dict[int, Any] = {}
        try:
            answer = self.op_client.ensure_list_field_options(list(requests.values()))
            confirmed = {
                field_id: entry
                for field_id, entry in answer["fields"].items()
                if field_id in requests and field_id not in answer["errors"]
            }
            for field_id, message in answer["errors"].items():
                self.logger.error("List field %s refused its options: %s", field_id, message)
        except Exception as exc:
            self.logger.error("Could not ensure the options of list fields %s: %s", sorted(requests), exc)
        created = {field_id: len(entry.get("created") or []) for field_id, entry in confirmed.items()}
        if any(created.values()):
            self.logger.info("Created list field options (field id: count): %s", created)
        failed = sorted(set(requests) - set(confirmed))
        if not failed:
            return rows, 0
        kept = [row for row in rows if not any(list_field_options.field_id_of(key) in failed for key in row)]
        self.logger.error(
            "Not writing %d values of list fields %s: their options are not confirmed", len(rows) - len(kept), failed
        )
        return kept, len(rows) - len(kept)

    def _write_approved_comments(self, collected_items: list, results: dict) -> None:
        """Write approved comment rows and account for missing acknowledgments."""
        all_comments = []
        for item in collected_items:
            for comment_entry in item["comments"]:
                all_comments.append(
                    {
                        "work_package_id": item["wp_id"],
                        "comment": comment_entry["comment"],
                        "user_id": comment_entry["user_id"],
                        "jira_comment_id": comment_entry.get("jira_comment_id"),
                        # Forward the original Jira comment date (#260).
                        "created_at": comment_entry.get("created_at"),
                    },
                )

        if all_comments:
            try:
                result = self.op_client.bulk_create_work_package_activities(all_comments)
                results["comments_migrated"] = result.get("created", 0)
                # Surface the Rails-side failure count instead of discarding it —
                # previously only ``created`` was read, so dropped comments
                # (e.g. all but the first per WP) looked like a clean success (#260).
                failed = int(result.get("failed", 0) or 0)
                acknowledged = int(result.get("created", 0)) + int(result.get("skipped", 0))
                failed = max(failed, len(all_comments) - acknowledged, int(result.get("success") is False))
                results["comments_failed"] = failed
                if failed or result.get("success") is False:
                    self.logger.warning(
                        "Bulk comment creation: %d created, %d failed (success=%s) — "
                        "failed comments were dropped; check the OpenProject Rails errors. "
                        "First errors: %s",
                        result.get("created", 0),
                        failed,
                        result.get("success"),
                        (result.get("errors") or [])[:3],
                    )
            except Exception as e:
                # A thrown bulk call drops the whole batch of comments; record
                # and warn rather than swallowing it at debug level (#260).
                self.logger.warning("Bulk comment creation failed (%d comments dropped): %s", len(all_comments), e)
                results["comments_failed"] = len(all_comments)

    def _write_watchers(self, collected_items: list, results: dict) -> None:
        """Send optional watcher assignments to their bulk endpoint."""
        if os.environ.get("J2O_SKIP_WATCHERS"):
            self.logger.debug("Skipping watchers (J2O_SKIP_WATCHERS=1)")
        else:
            all_watchers = []
            for item in collected_items:
                for user_id in item["watchers"]:
                    all_watchers.append(
                        {
                            "work_package_id": item["wp_id"],
                            "user_id": user_id,
                        },
                    )

            if all_watchers:
                try:
                    result = self.op_client.bulk_add_watchers(all_watchers)
                    results["watchers_added"] = result.get("created", 0)
                except Exception as e:
                    self.logger.debug("Bulk watcher addition failed: %s", e)

    def _process_project_content(self, project_key: str, batch_size: int, results: dict) -> dict:
        """Collect and write all mapped issues of one project."""
        project_results = {
            "processed": 0,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
        }

        known = mapped_project_issues(project_key, self.work_package_mapping)
        warn_if_restricted_mappings(self.jira_client, project_key, known, self.logger)
        self.logger.info("Processing content for project %s", project_key)

        # Collect content in batches
        collected_batch: list[dict[str, Any]] = []

        for issue in self.iter_project_issues(project_key):
            project_results["processed"] += 1
            wp_id = self._get_wp_id_for_issue(issue)

            if not wp_id:
                project_results["skipped"] += 1
                self.logger.debug(
                    "No WP mapping for %s, skipping",
                    issue.key,
                )
                continue

            # Collect content for bulk processing
            collected = self._collect_content_for_issue(issue, wp_id)
            collected_batch.append(collected)

            # Process batch when full
            if len(collected_batch) >= batch_size:
                batch_results = self._bulk_process_collected_content(collected_batch)
                project_results["updated"] += len(collected_batch)
                for metric, count in batch_results.items():
                    results[metric] += count

                self.logger.info(
                    "  Bulk processed %d work packages for %s (total: %d)",
                    len(collected_batch),
                    project_key,
                    project_results["updated"],
                )
                collected_batch = []

        # Process remaining items in batch
        if collected_batch:
            batch_results = self._bulk_process_collected_content(collected_batch)
            project_results["updated"] += len(collected_batch)
            for metric, count in batch_results.items():
                results[metric] += count

            self.logger.info(
                "  Bulk processed %d work packages for %s (final batch)",
                len(collected_batch),
                project_key,
            )
        return project_results

    def _process_level_content(
        self, project_key: str, batch_size: int, results: dict, scope: level_issues.LevelScope, level_map: dict
    ) -> dict:
        """Write the content of the level work packages of one project (ADR-009, step 4).

        The selection is repeated on a light search. A selected issue is written only into the
        work package its sub-project holds for it right now (read live by J2O Origin Key), and
        only when its mapping entry, if any, names that work package, level and sub-project; a
        lost mapping is recovered from the live read, a moved work package is refused. Watchers
        are not collected (a watcher need not be a reader). Comments take the same path as for
        any issue (ADR-007).
        """
        counters = dict.fromkeys(("processed", "updated", "skipped", "refused"), 0)
        light = level_issues.iter_level_issues(self.jira_client, project_key, batch_size, "security")
        selection = scope.select(project_key, light)
        allowed = {str(issue.id): target for issue, target in selection.selected}
        refused = list(selection.refused)
        live = level_issues.LiveSubProjects(self.op_client, self.logger)
        collected_batch: list[dict[str, Any]] = []
        full = level_issues.iter_level_issues(
            self.jira_client, project_key, batch_size, "*all", "renderedFields,changelog"
        )
        for issue in full:
            entry = level_map.get(str(issue.id))
            target = allowed.get(str(issue.id))
            if target is None and entry is None:
                counters["skipped"] += 1
                continue
            counters["processed"] += 1
            wp_id, reason = self._level_content_target(issue, target, entry, live)
            if reason is not None:
                refused.append(f"{issue.key}: {reason}")
                continue
            collected_batch.append(self._collect_content_for_issue(issue, wp_id, watchers=False))
            if len(collected_batch) >= batch_size:
                self._write_level_batch(collected_batch, results, counters)
                collected_batch = []
        if collected_batch:
            self._write_level_batch(collected_batch, results, counters)
        for reason in refused:
            self.logger.error("Level work package content refused: %s", reason)
        counters["refused"] = len(refused)
        return counters

    def _level_content_target(
        self,
        issue: Issue,
        target: level_issues.LevelTarget | None,
        entry: dict | None,
        live: level_issues.LiveSubProjects,
    ) -> tuple[int, str | None]:
        """The work package to write for a level issue, or the reason it is refused."""
        if target is None or level_issues.issue_level_id(issue) != target.level_id:
            return 0, "not selected for the level sub-project of its work package"
        return live.held(issue.key, target, entry)

    def _write_level_batch(self, collected: list[dict[str, Any]], results: dict, counters: dict) -> None:
        for metric, count in self._bulk_process_collected_content(collected).items():
            results[metric] += count
        counters["updated"] += len(collected)
