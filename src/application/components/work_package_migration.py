# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Migrate Jira issues to OpenProject work packages.

The legacy writer creates history-free issues. Use the skeleton and content
components and the protected comment archive for historical migrations.
"""

import contextlib
import json
import os
import sqlite3
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from jira import Issue
from src import config
from src.application.components.base_migration import BaseMigration, register_entity_types
from src.application.components.issue_security import (
    mapped_project_issues,
    permits_unrestricted_result,
    unrestricted_project_jql,
    warn_if_restricted_mappings,
)
from src.application.components.jira_field_values import without_empty_values
from src.application.components.work_package_files import sync_directory, write_issue_snapshot
from src.application.components.work_package_history import has_no_source_journals
from src.application.components.wp_defaults import fill_missing_fields, work_package_defaults
from src.application.transformers.issue_transformer import IssueTransformer
from src.config import logger
from src.display import ProgressTracker
from src.domain import provenance_fields
from src.domain.enums import JournalEntryType
from src.infrastructure.jira.comment_audience import issue_without_comments
from src.infrastructure.jira.issue_pagination import iter_issue_pages
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.jira.jira_issue_service import fetch_issue_page_with_retry
from src.infrastructure.openproject.openproject_bulk_create_service import (
    LegacyJournalReplayError,
    reject_legacy_journal_operations,
)
from src.infrastructure.openproject.openproject_client import OpenProjectClient, escape_ruby_single_quoted
from src.infrastructure.persistence.mapping_repo import JsonFileMappingRepository
from src.models import ComponentResult, WorkPackageMappingEntry
from src.utils.enhanced_timestamp_migrator import EnhancedTimestampMigrator
from src.utils.enhanced_user_association_migrator import EnhancedUserAssociationMigrator
from src.utils.markdown_converter import MarkdownConverter


@register_entity_types("work_packages", "issues")
class WorkPackageMigration(BaseMigration):
    """Handles the migration of issues from Jira to work packages in OpenProject.

    This class is responsible for:
    1. Extracting issues from Jira projects
    2. Creating corresponding work packages in OpenProject
    3. Mapping issues between the systems
    4. Handling attachments, comments, and relationships
    """

    START_DATE_FIELD_IDS_DEFAULT = [
        "customfield_18690",  # Target start
        "customfield_12590",  # Change start date
        "customfield_11490",  # Start
        "customfield_15082",  # Key Result: Start
    ]

    # Define mapping file pattern constant
    WORK_PACKAGE_MAPPING_FILE_PATTERN = "work_package_mapping_{}.json"
    ATTACHMENT_MAPPING_FILENAME = "attachment_mapping.json"

    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
    ) -> None:
        """Initialize the work package migration.

        Args:
            jira_client: JiraClient instance.
            op_client: OpenProjectClient instance.

        """
        super().__init__(jira_client, op_client)

        # Setup file paths
        self.jira_issues_file = self.data_dir / "jira_issues.json"
        self.op_work_packages_file = self.data_dir / "op_work_packages.json"
        self.work_package_mapping_file = self.data_dir / "work_package_mapping.json"
        self.attachment_mapping_file = self.data_dir / self.ATTACHMENT_MAPPING_FILENAME

        # Data storage
        self.jira_issues: dict[str, Any] = {}
        self.op_work_packages: dict[str, Any] = {}
        self.work_package_mapping: dict[str, Any] = {}
        self.attachment_mapping: dict[str, dict[str, int]] = {}  # {jira_key: {filename: op_attachment_id}}
        # Some refactored tests expect an attribute named issue_mapping used by
        # time entry migration helpers; initialize defensively.
        self.issue_mapping: dict[str, Any] = {}

        # Mappings
        self.project_mapping: dict[str, Any] = {}
        self.user_mapping: dict[str, Any] = {}
        self.issue_type_mapping: dict[str, Any] = {}
        self.status_mapping: dict[str, Any] = {}

        # Track mentioned users per project for membership assignment
        # Key: OpenProject project ID, Value: set of OpenProject user IDs
        self._mentioned_users_by_project: dict[int, set[int]] = {}

        # Initialize markdown converter (will be updated with mappings when available)
        self.markdown_converter = MarkdownConverter()

        # Initialize enhanced user association migrator
        self.enhanced_user_migrator = EnhancedUserAssociationMigrator(
            jira_client=jira_client,
            op_client=op_client,
        )

        # Initialize enhanced timestamp migrator
        self.enhanced_timestamp_migrator = EnhancedTimestampMigrator(
            jira_client=jira_client,
            op_client=op_client,
        )

        # Preload Jira status category information for history-based start date inference
        (
            self.status_category_by_id,
            self.status_category_by_name,
        ) = self._build_status_category_lookup()

        # Load existing mappings
        self._load_mappings()

        self.start_date_fields = self._load_start_date_fields()

        # Checkpoint/fast-forward tracking
        self._checkpoint_migration_id = "work_package_migration"
        try:
            data_dir_path = self.data_dir if isinstance(self.data_dir, Path) else Path(self.data_dir)
        except Exception:
            data_dir_path = Path()
        self._checkpoint_db_path = data_dir_path.parent / ".migration_checkpoints.db"
        self._project_latest_issue_ts: dict[str, datetime] = {}
        if config.migration_config.get("reset_wp_checkpoints"):
            self.logger.info("Resetting work package checkpoint store via CLI flag")
            self._reset_checkpoint_store()

        # On-the-fly version creation cache: (project_id, version_name) -> openproject_version_id
        self._version_cache: dict[tuple[int, str], int] = {}
        # Current project context for changelog processing (set during issue processing)
        self._current_project_id: int | None = None

        # Phase 1 decomposition: pure mapping methods live on IssueTransformer.
        # The transformer reads owner attributes lazily (``self._owner.<attr>``)
        # so post-construction reassignments (e.g. ``markdown_converter`` being
        # rebuilt by ``_update_markdown_converter_mappings``) remain visible.
        # See ``claudedocs/refactoring/work-package-migration-decomposition-plan.md``.
        self._transformer = IssueTransformer(owner=self)

        # Logging
        self.logger.debug(
            "WorkPackageMigration initialized with data dir: %s",
            self.data_dir,
        )

    def _load_mappings(self) -> None:
        """Load all required mappings from files."""
        from src.utils import data_handler

        # Load mappings via controller to ensure single source of truth
        try:
            self.project_mapping = config.mappings.get_mapping("project") or {}
            self.user_mapping = config.mappings.get_mapping("user") or {}
            self.issue_type_mapping = config.mappings.get_mapping("issue_type") or {}
            self.issue_type_id_mapping = config.mappings.get_mapping("issue_type_id") or {}
            self.status_mapping = config.mappings.get_mapping("status") or {}
        except Exception:
            # Fallback to direct file reads
            self.project_mapping = data_handler.load_dict(
                filename="project_mapping.json",
                directory=self.data_dir,
                default={},
            )
            self.user_mapping = data_handler.load_dict(
                filename="user_mapping.json",
                directory=self.data_dir,
                default={},
            )
            self.issue_type_mapping = data_handler.load_dict(
                filename="issue_type_mapping.json",
                directory=self.data_dir,
                default={},
            )
            self.issue_type_id_mapping = data_handler.load_dict(
                filename="issue_type_id_mapping.json",
                directory=self.data_dir,
                default={},
            )
            self.status_mapping = data_handler.load_dict(
                filename="status_mapping.json",
                directory=self.data_dir,
                default={},
            )

        # Augment user mapping with secondary indices for lookup by name, displayName, email
        self._augment_user_mapping_indices()

        # Load attachment mapping if available (from attachments migration)
        self._load_attachment_mapping()

        # Update markdown converter with loaded mappings
        self._update_markdown_converter_mappings()

    def _load_attachment_mapping(self) -> bool:
        """Load attachment mapping from attachments migration.

        The attachment mapping enables proper conversion of Jira attachment
        references to OpenProject API URLs in descriptions and comments.

        Returns:
            True if mapping was loaded successfully

        """
        if not self.attachment_mapping_file.exists():
            logger.debug(
                "Attachment mapping file not found: %s (attachments may not have been migrated yet)",
                self.attachment_mapping_file,
            )
            return False

        try:
            with self.attachment_mapping_file.open("r") as f:
                self.attachment_mapping = json.load(f)

            total_attachments = sum(len(v) for v in self.attachment_mapping.values())
            logger.info(
                "Loaded attachment mapping: %d issues with %d attachments",
                len(self.attachment_mapping),
                total_attachments,
            )
            return True

        except Exception as e:
            logger.warning("Failed to load attachment mapping: %s", e)
            return False

    def _augment_user_mapping_indices(self) -> None:
        """Augment user_mapping with secondary indices for lookup by jira_name, displayName, and email.

        The user_mapping.json is keyed by Jira user keys (e.g., 'JIRAUSER14306'), but changelog
        entries contain 'name' (e.g., 'caroline.kuhn'). This method adds secondary indices so
        lookups by name, displayName, or email will find the correct user entry.

        This fixes the BUG32 user fallback issue where 155K+ lookups failed because the mapping
        was only indexed by jira_key, not by the fields actually present in changelog data.
        """
        if not self.user_mapping:
            return

        # Build secondary indices
        secondary_entries: dict[str, dict[str, Any]] = {}
        for user_dict in self.user_mapping.values():
            if not isinstance(user_dict, dict):
                continue

            for field in ("jira_name", "jira_display_name", "jira_email", "openproject_login"):
                alias = user_dict.get(field)
                if alias and alias not in self.user_mapping:
                    secondary_entries[alias] = user_dict

        # Merge secondary indices into user_mapping
        if secondary_entries:
            self.user_mapping.update(secondary_entries)
            self.logger.debug(
                f"Augmented user_mapping with {len(secondary_entries)} secondary indices "
                f"(total keys: {len(self.user_mapping)})",
            )

    def _update_markdown_converter_mappings(self) -> None:
        """Update the markdown converter with current user and work package mappings."""
        # Create user mapping for markdown converter (Jira username -> OpenProject user info)
        # self.user_mapping values are dicts with openproject_id and openproject_login
        # MarkdownConverter expects dicts with 'op_id' and 'op_login' keys for proper HTML mentions
        user_mapping = {}
        account_id_mapping = {}  # For Jira Cloud [~accountId:xxx] format
        for username, user_dict in self.user_mapping.items():
            if not user_dict:
                continue
            op_login = user_dict.get("openproject_login")
            op_id = user_dict.get("openproject_id")

            if op_login or op_id:
                # Pass dict with both op_id and op_login for proper HTML mention generation
                user_info = {"op_id": op_id, "op_login": op_login or str(op_id)}
                user_mapping[username] = user_info

                # Also map by Jira accountId (for Cloud) and jira_id
                jira_account_id = user_dict.get("jira_account_id") or user_dict.get("jira_id")
                if jira_account_id:
                    account_id_mapping[jira_account_id] = user_info

        work_package_mapping = self._markdown_work_package_ids()

        # Update the markdown converter with new mappings (including attachment mapping)
        self.markdown_converter = MarkdownConverter(
            user_mapping=user_mapping,
            work_package_mapping=work_package_mapping,
            account_id_mapping=account_id_mapping,
            attachment_mapping=self.attachment_mapping,
        )

    def _markdown_work_package_ids(self) -> dict[str, int]:
        # For work package mapping, we need to load the existing mapping if available.
        # The on-disk wp_map is keyed by ``str(jira_id)`` outer with the human-readable
        # ``jira_key`` stored inside; older fixtures sometimes key directly by
        # ``jira_key``. Normalise via :class:`WorkPackageMappingEntry.from_legacy` so
        # we tolerate both shapes and skip bare-int rows (which carry no recoverable
        # ``jira_key``) — the markdown converter only needs ``jira_key → op_id``.
        work_package_mapping: dict[str, int] = {}
        for outer_key, raw_entry in getattr(self, "work_package_mapping", {}).items():
            if not isinstance(raw_entry, dict):
                # Bare-int legacy rows have no recoverable jira_key.
                continue
            inner_jira_key = raw_entry.get("jira_key")
            jira_key = str(inner_jira_key or outer_key)
            try:
                typed_entry = WorkPackageMappingEntry.from_legacy(jira_key, raw_entry)
            except (
                ValidationError,
                ValueError,
                TypeError,
                AttributeError,
            ):
                continue
            work_package_mapping[jira_key] = int(typed_entry.openproject_id)

        return work_package_mapping

    # Default fallback user id used when a journal author cannot be resolved
    # from the user mapping. See ``_resolve_journal_author_id`` (BUG #32).
    # Kept as a class attribute for backwards compatibility — the canonical
    # value now lives on :class:`IssueTransformer`.
    _BUG32_FALLBACK_USER_ID = IssueTransformer._BUG32_FALLBACK_USER_ID

    # Probe order for resolving Jira changelog/comment authors against
    # ``self.user_mapping``. Mirrors the multi-key fallback documented in
    # BUG #32 — the user_mapping is augmented with secondary indices on
    # ``name`` / ``displayName`` / ``emailAddress`` (and others) so any of
    # these raw Jira fields will resolve to the same OP user when present.
    # Kept as a class attribute for backwards compatibility — the canonical
    # value now lives on :class:`IssueTransformer`.
    _JOURNAL_AUTHOR_PROBE_KEYS: tuple[str, ...] = IssueTransformer._JOURNAL_AUTHOR_PROBE_KEYS

    # ------------------------------------------------------------------ #
    # Phase 1 decomposition: thin delegates to :class:`IssueTransformer`.
    #
    # The 16 pure-mapping methods listed below were moved verbatim into
    # :class:`~src.application.transformers.issue_transformer.IssueTransformer`.
    # The methods that remain on the class are *delegates* — they exist
    # solely to preserve the public surface used by callers and tests.
    #
    # Tests construct migration instances via ``WorkPackageMigration.__new__``
    # (bypassing ``__init__``) and call these delegates directly. To keep
    # those tests working, the transformer is created lazily on first
    # access via the :pyattr:`_transformer` property.
    # ------------------------------------------------------------------ #

    @property
    def _transformer(self) -> IssueTransformer:
        """Lazily-created :class:`IssueTransformer` bound to this owner."""
        cached = getattr(self, "_transformer_cache", None)
        if cached is None:
            cached = IssueTransformer(owner=self)
            self._transformer_cache = cached
        return cached

    @_transformer.setter
    def _transformer(self, value: IssueTransformer) -> None:
        """Allow ``__init__`` to seed the cached transformer eagerly."""
        self._transformer_cache = value

    def _resolve_journal_author_id(
        self,
        author_data: dict[str, Any],
        jira_key: str,
        kind: JournalEntryType,
    ) -> int:
        """Delegate to :meth:`IssueTransformer.resolve_journal_author_id`."""
        return self._transformer.resolve_journal_author_id(author_data, jira_key, kind)

    def _track_mentioned_users(self, text: str | None, project_id: int) -> None:
        """Delegate to :meth:`IssueTransformer.track_mentioned_users`."""
        return self._transformer.track_mentioned_users(text, project_id)

    def _get_or_create_version(self, version_name: str, project_id: int) -> int | None:
        """Get or create an OpenProject version on-the-fly.

        This enables fixVersion mapping during work package migration without
        requiring a separate versions migration step. Versions that don't exist
        in Jira anymore can still be created in OpenProject.

        Args:
            version_name: Name of the version (from Jira fixVersion)
            project_id: OpenProject project ID

        Returns:
            OpenProject version ID, or None if creation failed

        """
        if not version_name or not project_id:
            return None

        version_name = str(version_name).strip()
        if not version_name:
            return None
        quoted_name = "'" + escape_ruby_single_quoted(version_name) + "'"
        project_id = int(project_id)

        # Check cache first
        cache_key = (project_id, version_name)
        if cache_key in self._version_cache:
            return self._version_cache[cache_key]

        # Check if version exists in OpenProject
        try:
            query = f"""
                v = Version.find_by(project_id: {project_id}, name: {quoted_name})
                v ? {{ id: v.id, name: v.name }}.to_json : 'null'
            """
            result = self.op_client.execute_json_query(query)
            # Parse JSON if result is a string
            if isinstance(result, str):
                with contextlib.suppress(json.JSONDecodeError):
                    result = json.loads(result)
            if isinstance(result, dict) and result.get("id"):
                version_id = int(result["id"])
                self._version_cache[cache_key] = version_id
                self.logger.debug(
                    f"[VERSION] Found existing version '{version_name}' (ID: {version_id}) in project {project_id}",
                )
                return version_id
        except Exception as e:
            self.logger.warning("Could not verify existing version in project %s: %s", project_id, e)
            return None

        # Create version on-the-fly
        try:
            create_query = f"""
                v = Version.new(
                    project_id: {project_id},
                    name: {quoted_name},
                    status: 'open'
                )
                if v.save
                    {{ id: v.id, name: v.name, created: true }}.to_json
                else
                    {{ error: v.errors.full_messages.join(', ') }}.to_json
                end
            """
            result = self.op_client.execute_json_query(create_query)
            if isinstance(result, dict):
                if result.get("id"):
                    version_id = int(result["id"])
                    self._version_cache[cache_key] = version_id
                    self.logger.info(
                        f"[VERSION] Created version '{version_name}' (ID: {version_id}) in project {project_id}",
                    )
                    return version_id
                if result.get("error"):
                    self.logger.warning(f"[VERSION] Failed to create version '{version_name}': {result['error']}")
        except Exception as e:
            self.logger.warning(f"[VERSION] Error creating version '{version_name}' in project {project_id}: {e}")

        return None

    def _extract_final_workflow(self, jira_issue: Any) -> str | None:
        """Delegate to :meth:`IssueTransformer.extract_final_workflow`."""
        return self._transformer.extract_final_workflow(jira_issue)

    def _load_start_date_fields(self) -> list[str]:
        return self.start_date_field_ids()

    @classmethod
    def start_date_field_ids(cls) -> list[str]:
        """The Jira fields that hold an issue's start date, the configured ones last."""
        fields = list(cls.START_DATE_FIELD_IDS_DEFAULT)
        extra = config.migration_config.get("start_date_custom_fields")
        extras: list[str] = []
        if isinstance(extra, str):
            extras = [item.strip() for item in extra.split(",") if item and item.strip()]
        elif isinstance(extra, list):
            extras = [str(item).strip() for item in extra if item]
        for field_id in extras:
            if field_id and field_id not in fields:
                fields.append(field_id)
        return fields

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        """Delegate to :meth:`IssueTransformer.parse_datetime`."""
        return IssueTransformer.parse_datetime(value)

    @staticmethod
    def _ensure_checkpoint_table(conn: sqlite3.Connection) -> None:
        """Ensure the migration_checkpoints table exists with expected schema."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS migration_checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                migration_id TEXT NOT NULL,
                checkpoint_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                status TEXT NOT NULL,
                data TEXT,
                created_at TEXT,
                updated_at TEXT,
                error_message TEXT,
                retry_count INTEGER DEFAULT 0
            )
            """,
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_migration_checkpoints_migration
                ON migration_checkpoints (migration_id, checkpoint_type, entity_id)
            """,
        )

    def _get_checkpoint_timestamp(self, project_key: str) -> datetime | None:
        """Load the last successful migration timestamp for a project."""
        db_path = self._checkpoint_db_path
        try:
            if not db_path or not Path(db_path).exists():
                return None
        except Exception:
            return None

        try:
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                conn.row_factory = sqlite3.Row
                self._ensure_checkpoint_table(conn)
                row = conn.execute(
                    """
                    SELECT data, updated_at
                    FROM migration_checkpoints
                    WHERE migration_id = ?
                      AND checkpoint_type = ?
                      AND entity_id = ?
                      AND status = 'completed'
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (self._checkpoint_migration_id, "work_packages", project_key),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            self._handle_corrupt_checkpoint_db(exc)
            return None
        except Exception as exc:  # pragma: no cover - defensive logging only
            self.logger.debug(
                "Failed to read checkpoint metadata for %s: %s",
                project_key,
                exc,
            )
            return None

        if not row:
            return None

        payload: dict[str, Any] = {}
        data_raw = row["data"]
        if isinstance(data_raw, (str, bytes)):
            try:
                payload = json.loads(data_raw)
            except Exception:
                payload = {}

        timestamp_value = payload.get("last_success_at") or payload.get("timestamp") or row["updated_at"]
        return self._parse_datetime(timestamp_value)

    def _derive_snapshot_timestamp(self, snapshot: list[dict[str, Any]]) -> datetime | None:
        """Delegate to :meth:`IssueTransformer.derive_snapshot_timestamp`."""
        return self._transformer.derive_snapshot_timestamp(snapshot)

    @staticmethod
    def _build_key_exclusion_clause(existing_keys: set[str]) -> str | None:
        """Delegate to :meth:`IssueTransformer.build_key_exclusion_clause`."""
        return IssueTransformer.build_key_exclusion_clause(existing_keys)

    def _update_project_checkpoint(
        self,
        project_key: str,
        latest_timestamp: datetime,
        migrated_count: int,
    ) -> None:
        """Persist the latest successful migration timestamp for a project."""
        db_path = self._checkpoint_db_path
        try:
            db_path_obj = Path(db_path)
        except Exception:
            return

        try:
            db_path_obj.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        payload = {
            "project_key": project_key,
            "last_success_at": latest_timestamp.astimezone(UTC).isoformat(),
            "migrated_count": migrated_count,
        }

        try:
            with contextlib.closing(sqlite3.connect(str(db_path_obj))) as conn:
                self._ensure_checkpoint_table(conn)
                conn.execute(
                    """
                    DELETE FROM migration_checkpoints
                    WHERE migration_id = ?
                      AND checkpoint_type = ?
                      AND entity_id = ?
                    """,
                    (self._checkpoint_migration_id, "work_packages", project_key),
                )
                conn.execute(
                    """
                    INSERT INTO migration_checkpoints (
                        migration_id,
                        checkpoint_type,
                        entity_id,
                        status,
                        data,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._checkpoint_migration_id,
                        "work_packages",
                        project_key,
                        "completed",
                        json.dumps(payload),
                        datetime.now(tz=UTC).isoformat(),
                        datetime.now(tz=UTC).isoformat(),
                    ),
                )
                conn.commit()
                self.logger.debug(
                    "Checkpoint updated for %s at %s (migrated=%s)",
                    project_key,
                    latest_timestamp.isoformat(),
                    migrated_count,
                )
        except sqlite3.OperationalError as exc:
            self.logger.debug(
                "Checkpoint update failed for %s due to schema error: %s",
                project_key,
                exc,
            )
            self._handle_corrupt_checkpoint_db(exc)
        except sqlite3.DatabaseError as exc:
            self._handle_corrupt_checkpoint_db(exc)
        except Exception as exc:
            self.logger.warning(
                "Failed to update checkpoint for %s: %s",
                project_key,
                exc,
            )

    def _reset_checkpoint_store(self) -> None:
        """Remove or rotate the checkpoint database file."""
        try:
            path = Path(self._checkpoint_db_path)
        except Exception:
            return

        if not path:
            return

        try:
            if path.exists():
                rotated = path.with_suffix(f"{path.suffix}.{datetime.now(tz=UTC).strftime('%Y%m%d%H%M%S')}.bak")
                path.rename(rotated)
        except Exception as exc:
            self.logger.warning("Failed to rotate checkpoint store: %s", exc)
        finally:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

    def _handle_corrupt_checkpoint_db(self, exc: Exception) -> None:
        """Rename a corrupt checkpoint store and reset it."""
        self.logger.warning("Checkpoint store appears corrupt: %s. Resetting.", exc)
        self._reset_checkpoint_store()

    def _build_status_category_lookup(self) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Load Jira statuses and map their categories by id/name."""
        statuses = self._load_from_json("jira_statuses.json", default=None)
        fetched = False
        if not statuses:
            try:
                statuses = self.jira_client.get_all_statuses()
                fetched = True
            except Exception:
                statuses = []

        # Defensive: a misconfigured client (or a Mock-returning fixture in
        # tests) can deliver something that isn't list-shaped here. Treat
        # anything that isn't a list as "no data" rather than raising on
        # iteration.
        if not isinstance(statuses, list):
            statuses = []

        if fetched and statuses:
            try:
                self._save_to_json(statuses, "jira_statuses.json")
            except Exception:
                pass

        return self._index_status_categories(statuses)

    @staticmethod
    def _index_status_categories(
        statuses: list[dict[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        id_lookup: dict[str, dict[str, Any]] = {}
        name_lookup: dict[str, dict[str, Any]] = {}
        for status in statuses:
            if not isinstance(status, dict):
                continue
            status_id = str(status.get("id") or "").strip()
            status_name = str(status.get("name") or "").strip()
            category = status.get("statusCategory") or {}
            if status_id:
                id_lookup[status_id] = category
            if status_name:
                name_lookup[status_name.lower()] = category

        return id_lookup, name_lookup

    def _get_existing_work_packages(self, op_project_id: int) -> dict[str, dict[str, Any]]:
        """Get existing work packages from OpenProject for incremental updates.

        Returns a dict mapping Jira keys to OpenProject work package info.
        """
        snapshot = self.op_client.get_project_wp_cf_snapshot(op_project_id)
        self._load_confirmed_work_packages()
        existing = {}
        for row in snapshot:
            key = str(row.get("jira_issue_key") or self._verified_mapping_key(row, op_project_id) or "").strip()
            if not key:
                continue
            if key in existing:
                msg = f"More than one target work package has origin key {key} in project {op_project_id}"
                raise ValueError(msg)
            existing[key] = {"id": row.get("id"), "jira_key": key, "jira_source_id": row.get("jira_source_id")}
        return existing

    def _verified_mapping_key(self, target: dict[str, Any], project_id: int) -> str | None:
        """Recover a missing key only when the stored target carries this Jira source ID in this project."""
        source_id = target.get("jira_source_id")
        if not source_id:
            return None
        mapping = self.work_package_mapping.get(str(source_id))
        if (
            isinstance(mapping, dict)
            and mapping.get("openproject_id") == target.get("id")
            and mapping.get("openproject_project_id") == project_id
        ):
            return mapping.get("jira_key")
        return None

    @staticmethod
    def _reject_source_history(issue: Any) -> None:
        """Refuse legacy replay before projecting source values or contacting the target."""
        if not has_no_source_journals(issue, require_complete=False):
            raise LegacyJournalReplayError

    def _update_existing_work_package(
        self,
        jira_issue: Issue,
        existing_wp: dict[str, Any],
        op_project_id: int,
    ) -> None:
        """The legacy updater cannot distinguish imported journals from native history."""
        stored_id = existing_wp.get("jira_source_id")
        if stored_id and str(stored_id) != str(jira_issue.id):
            msg = f"Target origin ID for {jira_issue.key} disagrees with Jira; reconcile before retrying"
            raise RuntimeError(msg)
        if has_no_source_journals(jira_issue, require_complete=True):
            return
        self.logger.error(
            "Legacy journal replay blocked for %s, target %s in project %s",
            jira_issue.key,
            existing_wp.get("id"),
            op_project_id,
        )
        raise LegacyJournalReplayError

    @staticmethod
    def _update_existing_work_packages_batch(batch: list[tuple[Issue, dict[str, Any], int]]) -> tuple[int, int]:
        """Refuse a legacy journal replacement before reading or writing the target."""
        if any(not has_no_source_journals(issue, require_complete=True) for issue, _, _ in batch):
            raise LegacyJournalReplayError
        return 0, 0

    def _iter_all_project_issues(
        self,
        project_key: str,
    ) -> Iterator[Issue]:
        """Fetch project issues eligible for ordinary OpenProject visibility.

        Used for incremental migrations without a timestamp cutoff.
        Includes renderedFields expansion for comment data extraction.
        """
        batch_size = config.migration_config.get("batch_size", 100)
        jql = f"{unrestricted_project_jql(project_key)} ORDER BY created ASC, key ASC"
        fields = "*all"  # Include the issue security level when Jira exposes it.
        expand = "changelog,renderedFields"  # Include changelog and comments

        # Check for issue limit or specific test issues (for testing purposes)
        max_issues = None
        test_issues = None

        if os.getenv("J2O_TEST_ISSUES"):
            # Specific issue keys to test (comma-separated)
            test_issues = [k.strip() for k in os.getenv("J2O_TEST_ISSUES").split(",")]
            self.logger.info(f"Testing specific issues: {test_issues}")
            # Format issue keys for Jira JQL IN clause
            issue_keys_str = ",".join(json.dumps(k) for k in test_issues)
            jql = f"{unrestricted_project_jql(project_key)} AND key IN ({issue_keys_str}) ORDER BY created ASC, key ASC"
        elif os.getenv("J2O_MAX_ISSUES"):
            # Limit issue count
            try:
                max_issues = int(os.getenv("J2O_MAX_ISSUES"))
                self.logger.info(f"Limiting to {max_issues} issues (J2O_MAX_ISSUES set)")
            except ValueError:
                self.logger.warning("Invalid J2O_MAX_ISSUES value, ignoring")

        self.logger.info(f"Fetching issues for project '{project_key}' with JQL: {jql}")

        # Verify project exists first
        try:
            self.jira_client.jira.project(project_key)
        except Exception as e:
            from src.infrastructure.jira.jira_client import JiraResourceNotFoundError

            msg = f"Project '{project_key}' not found: {e!s}"
            raise JiraResourceNotFoundError(msg) from e

        def fetch_page(offset: int, size: int) -> list[Issue]:
            return self._fetch_issues_with_retry(
                jql=jql,
                start_at=offset,
                max_results=size,
                fields=fields,
                expand=expand,
                project_key=project_key,
            )

        yield from filter(
            permits_unrestricted_result, iter_issue_pages(project_key, fetch_page, batch_size, max_issues)
        )

    def iter_project_issues(self, project_key: str) -> Iterator[Issue]:
        """Generate issues for a project with memory-efficient pagination.

        This generator yields individual issues instead of loading all issues
        into memory at once, solving the unbounded memory growth problem.
        Includes renderedFields expansion for comment data extraction.

        Args:
            project_key: The key of the Jira project

        Yields:
            Individual Jira Issue objects with comment data

        Raises:
            JiraApiError: If the API request fails after retries
            JiraResourceNotFoundError: If the project is not found

        """
        batch_size = config.migration_config.get("batch_size", 100)

        # Reset per-project tracking for latest issue timestamps
        self._project_latest_issue_ts.pop(project_key, None)

        # Check for test issue limiting
        max_issues = None
        if os.getenv("J2O_MAX_ISSUES"):
            try:
                max_issues = int(os.getenv("J2O_MAX_ISSUES"))
                self.logger.info(f"Limiting to {max_issues} issues (J2O_MAX_ISSUES set)")
            except ValueError:
                self.logger.warning("Invalid J2O_MAX_ISSUES value, ignoring")

        jql = self._project_issue_query(project_key)
        fields = "*all"  # Include the issue security level when Jira exposes it.
        expand = "changelog,renderedFields"  # Include changelog and comments

        logger.notice("Starting paginated fetch for project '%s'...", project_key)

        # Verify project exists first (unless J2O_TEST_ISSUES is set)
        if not os.getenv("J2O_TEST_ISSUES"):
            try:
                self.jira_client.jira.project(project_key)
            except Exception as e:
                from src.infrastructure.jira.jira_client import JiraResourceNotFoundError

                msg = f"Project '{project_key}' not found: {e!s}"
                raise JiraResourceNotFoundError(msg) from e
        else:
            logger.info(
                "J2O_TEST_ISSUES set - skipping project verification for '%s'",
                project_key,
            )

        def fetch_page(offset: int, size: int) -> list[Issue]:
            return self._fetch_issues_with_retry(
                jql=jql,
                start_at=offset,
                max_results=size,
                fields=fields,
                expand=expand,
                project_key=project_key,
            )

        yield from filter(
            permits_unrestricted_result, iter_issue_pages(project_key, fetch_page, batch_size, max_issues)
        )

    def _project_issue_query(self, project_key: str) -> str:
        project = unrestricted_project_jql(project_key)
        order = "ORDER BY created ASC, key ASC"
        test_issues = os.getenv("J2O_TEST_ISSUES")
        if test_issues:
            issue_keys = ",".join(json.dumps(key.strip()) for key in test_issues.split(","))
            return f"{project} AND key IN ({issue_keys}) {order}"
        if os.environ.get("J2O_FAST_FORWARD", "1").lower() not in {"1", "true", "yes", "on"}:
            return f"{project} {order}"
        backoff = int(os.environ.get("J2O_FF_BACKOFF_SECONDS", "7200"))
        timestamp, existing = self._incremental_issue_state(project_key)
        if timestamp:
            cutoff = (timestamp - timedelta(seconds=backoff)).strftime("%Y-%m-%d %H:%M")
            self.logger.info("Using Jira checkpoint %s for %s (backoff %ss)", timestamp, project_key, backoff)
            return f'{project} AND updated >= "{cutoff}" ORDER BY updated ASC, key ASC'
        exclusion = self._build_key_exclusion_clause(existing)
        return f"{project} AND {exclusion} {order}" if exclusion else f"{project} {order}"

    def _incremental_issue_state(self, project_key: str) -> tuple[datetime | None, set[str]]:
        timestamp = self._get_checkpoint_timestamp(project_key)
        existing: set[str] = set()
        try:
            target = (self.project_mapping.get(project_key) or {}).get("openproject_id")
            if target:
                self.op_client.ensure_work_package_custom_field("Jira Issue Key", "string")
                self.op_client.ensure_work_package_custom_field("Jira Migration Date", "date")
                rows = self.op_client.get_project_wp_cf_snapshot(int(target))
                existing = {
                    row["jira_issue_key"].strip()
                    for row in rows
                    if isinstance(row, dict)
                    and isinstance(row.get("jira_issue_key"), str)
                    and row["jira_issue_key"].strip()
                }
                timestamp = timestamp or self._derive_snapshot_timestamp(rows)
        except Exception as exc:
            self.logger.debug("Failed to inspect existing work packages for %s: %s", project_key, exc)
        return timestamp, existing

    def _fetch_issues_with_retry(
        self,
        *,
        jql: str,
        start_at: int,
        max_results: int,
        fields: str | None,
        expand: str | None,
        project_key: str,
    ) -> list[Issue]:
        return fetch_issue_page_with_retry(
            self.jira_client,
            jql=jql,
            start_at=start_at,
            max_results=max_results,
            fields=fields,
            expand=expand,
            project_key=project_key,
        )

    def _get_project_total_issues(self, project_key: str) -> int | None:
        """Return total number of issues in a Jira project using search metadata.

        Performs a lightweight search with maxResults=1 to read the `total` count
        from Jira's search API response. Falls back to None if unavailable.

        Args:
            project_key: Jira project key

        Returns:
            Integer total if available, otherwise None

        """
        try:
            jql = unrestricted_project_jql(project_key)
            # Request minimal payload but ensure we get metadata
            resp: Any = self.jira_client.jira.search_issues(
                jql,
                startAt=0,
                maxResults=1,
                fields="id",
                expand=None,
                json_result=True,
            )
            # python-jira returns a dict when json_result=True
            if isinstance(resp, dict):
                total_val = resp.get("total")
                if isinstance(total_val, int):
                    return total_val
            # Some versions may return a ResultList with `.total`
            if hasattr(resp, "total"):
                try:
                    return int(resp.total)
                except Exception:
                    pass
        except Exception as e:
            logger.debug("Unable to get total issues for %s: %s", project_key, e)
        return None

    def _snapshot_issue(self, issue: Issue, project_key: str) -> dict[str, Any]:
        raw = issue_without_comments(getattr(issue, "raw", {}))
        fields = raw.get("fields") or {}
        with contextlib.suppress(AttributeError, ValueError, TypeError):
            updated = self._parse_datetime(fields.get("updated"))
            current = self._project_latest_issue_ts.get(project_key)
            if updated and (current is None or updated > current):
                self._project_latest_issue_ts[project_key] = updated
        snapshot = {
            "key": getattr(issue, "key", raw.get("key")),
            "id": getattr(issue, "id", raw.get("id")),
            "self": getattr(issue, "self", raw.get("self")),
            "fields": fields,
        }
        if "changelog" in raw:
            snapshot["changelog"] = raw["changelog"]
        return snapshot

    def _save_project_issues(self, rows: list[dict[str, Any]], project_key: str) -> None:
        try:
            write_issue_snapshot(rows, self.data_dir / f"jira_issues_{project_key}.json")
        except Exception as exc:
            self.logger.exception("Failed to write the issue snapshot for %s", project_key)
            backup_path = (
                self.data_dir / f"jira_issues_{project_key}_backup_{datetime.now(tz=UTC):%Y%m%d_%H%M%S_%f}.json"
            )
            try:
                write_issue_snapshot(rows, backup_path)
            except (OSError, ValueError, TypeError) as backup_error:
                self.logger.warning("Failed to create backup of state file: %s", backup_error)
            if config.migration_config.get("stop_on_error", False):
                from src.models.migration_error import MigrationError

                msg = f"Stopping due to JSON serialization failure for project {project_key}: {exc}"
                raise MigrationError(msg) from exc

    def _log_project_progress(self, count: int, key: str, tracker: Any) -> None:
        if count % 500 == 0:
            self.logger.info("Processed %s issues from %s", count, key)
            if tracker:
                tracker.add_log_item(f"Processed {count} issues")

    def _extract_jira_issues(self, project_key: str, project_tracker: Any = None) -> list[dict]:
        """Extract and persist the comment-filtered Jira issue snapshot."""
        try:
            estimate = self._get_project_total_issues(project_key)
            if project_tracker:
                label = f"{estimate} issues" if estimate else "estimating issues"
                project_tracker.update_description(f"Processing project {project_key} ({label})")
                if estimate:
                    project_tracker.add_log_item(f"Total issues: {estimate}")
            tracker = (
                ProgressTracker(f"Issues for {project_key}", estimate, "Recent Issues")
                if estimate and estimate > 0
                else contextlib.nullcontext()
            )
            rows = []
            with tracker as progress:
                for issue in self.iter_project_issues(project_key):
                    rows.append(self._snapshot_issue(issue, project_key))
                    if progress is not None:
                        progress.increment()
                    self._log_project_progress(len(rows), project_key, project_tracker)
            if project_tracker:
                project_tracker.add_log_item(f"Retrieved {len(rows)} issues from {project_key}")
            self._save_project_issues(rows, project_key)
            return rows
        except Exception as exc:
            msg = f"Jira issue extraction failed for project {project_key}: {exc}"
            self.logger.exception(msg)
            if project_tracker:
                project_tracker.add_log_item(msg)
            raise RuntimeError(msg) from exc

    def _source_type_id(self, source: Any) -> int:
        """Resolve a Jira SDK type through explicit numeric IDs and legacy mappings."""
        key = str(source.id)
        if key in self.issue_type_id_mapping:
            target = self.issue_type_id_mapping[key]
        elif key in self.issue_type_mapping:
            target = self.issue_type_mapping[key].get("openproject_id")
        else:
            target = getattr(config.mappings, "issue_type_id_mapping", {}).get(key)
        if not target:
            self.logger.warning("No mapping for Jira issue type %s (%s); using Task", source.name, key)
        return target or 1

    def _apply_source_users(self, jira_issue: Any, work_package: dict[str, Any]) -> None:
        association_result = self.enhanced_user_migrator.migrate_user_associations(
            jira_issue=jira_issue,
            work_package_data=work_package,
            preserve_creator_via_rails=True,
        )

        # Log any warnings from user association migration
        for warning in association_result["warnings"]:
            # Collapse frequent watcher unmapped logs
            if isinstance(warning, str) and warning.startswith("Watcher") and "unmapped" in warning:
                # Defer aggregated reporting to caller; embed a counter on work_package_data
                counters = work_package.setdefault("_log_counters", {})
                counters["watcher_unmapped"] = counters.get("watcher_unmapped", 0) + 1
            else:
                self.logger.warning("User association: %s", warning)

    def _apply_source_timestamps(self, jira_issue: Any, work_package: dict[str, Any]) -> None:
        result = self.enhanced_timestamp_migrator.migrate_timestamps(
            jira_issue=jira_issue,
            work_package_data=work_package,
            use_rails_for_immutable=False,
            author_id=work_package.get("author_id"),
        )
        for warning in result["warnings"]:
            self.logger.warning("Timestamp migration: %s", warning)
        for error in result["errors"]:
            self.logger.error("Timestamp migration error: %s", error)
        operations = result.get("rails_operations")
        if operations:
            work_package["_rails_operations"] = operations

    def _source_custom_values(self, fields: dict[str, Any]) -> list[dict[str, Any]]:
        source = {
            k: value
            for k, v in fields.items()
            if k.startswith("customfield_") and (value := without_empty_values(v)) is not None
        }
        if not source:
            return []
        try:
            mapping = self._load_custom_field_mapping()
        except (FileNotFoundError, RuntimeError) as exc:
            self.logger.warning("Custom field mapping unavailable: %s", exc)
            return []
        values = {}
        for key, value in source.items():
            field_id = mapping.get(key, {}).get("openproject_id")
            if field_id:
                values[field_id] = value
        return [{"id": key, "value": value} for key, value in values.items()]

    @staticmethod
    def _provenance_values(field_ids: dict[str, Any], values: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"id": field_ids[key], "value": value} for key, value in values.items() if field_ids.get(key) and value]

    def _source_provenance(self, issue: Any) -> list[dict[str, Any]]:
        field_ids = getattr(self, "_j2o_wp_cf_ids_full", {}) or {}
        today = date.today().isoformat()
        jira_config = config.jira_config or {}
        base_url = jira_config.get("J2O_JIRA_URL") or jira_config.get("url")
        values = {
            provenance_fields.ORIGIN_SYSTEM: provenance_fields.JIRA_SERVER_SOURCE_LABEL,
            provenance_fields.ORIGIN_ID: str(issue.id) if issue.id else None,
            provenance_fields.ORIGIN_KEY: issue.key,
            provenance_fields.ORIGIN_URL: f"{str(base_url).rstrip('/')}/browse/{issue.key}"
            if base_url and issue.key
            else None,
            provenance_fields.FIRST_MIGRATION_DATE: today,
            provenance_fields.LAST_UPDATE_DATE: today,
        }
        resolution = getattr(issue.fields, "resolution", None)
        if resolution:
            values[provenance_fields.JIRA_RESOLUTION] = getattr(resolution, "name", None) or str(resolution)
        versions = getattr(issue.fields, "versions", None)
        if isinstance(versions, list):
            values[provenance_fields.AFFECTS_VERSION] = ", ".join(str(getattr(v, "name", v)) for v in versions if v)
        return self._provenance_values(field_ids, values)

    def _prepare_work_package(self, jira_issue: Issue, project_id: int) -> dict[str, Any]:
        """Prepare a history-free SDK issue for the legacy bulk service."""
        self._reject_source_history(jira_issue)
        self._current_project_id = project_id
        self._ensure_provenance_field_ids()
        fields = jira_issue.fields
        subject = fields.summary
        if not subject or not str(subject).strip():
            subject = jira_issue.key or f"Untitled-{jira_issue.id}"
        description = getattr(fields, "description", "") or ""
        work_package = {
            "project_id": project_id,
            "type_id": self._source_type_id(fields.issuetype),
            "subject": subject,
            "jira_id": jira_issue.id,
            "jira_key": jira_issue.key,
            "jira_issue_id": jira_issue.id,
            "jira_issue_key": jira_issue.key,
        }
        self._apply_source_users(jira_issue, work_package)
        self._apply_source_timestamps(jira_issue, work_package)
        work_package["description"] = self.markdown_converter.convert(description) if description else description
        start_date = self._resolve_start_date(jira_issue)
        if start_date:
            work_package["start_date"] = start_date
        status = self._transformer.mapped_status_id(self._get_attr(getattr(fields, "status", None), "id"))
        if status:
            work_package["status_id"] = status
        custom_fields = self._source_custom_values(jira_issue.raw.get("fields", {}))
        custom_fields.extend(self._source_provenance(jira_issue))
        if custom_fields:
            work_package["custom_fields"] = custom_fields
        return work_package

    def _resolve_start_date(self, issue: Any) -> str | None:
        """Delegate to :meth:`IssueTransformer.resolve_start_date`."""
        return self._transformer.resolve_start_date(issue)

    def _resolve_start_date_from_history(self, issue: Any) -> str | None:
        """Delegate to :meth:`IssueTransformer.resolve_start_date_from_history`."""
        return self._transformer.resolve_start_date_from_history(issue)

    @staticmethod
    def _get_attr(obj: Any, key: str) -> Any:
        """Delegate to :meth:`IssueTransformer.get_attr`."""
        return IssueTransformer.get_attr(obj, key)

    def _process_changelog_item(self, item: dict[str, Any]) -> dict[str, list[Any]] | None:
        """Delegate to :meth:`IssueTransformer.process_changelog_item`."""
        return self._transformer.process_changelog_item(item)

    def _extract_changelog_histories(self, issue: Any) -> list[Any]:
        """Delegate to :meth:`IssueTransformer.extract_changelog_histories`."""
        return IssueTransformer.extract_changelog_histories(issue)

    @staticmethod
    def _is_in_progress_category(category: dict[str, Any]) -> bool:
        """Delegate to :meth:`IssueTransformer.is_in_progress_category`."""
        return IssueTransformer.is_in_progress_category(category)

    def _extract_issue_meta(self, issue: Any) -> dict[str, Any]:
        """Delegate to :meth:`IssueTransformer.extract_issue_meta`."""
        return self._transformer.extract_issue_meta(issue)

    def _dict_provenance(self, issue: dict[str, Any]) -> list[dict[str, Any]]:
        values = {
            provenance_fields.ORIGIN_SYSTEM: "jira",
            provenance_fields.EXTERNAL_ID: str(issue["id"]) if issue.get("id") else None,
            provenance_fields.EXTERNAL_KEY: issue.get("key"),
        }
        self._j2o_wp_cf_ids = self._custom_field_ids(
            getattr(self, "_j2o_wp_cf_ids", None),
            ((name, "string", False) for name in values),
        )
        return self._provenance_values(self._j2o_wp_cf_ids, values)

    def _custom_field_ids(
        self,
        cached: dict[str, int] | None,
        specs: Iterable[tuple[str, str, bool]],
    ) -> dict[str, int]:
        """Reuse confirmed IDs and retry unresolved OpenProject fields on the next row."""
        field_ids = cached if isinstance(cached, dict) else {}
        for name, field_format, searchable in specs:
            if name in field_ids:
                continue
            try:
                field = self.op_client.ensure_custom_field(
                    name, field_format=field_format, cf_type="WorkPackageCustomField", searchable=searchable
                )
                field_id = int(str(field["id"]))
                if field_id <= 0:
                    msg = "expected a positive custom field ID"
                    raise ValueError(msg)
            except Exception as exc:
                self.logger.warning("Could not resolve work package provenance field %s: %s", name, exc)
            else:
                field_ids[name] = field_id
        return field_ids

    def prepare_work_package(self, jira_issue: dict[str, Any] | Issue, project_id: int) -> dict[str, Any]:
        """Prepare either a cached issue mapping or a live Jira issue."""
        if not isinstance(jira_issue, dict):
            return self._prepare_work_package(jira_issue, project_id)
        self._reject_source_history(jira_issue)
        reject_legacy_journal_operations([jira_issue])
        key = jira_issue.get("key", "")
        fields = jira_issue.get("fields")
        description = jira_issue.get("description", self._get_attr(fields, "description")) or ""
        if description:
            description = self.markdown_converter.convert(description)
        summary = jira_issue.get("summary", self._get_attr(fields, "summary"))
        subject = (summary or "").strip() or key or f"Untitled-{jira_issue.get('id', '')}"
        result: dict[str, Any] = {
            "project_id": project_id,
            "subject": subject,
            "description": f"Jira Issue: {key}\n\n{description}",
            "jira_key": key,
            "jira_id": jira_issue.get("id", ""),
            "_links": {},
        }
        start_date = self._resolve_start_date(jira_issue)
        if start_date:
            result["start_date"] = start_date
        custom_fields = self._dict_provenance(jira_issue)
        if custom_fields:
            result["custom_fields"] = custom_fields
        type_info = jira_issue.get("issue_type") or self._get_attr(fields, "issuetype") or {}
        if type_info.get("id") or type_info.get("name"):
            result["type_id"] = self._map_issue_type(type_info.get("id"), type_info.get("name"))
            result["_links"]["type"] = {"href": f"/api/v3/types/{result['type_id']}"}
        status = jira_issue.get("status") or self._get_attr(fields, "status") or {}
        if status.get("id") or status.get("name"):
            result["status_id"] = self._map_status(status.get("id"), status.get("name"))
            result["_links"]["status"] = {"href": f"/api/v3/statuses/{result['status_id']}"}
        self._sanitize_wp_dict(result)
        return result

    def _map_issue_type(
        self,
        type_id: str | None = None,
        type_name: str | None = None,
    ) -> int:
        """Delegate to :meth:`IssueTransformer.map_issue_type`."""
        return self._transformer.map_issue_type(type_id, type_name)

    def _map_status(
        self,
        status_id: str | None = None,
        status_name: str | None = None,
    ) -> int:
        """Delegate to :meth:`IssueTransformer.map_status`."""
        return self._transformer.map_status(status_id, status_name)

    def _sanitize_wp_dict(self, wp: dict[str, Any]) -> None:
        """Delegate to :meth:`IssueTransformer.sanitize_wp_dict`."""
        return IssueTransformer.sanitize_wp_dict(wp)

    def _change_detection_projects(self) -> list[str]:
        selected = os.getenv("J2O_TEST_ISSUES")
        if selected:
            return sorted({key.strip().split("-", 1)[0] for key in selected.split(",") if "-" in key})
        projects = self.jira_client.get_projects()
        configured = config.selected_jira_projects()
        return [
            project["key"]
            for project in projects
            if project.get("key") and (not configured or project["key"] in configured)
        ]

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Fetch comment-filtered work packages for change detection."""
        if entity_type not in {"work_packages", "issues"}:
            msg = f"WorkPackageMigration does not support entity type: {entity_type}. Supported types: ['work_packages', 'issues']"
            raise ValueError(msg)
        issues = []
        for project_key in self._change_detection_projects():
            for issue in self.iter_project_issues(project_key):
                raw = issue_without_comments(issue.raw)
                issues.append(
                    {
                        "id": issue.id,
                        "key": issue.key,
                        "fields": raw.get("fields", {}),
                        "raw": raw,
                        "project_key": project_key,
                    }
                )
        return issues

    def _ensure_provenance_field_ids(self) -> None:
        """Cache the field IDs used when mapping work packages and their change history."""
        self._j2o_wp_cf_ids_full = self._custom_field_ids(
            getattr(self, "_j2o_wp_cf_ids_full", None),
            provenance_fields.WORK_PACKAGE_FIELD_SPECS,
        )

    def _load_custom_field_mapping(self) -> dict[str, Any]:
        """Load or rebuild custom field mapping from cache or OpenProject metadata.

        Following idempotency requirements (see ADR 2025-10-20), this method:
        1. Tries to load cached mapping from disk (performance optimization)
        2. If cache missing, queries OpenProject custom fields (authoritative source)
        3. Builds mapping by matching Jira field names to OpenProject field names
        4. Saves rebuilt mapping to cache for future use

        Returns:
            Dictionary mapping Jira custom field IDs to OpenProject custom field info

        Raises:
            RuntimeError: If there's an error querying OpenProject or building mapping

        """
        mapping_file = Path(self.data_dir) / "custom_field_mapping.json"

        # Try loading from cache first (performance optimization)
        if mapping_file.exists():
            try:
                with mapping_file.open() as f:
                    cached_mapping = json.load(f)
                    # Accept empty mapping as valid cache (no custom fields to map)
                    self.logger.info(
                        "Loaded custom field mapping from cache: %d entries",
                        len(cached_mapping),
                    )
                    return cached_mapping
            except Exception as e:
                self.logger.warning(
                    "Failed to load cached mapping from %s: %s. Will rebuild.",
                    mapping_file,
                    e,
                )

        # Cache miss or empty: rebuild from OpenProject (authoritative source)
        self.logger.info(
            "Rebuilding custom field mapping from OpenProject metadata",
        )

        try:
            # Query all custom fields from OpenProject
            op_custom_fields = self.op_client.get_custom_fields(force_refresh=True)

            # Build name-based lookup (same logic as custom_field_migration.py)
            op_fields_by_name = {
                field.get("name", "").lower(): field for field in op_custom_fields if field.get("name")
            }

            # Load Jira custom fields to build mapping
            jira_fields_file = Path(self.data_dir) / "jira_custom_fields.json"
            if not jira_fields_file.exists():
                self.logger.warning(
                    "Jira custom fields file not found: %s. Returning empty mapping.",
                    jira_fields_file,
                )
                return {}

            with jira_fields_file.open() as f:
                jira_custom_fields = json.load(f)

            # Build mapping by matching names
            mapping = {}
            for jira_field in jira_custom_fields:
                jira_id = jira_field.get("id")
                jira_name = jira_field.get("name", "")
                jira_name_lower = jira_name.lower()

                op_field = op_fields_by_name.get(jira_name_lower)

                if op_field:
                    mapping[jira_id] = {
                        "jira_id": jira_id,
                        "jira_name": jira_name,
                        "openproject_id": op_field.get("id"),
                        "openproject_name": op_field.get("name"),
                        "openproject_type": op_field.get("field_format", "text"),
                        "matched_by": "name",
                    }

            self.logger.info(
                "Built custom field mapping from OpenProject: %d entries",
                len(mapping),
            )

            # Save to cache for future use (performance optimization)
            try:
                mapping_file.parent.mkdir(parents=True, exist_ok=True)
                with mapping_file.open("w") as f:
                    json.dump(mapping, f, indent=2)
                self.logger.info("Saved custom field mapping cache to %s", mapping_file)
            except Exception as e:
                self.logger.warning(
                    "Failed to save mapping cache to %s: %s. Continuing anyway.",
                    mapping_file,
                    e,
                )

            return mapping

        except Exception as e:
            msg = f"Error rebuilding custom field mapping from OpenProject: {e}"
            raise RuntimeError(msg) from e

    def _record_created_work_packages(
        self,
        created_list: list[dict[str, Any]],
        meta_slice: list[dict[str, Any]],
        op_project_id: int,
    ) -> None:
        """Record bulk-create results into ``self.work_package_mapping``.

        Walks ``created_list`` (the ``created`` array returned by
        :meth:`OpenProjectClient.bulk_create_records`) and pairs each row
        with the metadata that produced it via the ``index`` field. The
        on-disk mapping shape — ``{str(jira_id): {**meta, "openproject_id":
        op_id, "openproject_project_id": int(op_project_id)}}`` — is the
        public contract consumed by 20+ downstream migrations and is
        preserved verbatim from the four call-sites this helper replaces.

        Args:
            created_list: ``res["created"]`` from the bulk create call.
                Each item must carry ``index`` (paired meta position) and
                ``id`` (the new ``work_packages.id``).
            meta_slice: The ``work_packages_meta`` list (full or batch
                slice) the indices refer into.
            op_project_id: The OpenProject project the work packages
                belong to. Stored on every recorded row.

        """
        try:
            _ = self.work_package_mapping  # ensure attribute exists
        except AttributeError:
            self.work_package_mapping = {}
        for item in created_list:
            try:
                idx = item.get("index")
                op_id = item.get("id")
                if isinstance(idx, int) and 0 <= idx < len(meta_slice):
                    meta = meta_slice[idx]
                    jira_id = meta.get("jira_id")
                    if jira_id is not None:
                        self.work_package_mapping[str(jira_id)] = {
                            **meta,
                            "openproject_id": op_id,
                            "openproject_project_id": int(op_project_id),
                        }
            except (
                TypeError,
                AttributeError,
                ValueError,
            ):
                continue

    def _target_project_id(self, project_key: str) -> int | None:
        """Resolve a mapped project or find it by its OpenProject identifier."""
        for entry in self.project_mapping.values():
            if entry.get("jira_key") == project_key and entry.get("openproject_id"):
                return int(entry["openproject_id"])
        identifier = project_key.lower()
        query = f"Project.find_by(identifier: '{escape_ruby_single_quoted(identifier)}')&.id"
        target = self.op_client.execute_large_query_to_json_file(query, timeout=180)
        if isinstance(target, list):
            if len(target) > 1:
                msg = f"Ambiguous target project for {project_key}"
                raise ValueError(msg)
            target = next(iter(target), None)
        if not target:
            return None
        project_id = int(target)
        self.project_mapping[project_key] = {
            "jira_key": project_key,
            "openproject_id": project_id,
            "openproject_identifier": identifier,
        }
        return project_id

    def _load_confirmed_work_packages(self) -> None:
        """Preserve mappings established by earlier runs before accepting new target IDs."""
        mapping_path = Path(self.data_dir) / "work_package_mapping.json"
        try:
            with mapping_path.open(encoding="utf-8") as source:
                existing = json.load(source)
        except FileNotFoundError:
            existing = {}
        if not isinstance(existing, dict):
            msg = f"Invalid work package mapping in {mapping_path}"
            raise ValueError(msg)
        self.work_package_mapping = {**existing, **self.work_package_mapping}

    def _write_new_work_packages(
        self,
        records: list[dict[str, Any]],
        metadata: list[dict[str, Any]],
        project_key: str,
        project_id: int,
    ) -> int:
        """Submit once and persist the source IDs of confirmed creates, including partial batches."""
        reject_legacy_journal_operations(records)
        fallback_admin = None
        with contextlib.suppress(Exception):
            fallback_admin = self.op_client.execute_large_query_to_json_file(
                "User.where(admin: true).limit(1).pluck(:id).first",
                timeout=60,
            )
        _apply_required_defaults(
            records,
            project_id=project_id,
            op_client=self.op_client,
            fallback_admin_user_id=fallback_admin if type(fallback_admin) is int else None,
        )
        for record in records:
            if not record.get("project_id"):
                record["project_id"] = project_id
            record.pop("_log_counters", None)
            # The Rails bulk endpoint accepts numeric foreign keys and jira_id for provenance.
            record.pop("_links", None)
            record.pop("jira_issue_id", None)
        intent = self._claim_new_work_packages(metadata, project_key, project_id)
        try:
            result = self.op_client.bulk_create_records(
                "WorkPackage",
                records,
                timeout=900,
                result_basename=f"work_packages_{project_key}",
            )
        except LegacyJournalReplayError:
            raise
        except Exception as exc:
            msg = f"Unconfirmed bulk write in {project_key}; reconcile the target by Jira origin ID before retrying"
            raise RuntimeError(msg) from exc
        created = self._confirm_created_work_packages(result, metadata, project_id, project_key)
        sync_directory(intent.parent)
        intent.unlink()
        sync_directory(intent.parent)
        return created

    def _claim_new_work_packages(self, metadata: list[dict[str, Any]], project_key: str, project_id: int) -> Path:
        """Reserve the sole legacy writer and leave the source IDs recoverable if the call is interrupted."""
        directory = Path(self.data_dir)
        directory.mkdir(parents=True, exist_ok=True)
        pending = directory / "legacy-work-package-create.pending.json"
        try:
            fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            msg = f"Legacy bulk create already in progress or interrupted; reconcile the target IDs in {pending} before removing it"
            raise RuntimeError(msg) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as intent:
                self._validate_new_source_ids(metadata)
                json.dump(
                    {
                        "project_key": project_key,
                        "openproject_project_id": project_id,
                        "issues": [{"jira_id": row["jira_id"], "jira_key": row["jira_key"]} for row in metadata],
                    },
                    intent,
                )
                intent.flush()
                os.fsync(intent.fileno())
            sync_directory(directory)
        except Exception:
            pending.unlink()
            raise
        return pending

    def _validate_new_source_ids(self, metadata: list[dict[str, Any]]) -> None:
        self._load_confirmed_work_packages()
        incoming_ids: set[str] = set()
        for meta in metadata:
            jira_id = str(meta["jira_id"])
            if jira_id in incoming_ids or jira_id in self.work_package_mapping:
                msg = f"Existing mapping for {meta['jira_key']} disagrees with the target inventory; reconcile before retrying"
                raise RuntimeError(msg)
            incoming_ids.add(jira_id)

    def _confirm_created_work_packages(
        self,
        result: Any,
        metadata: list[dict[str, Any]],
        project_id: int,
        project_key: str,
    ) -> int:
        """Require one unique positive target ID for each submitted index."""
        envelope = result if isinstance(result, dict) else {}
        created = envelope.get("created")
        rows = created if isinstance(created, list) else []
        confirmed: list[dict[str, Any]] = []
        seen_indices: set[int] = set()
        seen_ids: set[int] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            idx, op_id = row.get("index"), row.get("id")
            if not self._valid_created_pair(idx, op_id, len(metadata)):
                continue
            if idx in seen_indices or op_id in seen_ids:
                msg = f"Conflicting bulk result in {project_key}; reconcile target IDs before retrying"
                raise RuntimeError(msg)
            confirmed.append(row)
            seen_indices.add(idx)
            seen_ids.add(op_id)
        if confirmed:
            self._record_created_work_packages(confirmed, metadata, project_id)
            JsonFileMappingRepository(Path(self.data_dir)).set("work_package_mapping", self.work_package_mapping)
        if (
            len(confirmed) != len(metadata)
            or len(rows) != len(confirmed)
            or envelope.get("status") != "success"
            or envelope.get("errors")
            or envelope.get("created_count", len(confirmed)) != len(confirmed)
            or envelope.get("total", len(metadata)) != len(metadata)
            or envelope.get("error_count", 0) != 0
        ):
            msg = f"OpenProject confirmed {len(confirmed)} of {len(metadata)} creates in {project_key}; reconcile before retrying"
            raise RuntimeError(msg)
        return len(confirmed)

    @staticmethod
    def _valid_created_pair(index: Any, target_id: Any, size: int) -> bool:
        return type(index) is int and 0 <= index < size and type(target_id) is int and target_id > 0

    def _migrate_project_issues(self, project_key: str, project_id: int, batch_size: int) -> tuple[int, int]:
        existing = self._get_existing_work_packages(project_id)
        known = mapped_project_issues(project_key, self.work_package_mapping, existing)
        warn_if_restricted_mappings(self.jira_client, project_key, known, self.logger)
        created = issues = 0
        records: list[dict[str, Any]] = []
        metadata: list[dict[str, Any]] = []
        for issue in self._iter_all_project_issues(project_key):
            issues += 1
            jira_key, jira_id = getattr(issue, "key", None), getattr(issue, "id", None)
            if not jira_id or not jira_key:
                msg = f"Missing Jira issue identity in project {project_key}"
                raise ValueError(msg)
            if jira_key in existing:
                self._update_existing_work_package(issue, existing[jira_key], project_id)
                continue
            record = self.prepare_work_package(issue, project_id)
            reject_legacy_journal_operations([record])
            records.append(record)
            metadata.append(
                {
                    **self._extract_issue_meta(issue),
                    "jira_id": jira_id,
                    "jira_key": jira_key,
                    "project_key": project_key,
                }
            )
            if len(records) >= batch_size:
                created += self._write_new_work_packages(records, metadata, project_key, project_id)
                records, metadata = [], []
        if records:
            created += self._write_new_work_packages(records, metadata, project_key, project_id)
        return created, issues

    def _migrate_work_packages(self) -> dict[str, Any]:
        """Create new history-free work packages and report each confirmed target ID."""
        results: dict[str, Any] = {"total_created": 0, "projects": [], "total_issues": 0}
        projects = config.selected_jira_projects() or list(
            dict.fromkeys(entry["jira_key"] for entry in self.project_mapping.values() if entry.get("jira_key"))
        )
        batch_size = max(1, int(config.migration_config.get("batch_size", 100)))
        for project_key in projects:
            project_id = self._target_project_id(project_key)
            if project_id is None:
                self.logger.warning("Project %s has no OpenProject target; skipping", project_key)
                results["projects"].append({"project_key": project_key, "created": 0, "skipped": True})
                continue
            created, issues = self._migrate_project_issues(project_key, project_id, batch_size)
            results["projects"].append({"project_key": project_key, "created": created, "issues": issues})
            results["total_created"] += created
            results["total_issues"] += issues
        return results

    def run(self) -> ComponentResult:  # type: ignore[override]
        start_time = datetime.now(tz=UTC)
        try:
            migration_results = self._migrate_work_packages()
            end_time = datetime.now(tz=UTC)
            duration_seconds = (end_time - start_time).total_seconds()
            result = ComponentResult(
                status="success",
                success=True,
                timestamp=end_time.isoformat(),
                start_time=start_time.isoformat(),
                duration_seconds=duration_seconds,
                data=migration_results,
            )
            if isinstance(migration_results, dict) and "total_created" in migration_results:
                result.success_count = migration_results["total_created"]
            return result
        except Exception as e:
            end_time = datetime.now(tz=UTC)
            duration_seconds = (end_time - start_time).total_seconds()
            return ComponentResult(
                status="error",
                success=False,
                failed_count=1,
                error=str(e),
                timestamp=end_time.isoformat(),
                start_time=start_time.isoformat(),
                duration_seconds=duration_seconds,
            )


def _normalized_status(record: dict[str, Any], default: int, available: set[int]) -> Any:
    status = record.get("status_id")
    if not status:
        return default
    try:
        status_id = int(status)
    except TypeError, ValueError:
        return default
    return default if available and status_id not in available else status


def _check_due_date(record: dict[str, Any]) -> None:
    start, due = record.get("start_date"), record.get("due_date")
    if not (start and due):
        return
    try:
        start = datetime.strptime(start, "%Y-%m-%d").date() if isinstance(start, str) else start
        due = datetime.strptime(due, "%Y-%m-%d").date() if isinstance(due, str) else due
        if isinstance(start, date) and isinstance(due, date) and due < start:
            record["due_date"] = None
    except ValueError, TypeError:
        record["due_date"] = None


def _apply_required_defaults(
    records: list[dict[str, Any]],
    *,
    project_id: int | None,
    op_client: Any,
    fallback_admin_user_id: int | str | None,
) -> None:
    """Normalize the legacy bulk payload, including the type link and target date constraint."""
    defaults, statuses = work_package_defaults(op_client, project_id, fallback_admin_user_id)
    valid_statuses = {int(value) for value in statuses if str(value).isdigit()}
    for record in records:
        if not record.get("type_id"):
            record.setdefault("_links", {})["type"] = {"href": f"/api/v3/types/{defaults['type_id']}"}
        record["status_id"] = _normalized_status(record, defaults["status_id"], valid_statuses)
        fill_missing_fields(record, defaults)
        _check_due_date(record)
