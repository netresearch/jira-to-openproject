"""Work package migration module for Jira to OpenProject migration.
Handles the migration of issues from Jira to work packages in OpenProject.

IMPORTANT: Journal Migration Reference
======================================
This module contains Bug #22 fix (lines 1758-1776): Always create operations for ALL changelogs.
See claudedocs/ADR_003_journal_migration_complete_journey.md for complete context.

Critical Pattern (Bug #22 lesson):
    # ✅ ALWAYS create operations for ALL changelogs, even with empty notes
    notes = "\\n".join(changelog_notes) if changelog_notes else ""
    work_package["_rails_operations"].append({...})

    # ❌ NEVER use conditional creation (loses 95% of history)
    if changelog_notes:
        work_package["_rails_operations"].append({...})
"""

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from jira import Issue
from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import ProgressTracker, configure_logging
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult
from src.utils import data_handler
from src.utils.enhanced_audit_trail_migrator import EnhancedAuditTrailMigrator
from src.utils.enhanced_timestamp_migrator import EnhancedTimestampMigrator
from src.utils.enhanced_user_association_migrator import EnhancedUserAssociationMigrator
from src.utils.markdown_converter import MarkdownConverter

try:
    from src.config import logger  # type: ignore
except Exception:
    logger = configure_logging("INFO", None)


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

        # Data storage
        self.jira_issues: dict[str, Any] = {}
        self.op_work_packages: dict[str, Any] = {}
        self.work_package_mapping: dict[str, Any] = {}
        # Some refactored tests expect an attribute named issue_mapping used by
        # time entry migration helpers; initialize defensively.
        self.issue_mapping: dict[str, Any] = {}

        # Mappings
        self.project_mapping: dict[str, Any] = {}
        self.user_mapping: dict[str, Any] = {}
        self.issue_type_mapping: dict[str, Any] = {}
        self.status_mapping: dict[str, Any] = {}

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

        # Initialize enhanced audit trail migrator
        self.enhanced_audit_trail_migrator = EnhancedAuditTrailMigrator(
            jira_client=jira_client,
            op_client=op_client,
        )

        # Time entry migration is now a separate component. Do not initialize here.

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
            from src import config as _cfg

            self.project_mapping = _cfg.mappings.get_mapping("project") or {}
            self.user_mapping = _cfg.mappings.get_mapping("user") or {}
            self.issue_type_mapping = _cfg.mappings.get_mapping("issue_type") or {}
            self.issue_type_id_mapping = _cfg.mappings.get_mapping("issue_type_id") or {}
            self.status_mapping = _cfg.mappings.get_mapping("status") or {}
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

        # Update markdown converter with loaded mappings
        self._update_markdown_converter_mappings()

    def _update_markdown_converter_mappings(self) -> None:
        """Update the markdown converter with current user and work package mappings."""
        # Create user mapping for markdown converter (Jira username -> OpenProject user login)
        # self.user_mapping values are dicts with openproject_id and openproject_login
        user_mapping = {}
        account_id_mapping = {}  # For Jira Cloud [~accountId:xxx] format
        for username, user_dict in self.user_mapping.items():
            if not user_dict:
                continue
            # Prefer login for @mention format, fall back to ID
            op_login = user_dict.get("openproject_login")
            op_id = user_dict.get("openproject_id")
            op_user = op_login if op_login else (str(op_id) if op_id else None)

            if op_user:
                user_mapping[username] = op_user

                # Also map by Jira accountId (for Cloud) and jira_id
                jira_account_id = user_dict.get("jira_account_id") or user_dict.get("jira_id")
                if jira_account_id:
                    account_id_mapping[jira_account_id] = op_user

        # For work package mapping, we need to load the existing mapping if available
        work_package_mapping = {}
        if hasattr(self, "work_package_mapping") and self.work_package_mapping:
            work_package_mapping = {
                entry.get("jira_key", ""): entry.get("openproject_id", "")
                for entry in self.work_package_mapping.values()
                if entry.get("jira_key") and entry.get("openproject_id")
            }

        # Update the markdown converter with new mappings
        self.markdown_converter = MarkdownConverter(
            user_mapping=user_mapping,
            work_package_mapping=work_package_mapping,
            account_id_mapping=account_id_mapping,
        )

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

        # Normalize version name
        version_name = str(version_name).strip()
        if not version_name:
            return None

        # Check cache first
        cache_key = (project_id, version_name)
        if cache_key in self._version_cache:
            return self._version_cache[cache_key]

        # Check if version exists in OpenProject
        try:
            query = f"""
                v = Version.find_by(project_id: {project_id}, name: {version_name!r})
                v ? {{ id: v.id, name: v.name }}.to_json : 'null'
            """
            result = self.op_client.execute_json_query(query)
            # Parse JSON if result is a string
            if isinstance(result, str):
                try:
                    import json

                    result = json.loads(result)
                except (json.JSONDecodeError, TypeError):
                    pass
            if isinstance(result, dict) and result.get("id"):
                version_id = int(result["id"])
                self._version_cache[cache_key] = version_id
                self.logger.debug(
                    f"[VERSION] Found existing version '{version_name}' (ID: {version_id}) in project {project_id}"
                )
                return version_id
        except Exception as e:
            self.logger.debug(f"[VERSION] Error checking version existence: {e}")

        # Create version on-the-fly
        try:
            create_query = f"""
                v = Version.new(
                    project_id: {project_id},
                    name: {version_name!r},
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
                        f"[VERSION] Created version '{version_name}' (ID: {version_id}) in project {project_id}"
                    )
                    return version_id
                if result.get("error"):
                    self.logger.warning(f"[VERSION] Failed to create version '{version_name}': {result['error']}")
        except Exception as e:
            self.logger.warning(f"[VERSION] Error creating version '{version_name}' in project {project_id}: {e}")

        return None

    def _extract_final_workflow(self, jira_issue: Any) -> str | None:
        """Extract the final/current workflow scheme name from Jira changelog.

        The Jira "Workflow" field in changelog represents workflow scheme changes
        (not status changes). We extract the most recent "toString" value to get
        the current workflow scheme name.

        Args:
            jira_issue: The Jira issue object

        Returns:
            Final workflow scheme name, or None if not found

        """
        try:
            # Access changelog from issue
            changelog = getattr(jira_issue, "changelog", None)
            if not changelog:
                return None

            histories = getattr(changelog, "histories", None)
            if not histories:
                return None

            # Find all Workflow field changes, sorted by date (most recent last)
            workflow_changes = []
            for history in histories:
                items = getattr(history, "items", [])
                for item in items:
                    field = getattr(item, "field", None) or (item.get("field") if isinstance(item, dict) else None)
                    if field == "Workflow":
                        to_string = getattr(item, "toString", None) or (
                            item.get("toString") if isinstance(item, dict) else None
                        )
                        if to_string:
                            created = getattr(history, "created", "")
                            workflow_changes.append((created, to_string))

            if workflow_changes:
                # Sort by timestamp and get the most recent
                workflow_changes.sort(key=lambda x: x[0])
                final_workflow = workflow_changes[-1][1]
                self.logger.debug(f"[WORKFLOW] Extracted final workflow scheme: {final_workflow}")
                return str(final_workflow)

        except Exception as e:
            self.logger.debug(f"[WORKFLOW] Error extracting workflow: {e}")

        return None

    def _load_start_date_fields(self) -> list[str]:
        fields = list(self.START_DATE_FIELD_IDS_DEFAULT)
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
        """Best-effort parsing for Jira/ISO datetime payloads."""
        if isinstance(value, datetime):
            if value.tzinfo:
                return value.astimezone(UTC)
            return value.replace(tzinfo=UTC)

        if not isinstance(value, str):
            return None

        candidate = value.strip()
        if not candidate:
            return None

        # Normalize Z suffix to ISO compatible form
        candidate_iso = candidate.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(candidate_iso)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            else:
                parsed = parsed.astimezone(UTC)
            return parsed
        except ValueError:
            pass

        patterns = [
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%dT%H:%M",
        ]
        for pattern in patterns:
            try:
                parsed = datetime.strptime(candidate, pattern)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                else:
                    parsed = parsed.astimezone(UTC)
                return parsed
            except ValueError:
                continue
        return None

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
            with sqlite3.connect(str(db_path)) as conn:
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
        parsed = self._parse_datetime(timestamp_value)
        return parsed

    def _derive_snapshot_timestamp(self, snapshot: list[dict[str, Any]]) -> datetime | None:
        """Derive the most recent migration timestamp from existing OpenProject rows."""
        latest: datetime | None = None
        for entry in snapshot or []:
            if not isinstance(entry, dict):
                continue
            for key in ("jira_migration_date", "updated_at", "updated_at_utc"):
                candidate = entry.get(key)
                parsed = self._parse_datetime(candidate)
                if parsed and (latest is None or parsed > latest):
                    latest = parsed
        return latest

    @staticmethod
    def _build_key_exclusion_clause(existing_keys: set[str]) -> str | None:
        """Return a JQL fragment for excluding already-migrated issue keys."""
        if not existing_keys:
            return None
        limited = [key for key in sorted({k.strip() for k in existing_keys if k and k.strip()}) if key][
            :200
        ]  # Keep under 8KB URL limit (200 keys × ~10 chars = ~2KB)
        if not limited:
            return None
        return f"key NOT IN ({','.join(limited)})"

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
            with sqlite3.connect(str(db_path_obj)) as conn:
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
        except Exception as exc:  # noqa: BLE001
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

        id_lookup: dict[str, dict[str, Any]] = {}
        name_lookup: dict[str, dict[str, Any]] = {}
        for status in statuses or []:
            if not isinstance(status, dict):
                continue
            status_id = str(status.get("id") or "").strip()
            status_name = str(status.get("name") or "").strip()
            category = status.get("statusCategory") or {}
            if status_id:
                id_lookup[status_id] = category
            if status_name:
                name_lookup[status_name.lower()] = category

        if fetched and statuses:
            try:
                self._save_to_json(statuses, "jira_statuses.json")
            except Exception:
                pass

        return id_lookup, name_lookup

    def _get_existing_work_packages(self, op_project_id: int) -> dict[str, dict[str, Any]]:
        """Get existing work packages from OpenProject for incremental updates.

        Returns a dict mapping Jira keys to OpenProject work package info.
        """
        try:
            snapshot = self.op_client.get_project_wp_cf_snapshot(op_project_id)
            existing_map = {}
            for row in snapshot:
                jira_key = row.get("jira_issue_key")
                if jira_key:
                    existing_map[str(jira_key).strip()] = {
                        "id": row.get("id"),
                        "jira_key": jira_key,
                    }
            return existing_map
        except Exception as e:
            self.logger.warning(f"Failed to fetch existing work packages: {e}")
            return {}

    def _update_existing_work_package(
        self,
        jira_issue: Issue,
        existing_wp: dict[str, Any],
        op_project_id: int,
    ) -> None:
        """Update an existing work package with new comments from Jira.

        Only adds comments that don't already exist in OpenProject.
        """
        from datetime import datetime, timedelta

        try:
            wp_id = existing_wp.get("id")
            jira_key = getattr(jira_issue, "key", None)

            if not wp_id:
                return

            # Bug #9 fix: Delete existing v2+ journals before adding new ones (idempotent migration)
            # This matches the deletion logic in create_work_package_journals.rb
            delete_journals_code = f"""
                v2_plus_journals = Journal.where(journable_id: {wp_id}, journable_type: 'WorkPackage').where('version > 1')
                v2_plus_count = v2_plus_journals.count
                if v2_plus_count > 0
                    v2_plus_ids = v2_plus_journals.pluck(:id)
                    data_ids = v2_plus_journals.pluck(:data_id).compact

                    # Delete associated customizable_journals first
                    Journal::CustomizableJournal.where(journal_id: v2_plus_ids).delete_all if v2_plus_ids.any?

                    # Delete the journals themselves
                    v2_plus_journals.delete_all

                    # Delete orphaned work_package_journals
                    Journal::WorkPackageJournal.where(id: data_ids).delete_all if data_ids.any?

                    puts "CLEANUP: WP#" + {wp_id}.to_s + " deleted " + v2_plus_count.to_s + " existing v2+ journals for re-migration"
                end
                puts "OK"
            """
            cleanup_result = self.op_client.execute_large_query_to_json_file(delete_journals_code, timeout=60)
            if cleanup_result and "CLEANUP" in str(cleanup_result):
                self.logger.info(f"Cleaned up v2+ journals for WP#{wp_id} ({jira_key})")

            # Extract BOTH comments AND changelog entries from Jira
            comments = self.enhanced_audit_trail_migrator.extract_comments_from_issue(jira_issue)
            changelog_entries = self.enhanced_audit_trail_migrator.extract_changelog_from_issue(jira_issue)

            # Merge comments and changelog entries into unified journal entries
            all_journal_entries = []

            # Add comments as journal entries
            for comment in comments:
                all_journal_entries.append(
                    {
                        "type": "comment",
                        "timestamp": comment.get("created", ""),
                        "data": comment,
                    }
                )

            # Add changelog entries as journal entries
            for entry in changelog_entries:
                all_journal_entries.append(
                    {
                        "type": "changelog",
                        "timestamp": entry.get("created", ""),
                        "data": entry,
                    }
                )

            # If no journal entries, return
            if not all_journal_entries:
                return

            # Sort ALL entries chronologically by timestamp
            all_journal_entries.sort(key=lambda x: x.get("timestamp", ""))

            # DEBUG: Log entries to understand why collision detection isn't executing
            self.logger.info(f"[DEBUG] {jira_key}: all_journal_entries has {len(all_journal_entries)} entries")
            if len(all_journal_entries) > 0:
                for idx, entry in enumerate(all_journal_entries):
                    self.logger.info(
                        f"[DEBUG] {jira_key}: Entry[{idx}] type={entry.get('type')} timestamp={entry.get('timestamp')}"
                    )

            # Fix Attempt #5: Detect and resolve timestamp collisions
            # When comment and changelog entry have identical timestamps, add microsecond offsets
            # to ensure unique timestamps and valid validity_period ranges
            for i in range(1, len(all_journal_entries)):
                current_timestamp = all_journal_entries[i].get("timestamp", "")
                previous_timestamp = all_journal_entries[i - 1].get("timestamp", "")

                # Check if timestamps collide
                if current_timestamp and previous_timestamp and current_timestamp == previous_timestamp:
                    # Parse the timestamp
                    try:
                        if "T" in current_timestamp:
                            # ISO8601 format: 2011-08-23T13:41:21.000+0000
                            # Parse timestamp
                            dt = datetime.fromisoformat(current_timestamp.replace("Z", "+00:00"))
                            # Add 1 SECOND to separate colliding entries (OpenProject uses second-precision timestamps)
                            dt = dt + timedelta(seconds=1)
                            # Convert back to ISO8601 format
                            all_journal_entries[i]["timestamp"] = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+0000"
                            self.logger.info(
                                f"Resolved timestamp collision for {jira_key}: {previous_timestamp} → {all_journal_entries[i]['timestamp']}"
                            )
                    except Exception as e:
                        self.logger.warning(f"Failed to resolve timestamp collision for {jira_key}: {e}")

            # Get existing Journal entries for this work package
            existing_journals_query = f"""
                Journal.where(journable_id: {wp_id}, journable_type: 'WorkPackage')
                       .where.not(notes: [nil, ''])
                       .pluck(:notes, :created_at)
            """
            existing_journals = self.op_client.execute_large_query_to_json_file(
                existing_journals_query,
                timeout=60,
            )

            existing_notes = set()
            if isinstance(existing_journals, list):
                for entry in existing_journals:
                    if isinstance(entry, list) and len(entry) > 0:
                        existing_notes.add(entry[0])

            # Filter out journal entries that already exist (check comments only for duplicates)
            new_journal_entries = []
            for journal_entry in all_journal_entries:
                entry_data = journal_entry["data"]

                # For comments, check if already exists
                if journal_entry["type"] == "comment":
                    comment_body = entry_data.get("body", "")
                    if not comment_body or comment_body in existing_notes:
                        continue

                # For changelog entries, always include (they don't have text to deduplicate)
                new_journal_entries.append(journal_entry)

            # Create journals with non-overlapping validity_period ranges
            for i, journal_entry in enumerate(new_journal_entries):
                entry_type = journal_entry["type"]
                entry_data = journal_entry["data"]
                entry_timestamp = journal_entry["timestamp"]

                # Extract author information
                author_info = entry_data.get("author") or {}
                author_name = author_info.get("name")
                user_dict = self.user_mapping.get(author_name) if author_name else None
                entry_author_id = user_dict.get("openproject_id") if user_dict else None
                if not entry_author_id:
                    entry_author_id = 1  # Fallback to admin

                # Build journal notes based on entry type
                if entry_type == "comment":
                    raw_body = entry_data.get("body", "")
                    # Convert Jira wiki markup to OpenProject markdown
                    if raw_body and hasattr(self, "markdown_converter") and self.markdown_converter:
                        try:
                            journal_notes = self.markdown_converter.convert(raw_body)
                        except Exception:
                            journal_notes = raw_body
                    else:
                        journal_notes = raw_body
                else:  # changelog
                    # Format changelog items as notes
                    journal_notes = "Jira changelog:\n"
                    items = entry_data.get("items", [])
                    for item in items:
                        field = item.get("field", "")
                        from_val = item.get("fromString") or item.get("from", "")
                        to_val = item.get("toString") or item.get("to", "")
                        # Convert field values through markdown converter (may contain user mentions)
                        if hasattr(self, "markdown_converter") and self.markdown_converter:
                            if from_val:
                                from_val = self.markdown_converter.convert(str(from_val))
                            if to_val:
                                to_val = self.markdown_converter.convert(str(to_val))
                        journal_notes += f"- {field}: {from_val} → {to_val}\n"

                comment_body = journal_notes
                comment_author_id = entry_author_id
                comment_created = entry_timestamp

                # Determine if this is the last journal entry
                is_last_comment = i == len(new_journal_entries) - 1

                # Calculate validity_period
                # - For all except last: closed range ending at next comment's timestamp
                # - For last comment: open-ended range
                if is_last_comment:
                    # Last comment: OPEN-ENDED range (most recent journal version has no end)
                    if comment_created and "T" in comment_created:
                        validity_start_iso = comment_created
                    elif comment_created:
                        try:
                            # Try parsing with milliseconds first
                            dt = datetime.strptime(comment_created, "%Y-%m-%d %H:%M:%S.%f")
                            validity_start_iso = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                        except ValueError:
                            try:
                                # Try parsing without milliseconds
                                dt = datetime.strptime(comment_created, "%Y-%m-%d %H:%M:%S")
                                validity_start_iso = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                            except ValueError as e:
                                self.logger.warning(f"Failed to parse timestamp '{comment_created}': {e}, using as-is")
                                validity_start_iso = comment_created
                    else:
                        validity_start_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

                    # Bug #15 fix Attempt #2: Open-ended range for most recent journal version
                    # Mark this as open-ended by setting validity_end_iso to None
                    validity_end_iso = None
                    validity_period = f'["{validity_start_iso}",)'  # Open-ended range: no end time
                else:
                    # Not last: closed range ending at next journal entry's timestamp
                    next_journal_entry = new_journal_entries[i + 1]
                    next_created = next_journal_entry["timestamp"]

                    # Convert both timestamps to ISO8601 - ALWAYS format to preserve milliseconds
                    if comment_created:
                        try:
                            # First try parsing ISO8601 format with timezone (from collision detection)
                            dt = datetime.strptime(comment_created, "%Y-%m-%dT%H:%M:%S.%f%z")
                            validity_start_iso = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                        except ValueError:
                            try:
                                # Try parsing with milliseconds (database format)
                                dt = datetime.strptime(comment_created, "%Y-%m-%d %H:%M:%S.%f")
                                validity_start_iso = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                            except ValueError:
                                try:
                                    # Try parsing without milliseconds
                                    dt = datetime.strptime(comment_created, "%Y-%m-%d %H:%M:%S")
                                    validity_start_iso = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                                except ValueError as e:
                                    self.logger.warning(
                                        f"Failed to parse timestamp '{comment_created}': {e}, using as-is"
                                    )
                                    validity_start_iso = comment_created
                    else:
                        validity_start_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

                    if next_created:
                        try:
                            # First try parsing ISO8601 format with timezone (from collision detection)
                            dt = datetime.strptime(next_created, "%Y-%m-%dT%H:%M:%S.%f%z")
                            validity_end_iso = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                        except ValueError:
                            try:
                                # Try parsing with milliseconds (database format)
                                dt = datetime.strptime(next_created, "%Y-%m-%d %H:%M:%S.%f")
                                validity_end_iso = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                            except ValueError:
                                try:
                                    # Try parsing without milliseconds
                                    dt = datetime.strptime(next_created, "%Y-%m-%d %H:%M:%S")
                                    validity_end_iso = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                                except ValueError as e:
                                    self.logger.warning(f"Failed to parse timestamp '{next_created}': {e}, using as-is")
                                    validity_end_iso = next_created
                    # else: Leave validity_end_iso as None for open-ended range (last comment)

                    # Only set validity_period string if validity_end_iso is not None
                    if validity_end_iso is not None:
                        validity_period = f'["{validity_start_iso}", "{validity_end_iso}")'
                    else:
                        validity_period = None

                # Bug #14/#15 fix - Build Ruby validity_period code as single-line expressions
                if validity_end_iso is None:
                    # Open-ended range for most recent journal version
                    validity_period_ruby = (
                        f"journal.validity_period = Range.new(Time.parse('{validity_start_iso}'), nil)"
                    )
                else:
                    # Closed range for intermediate comments - use Range.new() for consistency
                    validity_period_ruby = f"journal.validity_period = Range.new(Time.parse('{validity_start_iso}'), Time.parse('{validity_end_iso}'))"

                # Bug #11 fix #7 - Populate WorkPackageJournal with work package snapshot
                rails_code = f"""
                    wp = WorkPackage.find({wp_id})
                    max_version = Journal.where(journable_id: {wp_id}, journable_type: 'WorkPackage').maximum(:version) || 0

                    # Bug #15 fix Attempt #4 - Update previous journal's validity_period end time using HALF-OPEN range
                    # Find the most recent journal (before we add the new one)
                    most_recent_journal = Journal.where(journable_id: {wp_id}, journable_type: 'WorkPackage')
                                                 .order(version: :desc)
                                                 .first

                    # Update the most recent journal's validity_period to end at the new comment's start time
                    # Bug #16 fix: Only update if new_comment_start_time >= current_start to avoid invalid ranges
                    if most_recent_journal
                        new_comment_start_time = Time.parse('{validity_start_iso}')
                        # Get the current start time of the most recent journal
                        current_start = most_recent_journal.validity_period.begin

                        # Bug #16 fix: Check if comment timestamp is AFTER journal start
                        # If comment timestamp is BEFORE journal start, skip update to avoid creating invalid range [T1, T0] where T1 > T0
                        if new_comment_start_time >= current_start
                            # Create new HALF-OPEN range [start, end) where end is EXCLUSIVE to prevent overlap
                            # This ensures no conflict with the new journal starting at new_comment_start_time
                            most_recent_journal.validity_period = Range.new(current_start, new_comment_start_time, true)
                            most_recent_journal.save(validate: false)
                        end
                    end

                    # Create Journal first
                    journal = Journal.new(
                        journable_id: {wp_id},
                        journable_type: 'WorkPackage',
                        user_id: {comment_author_id},
                        version: max_version + 1,
                        notes: {comment_body!r}
                    )

                    # Create WorkPackageJournal with snapshot of work package attributes (Bug #11 fix #7)
                    data = Journal::WorkPackageJournal.new(
                        type_id: wp.type_id,
                        project_id: wp.project_id,
                        subject: wp.subject,
                        description: wp.description,
                        due_date: wp.due_date,
                        category_id: wp.category_id,
                        status_id: wp.status_id,
                        assigned_to_id: wp.assigned_to_id,
                        priority_id: wp.priority_id,
                        version_id: wp.version_id,
                        author_id: wp.author_id,
                        done_ratio: wp.done_ratio,
                        estimated_hours: wp.estimated_hours,
                        start_date: wp.start_date,
                        parent_id: wp.parent_id,
                        responsible_id: wp.responsible_id,
                        budget_id: wp.budget_id,
                        story_points: wp.story_points,
                        remaining_hours: wp.remaining_hours,
                        derived_estimated_hours: wp.derived_estimated_hours,
                        schedule_manually: wp.schedule_manually,
                        duration: wp.duration,
                        ignore_non_working_days: wp.ignore_non_working_days,
                        derived_remaining_hours: wp.derived_remaining_hours,
                        derived_done_ratio: wp.derived_done_ratio,
                        project_phase_definition_id: wp.project_phase_definition_id
                    )
                    journal.data = data
                    # Bug #14/#15 fix - Set validity_period as Ruby Range object before save
                    {validity_period_ruby}

                    if journal.save(validate: false)
                        journal.update_column(:created_at, '{comment_created}') if '{comment_created}' != ''
                        puts journal.id
                    else
                        puts "ERROR: " + journal.errors.full_messages.join(", ")
                    end
                """

                result = self.op_client.execute_large_query_to_json_file(rails_code, timeout=60)

            if len(new_journal_entries) > 0:
                self.logger.info(f"Added {len(new_journal_entries)} new journal entries to {jira_key} (WP#{wp_id})")

        except Exception as e:
            self.logger.warning(f"Failed to update existing work package {existing_wp.get('jira_key')}: {e}")

    def _build_rails_ops_for_issue(
        self,
        jira_issue: Issue,
        existing_wp: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Build rails_ops for a single Jira issue's journals with full pre-computation.

        Pre-computes in Python (fast, parallel):
        - Version numbers (v2, v3, v4...)
        - validity_period ranges as ISO strings
        - field_changes mapped to OpenProject field names/IDs
        - user_id from mapping
        - notes from comments/changelog
        - cf_state_snapshot for J2O Workflow/Resolution

        Ruby only does (requires DB access):
        - Read WP initial state
        - Apply field_changes to build state_snapshot
        - Bulk INSERT

        Returns list of operations ready for Ruby bulk processing.
        """
        rails_ops: list[dict[str, Any]] = []
        jira_key = getattr(jira_issue, "key", "unknown")

        try:
            # Extract comments and changelog
            comments = self.enhanced_audit_trail_migrator.extract_comments_from_issue(jira_issue)
            changelog_entries = self.enhanced_audit_trail_migrator.extract_changelog_from_issue(jira_issue)

            # Merge into unified entries
            all_entries: list[dict[str, Any]] = []
            for comment in comments:
                all_entries.append(
                    {
                        "type": "comment",
                        "timestamp": comment.get("created", ""),
                        "data": comment,
                    }
                )
            for entry in changelog_entries:
                all_entries.append(
                    {
                        "type": "changelog",
                        "timestamp": entry.get("created", ""),
                        "data": entry,
                    }
                )

            if not all_entries:
                return rails_ops

            # Sort chronologically
            all_entries.sort(key=lambda x: x.get("timestamp", ""))

            # Resolve timestamp collisions (add 1 second offset) and track all timestamps
            resolved_timestamps: list[str] = []
            for i, entry in enumerate(all_entries):
                curr_ts = entry.get("timestamp", "")
                if i > 0 and curr_ts:
                    prev_ts = resolved_timestamps[i - 1] if resolved_timestamps else ""
                    if prev_ts and curr_ts <= prev_ts:
                        # Collision: add 1 second to previous
                        try:
                            if "T" in prev_ts:
                                dt = datetime.fromisoformat(prev_ts.replace("Z", "+00:00").replace("+0000", "+00:00"))
                            else:
                                dt = datetime.fromisoformat(prev_ts)
                            dt = dt + timedelta(seconds=1)
                            curr_ts = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+0000"
                            entry["timestamp"] = curr_ts
                        except Exception:
                            pass
                resolved_timestamps.append(curr_ts)

            # Pre-compute validity_period ranges
            # Each entry's validity starts at its timestamp and ends at next entry's timestamp
            validity_periods: list[tuple[str, str | None]] = []
            for i, entry in enumerate(all_entries):
                start_ts = entry.get("timestamp", "")
                if i < len(all_entries) - 1:
                    # Not last: ends at next entry's start
                    end_ts = all_entries[i + 1].get("timestamp", "")
                else:
                    # Last entry: open-ended (None)
                    end_ts = None
                validity_periods.append((start_ts, end_ts))

            # Field mapping from Jira to OpenProject
            jira_to_op_field = {
                "summary": "subject",
                "description": "description",
                "status": "status_id",
                "assignee": "assigned_to_id",
                "priority": "priority_id",
                "issuetype": "type_id",
                "reporter": "author_id",
                "Fix Version": "version_id",
                "fixVersion": "version_id",
                "Component": "category_id",
                "component": "category_id",
                "duedate": "due_date",
                "Story Points": "story_points",
            }

            # Track J2O Workflow/Resolution for cf_state_snapshot
            cf_workflow_value: str | None = None
            cf_resolution_value: str | None = None

            # Build rails_ops with full pre-computation
            for i, entry in enumerate(all_entries):
                entry_type = entry["type"]
                entry_data = entry["data"]
                entry_ts = entry.get("timestamp", "")
                validity_start, validity_end = validity_periods[i]

                # Get author and map to OP user_id
                author_info = entry_data.get("author") or {}
                author_name = author_info.get("name")
                user_dict = self.user_mapping.get(author_name) if author_name else None
                user_id = user_dict.get("openproject_id") if user_dict else 1

                # Build field_changes for changelog entries (mapped to OP field names)
                field_changes: dict[str, Any] = {}
                notes = ""

                if entry_type == "comment":
                    # Comments have no field changes, just notes
                    raw_body = entry_data.get("body", "")
                    # Convert Jira markup to markdown if converter available
                    if hasattr(self, "markdown_converter") and self.markdown_converter:
                        try:
                            notes = self.markdown_converter.convert(raw_body)
                        except Exception:
                            notes = raw_body
                    else:
                        notes = raw_body
                else:
                    # Changelog: extract field changes and build notes
                    items = entry_data.get("items", [])
                    notes_lines = []

                    for item in items:
                        jira_field = item.get("field", "")
                        field_id = item.get("fieldId", "")
                        from_val = item.get("from")
                        from_str = item.get("fromString", "")
                        to_val = item.get("to")
                        to_str = item.get("toString", "")

                        # Track if this field was successfully mapped to field_changes
                        field_mapped = False

                        # Map to OP field name
                        op_field = jira_to_op_field.get(jira_field.lower(), jira_to_op_field.get(jira_field))

                        if op_field:
                            # Use ID values for _id fields, string values otherwise
                            if op_field.endswith("_id"):
                                # Map IDs through our mappings
                                if op_field == "status_id" and to_val:
                                    mapped_to = (
                                        self.status_mapping.get(to_val, {}).get("openproject_id")
                                        if self.status_mapping
                                        else None
                                    )
                                    mapped_from = (
                                        self.status_mapping.get(from_val, {}).get("openproject_id")
                                        if self.status_mapping and from_val
                                        else None
                                    )
                                    if mapped_to:
                                        field_changes[op_field] = [mapped_from, mapped_to]
                                        field_mapped = True
                                elif op_field == "type_id" and to_val:
                                    mapped_to = (
                                        self.issue_type_mapping.get(to_val, {}).get("openproject_id")
                                        if self.issue_type_mapping
                                        else None
                                    )
                                    mapped_from = (
                                        self.issue_type_mapping.get(from_val, {}).get("openproject_id")
                                        if self.issue_type_mapping and from_val
                                        else None
                                    )
                                    if mapped_to:
                                        field_changes[op_field] = [mapped_from, mapped_to]
                                        field_mapped = True
                                elif op_field == "assigned_to_id":
                                    # Map user names to IDs
                                    mapped_to = (
                                        self.user_mapping.get(to_str, {}).get("openproject_id")
                                        if self.user_mapping and to_str
                                        else None
                                    )
                                    mapped_from = (
                                        self.user_mapping.get(from_str, {}).get("openproject_id")
                                        if self.user_mapping and from_str
                                        else None
                                    )
                                    field_changes[op_field] = [mapped_from, mapped_to]
                                    field_mapped = True
                                elif op_field == "author_id":
                                    mapped_to = (
                                        self.user_mapping.get(to_str, {}).get("openproject_id")
                                        if self.user_mapping and to_str
                                        else None
                                    )
                                    mapped_from = (
                                        self.user_mapping.get(from_str, {}).get("openproject_id")
                                        if self.user_mapping and from_str
                                        else None
                                    )
                                    field_changes[op_field] = [mapped_from, mapped_to]
                                    field_mapped = True
                                elif op_field == "priority_id" and to_str:
                                    # Priority uses string names that Ruby will resolve
                                    field_changes[op_field] = [from_str, to_str]
                                    field_mapped = True
                                else:
                                    # Generic ID field
                                    field_changes[op_field] = [from_val, to_val]
                                    field_mapped = True
                            else:
                                # Non-ID fields use string values
                                field_changes[op_field] = [from_str, to_str]
                                field_mapped = True

                        # Track J2O Workflow/Resolution for cf_state_snapshot
                        if jira_field.lower() == "workflow" or field_id == "customfield_10500":
                            cf_workflow_value = to_str or to_val
                        elif jira_field.lower() == "resolution":
                            cf_resolution_value = to_str or to_val

                        # Build human-readable notes ONLY for unmapped fields (not already in field_changes)
                        if not field_mapped and jira_field:
                            notes_lines.append(f"**{jira_field}**: {from_str or '(none)'} → {to_str or '(none)'}")

                    notes = "\n".join(notes_lines) if notes_lines else ""

                # Build cf_state_snapshot for J2O custom fields
                cf_state_snapshot: dict[str, str] | None = None
                if cf_workflow_value or cf_resolution_value:
                    cf_state_snapshot = {}
                    # We need the CF IDs - these are loaded elsewhere, use placeholders
                    # Ruby will look these up by name if needed
                    if cf_workflow_value:
                        cf_state_snapshot["workflow"] = cf_workflow_value
                    if cf_resolution_value:
                        cf_state_snapshot["resolution"] = cf_resolution_value

                # Build the operation with all pre-computed data
                op: dict[str, Any] = {
                    "type": "journal",
                    "created_at": entry_ts,
                    "user_id": user_id,
                    "notes": notes,
                    "version": i + 2,  # v2, v3, v4... (v1 is creation journal)
                    "validity_period_start": validity_start,
                    "validity_period_end": validity_end,  # None for last entry (open-ended)
                }

                # Only include field_changes if non-empty
                if field_changes:
                    op["field_changes"] = field_changes

                # Only include cf_state_snapshot if we have values
                if cf_state_snapshot:
                    op["cf_state_snapshot"] = cf_state_snapshot

                rails_ops.append(op)

        except Exception as e:
            self.logger.warning(f"Failed to build rails_ops for {jira_key}: {e}")
            import traceback

            self.logger.debug(traceback.format_exc())

        return rails_ops

    def _update_existing_work_packages_batch(
        self,
        batch: list[tuple[Issue, dict[str, Any], int]],
    ) -> tuple[int, int]:
        """Process a batch of existing work packages with parallel Jira fetch.

        This method:
        1. Fetches Jira changelogs in parallel (safe - Jira API is thread-safe)
        2. Builds rails_ops for all WPs
        3. Sends ONE Rails call to process all WPs

        Returns (success_count, error_count)
        """
        if not batch:
            return 0, 0

        batch_size = len(batch)
        self.logger.info(f"Processing batch of {batch_size} WPs with parallel Jira fetch")

        # Step 1: Parallel fetch Jira changelogs and build rails_ops
        # ThreadPoolExecutor is safe for Jira API calls
        batch_data = []

        def fetch_and_build(item):
            issue, existing_wp, project_id = item
            jira_key = getattr(issue, "key", "unknown")
            wp_id = existing_wp.get("id")
            rails_ops = self._build_rails_ops_for_issue(issue, existing_wp)
            return {
                "wp_id": wp_id,
                "jira_key": jira_key,
                "rails_ops": rails_ops,
            }

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(fetch_and_build, item): item for item in batch}
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result["rails_ops"]:  # Only include WPs with actual operations
                        batch_data.append(result)
                except Exception as e:
                    item = futures[future]
                    jira_key = getattr(item[0], "key", "unknown")
                    self.logger.warning(f"Failed to fetch/build for {jira_key}: {e}")

        if not batch_data:
            self.logger.info("No operations to process in this batch")
            return batch_size, 0  # All succeeded with 0 changes

        self.logger.info(f"Built operations for {len(batch_data)} WPs, executing batch Ruby script")

        # Step 2: Execute batch Ruby script
        try:
            # Read the batch Ruby script
            script_path = Path(__file__).parent.parent / "ruby" / "create_work_package_journals_batch.rb"
            with open(script_path) as f:
                ruby_script = f.read()

            # Execute via Rails console using execute_script_with_data
            # This properly transfers batch_data as input_data and handles JSON output markers
            result = self.op_client.execute_script_with_data(ruby_script, batch_data, timeout=300)

            # Parse results from the returned dict
            success_count = 0
            error_count = 0

            if isinstance(result, dict) and result.get("status") == "success":
                results = result.get("data", [])
                if isinstance(results, list):
                    for r in results:
                        if r.get("error"):
                            self.logger.warning(f"Batch error for {r.get('jira_key')}: {r.get('error')}")
                            error_count += 1
                        else:
                            created = r.get("created", 0)
                            if created > 0:
                                self.logger.debug(
                                    f"Added {created} journals to {r.get('jira_key')} (WP#{r.get('wp_id')})"
                                )
                            success_count += 1
                else:
                    self.logger.warning(f"Unexpected data format in batch result: {type(results)}")
                    error_count = len(batch_data)
            elif isinstance(result, dict) and result.get("status") == "error":
                self.logger.warning(f"Batch execution error: {result.get('message', 'Unknown error')}")
                error_count = len(batch_data)
            else:
                self.logger.warning(f"Unexpected batch result format: {str(result)[:200]}")
                error_count = len(batch_data)

            return success_count, error_count

        except Exception as e:
            self.logger.warning(f"Batch execution failed: {e}")
            return 0, len(batch_data)

    def _iter_all_project_issues(
        self,
        project_key: str,
    ) -> Iterator[Issue]:
        """Fetch ALL Jira issues for a project without any filtering.

        Used for incremental migrations to process all issues.
        Includes renderedFields expansion for comment data extraction.
        """
        start_at = 0
        batch_size = config.migration_config.get("batch_size", 100)
        jql = f'project = "{project_key}" ORDER BY created ASC'
        fields = None  # Get all fields
        expand = "changelog,renderedFields"  # Include changelog and comments

        # Check for issue limit or specific test issues (for testing purposes)
        max_issues = None
        test_issues = None

        if os.getenv("J2O_TEST_ISSUES"):
            # Specific issue keys to test (comma-separated)
            test_issues = [k.strip() for k in os.getenv("J2O_TEST_ISSUES").split(",")]
            self.logger.info(f"Testing specific issues: {test_issues}")
            # Format issue keys for Jira JQL IN clause
            issue_keys_str = ",".join(f'"{k}"' for k in test_issues)
            jql = f'project = "{project_key}" AND key IN ({issue_keys_str}) ORDER BY created ASC'
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
            from src.clients.jira_client import JiraResourceNotFoundError

            msg = f"Project '{project_key}' not found: {e!s}"
            raise JiraResourceNotFoundError(msg) from e

        total_yielded = 0
        while True:
            issues_batch = self._fetch_issues_with_retry(
                jql=jql,
                start_at=start_at,
                max_results=batch_size,
                fields=fields,
                expand=expand,
                project_key=project_key,
            )

            if not issues_batch:
                break

            for issue in issues_batch:
                yield issue
                total_yielded += 1
                # Check if we've reached the limit
                if max_issues and total_yielded >= max_issues:
                    self.logger.info(f"Reached issue limit ({max_issues}), stopping")
                    return

            self.logger.debug(f"Yielded {len(issues_batch)} issues (total: {total_yielded}) for {project_key}")

            if len(issues_batch) < batch_size:
                break

            start_at += len(issues_batch)

        self.logger.info(f"Finished yielding {total_yielded} issues for project '{project_key}'")

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
        start_at = 0
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

        # Check for specific test issues first
        if os.getenv("J2O_TEST_ISSUES"):
            test_issues = [k.strip() for k in os.getenv("J2O_TEST_ISSUES").split(",")]
            self.logger.info(f"Testing specific issues: {test_issues}")
            issue_keys_str = ",".join(f'"{k}"' for k in test_issues)
            jql = f'project = "{project_key}" AND key IN ({issue_keys_str}) ORDER BY created ASC'
            fast_forward = False  # Disable fast-forward for testing
            backoff_seconds = 0
        else:
            # Build delta JQL if fast-forward is enabled
            fast_forward_flag = str(os.environ.get("J2O_FAST_FORWARD", "1")).lower()
            fast_forward = fast_forward_flag in {"1", "true", "yes", "on"}
            backoff_seconds = int(os.environ.get("J2O_FF_BACKOFF_SECONDS", "7200"))  # default 2h
            base_condition = f'project = "{project_key}"'
            ordering = "ORDER BY created ASC"
            jql = f"{base_condition} {ordering}"
        existing_keys: set[str] = set()

        if fast_forward:
            checkpoint_ts = self._get_checkpoint_timestamp(project_key)
            snapshot: list[dict[str, Any]] = []
            op_project_id = None
            try:
                project_entry = self.project_mapping.get(project_key, {}) or {}
                op_project_id = project_entry.get("openproject_id")
            except Exception:
                op_project_id = None

            if op_project_id:
                try:
                    _ = self.op_client.ensure_work_package_custom_field("Jira Issue Key", "string")
                    _ = self.op_client.ensure_work_package_custom_field("Jira Migration Date", "date")
                    snapshot = self.op_client.get_project_wp_cf_snapshot(int(op_project_id))
                    existing_keys = {
                        str(row.get("jira_issue_key")).strip()
                        for row in snapshot
                        if isinstance(row, dict)
                        and isinstance(row.get("jira_issue_key"), str)
                        and row.get("jira_issue_key").strip()
                    }
                    if not checkpoint_ts:
                        checkpoint_ts = self._derive_snapshot_timestamp(snapshot)
                except Exception as op_err:  # pragma: no cover - observational logging
                    self.logger.debug(
                        "Failed to inspect existing work packages for %s: %s",
                        project_key,
                        op_err,
                    )

            exclusion_clause = self._build_key_exclusion_clause(existing_keys)

            if checkpoint_ts:
                target_ts = checkpoint_ts
                effective_cutoff = target_ts - timedelta(seconds=backoff_seconds)
                cutoff_str = effective_cutoff.strftime("%Y-%m-%d %H:%M")
                ordering = "ORDER BY updated ASC"
                jql = f'{base_condition} AND updated >= "{cutoff_str}" {ordering}'
                self.logger.info(
                    "Fast-forward enabled for %s using checkpoint %s (effective cutoff %s, backoff %ss)",
                    project_key,
                    target_ts.isoformat(),
                    effective_cutoff.isoformat(),
                    backoff_seconds,
                )
            else:
                if exclusion_clause:
                    jql = f"{base_condition} AND {exclusion_clause} {ordering}"
                else:
                    jql = f"{base_condition} {ordering}"
                self.logger.info(
                    "Fast-forward requested for %s but no checkpoint available; using fallback JQL",
                    project_key,
                )
        fields = None  # Get all fields
        expand = "changelog,renderedFields"  # Include changelog and comments

        logger.notice("Starting paginated fetch for project '%s'...", project_key)

        # Verify project exists first (unless J2O_TEST_ISSUES is set)
        if not os.getenv("J2O_TEST_ISSUES"):
            try:
                self.jira_client.jira.project(project_key)
            except Exception as e:
                from src.clients.jira_client import JiraResourceNotFoundError

                msg = f"Project '{project_key}' not found: {e!s}"
                raise JiraResourceNotFoundError(msg) from e
        else:
            logger.info(
                "J2O_TEST_ISSUES set - skipping project verification for '%s'",
                project_key,
            )

        total_yielded = 0
        while True:
            # Fetch batch with retry logic
            issues_batch = self._fetch_issues_with_retry(
                jql=jql,
                start_at=start_at,
                max_results=batch_size,
                fields=fields,
                expand=expand,
                project_key=project_key,
            )

            if not issues_batch:
                logger.debug(
                    "No more issues found for %s at startAt=%s",
                    project_key,
                    start_at,
                )
                break

            # Yield individual issues
            for issue in issues_batch:
                yield issue
                total_yielded += 1
                # Check if we've reached the limit
                if max_issues and total_yielded >= max_issues:
                    self.logger.info(f"Reached issue limit ({max_issues}), stopping")
                    return

            logger.info(
                "Fetched batch: %s issues (total: %s) for project '%s'",
                len(issues_batch),
                total_yielded,
                project_key,
            )

            # Check if this was the last page
            if len(issues_batch) < batch_size:
                break

            start_at += len(issues_batch)

        logger.info(
            "Finished yielding %s issues for project '%s'",
            total_yielded,
            project_key,
        )

    def _fetch_issues_with_retry(
        self,
        jql: str,
        start_at: int,
        max_results: int,
        fields: str | None,
        expand: str | None,
        project_key: str,
    ) -> list[Issue]:
        """Fetch issues with exponential backoff for rate limiting.

        Args:
            jql: JQL query string
            start_at: Starting index for pagination
            max_results: Maximum results per page
            fields: Fields to retrieve
            expand: Expand options
            project_key: Project key for logging

        Returns:
            List of issues for this page

        Raises:
            Exception: If all retries are exhausted

        """
        max_retries = 5
        base_delay = 1.0

        for attempt in range(max_retries + 1):
            try:
                logger.debug(
                    "Fetching issues for %s: startAt=%s, maxResults=%s (attempt %s)",
                    project_key,
                    start_at,
                    max_results,
                    attempt + 1,
                )

                return self.jira_client.jira.search_issues(
                    jql,
                    startAt=start_at,
                    maxResults=max_results,
                    fields=fields,
                    expand=expand,
                    json_result=False,  # Get jira.Issue objects
                )

            except requests.exceptions.HTTPError as e:
                if e.response and e.response.status_code == 429 and attempt < max_retries:
                    delay = base_delay * (2**attempt)
                    logger.warning(
                        "Rate limited for %s. Retrying in %ss (attempt %s/%s)",
                        project_key,
                        delay,
                        attempt + 1,
                        max_retries + 1,
                    )
                    time.sleep(delay)
                    continue
                raise

            except requests.exceptions.RequestException as e:
                if attempt < max_retries:
                    delay = base_delay * (2**attempt)
                    logger.warning(
                        "Request failed for %s. Retrying in %ss (attempt %s/%s): %s",
                        project_key,
                        delay,
                        attempt + 1,
                        max_retries + 1,
                        e,
                    )
                    time.sleep(delay)
                    continue
                raise

            except Exception as e:
                error_msg = f"Failed to get issues page for project {project_key} at startAt={start_at}: {e!s}"
                logger.exception(error_msg)
                from src.clients.jira_client import JiraApiError

                raise JiraApiError(error_msg) from e
        return None

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
            jql = f'project = "{project_key}"'
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

    def _extract_jira_issues(
        self,
        project_key: str,
        project_tracker: Any = None,
    ) -> list[dict]:
        """Extract all issues from a specific Jira project using pagination.

        This method uses the new iter_project_issues generator to avoid loading
        all issues into memory at once, while preserving the existing interface
        for JSON file saving and project tracking.
        Includes renderedFields expansion for comment data extraction.

        Args:
            project_key: The Jira project key to extract issues from
            project_tracker: Optional project tracker for logging

        Returns:
            List of all issues from the project with comment data (as dictionaries)

        """
        self.logger.info(f"Extracting issues from Jira project: {project_key}")

        try:
            all_issues = []
            issue_count = 0

            # Match pagination parameters used by iter_project_issues
            start_at = 0
            batch_size = config.migration_config.get("batch_size", 100)
            jql = f'project = "{project_key}" ORDER BY created ASC'
            fields = None
            expand = "changelog,renderedFields"

            # Prefer generator when available (tests patch this)
            first_batch = None
            if hasattr(self, "iter_project_issues"):
                gen_list = list(self.iter_project_issues(project_key))
                first_batch = gen_list
            else:
                # Fallback: fetch first page to read `total` metadata
                first_batch = self._fetch_issues_with_retry(
                    jql=jql,
                    start_at=start_at,
                    max_results=batch_size,
                    fields=fields,
                    expand=expand,
                    project_key=project_key,
                )

            total_estimate: int | None = None
            if first_batch is not None and hasattr(first_batch, "total"):
                try:
                    total_estimate = int(first_batch.total)
                except Exception:
                    total_estimate = None
            if total_estimate is None:
                # Fallback to separate metadata query only if `.total` missing
                total_estimate = self._get_project_total_issues(project_key)

            if total_estimate:
                self.logger.info(
                    "Jira reports %s total issues for project %s",
                    total_estimate,
                    project_key,
                )
                if project_tracker:
                    project_tracker.update_description(
                        f"Processing project {project_key} ({total_estimate} issues)",
                    )
                    project_tracker.add_log_item(
                        f"Total issues: {total_estimate}",
                    )
            elif project_tracker:
                project_tracker.update_description(
                    f"Processing project {project_key} (estimating issues)",
                )

            def _process_issue(issue: Issue) -> None:
                nonlocal issue_count, all_issues
                raw = getattr(issue, "raw", {}) if hasattr(issue, "raw") else {}
                issue_dict = {
                    "key": getattr(issue, "key", raw.get("key")),
                    "id": getattr(issue, "id", raw.get("id")),
                    "self": getattr(issue, "self", raw.get("self")),
                    "fields": raw.get("fields", {}),
                    "changelog": raw.get("changelog"),
                }
                all_issues.append(issue_dict)

                try:
                    fields = issue_dict.get("fields", {}) or {}
                    updated_dt = self._parse_datetime(fields.get("updated"))
                    if updated_dt:
                        current = self._project_latest_issue_ts.get(project_key)
                        if current is None or updated_dt > current:
                            self._project_latest_issue_ts[project_key] = updated_dt
                except Exception:
                    # Timestamp tracking is best-effort; ignore parsing failures
                    pass

                issue_count += 1
                if issue_count % 500 == 0:
                    self.logger.info(
                        f"Processed {issue_count} issues from {project_key}",
                    )
                    if project_tracker:
                        project_tracker.add_log_item(
                            f"Processed {issue_count} issues",
                        )

            # Process all pages, with an issue-level progress bar when total is known
            if total_estimate and total_estimate > 0 and not isinstance(first_batch, list):
                with ProgressTracker(
                    f"Issues for {project_key}",
                    total_estimate,
                    "Recent Issues",
                ) as issue_tracker:
                    current_batch = first_batch or []
                    while current_batch:
                        for issue in current_batch:
                            _process_issue(issue)
                            issue_tracker.increment()
                            if issue_count % 500 == 0:
                                issue_tracker.add_log_item(
                                    f"Processed {issue_count} issues",
                                )
                            if len(current_batch) < batch_size:
                                break
                            start_at += len(current_batch)
                            current_batch = (
                                self._fetch_issues_with_retry(
                                    jql=jql,
                                    start_at=start_at,
                                    max_results=batch_size,
                                    fields=fields,
                                    expand=expand,
                                    project_key=project_key,
                                )
                                or []
                            )
            # No total available: process without an inner issue progress bar
            elif isinstance(first_batch, list):
                for issue in first_batch:
                    _process_issue(issue)
            else:
                current_batch = first_batch or []
                while current_batch:
                    for issue in current_batch:
                        _process_issue(issue)
                    if len(current_batch) < batch_size:
                        break
                    start_at += len(current_batch)
                    current_batch = (
                        self._fetch_issues_with_retry(
                            jql=jql,
                            start_at=start_at,
                            max_results=batch_size,
                            fields=fields,
                            expand=expand,
                            project_key=project_key,
                        )
                        or []
                    )

            # Final logging
            self.logger.info(
                f"Extracted {len(all_issues)} issues from project {project_key}",
            )
            if project_tracker:
                project_tracker.add_log_item(
                    f"Retrieved {len(all_issues)} issues from {project_key}",
                )

            # Save issues to file for later reference, using safe save
            try:
                self._save_to_json(all_issues, f"jira_issues_{project_key}.json")
                self.logger.info(
                    f"Extracted and saved {len(all_issues)} issues from project {project_key}",
                )
            except Exception as e:
                self.logger.exception("Failed to save issues to file: %s", e)
                # Try to save to alternate location as backup with a serializable fallback
                backup_path = self.data_dir / (
                    f"jira_issues_{project_key}_backup_{datetime.now(tz=UTC).strftime('%Y%m%d_%H%M%S')}.json"
                )
                try:
                    serializable: list[dict[str, Any]] = []
                    for it in all_issues:
                        if hasattr(it, "raw") and isinstance(it.raw, dict):
                            serializable.append(it.raw)
                        elif isinstance(it, dict):
                            serializable.append(it)
                        else:
                            serializable.append(
                                {
                                    "id": getattr(it, "id", None),
                                    "key": getattr(it, "key", None),
                                    "fields": getattr(getattr(it, "raw", {}), "get", lambda *_: {})("fields", {}),
                                },
                            )
                    with backup_path.open("w") as f:
                        json.dump(serializable, f, indent=2)
                    self.logger.info(
                        f"Saved backup (fallback-serialized) of issues to {backup_path}",
                    )
                except Exception as backup_err:
                    self.logger.warning(
                        f"Failed to create backup of state file: {backup_err}",
                    )
                # Honor stop-on-error: abort on serialization failure
                if config.migration_config.get("stop_on_error", False):
                    try:
                        from src.models.migration_error import MigrationError
                    except Exception:  # pragma: no cover

                        class MigrationError(Exception):
                            pass

                    raise MigrationError(
                        f"Stopping due to JSON serialization failure for project {project_key}: {e}",
                    ) from e

            return all_issues

        except Exception as e:
            error_msg = f"Failed to extract issues from project {project_key}: {e}"
            self.logger.exception(error_msg)
            if project_tracker:
                project_tracker.add_log_item(error_msg)
            # Reraise with more context
            msg = f"Jira issue extraction failed for project {project_key}: {e}"
            raise RuntimeError(msg) from e

    def _prepare_work_package(
        self,
        jira_issue: dict[str, Any],
        project_id: int,
    ) -> dict[str, Any]:
        """Internal method to prepare a work package object from a Jira issue (without creating it).

        Args:
            jira_issue: The Jira issue dictionary or jira.Issue object
            project_id: The ID of the OpenProject project

        Returns:
            Dictionary with work package data

        """
        # Set current project context for changelog processing (used by _process_changelog_item)
        self._current_project_id = project_id

        # BUG #21 FIX: Early load J2O CF IDs for Workflow/Resolution tracking in changelog processing
        # This must happen BEFORE changelog processing so cf_field_changes can be populated
        if not hasattr(self, "_j2o_wp_cf_ids_full") or not isinstance(self._j2o_wp_cf_ids_full, dict):
            cf_specs = (
                ("J2O Origin System", "string", False),
                ("J2O Origin ID", "string", True),
                ("J2O Origin Key", "string", True),
                ("J2O Origin URL", "string", False),
                ("J2O First Migration Date", "date", False),
                ("J2O Last Update Date", "date", False),
                ("J2O Jira Workflow", "string", True),
                ("J2O Jira Resolution", "string", True),
                ("J2O Affects Version", "string", True),  # Searchable: Jira "Version" field (where bug occurs)
            )
            cf_ids: dict[str, int] = {}
            for name, fmt, searchable in cf_specs:
                try:
                    cf = self.op_client.ensure_custom_field(
                        name, field_format=fmt, cf_type="WorkPackageCustomField", searchable=searchable
                    )
                    if isinstance(cf, dict) and cf.get("id"):
                        cf_ids[name] = int(cf["id"])
                except Exception:
                    continue
            self._j2o_wp_cf_ids_full = cf_ids
            self.logger.info(f"[BUG21] Loaded J2O CF IDs: {cf_ids}")

        # Extract the necessary fields from the Jira Issue object
        issue_type_id = jira_issue.fields.issuetype.id
        issue_type_name = jira_issue.fields.issuetype.name

        status_id = None
        if hasattr(jira_issue.fields, "status"):
            status_id = getattr(jira_issue.fields.status, "id", None)

        if hasattr(jira_issue.fields, "assignee") and jira_issue.fields.assignee:
            getattr(jira_issue.fields.assignee, "name", None)

        # Extract creator and reporter
        if hasattr(jira_issue.fields, "creator") and jira_issue.fields.creator:
            getattr(jira_issue.fields.creator, "name", None)

        if hasattr(jira_issue.fields, "reporter") and jira_issue.fields.reporter:
            getattr(jira_issue.fields.reporter, "name", None)

        # Enhanced timestamp migration will be handled after user associations

        # Extract watchers
        if hasattr(jira_issue.fields, "watches") and jira_issue.fields.watches:
            watcher_count = getattr(jira_issue.fields.watches, "watchCount", 0)
            if watcher_count > 0:
                try:
                    # Fetch watchers if there are any
                    watchers_data = self.jira_client.get_issue_watchers(jira_issue.key)
                    if watchers_data:
                        [watcher.get("name") for watcher in watchers_data]
                except Exception as e:
                    self.logger.exception(
                        "Failed to fetch watchers for issue %s: %s",
                        jira_issue.key,
                        e,
                    )

        # Extract custom fields
        custom_fields = {
            field_name: field_value
            for field_name, field_value in jira_issue.raw.get("fields", {}).items()
            if field_name.startswith("customfield_") and field_value is not None
        }

        subject = jira_issue.fields.summary
        description = getattr(jira_issue.fields, "description", "") or ""

        # Convert Jira wiki markup to OpenProject markdown
        if description:
            description = self.markdown_converter.convert(description)

        jira_id = jira_issue.id
        jira_key = jira_issue.key

        # Ensure subject is non-empty; fall back to Jira key if missing
        if not subject or not str(subject).strip():
            subject = jira_key or f"Untitled-{jira_id}"

        # Map the issue type
        type_id = None

        # First try to look up directly in issue_type_id_mapping, which is keyed by ID
        # and has a direct OpenProject ID as value
        if self.issue_type_id_mapping and str(issue_type_id) in self.issue_type_id_mapping:
            type_id = self.issue_type_id_mapping[str(issue_type_id)]
        # Then try to look up by ID in the issue_type_mapping
        elif str(issue_type_id) in self.issue_type_mapping:
            type_id = self.issue_type_mapping[str(issue_type_id)].get("openproject_id")
        # Finally, check in mappings object if available
        elif config.mappings and hasattr(config.mappings, "issue_type_id_mapping"):
            # Try to get the ID from the mappings object
            type_id = config.mappings.issue_type_id_mapping.get(str(issue_type_id))

        # Debug mapping information
        self.logger.debug(
            f"Mapping issue type: {issue_type_name} (ID: {issue_type_id}) -> OpenProject type ID: {type_id}",
        )

        # If no type mapping exists, default to Task
        if not type_id:
            type_display = issue_type_name or "Unknown"
            warning_msg = f"No mapping found for issue type {type_display} (ID: {issue_type_id}), defaulting to Task"
            self.logger.warning(warning_msg)
            type_id = 1

        # Map the status (extract integer openproject_id from mapping)
        status_op_id = None
        if status_id:
            try:
                mapping_entry = self.status_mapping.get(str(status_id))
                if isinstance(mapping_entry, dict):
                    val = mapping_entry.get("openproject_id") or mapping_entry.get("id")
                    if isinstance(val, str) and val.isdigit():
                        status_op_id = int(val)
                    elif isinstance(val, int):
                        status_op_id = val
                elif isinstance(mapping_entry, str) and mapping_entry.isdigit():
                    status_op_id = int(mapping_entry)
                elif isinstance(mapping_entry, int):
                    status_op_id = mapping_entry
            except Exception:
                status_op_id = None

        # Enhanced user association migration with comprehensive edge case handling
        work_package = {
            "project_id": project_id,
            "type_id": type_id,
            "subject": subject,
            "jira_id": jira_id,
            "jira_key": jira_key,
            # Provide explicit provenance key for CF assignment in Rails bulk
            "jira_issue_key": jira_key,
        }

        # Use enhanced user association migrator for robust user mapping
        association_result = self.enhanced_user_migrator.migrate_user_associations(
            jira_issue=jira_issue,
            work_package_data=work_package,
            preserve_creator_via_rails=True,
        )

        # Log any warnings from user association migration
        if association_result["warnings"]:
            for warning in association_result["warnings"]:
                # Collapse frequent watcher unmapped logs
                if isinstance(warning, str) and warning.startswith("Watcher") and "unmapped" in warning:
                    # Defer aggregated reporting to caller; embed a counter on work_package_data
                    counters = work_package.setdefault("_log_counters", {})
                    counters["watcher_unmapped"] = counters.get("watcher_unmapped", 0) + 1
                else:
                    self.logger.warning("User association: %s", warning)

        # Extract user association results
        assigned_to_id = work_package.get("assigned_to_id")
        author_id = work_package.get("author_id")
        watcher_ids = work_package.get("watcher_ids", [])

        # Enhanced timestamp migration with comprehensive datetime preservation
        timestamp_result = self.enhanced_timestamp_migrator.migrate_timestamps(
            jira_issue=jira_issue,
            work_package_data=work_package,
            use_rails_for_immutable=True,
            author_id=author_id,
        )

        # Log any warnings from timestamp migration
        if timestamp_result["warnings"]:
            for warning in timestamp_result["warnings"]:
                self.logger.warning("Timestamp migration: %s", warning)

        # Log any errors from timestamp migration
        if timestamp_result["errors"]:
            for error in timestamp_result["errors"]:
                self.logger.error("Timestamp migration error: %s", error)

        # Store Rails operations for immutable timestamp setting (executed after save)
        # Bug #23 debug: Track timestamp operations
        ts_ops = timestamp_result.get("rails_operations", [])
        self.logger.info(f"[BUG23] {jira_key}: timestamp_result has {len(ts_ops)} rails_operations")
        if ts_ops:
            work_package["_rails_operations"] = ts_ops
            self.logger.info(f"[BUG23] {jira_key}: Set _rails_operations from timestamp_result")

        # Extract and migrate comments AND changelog (Fix Attempt #5 for NEW work packages)
        try:
            # Extract BOTH comments AND changelog entries from Jira
            comments = self.enhanced_audit_trail_migrator.extract_comments_from_issue(jira_issue)
            changelog_entries = self.enhanced_audit_trail_migrator.extract_changelog_from_issue(jira_issue)

            # Merge comments and changelog entries into unified journal entries
            all_journal_entries = []

            # Add comments as journal entries
            for comment in comments:
                all_journal_entries.append(
                    {
                        "type": "comment",
                        "timestamp": comment.get("created", ""),
                        "data": comment,
                    }
                )

            # Add changelog entries as journal entries
            for entry in changelog_entries:
                all_journal_entries.append(
                    {
                        "type": "changelog",
                        "timestamp": entry.get("created", ""),
                        "data": entry,
                    }
                )

            if all_journal_entries:
                self.logger.debug(
                    f"Found {len(comments)} comment(s) and {len(changelog_entries)} changelog entries for {jira_key}"
                )

                # Sort ALL entries chronologically by timestamp
                all_journal_entries.sort(key=lambda x: x.get("timestamp", ""))

                # DEBUG: Log entries to understand why collision detection isn't executing
                self.logger.info(
                    f"[DEBUG] {jira_key}: all_journal_entries has {len(all_journal_entries)} entries (CREATE path)"
                )
                if len(all_journal_entries) > 0:
                    for idx, entry in enumerate(all_journal_entries):
                        self.logger.info(
                            f"[DEBUG] {jira_key}: Entry[{idx}] type={entry.get('type')} timestamp={entry.get('timestamp')}"
                        )

                # Fix Attempt #6 (Bug #32): Detect and resolve timestamp collisions
                # When multiple entries have identical timestamps, increment each duplicate sequentially
                # to ensure unique timestamps and valid validity_period ranges
                from datetime import datetime, timedelta

                # Track all timestamps that have been used (original + modified)
                used_timestamps = set()

                for i in range(len(all_journal_entries)):
                    current_timestamp = all_journal_entries[i].get("timestamp", "")

                    if not current_timestamp:
                        continue

                    # Check if this timestamp has already been used
                    if current_timestamp in used_timestamps:
                        # This is a collision - need to find a unique timestamp
                        try:
                            if "T" in current_timestamp:
                                # ISO8601 format: 2011-08-23T13:41:21.000+0000
                                original_dt = datetime.fromisoformat(current_timestamp.replace("Z", "+00:00"))

                                # Keep incrementing by 1 second until we find an unused timestamp
                                offset_seconds = 1
                                while True:
                                    new_dt = original_dt + timedelta(seconds=offset_seconds)
                                    new_timestamp = (
                                        new_dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+0000"
                                    )

                                    if new_timestamp not in used_timestamps:
                                        # Found a unique timestamp
                                        all_journal_entries[i]["timestamp"] = new_timestamp
                                        used_timestamps.add(new_timestamp)
                                        self.logger.info(
                                            f"Resolved timestamp collision for {jira_key}: {current_timestamp} → {new_timestamp} (+{offset_seconds}s)"
                                        )
                                        break

                                    offset_seconds += 1
                                    if offset_seconds > 100:
                                        # Safety limit - should never reach this
                                        self.logger.error(
                                            f"Failed to resolve timestamp collision for {jira_key} after 100 attempts"
                                        )
                                        used_timestamps.add(current_timestamp)
                                        break
                        except Exception as e:
                            self.logger.warning(f"Failed to resolve timestamp collision for {jira_key}: {e}")
                            used_timestamps.add(current_timestamp)
                    else:
                        # Timestamp is unique - add to used set
                        used_timestamps.add(current_timestamp)

                # Now create Rails operations for all journal entries with unique timestamps
                # Bug #23 debug: Track operation array initialization
                if "_rails_operations" not in work_package:
                    self.logger.info(f"[BUG23] {jira_key}: Initializing _rails_operations (not in work_package)")
                    work_package["_rails_operations"] = []
                else:
                    self.logger.info(
                        f"[BUG23] {jira_key}: _rails_operations already exists, has {len(work_package['_rails_operations'])} items"
                    )

                for entry in all_journal_entries:
                    entry_type = entry.get("type")
                    entry_data = entry.get("data", {})
                    entry_timestamp = entry.get("timestamp", "")

                    if entry_type == "comment":
                        # Bug #32 fix: Enhanced user attribution - try multiple fields
                        author_data = entry_data.get("author") or {}
                        comment_author_id = None
                        author_name = None

                        # Try multiple fields: name, displayName, emailAddress
                        for key in ["name", "displayName", "emailAddress"]:
                            if author_data.get(key):
                                user_dict = self.user_mapping.get(author_data[key])
                                if user_dict:
                                    comment_author_id = user_dict.get("openproject_id")
                                    if comment_author_id:
                                        author_name = author_data[key]
                                        self.logger.debug(
                                            f"{jira_key}: Found user via {key}: {author_name} → {comment_author_id}"
                                        )
                                        break

                        if not comment_author_id:
                            # Use fallback user (148941)
                            comment_author_id = 148941
                            attempted_fields = {
                                k: author_data.get(k)
                                for k in ["name", "displayName", "emailAddress"]
                                if k in author_data
                            }
                            self.logger.warning(
                                f"[BUG32] {jira_key}: User not found in mapping for comment (tried: {attempted_fields}), using fallback user {comment_author_id}"
                            )
                        raw_comment_body = entry_data.get("body", "")
                        # Convert Jira wiki markup to OpenProject markdown
                        if raw_comment_body and hasattr(self, "markdown_converter") and self.markdown_converter:
                            try:
                                comment_body = self.markdown_converter.convert(raw_comment_body)
                            except Exception:
                                comment_body = raw_comment_body
                        else:
                            comment_body = raw_comment_body
                        work_package["_rails_operations"].append(
                            {
                                "type": "create_comment",
                                "jira_key": jira_key,
                                "user_id": comment_author_id,
                                "notes": comment_body,
                                "created_at": entry_timestamp,
                            }
                        )
                        self.logger.info(
                            f"[BUG23] {jira_key}: Added comment operation, total operations: {len(work_package['_rails_operations'])}"
                        )
                    elif entry_type == "changelog":
                        # Bug #32 fix: Enhanced user attribution - try multiple fields
                        author_data = entry_data.get("author") or {}
                        changelog_author_id = None
                        author_name = None

                        # Try multiple fields: name, displayName, emailAddress
                        for key in ["name", "displayName", "emailAddress"]:
                            if author_data.get(key):
                                user_dict = self.user_mapping.get(author_data[key])
                                if user_dict:
                                    changelog_author_id = user_dict.get("openproject_id")
                                    if changelog_author_id:
                                        author_name = author_data[key]
                                        self.logger.debug(
                                            f"{jira_key}: Found user via {key}: {author_name} → {changelog_author_id}"
                                        )
                                        break

                        if not changelog_author_id:
                            # Use fallback user (148941)
                            changelog_author_id = 148941
                            attempted_fields = {
                                k: author_data.get(k)
                                for k in ["name", "displayName", "emailAddress"]
                                if k in author_data
                            }
                            self.logger.warning(
                                f"[BUG32] {jira_key}: User not found in mapping for changelog (tried: {attempted_fields}), using fallback user {changelog_author_id}"
                            )

                        # Bug #28 fix: Process field changes as structured data
                        # Bug #16 fix: Also capture unmapped field changes as notes (prevent data loss)
                        # Bug #21 fix: Track Workflow/Resolution as CF changes, not notes
                        changelog_items = entry_data.get("items", [])
                        field_changes = {}
                        cf_field_changes = {}  # Bug #21: Track CF changes (Workflow/Resolution)
                        unmapped_changes = []  # Bug #16: Track unmapped Jira fields

                        # Get CF IDs for Workflow/Resolution/Affects Version tracking
                        cf_map = getattr(self, "_j2o_wp_cf_ids_full", {}) or {}
                        workflow_cf_id = cf_map.get("J2O Jira Workflow")
                        resolution_cf_id = cf_map.get("J2O Jira Resolution")
                        affects_version_cf_id = cf_map.get("J2O Affects Version")

                        for item in changelog_items:
                            field_change = self._process_changelog_item(item)
                            if field_change:
                                field_changes.update(field_change)
                            else:
                                # Bug #16 fix: Capture unmapped field changes as text notes
                                # Bug #21 fix: Workflow/Resolution tracked as CF changes, not notes
                                field_name = item.get("field", "unknown")
                                from_val = item.get("fromString") or item.get("from") or ""
                                to_val = item.get("toString") or item.get("to") or ""

                                # Convert field values through markdown converter (may contain user mentions/emoticons)
                                if hasattr(self, "markdown_converter") and self.markdown_converter:
                                    if from_val:
                                        from_val = self.markdown_converter.convert(str(from_val))
                                    if to_val:
                                        to_val = self.markdown_converter.convert(str(to_val))

                                # Bug #21: Track Workflow/Resolution as CF field changes
                                if field_name == "Workflow" and workflow_cf_id:
                                    cf_field_changes[workflow_cf_id] = [from_val or None, to_val or None]
                                    self.logger.info(
                                        f"[BUG21] {jira_key}: Workflow CF change: '{from_val}' → '{to_val}'"
                                    )
                                elif field_name == "resolution" and resolution_cf_id:
                                    cf_field_changes[resolution_cf_id] = [from_val or None, to_val or None]
                                    self.logger.info(
                                        f"[BUG21] {jira_key}: Resolution CF change: '{from_val}' → '{to_val}'"
                                    )
                                elif field_name == "Version" and affects_version_cf_id:
                                    # Jira "Version" = Affects Version (where bug occurs)
                                    cf_field_changes[affects_version_cf_id] = [from_val or None, to_val or None]
                                    self.logger.info(
                                        f"{jira_key}: Affects Version CF change: '{from_val}' → '{to_val}'"
                                    )
                                elif field_name == "Key" and from_val:
                                    # Project move: Key field shows issue key change (e.g., NRTECH-468 → NRS-182)
                                    # Generate compact comment with link to previous project if mapped
                                    from_project_key = from_val.rsplit("-", 1)[0] if "-" in from_val else None
                                    proj_map = getattr(self, "project_mapping", {}) or {}
                                    if from_project_key and from_project_key in proj_map:
                                        op_identifier = (
                                            proj_map[from_project_key].get("openproject_identifier", "").lower()
                                        )
                                        if op_identifier:
                                            unmapped_changes.append(
                                                f"Moved from [{from_val}](/projects/{op_identifier}/work_packages)"
                                            )
                                        else:
                                            unmapped_changes.append(f"Moved from {from_val}")
                                    else:
                                        unmapped_changes.append(f"Moved from {from_val}")
                                elif field_name == "project" and from_val:
                                    # Project move: project field shows project name change
                                    # Generate compact comment with link to previous project if mapped
                                    proj_map = getattr(self, "project_mapping", {}) or {}
                                    from_project_mapping = None
                                    for key, info in proj_map.items():
                                        if info.get("jira_name") == from_val or key == from_val:
                                            from_project_mapping = info
                                            break
                                    if from_project_mapping:
                                        op_identifier = from_project_mapping.get("openproject_identifier", "").lower()
                                        if op_identifier:
                                            unmapped_changes.append(
                                                f"Moved from project [{from_val}](/projects/{op_identifier})"
                                            )
                                        else:
                                            unmapped_changes.append(f"Moved from project '{from_val}'")
                                    else:
                                        unmapped_changes.append(f"Moved from project '{from_val}'")
                                elif from_val or to_val:
                                    # Other unmapped fields still go to notes
                                    unmapped_changes.append(
                                        f"Jira: {field_name} changed from '{from_val}' to '{to_val}'"
                                    )

                        # Bug #16 fix: Generate notes for unmapped field changes
                        changelog_notes = "\n".join(unmapped_changes) if unmapped_changes else ""

                        # Bug #22 fix: Always create journal for changelogs (preserves workflow transitions)
                        # Bug #28 fix: Include field_changes for structured field tracking
                        # Bug #16 fix: Include unmapped changes in notes to prevent data loss
                        operation = {
                            "type": "create_comment",
                            "jira_key": jira_key,
                            "user_id": changelog_author_id,
                            "notes": changelog_notes,  # Bug #16: Now contains unmapped field changes
                            "created_at": entry_timestamp,
                        }

                        # Add field_changes if any were processed
                        if field_changes:
                            # BUG #32 DEBUG: Log field_changes keys to identify invalid fields
                            self.logger.info(f"[BUG32] {jira_key}: field_changes keys: {list(field_changes.keys())}")
                            operation["field_changes"] = field_changes

                        # Bug #21: Add CF field changes (Workflow/Resolution)
                        if cf_field_changes:
                            operation["cf_field_changes"] = cf_field_changes
                            self.logger.info(
                                f"[BUG21] {jira_key}: Added {len(cf_field_changes)} CF field changes to operation"
                            )

                        # Bug #16 debug: Log when unmapped changes are captured
                        if unmapped_changes:
                            self.logger.info(
                                f"[BUG16] {jira_key}: Captured {len(unmapped_changes)} unmapped field changes as notes"
                            )

                        work_package["_rails_operations"].append(operation)
                        self.logger.info(
                            f"[BUG23] {jira_key}: Added changelog operation with {len(field_changes)} field changes, total operations: {len(work_package['_rails_operations'])}"
                        )

                # BUG #9 FIX (CRITICAL): Build progressive state snapshots for all operations
                # Process operations in REVERSE order to reconstruct historical state from FINAL state
                if work_package.get("_rails_operations"):
                    try:
                        # BUG #12 FIX (CRITICAL): Sort operations by timestamp BEFORE assigning state_snapshots
                        # Python assigns state_snapshots by array index, but Ruby re-sorts by timestamp.
                        # Without this sort, timestamp operations (set_updated_at, set_journal_user) end up
                        # at wrong positions after Ruby's sort, causing state_snapshot misalignment.
                        # This ensures Python's assignment order matches Ruby's processing order.
                        work_package["_rails_operations"].sort(
                            key=lambda op: op.get("created_at") or op.get("timestamp") or "9999-12-31T23:59:59",
                        )
                        self.logger.info(
                            f"[BUG12] {jira_key}: Sorted {len(work_package['_rails_operations'])} operations by timestamp before state_snapshot assignment"
                        )

                        # BUG #10 FIX: Initialize current_state with ACTUAL FINAL values
                        # These local variables contain the CURRENT/FINAL state from Jira
                        # Using work_package.get() returned None because values weren't added yet
                        current_state = {
                            "type_id": type_id,  # BUG #10: Use local var (already mapped from Jira issuetype)
                            "project_id": project_id,  # BUG #10: Use local var
                            "subject": subject,  # BUG #10: Use local var
                            "description": description,  # BUG #10: Use local var
                            "due_date": work_package.get("due_date"),
                            "category_id": work_package.get("category_id"),
                            "status_id": status_op_id,  # BUG #10: Use local var (already mapped from Jira status)
                            "assigned_to_id": assigned_to_id,  # BUG #10: Use local var (from user association)
                            "priority_id": work_package.get("priority_id"),
                            "version_id": work_package.get("version_id"),
                            "author_id": author_id,  # BUG #10: Use local var (from user association)
                            "done_ratio": work_package.get("done_ratio"),
                            "estimated_hours": work_package.get("estimated_hours"),
                            "start_date": work_package.get("start_date"),
                            "parent_id": work_package.get("parent_id"),
                        }
                        # BUG #10 DEBUG: Log initial state values for validation
                        self.logger.info(
                            f"[BUG10] {jira_key}: Initial state - type_id={type_id}, status_id={status_op_id}, assigned_to_id={assigned_to_id}, author_id={author_id}"
                        )

                        # Process operations in REVERSE (most recent to oldest)
                        # For each operation, store current state, then UNDO changes to get previous state
                        for i in range(len(work_package["_rails_operations"]) - 1, -1, -1):
                            op = work_package["_rails_operations"][i]

                            # Store CURRENT state as snapshot (state AFTER this operation)
                            op["state_snapshot"] = current_state.copy()

                            # If this operation has field_changes, UNDO them to get state BEFORE this operation
                            if op.get("field_changes"):
                                for field_name, change_value in op["field_changes"].items():
                                    # Extract OLD value from [old, new] array
                                    if isinstance(change_value, list) and len(change_value) >= 2:
                                        old_value = change_value[0]
                                        # Apply OLD value to reconstruct previous state
                                        if field_name in current_state:
                                            current_state[field_name] = old_value

                        self.logger.info(
                            f"[BUG9] {jira_key}: Built state snapshots for {len(work_package['_rails_operations'])} operations"
                        )
                        # BUG #11 DEBUG: Log state_snapshot type_id and status_id for first, middle, and last operations
                        ops = work_package["_rails_operations"]
                        for debug_idx in [0, len(ops) // 2, len(ops) - 1]:
                            if debug_idx < len(ops):
                                ss = ops[debug_idx].get("state_snapshot", {})
                                self.logger.info(
                                    f"[BUG11] {jira_key}: Op {debug_idx + 1} state_snapshot: type_id={ss.get('type_id')}, status_id={ss.get('status_id')}"
                                )

                        # BUG #21 FIX: Build CF state snapshots for Workflow/Resolution/Affects Version tracking
                        # Different from state_snapshot: build FORWARD (oldest to newest), applying changes
                        # This ensures v1 has initial values and vN has final values
                        cf_map = getattr(self, "_j2o_wp_cf_ids_full", {}) or {}
                        workflow_cf_id = cf_map.get("J2O Jira Workflow")
                        resolution_cf_id = cf_map.get("J2O Jira Resolution")
                        affects_version_cf_id = cf_map.get("J2O Affects Version")

                        if workflow_cf_id or resolution_cf_id or affects_version_cf_id:
                            # Start with empty CF state - no values before any changes
                            current_cf_state: dict[int, str | None] = {}

                            self.logger.info(f"[BUG21] {jira_key}: Starting CF state build (FORWARD order)")

                            # Process operations in FORWARD order (oldest to newest)
                            # For each operation, first apply any CF changes, then store the resulting state
                            for i, op in enumerate(work_package["_rails_operations"]):
                                op_type = op.get("type", "unknown")
                                has_cf_changes = "cf_field_changes" in op and op["cf_field_changes"]

                                # If this operation has cf_field_changes, APPLY the NEW value
                                if has_cf_changes:
                                    for cf_id, change_value in op["cf_field_changes"].items():
                                        # Extract NEW value from [old, new] array
                                        if isinstance(change_value, list) and len(change_value) >= 2:
                                            new_value = change_value[1]
                                            # Apply NEW value to get state AFTER this operation
                                            current_cf_state[cf_id] = new_value
                                            self.logger.info(
                                                f"[BUG21] {jira_key}: Op {i + 1} ({op_type}): Applied CF {cf_id}={new_value}"
                                            )

                                # Store CF state as snapshot (CF values AFTER this operation)
                                op["cf_state_snapshot"] = current_cf_state.copy()

                                # Debug: Log snapshot for first few and last operations
                                if i < 3 or i >= len(work_package["_rails_operations"]) - 2:
                                    self.logger.info(
                                        f"[BUG21] {jira_key}: Op {i + 1} ({op_type}): cf_state_snapshot={current_cf_state}"
                                    )

                            self.logger.info(
                                f"[BUG21] {jira_key}: Built CF state snapshots for {len(work_package['_rails_operations'])} operations"
                            )
                    except Exception as snapshot_error:
                        self.logger.warning(f"[BUG9] {jira_key}: Failed to build state snapshots: {snapshot_error}")
                        # Continue without snapshots - Ruby template will fall back to current behavior

        except Exception as e:
            self.logger.warning(
                f"Failed to extract comments/changelog for {jira_key}: {e}. "
                "Work package will be created without journal entries.",
            )

        # Update work package data with description (work_package_data was created earlier)
        work_package = work_package
        work_package["description"] = description

        # Add optional fields if available
        start_date = self._resolve_start_date(jira_issue)
        if start_date:
            work_package["start_date"] = start_date
        if status_op_id:
            work_package["status_id"] = status_op_id
        if assigned_to_id:
            work_package["assigned_to_id"] = assigned_to_id
        if author_id:
            work_package["author_id"] = author_id
        if watcher_ids:
            work_package["watcher_ids"] = watcher_ids

        # Timestamps are now handled by enhanced timestamp migrator

        # Process custom fields
        if custom_fields:
            # Load custom field mappings
            try:
                custom_field_mapping = self._load_custom_field_mapping()
                custom_field_values = {}

                for jira_field_id, field_value in custom_fields.items():
                    if jira_field_id in custom_field_mapping:
                        op_field = custom_field_mapping[jira_field_id]
                        op_field_id = op_field.get("openproject_id")

                        if op_field_id:
                            # Use raw field value for now (TODO: implement proper processing)
                            processed_value = field_value
                            if processed_value is not None:
                                custom_field_values[op_field_id] = processed_value

                if custom_field_values:
                    work_package["custom_fields"] = [
                        {"id": field_id, "value": field_value} for field_id, field_value in custom_field_values.items()
                    ]
            except (FileNotFoundError, RuntimeError) as e:
                self.logger.warning(f"Custom field mapping not available: {e}")
                # Continue without custom field mapping

        # Add raw jira id and key for debugging
        work_package["jira_issue_id"] = jira_id
        work_package["jira_issue_key"] = jira_key

        # Attach standardized J2O Origin CFs
        try:
            if not hasattr(self, "_j2o_wp_cf_ids_full") or not isinstance(self._j2o_wp_cf_ids_full, dict):
                cf_specs = (
                    ("J2O Origin System", "string", False),
                    ("J2O Origin ID", "string", True),  # Searchable for finding by Jira ID
                    ("J2O Origin Key", "string", True),  # Searchable for finding by Jira Key (e.g., NRS-2358)
                    ("J2O Origin URL", "string", False),
                    ("J2O First Migration Date", "date", False),
                    ("J2O Last Update Date", "date", False),
                    ("J2O Jira Workflow", "string", True),  # Searchable: Current/final Jira workflow scheme
                    ("J2O Jira Resolution", "string", True),  # Searchable: Final Jira resolution (Done, Fixed, etc.)
                    ("J2O Affects Version", "string", True),  # Searchable: Jira "Version" field (where bug occurs)
                )
                cf_ids: dict[str, int] = {}
                for name, fmt, searchable in cf_specs:
                    try:
                        cf = self.op_client.ensure_custom_field(
                            name, field_format=fmt, cf_type="WorkPackageCustomField", searchable=searchable
                        )
                        if isinstance(cf, dict) and cf.get("id"):
                            cf_ids[name] = int(cf["id"])  # type: ignore[arg-type]
                    except Exception:
                        continue
                self._j2o_wp_cf_ids_full = cf_ids

            cf_vals: list[dict[str, object]] = []
            cf_map = getattr(self, "_j2o_wp_cf_ids_full", {}) or {}
            if cf_map.get("J2O Origin System"):
                cf_vals.append({"id": cf_map["J2O Origin System"], "value": "Jira Server on-prem 9.11"})
            if cf_map.get("J2O Origin ID") and jira_id:
                cf_vals.append({"id": cf_map["J2O Origin ID"], "value": str(jira_id)})
            if cf_map.get("J2O Origin Key") and jira_key:
                cf_vals.append({"id": cf_map["J2O Origin Key"], "value": jira_key})
            base_url = (config.jira_config or {}).get("J2O_JIRA_URL") or (config.jira_config or {}).get("url")
            if cf_map.get("J2O Origin URL") and jira_key and base_url:
                try:
                    url_val = "/".join([str(base_url).rstrip("/"), "browse", str(jira_key)])
                except Exception:
                    url_val = f"{base_url}/browse/{jira_key}"
                cf_vals.append({"id": cf_map["J2O Origin URL"], "value": url_val})
            from datetime import date as _date

            today_str = _date.today().isoformat()
            if cf_map.get("J2O First Migration Date"):
                cf_vals.append({"id": cf_map["J2O First Migration Date"], "value": today_str})
            if cf_map.get("J2O Last Update Date"):
                cf_vals.append({"id": cf_map["J2O Last Update Date"], "value": today_str})
            # Extract final workflow scheme from changelog (last "Workflow" field change)
            if cf_map.get("J2O Jira Workflow"):
                final_workflow = self._extract_final_workflow(jira_issue)
                if final_workflow:
                    cf_vals.append({"id": cf_map["J2O Jira Workflow"], "value": final_workflow})
            # Extract resolution from Jira issue (direct field, not changelog)
            if cf_map.get("J2O Jira Resolution"):
                try:
                    resolution = getattr(jira_issue.fields, "resolution", None)
                    if resolution:
                        resolution_name = getattr(resolution, "name", None) or str(resolution)
                        if resolution_name:
                            cf_vals.append({"id": cf_map["J2O Jira Resolution"], "value": resolution_name})
                except Exception:
                    pass
            # Extract Affects Version from Jira issue (versions field = where bug occurs)
            if cf_map.get("J2O Affects Version"):
                try:
                    versions = getattr(jira_issue.fields, "versions", None)
                    if versions and isinstance(versions, list) and len(versions) > 0:
                        # Join multiple versions with comma if present
                        version_names = [getattr(v, "name", str(v)) for v in versions if v]
                        if version_names:
                            cf_vals.append({"id": cf_map["J2O Affects Version"], "value": ", ".join(version_names)})
                except Exception:
                    pass
            if cf_vals:
                existing = work_package.get("custom_fields")
                if isinstance(existing, list):
                    work_package["custom_fields"] = existing + cf_vals
                else:
                    work_package["custom_fields"] = cf_vals
        except Exception:
            pass

        return work_package

    def _resolve_start_date(self, issue: Any) -> str | None:
        """Resolve start date from configured Jira custom fields."""
        candidates: list[str] = list(self.start_date_fields)

        # jira.Issue style access
        if hasattr(issue, "fields"):
            fields_obj = getattr(issue, "fields", None)
            for field_id in candidates:
                try:
                    value = getattr(fields_obj, field_id)
                except AttributeError:
                    value = None
                if value:
                    normalized = self.enhanced_timestamp_migrator._normalize_timestamp(str(value))
                    if normalized:
                        return normalized.split("T", 1)[0]

        # Raw dict fields from jira.Issue
        raw_fields = {}
        if hasattr(issue, "raw"):
            raw_fields = getattr(issue, "raw", {}).get("fields", {})
        if isinstance(issue, dict):
            raw_fields = issue.get("fields", issue)

        if isinstance(raw_fields, dict):
            for field_id in candidates:
                value = raw_fields.get(field_id)
                if value:
                    normalized = self.enhanced_timestamp_migrator._normalize_timestamp(str(value))
                    if normalized:
                        return normalized.split("T", 1)[0]

        # Fallback: derive start date from Jira status history
        history_start = self._resolve_start_date_from_history(issue)
        if history_start:
            return history_start

        return None

    def _resolve_start_date_from_history(self, issue: Any) -> str | None:
        """Infer start date from the first transition into an 'In Progress' category."""
        histories = self._extract_changelog_histories(issue)
        if not histories:
            return None

        # Sort histories chronologically (oldest first) using their created timestamp
        normalized_histories: list[tuple[str, Any]] = []
        for history in histories:
            created_raw = self._get_attr(history, "created")
            if not created_raw:
                continue
            normalized = self.enhanced_timestamp_migrator._normalize_timestamp(str(created_raw))
            if not normalized:
                continue
            normalized_histories.append((normalized, history))

        normalized_histories.sort(key=lambda pair: pair[0])

        for normalized, history in normalized_histories:
            items = self._get_attr(history, "items") or []
            for item in items:
                field_name = str(self._get_attr(item, "field") or "").lower()
                if field_name != "status":
                    continue

                status_id = str(self._get_attr(item, "to") or "").strip()
                status_name = str(self._get_attr(item, "toString") or "").strip().lower()

                category = {}
                if status_id and status_id in self.status_category_by_id:
                    category = self.status_category_by_id[status_id] or {}
                elif status_name and status_name in self.status_category_by_name:
                    category = self.status_category_by_name[status_name] or {}

                if not category and status_name:
                    # Attempt loose lookup by name if exact match missing
                    category = next(
                        (val for key, val in self.status_category_by_name.items() if key == status_name),
                        {},
                    )

                if self._is_in_progress_category(category):
                    return normalized.split("T", 1)[0]

        return None

    @staticmethod
    def _get_attr(obj: Any, key: str) -> Any:
        """Safely fetch attribute/key from Jira objects or dicts."""
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    def _process_changelog_item(self, item: dict[str, Any]) -> dict[str, list[Any]] | None:
        """Process a single changelog item into OpenProject field changes.

        Bug #28 fix: Transform Jira changelog entries into structured field changes
        for journal.data instead of text-only comments.

        Args:
            item: Jira changelog item with 'field', 'fromString', 'toString'

        Returns:
            Dictionary mapping OpenProject field names to [old_value, new_value] or None

        """
        field = item.get("field")
        if not field:
            return None

        # Field mapping from Jira to OpenProject
        # BUG #32 FIX (REGRESSION #3): Only map fields that exist in Journal::WorkPackageJournal
        # BUG #17 FIX: Added timeestimate and timeoriginalestimate mappings
        # fixVersion: Enabled with on-the-fly version creation
        field_mappings = {
            "summary": "subject",
            "description": "description",
            "status": "status_id",
            "assignee": "assigned_to_id",
            "priority": "priority_id",
            "issuetype": "type_id",
            # "resolution": "resolution",  # NOT a valid Journal::WorkPackageJournal attribute
            # "labels": "tags",  # NOT a valid Journal::WorkPackageJournal attribute
            "Fix Version": "version_id",  # On-the-fly version creation enabled
            # "component": "category_id",  # Requires category_mapping - falls through to Bug #16 notes for now
            "reporter": "author_id",
            # BUG #17 FIX: Time estimate fields (Jira stores in seconds, OpenProject in hours)
            "timeestimate": "remaining_hours",
            "timeoriginalestimate": "estimated_hours",
        }

        # BUG #32 FIX (REGRESSION #3): Skip unmapped fields to prevent invalid Journal::WorkPackageJournal attributes
        if field not in field_mappings:
            return None

        op_field = field_mappings[field]

        # Get values from changelog item
        from_value = item.get("fromString") or item.get("from")
        to_value = item.get("toString") or item.get("to")

        # Special handling for user fields (assignee, reporter)
        if field in ["assignee", "reporter"]:
            # BUG #20 FIX: Use 'from'/'to' (username) not 'fromString'/'toString' (display name)
            # Jira changelog provides:
            #   - from/to: username (e.g., "enrico.tischendorf")
            #   - fromString/toString: display name (e.g., "Enrico Tischendorf")
            # Our user_mapping is keyed by username, so we must use from/to
            from_username = item.get("from")
            to_username = item.get("to")

            # Map usernames to OpenProject IDs
            from_id = None
            to_id = None

            if from_username and self.user_mapping:
                user_dict = self.user_mapping.get(from_username)
                from_id = user_dict.get("openproject_id") if user_dict else None
                if not user_dict:
                    self.logger.debug(f"[BUG20] User not found in mapping: {from_username}")

            if to_username and self.user_mapping:
                user_dict = self.user_mapping.get(to_username)
                to_id = user_dict.get("openproject_id") if user_dict else None
                if not user_dict:
                    self.logger.debug(f"[BUG20] User not found in mapping: {to_username}")

            # BUG #19 FIX: Skip no-change mappings (from == to, including both None)
            # This prevents "retracted" phantom journals from operations with no actual change
            if from_id == to_id:
                return None

            return {op_field: [from_id, to_id]}

        # BUG #11 FIX: Map Jira IDs to OpenProject IDs for status, issuetype
        # The progressive state building needs OpenProject integer IDs, not Jira string values
        if field == "issuetype":
            # Get Jira type IDs (not string names)
            from_jira_id = item.get("from")  # e.g., "3" for Task
            to_jira_id = item.get("to")  # e.g., "10404" for Access

            # Map to OpenProject type IDs
            from_op_id = None
            to_op_id = None

            if from_jira_id and self.issue_type_id_mapping:
                from_op_id = self.issue_type_id_mapping.get(str(from_jira_id))
            if to_jira_id and self.issue_type_id_mapping:
                to_op_id = self.issue_type_id_mapping.get(str(to_jira_id))

            # BUG #11 DEBUG: Log type mapping results
            self.logger.info(
                f"[BUG11-TYPE] issuetype change: from_jira={from_jira_id} -> from_op={from_op_id}, to_jira={to_jira_id} -> to_op={to_op_id}"
            )

            # BUG #19 FIX: Skip no-change mappings
            if from_op_id == to_op_id:
                return None

            return {op_field: [from_op_id, to_op_id]}

        if field == "status":
            # Get Jira status IDs (not string names)
            from_jira_id = item.get("from")  # e.g., "1" for Open
            to_jira_id = item.get("to")  # e.g., "6" for Closed

            # Map to OpenProject status IDs
            from_op_id = None
            to_op_id = None

            if from_jira_id and self.status_mapping:
                mapping = self.status_mapping.get(str(from_jira_id))
                from_op_id = mapping.get("openproject_id") if mapping else None
            if to_jira_id and self.status_mapping:
                mapping = self.status_mapping.get(str(to_jira_id))
                to_op_id = mapping.get("openproject_id") if mapping else None

            # BUG #11 DEBUG: Log status mapping results
            self.logger.info(
                f"[BUG11-STATUS] status change: from_jira={from_jira_id} -> from_op={from_op_id}, to_jira={to_jira_id} -> to_op={to_op_id}"
            )

            # BUG #19 FIX: Skip no-change mappings
            if from_op_id == to_op_id:
                return None

            return {op_field: [from_op_id, to_op_id]}

        # Handle Fix Version -> version_id with on-the-fly version creation
        if field == "Fix Version":
            # Version names are in fromString/toString (like user display names)
            from_version_name = item.get("fromString")
            to_version_name = item.get("toString")

            # Map to OpenProject version IDs (create if needed)
            from_version_id = None
            to_version_id = None

            if self._current_project_id:
                if from_version_name:
                    from_version_id = self._get_or_create_version(from_version_name, self._current_project_id)
                if to_version_name:
                    to_version_id = self._get_or_create_version(to_version_name, self._current_project_id)

            self.logger.info(
                f"[FIXVERSION] project_id={self._current_project_id}, from='{from_version_name}' -> {from_version_id}, to='{to_version_name}' -> {to_version_id}"
            )

            # Skip no-change mappings
            if from_version_id == to_version_id:
                return None

            return {op_field: [from_version_id, to_version_id]}

        # For priority, keep string values for now (mapping not critical for journals)
        if field == "priority":
            # BUG #19 FIX: Skip no-change mappings
            if from_value == to_value:
                return None
            return {op_field: [from_value, to_value]}

        # BUG #17 FIX: Handle time estimate fields - convert Jira seconds to OpenProject hours
        if field in ["timeestimate", "timeoriginalestimate"]:
            from_seconds = item.get("from")
            to_seconds = item.get("to")

            def seconds_to_hours(seconds_str: str | None) -> float | None:
                """Convert Jira time (seconds) to OpenProject hours."""
                if not seconds_str:
                    return None
                try:
                    seconds = int(seconds_str)
                    return round(seconds / 3600, 2)  # Convert to hours with 2 decimal places
                except (ValueError, TypeError):
                    return None

            from_hours = seconds_to_hours(from_seconds)
            to_hours = seconds_to_hours(to_seconds)

            self.logger.info(
                f"[BUG17-TIME] {field} change: from={from_seconds}s ({from_hours}h) -> to={to_seconds}s ({to_hours}h)"
            )

            # BUG #19 FIX: Skip no-change mappings
            if from_hours == to_hours:
                return None

            return {op_field: [from_hours, to_hours]}

        # Generic field change (subject, description, etc.)
        # BUG #19 FIX: Skip no-change mappings
        if from_value == to_value:
            return None
        return {op_field: [from_value, to_value]}

    def _extract_changelog_histories(self, issue: Any) -> list[Any]:
        """Return changelog histories from either jira.Issue or dict payloads."""
        if hasattr(issue, "changelog") and issue.changelog:
            histories = getattr(issue.changelog, "histories", None)
            if histories:
                return list(histories)

        raw = getattr(issue, "raw", None)
        if isinstance(raw, dict):
            histories = raw.get("changelog", {}).get("histories")
            if isinstance(histories, list):
                return histories

        if isinstance(issue, dict):
            histories = issue.get("changelog", {}).get("histories")
            if isinstance(histories, list):
                return histories

        return []

    @staticmethod
    def _is_in_progress_category(category: dict[str, Any]) -> bool:
        """Return True when the status category represents 'In Progress'."""
        if not category:
            return False

        key = str(category.get("key", "")).lower()
        name = str(category.get("name", "")).lower()
        cat_id = str(category.get("id", "")).lower()

        in_progress_keys = {"indeterminate", "in_progress", "in-progress"}
        if key in in_progress_keys:
            return True
        if name == "in progress":
            return True
        if cat_id == "4":  # Jira default id for In Progress category
            return True
        return False

    def _extract_issue_meta(self, issue: Any) -> dict[str, Any]:
        """Extract non-AR metadata from a Jira issue for reporting.

        Safe best-effort extraction from either jira.Issue or dict-like payloads.
        Does not mutate inputs and never raises.
        """
        meta: dict[str, Any] = {}
        try:
            start_date = self._resolve_start_date(issue)
            if start_date:
                meta["start_date"] = start_date
            # Handle jira.Issue style
            if hasattr(issue, "key") and hasattr(issue, "fields"):
                f = getattr(issue, "fields", None)
                meta["jira_key"] = getattr(issue, "key", None)
                meta["jira_id"] = getattr(issue, "id", None)
                if f is not None:

                    def _name(obj: Any) -> Any:
                        try:
                            return getattr(obj, "name", None)
                        except Exception:
                            return None

                    meta["issuetype_id"] = getattr(getattr(f, "issuetype", None), "id", None)
                    meta["issuetype_name"] = _name(getattr(f, "issuetype", None))
                    meta["status_id"] = getattr(getattr(f, "status", None), "id", None)
                    meta["status_name"] = _name(getattr(f, "status", None))
                    meta["priority_id"] = getattr(getattr(f, "priority", None), "id", None)
                    meta["priority_name"] = _name(getattr(f, "priority", None))
                    meta["reporter"] = getattr(getattr(f, "reporter", None), "name", None)
                    meta["assignee"] = getattr(getattr(f, "assignee", None), "name", None)
                    meta["created"] = getattr(f, "created", None)
                    meta["updated"] = getattr(f, "updated", None)
                    meta["duedate"] = getattr(f, "duedate", None)
                    try:
                        labels = list(getattr(f, "labels", []) or [])
                    except Exception:
                        labels = []
                    meta["labels"] = labels
                    try:
                        comps = getattr(f, "components", []) or []
                        meta["components"] = [getattr(c, "name", None) for c in comps if c]
                    except Exception:
                        meta["components"] = []
                    # Optional relations
                    try:
                        parent = getattr(f, "parent", None)
                        if parent is not None:
                            meta["parent_key"] = getattr(parent, "key", None)
                    except Exception:
                        pass
            else:
                # Dict-like payloads (tests / fallback)
                d = issue or {}
                fields = d.get("fields") if isinstance(d, dict) else {}
                meta["jira_key"] = d.get("key") if isinstance(d, dict) else None
                meta["jira_id"] = d.get("id") if isinstance(d, dict) else None

                def _get(path_keys: list[str]) -> Any:
                    cur: Any = fields if isinstance(fields, dict) else {}
                    for k in path_keys:
                        if not isinstance(cur, dict):
                            return None
                        cur = cur.get(k)
                    return cur

                meta["issuetype_id"] = _get(["issuetype", "id"])
                meta["issuetype_name"] = _get(["issuetype", "name"])
                meta["status_id"] = _get(["status", "id"])
                meta["status_name"] = _get(["status", "name"])
                meta["priority_id"] = _get(["priority", "id"])
                meta["priority_name"] = _get(["priority", "name"])
                meta["reporter"] = _get(["reporter", "name"]) or _get(["reporter", "displayName"])
                meta["assignee"] = _get(["assignee", "name"]) or _get(["assignee", "displayName"])
                meta["created"] = _get(["created"]) or d.get("created")
                meta["updated"] = _get(["updated"]) or d.get("updated")
                meta["duedate"] = _get(["duedate"]) or d.get("duedate")
                labels = _get(["labels"]) or []
                if not isinstance(labels, list):
                    labels = []
                meta["labels"] = labels
                comps = _get(["components"]) or []
                if isinstance(comps, list):
                    meta["components"] = [c.get("name") for c in comps if isinstance(c, dict)]
                else:
                    meta["components"] = []
                parent = _get(["parent"]) or {}
                if isinstance(parent, dict):
                    meta["parent_key"] = parent.get("key")
        except Exception:
            # Never fail migration because of meta extraction
            pass
        return meta

    def prepare_work_package(
        self,
        jira_issue: dict[str, Any],
        project_id: int,
    ) -> dict[str, Any]:
        """Prepare a work package object from a Jira issue (without creating it).

        Public method that calls the internal _prepare_work_package method.

        Args:
            jira_issue: The Jira issue dictionary or jira.Issue object
            project_id: The ID of the OpenProject project

        Returns:
            Dictionary with work package data

        """
        # In tests, we receive a dictionary directly
        if isinstance(jira_issue, dict):
            # Get the key and description
            jira_key = jira_issue.get("key", "")
            description = jira_issue.get("description", "")

            # Convert Jira wiki markup to OpenProject markdown
            if description:
                description = self.markdown_converter.convert(description)

            # Format the description to include the Jira key
            formatted_description = f"Jira Issue: {jira_key}\n\n{description}"

            # Subject with fallback to Jira key
            subject_val = (jira_issue.get("summary", "") or "").strip()
            if not subject_val:
                subject_val = jira_key or f"Untitled-{jira_issue.get('id', '')}"

            # Convert the dictionary format used in tests to work package format
            work_package = {
                "project_id": project_id,
                "subject": subject_val,
                "description": formatted_description,
                "jira_key": jira_key,
                "jira_id": jira_issue.get("id", ""),
                "_links": {},
            }

            start_date = self._resolve_start_date(jira_issue)
            if start_date:
                work_package["start_date"] = start_date

            # Attach origin mapping custom fields for provenance
            try:
                if not hasattr(self, "_j2o_wp_cf_ids") or not isinstance(self._j2o_wp_cf_ids, dict):
                    # Ensure minimal set of provenance CFs (WP scope)
                    cf_names = (
                        ("J2O Origin System", "string"),
                        ("J2O External ID", "string"),
                        ("J2O External Key", "string"),
                    )
                    cf_ids: dict[str, int] = {}
                    for n, fmt in cf_names:
                        try:
                            cf = self.op_client.ensure_custom_field(
                                n, field_format=fmt, cf_type="WorkPackageCustomField"
                            )
                            if isinstance(cf, dict) and cf.get("id"):
                                cf_ids[n] = int(cf["id"])  # type: ignore[arg-type]
                        except Exception:
                            continue
                    self._j2o_wp_cf_ids = cf_ids

                cf_vals: list[dict[str, object]] = []
                cf_map = getattr(self, "_j2o_wp_cf_ids", {}) or {}
                # Populate values
                if cf_map.get("J2O Origin System"):
                    cf_vals.append({"id": cf_map["J2O Origin System"], "value": "jira"})
                ext_id = jira_issue.get("id")
                if cf_map.get("J2O External ID") and ext_id:
                    cf_vals.append({"id": cf_map["J2O External ID"], "value": str(ext_id)})
                if cf_map.get("J2O External Key") and jira_key:
                    cf_vals.append({"id": cf_map["J2O External Key"], "value": jira_key})
                if cf_vals:
                    work_package["custom_fields"] = cf_vals
            except Exception:
                # Non-fatal: continue without provenance CFs
                pass

            # Add type if available (explicit type_id and _links for compatibility)
            issue_type = jira_issue.get("issue_type", {})
            if issue_type:
                type_id_value = issue_type.get("id")
                type_name_value = issue_type.get("name")
                if type_id_value or type_name_value:
                    mapped_type_id = self._map_issue_type(type_id_value, type_name_value)
                    work_package["type_id"] = mapped_type_id
                    work_package["_links"]["type"] = {"href": f"/api/v3/types/{mapped_type_id}"}

            # Add status if available (explicit status_id and _links)
            status = jira_issue.get("status", {})
            if status:
                status_id_value = status.get("id")
                status_name_value = status.get("name")
                if status_id_value or status_name_value:
                    mapped_status_id = self._map_status(status_id_value, status_name_value)
                    work_package["status_id"] = mapped_status_id
                    work_package["_links"]["status"] = {"href": f"/api/v3/statuses/{mapped_status_id}"}

            # Sanitize for AR compatibility (extract ids, drop _links)
            self._sanitize_wp_dict(work_package)

            return work_package
        # It's a Jira issue object, use the internal method
        return self._prepare_work_package(jira_issue, project_id)

    def _map_issue_type(
        self,
        type_id: str | None = None,
        type_name: str | None = None,
    ) -> int:
        """Map Jira issue type to OpenProject type ID."""
        if not type_id and not type_name:
            msg = "Either type_id or type_name must be provided for issue type mapping"
            raise ValueError(
                msg,
            )

        # Try to find in mapping by ID
        if type_id and self.issue_type_id_mapping and str(type_id) in self.issue_type_id_mapping:
            return self.issue_type_id_mapping[str(type_id)]

        # Try to find in mapping by ID in issue_type_mapping
        if type_id and str(type_id) in self.issue_type_mapping:
            mapped_id = self.issue_type_mapping[str(type_id)].get("openproject_id")
            if mapped_id:
                return mapped_id

        # Default to Task (typically ID 1 in OpenProject)
        type_display = type_name or "Unknown"
        self.logger.warning(
            f"No mapping found for issue type {type_display} (ID: {type_id}), defaulting to Task",
        )
        return 1

    def _map_status(
        self,
        status_id: str | None = None,
        status_name: str | None = None,
    ) -> int:
        """Map Jira status to OpenProject status ID."""
        if not status_id and not status_name:
            msg = "Either status_id or status_name must be provided for status mapping"
            raise ValueError(
                msg,
            )

        # Try to find in mapping by ID
        if status_id and self.status_mapping and str(status_id) in self.status_mapping:
            mapped_id = self.status_mapping[str(status_id)].get("openproject_id")
            if mapped_id:
                return mapped_id

        # Default to "New" status (typically ID 1 in OpenProject)
        status_display = status_name or "Unknown"
        self.logger.warning(
            f"No mapping found for status {status_display} (ID: {status_id}), defaulting to New",
        )
        return 1

    def _sanitize_wp_dict(self, wp: dict[str, Any]) -> None:
        """Sanitize a prepared work package dict in-place for AR compatibility.

        - Extract type_id and status_id from API-style _links if provided
        - Remove the _links key entirely to avoid unknown attribute errors
        - Ensure string fields are properly escaped
        """
        # Ensure string values for certain fields
        if "subject" in wp:
            try:
                wp["subject"] = str(wp["subject"]).replace('"', '\\"').replace("'", "\\'")
            except Exception:
                wp["subject"] = str(wp.get("subject", ""))
        if "description" in wp:
            try:
                wp["description"] = str(wp["description"]).replace('"', '\\"').replace("'", "\\'")
            except Exception:
                wp["description"] = str(wp.get("description", ""))

        # Sanitize OpenProject API-style links that are not valid AR attributes
        links = wp.get("_links")
        if isinstance(links, dict):
            # Extract type_id from links if present and not already provided
            try:
                if "type_id" not in wp and isinstance(links.get("type"), dict):
                    href = links["type"].get("href")
                    if isinstance(href, str) and href.strip():
                        type_id_str = href.rstrip("/").split("/")[-1]
                        if type_id_str.isdigit():
                            wp["type_id"] = int(type_id_str)
            except Exception:
                pass

            # Extract status_id from links if present and not already provided
            try:
                if "status_id" not in wp and isinstance(links.get("status"), dict):
                    href = links["status"].get("href")
                    if isinstance(href, str) and href.strip():
                        status_id_str = href.rstrip("/").split("/")[-1]
                        if status_id_str.isdigit():
                            wp["status_id"] = int(status_id_str)
            except Exception:
                pass

        # Remove _links entirely to avoid AR unknown attribute errors
        wp.pop("_links", None)
        # Remove non-AR/meta keys that must not reach Rails mass-assignment
        wp.pop("watcher_ids", None)
        wp.pop("jira_id", None)
        wp.pop("jira_key", None)
        wp.pop("type_name", None)

    def _get_current_entities_for_type(self, entity_type: str) -> list[dict[str, Any]]:
        """Get current entities of the specified type from Jira.

        This method now uses memory-efficient pagination instead of loading
        all issues into memory at once.

        Args:
            entity_type: Type of entities to retrieve

        Returns:
            List of current entities from Jira

        Raises:
            ValueError: If entity_type is not supported by this migration

        """
        if entity_type in {"work_packages", "issues"}:
            # Process issues from configured projects using generator
            all_issues = []

            # Check if J2O_TEST_ISSUES is set - if so, bypass normal project fetching
            test_issues_env = os.getenv("J2O_TEST_ISSUES")
            if test_issues_env:
                # Extract unique project keys from test issue keys (e.g., "NRS-182" → "NRS")
                test_issue_keys = [k.strip() for k in test_issues_env.split(",")]
                project_keys_from_issues = set()
                for issue_key in test_issue_keys:
                    if "-" in issue_key:
                        project_key = issue_key.split("-")[0]
                        project_keys_from_issues.add(project_key)

                # Create minimal project structure for each extracted project key
                projects_to_migrate = [{"key": pk, "name": pk} for pk in sorted(project_keys_from_issues)]
                logger.info(
                    f"J2O_TEST_ISSUES set - bypassing Jira project fetch, "
                    f"using extracted project keys: {sorted(project_keys_from_issues)}",
                )
            else:
                # Normal flow: Get ALL Jira projects first
                all_projects = self.jira_client.get_projects()
                logger.info(f"Retrieved {len(all_projects)} total Jira projects from API")

                # Filter to only configured projects from config.jira.projects
                try:
                    configured_projects = config.jira_config.get("projects") or []
                except Exception:
                    configured_projects = []

                if configured_projects:
                    # Filter projects to only those in configuration
                    projects_to_migrate = [p for p in all_projects if p.get("key") in configured_projects]
                    logger.info(
                        f"Filtered to {len(projects_to_migrate)} configured projects: {configured_projects}",
                    )
                else:
                    # No filter - migrate all projects
                    projects_to_migrate = all_projects
                    logger.warning(
                        "No projects configured in config.jira.projects - will process ALL projects",
                    )

                if not projects_to_migrate:
                    logger.warning(
                        f"No projects to migrate after filtering. Configured: {configured_projects}, "
                        f"Available: {[p.get('key') for p in all_projects[:10]]}",
                    )
                    return []

            # Process each configured project
            for project in projects_to_migrate:
                project_key = project.get("key")
                if project_key:
                    logger.info(
                        f"Starting issue fetch for project {project_key} (change detection)",
                    )
                    project_issue_count = 0

                    # Use the new generator method for memory-efficient processing
                    for issue in self.iter_project_issues(project_key):
                        # Convert Issue object to dict format expected by the rest of the code
                        issue_dict = {
                            "id": issue.id,
                            "key": issue.key,
                            "fields": issue.fields.__dict__,
                            "raw": issue.raw,
                            "project_key": project_key,
                        }
                        all_issues.append(issue_dict)
                        project_issue_count += 1

                        # Log progress periodically
                        if len(all_issues) % config.migration_config.get("batch_size", 100) == 0:
                            logger.info(f"Processed {len(all_issues)} issues so far...")

                    logger.info(
                        f"Completed {project_key}: fetched {project_issue_count} issues for change detection",
                    )

            logger.info(
                f"Finished processing {len(all_issues)} total issues from {len(projects_to_migrate)} configured projects for change detection",
            )
            return all_issues
        msg = (
            f"WorkPackageMigration does not support entity type: {entity_type}. "
            f"Supported types: ['work_packages', 'issues']"
        )
        raise ValueError(msg)

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

    def _migrate_work_packages(self) -> dict[str, Any]:
        """Simplified migration implementation to unblock execution.

        Iterates configured Jira projects, prepares work package payloads,
        applies required defaults, and bulk-creates WorkPackages via the
        OpenProject client. Returns a summary dict with counts per project.
        """
        self.logger.info("Starting simplified work package migration (module-level)")

        results: dict[str, Any] = {"total_created": 0, "projects": [], "total_issues": 0}

        # Start with configured projects from config.yaml
        try:
            configured_projects = config.jira_config.get("projects") or []
        except Exception:
            configured_projects = []

        if configured_projects:
            # Use explicitly configured projects
            jira_projects = configured_projects
            self.logger.info(f"Using configured projects: {jira_projects}")
        else:
            # Fall back to projects in mapping if no config
            jira_projects = list(
                {entry.get("jira_key") for entry in (self.project_mapping or {}).values() if entry.get("jira_key")}
            )
            self.logger.info(f"No configured projects, using {len(jira_projects)} projects from mapping")

        if not jira_projects:
            self.logger.warning("No Jira projects to migrate (no config and mapping empty)")
            return results

        batch_size = config.migration_config.get("batch_size", 100)

        for project_key in jira_projects:
            # Resolve OpenProject project id - check mapping first
            op_project_id = None
            for entry in self.project_mapping.values():
                if entry.get("jira_key") == project_key and entry.get("openproject_id"):
                    op_project_id = entry["openproject_id"]
                    self.logger.info(f"Found {project_key} in mapping: OP ID {op_project_id}")
                    break

            # If not in mapping, look up in OpenProject by identifier (lowercase project key)
            if not op_project_id:
                try:
                    identifier = project_key.lower()
                    ruby_query = f"Project.find_by(identifier: '{identifier}')&.id"
                    result = self.op_client.execute_large_query_to_json_file(ruby_query, timeout=180)
                    # Handle case where result is a list (multiple projects with same identifier)
                    if isinstance(result, list):
                        op_project_id = result[0] if result else None
                        if len(result) > 1:
                            self.logger.warning(
                                f"Multiple projects found for identifier '{identifier}': {result}. Using first: {op_project_id}",
                            )
                    else:
                        op_project_id = result

                    if op_project_id:
                        self.logger.info(f"Found {project_key} in OpenProject: ID {op_project_id}")
                        # Add to mapping for future use
                        if self.project_mapping is None:
                            self.project_mapping = {}
                        self.project_mapping[project_key] = {
                            "jira_key": project_key,
                            "openproject_id": int(op_project_id),
                            "openproject_identifier": identifier,
                        }
                    else:
                        self.logger.warning(
                            f"Project {project_key} not found in OpenProject (tried identifier '{identifier}'); skipping"
                        )
                        results["projects"].append({"project_key": project_key, "created": 0, "skipped": True})
                        continue
                except Exception as e:
                    self.logger.error(f"Failed to lookup OpenProject project for {project_key}: {e}")
                    results["projects"].append(
                        {"project_key": project_key, "created": 0, "skipped": True, "error": str(e)}
                    )
                    continue

            created_count = 0
            issues_seen = 0
            batch: list[dict[str, Any]] = []
            # Early termination tracking
            total_attempted = 0
            batches_processed = 0

            # Fetch existing work packages for incremental update detection
            existing_wp_map = self._get_existing_work_packages(int(op_project_id))
            self.logger.info(f"Found {len(existing_wp_map)} existing work packages for project {project_key}")

            try:
                work_packages_meta: list[dict[str, Any]] = []
                # Collect existing WP updates for parallel processing
                existing_wp_updates: list[tuple[Issue, dict[str, Any], int]] = []

                # Fetch ALL issues without fast-forward filtering
                for issue in self._iter_all_project_issues(project_key):
                    issues_seen += 1

                    # Check if work package already exists
                    jira_key = getattr(issue, "key", None)
                    if jira_key and jira_key in existing_wp_map:
                        # Collect for parallel processing instead of sequential
                        existing_wp_updates.append((issue, existing_wp_map[jira_key], int(op_project_id)))
                        continue

                    # Create new work package
                    wp = self.prepare_work_package(issue, int(op_project_id))
                    batch.append(wp)
                    # Track minimal metadata for mapping
                    try:
                        jira_id = getattr(issue, "id", None)
                    except Exception:
                        jira_id = None
                    try:
                        jira_key = getattr(issue, "key", None)
                    except Exception:
                        jira_key = None
                    # Enrich meta with non-AR fields for reporting/debug
                    meta = {"jira_id": jira_id, "jira_key": jira_key, "project_key": project_key}
                    try:
                        extra = self._extract_issue_meta(issue)
                        # Prefer our already extracted ids/keys
                        extra.pop("jira_id", None)
                        extra.pop("jira_key", None)
                        meta.update(extra)
                    except Exception:
                        pass
                    work_packages_meta.append(meta)
                    if len(batch) >= batch_size:
                        # Ensure project_id is present on every record in the batch
                        try:
                            for _rec in batch:
                                if "project_id" not in _rec or _rec.get("project_id") in (None, 0, ""):
                                    _rec["project_id"] = int(op_project_id)
                        except Exception:
                            pass

                        # Determine a fallback admin user id once (best-effort)
                        fallback_admin_user_id: int | str | None = None
                        try:
                            admin_id = self.op_client.execute_large_query_to_json_file(
                                "User.where(admin: true).limit(1).pluck(:id).first",
                                timeout=60,
                            )
                            if isinstance(admin_id, int):
                                fallback_admin_user_id = admin_id
                        except Exception:
                            fallback_admin_user_id = None
                        try:
                            _apply_required_defaults(
                                batch,
                                project_id=int(op_project_id),
                                op_client=self.op_client,
                                fallback_admin_user_id=fallback_admin_user_id,
                            )
                        except Exception as e:
                            self.logger.warning("Defaults application failed for %s: %s", project_key, e)

                        # Save batch size for tracking before processing
                        current_batch_size = len(batch)

                        try:
                            # Remove _log_counters before sending to Rails (not a valid attribute)
                            for wp in batch:
                                wp.pop("_log_counters", None)
                            res = self.op_client.bulk_create_records(
                                "WorkPackage",
                                batch,
                                timeout=900,
                                result_basename=f"work_packages_{project_key}",
                            )
                            if isinstance(res, dict):
                                # Persist the bulk result for diagnostics (include paired meta)
                                try:
                                    debug_path = (
                                        Path(self.data_dir)
                                        / f"bulk_result_{project_key}_{datetime.now(tz=UTC).strftime('%Y%m%d_%H%M%S')}.json"
                                    )
                                    with debug_path.open("w", encoding="utf-8") as f:
                                        json.dump({"result": res, "meta": work_packages_meta}, f, indent=2)
                                    self.logger.info("Saved bulk result to %s", debug_path)
                                except Exception:
                                    pass
                                # Always process the created list to build mapping
                                created_list = res.get("created", [])
                                if isinstance(created_list, list) and created_list:
                                    try:
                                        _ = self.work_package_mapping  # ensure attribute exists
                                    except Exception:
                                        self.work_package_mapping = {}
                                    for item in created_list:
                                        try:
                                            idx = item.get("index")
                                            op_id = item.get("id")
                                            if isinstance(idx, int) and 0 <= idx < len(work_packages_meta):
                                                meta = work_packages_meta[idx]
                                                jira_id = meta.get("jira_id")
                                                if jira_id is not None:
                                                    self.work_package_mapping[str(jira_id)] = {
                                                        **meta,
                                                        "openproject_id": op_id,
                                                        "openproject_project_id": int(op_project_id),
                                                    }
                                        except Exception:
                                            continue
                                # Compute created count
                                c = res.get("created_count") or res.get("total_created")
                                if c is None:
                                    c = len(created_list) if isinstance(created_list, list) else 0
                                created_count += int(c or 0)

                                # Track batch for early termination detection
                                total_attempted += current_batch_size
                                batches_processed += 1

                                # Early termination: if we've processed 3+ batches (300+ items) with 0% success rate, stop
                                if batches_processed >= 3 and created_count == 0 and total_attempted >= 300:
                                    self.logger.error(
                                        "EARLY TERMINATION: Processed %d batches (%d work packages attempted) with 0%% success rate for %s. "
                                        "All items are failing validation. Stopping to prevent wasted processing. "
                                        "Please review bulk result files in %s for error details.",
                                        batches_processed,
                                        total_attempted,
                                        project_key,
                                        self.data_dir,
                                    )
                                    # Break out of the issue iteration loop
                                    raise StopIteration("Early termination due to 100% failure rate")
                        except StopIteration:
                            # Early termination triggered - exit cleanly
                            self.logger.warning(
                                "Migration stopped early for %s after %d failed attempts", project_key, total_attempted
                            )
                            break
                        except Exception as e:
                            self.logger.exception("Bulk create failed for %s: %s", project_key, e)
                            # Fallback: adaptively reduce batch size and retry in smaller chunks
                            try:
                                sizes = [max(1, len(batch) // 2), max(10, len(batch) // 4), 5, 1]
                                for sz in sizes:
                                    if sz >= len(batch) and sz != 1:
                                        continue
                                    self.logger.info(
                                        "Retrying %s in %s sub-batches of size %s",
                                        project_key,
                                        (len(batch) + sz - 1) // sz,
                                        sz,
                                    )
                                    for start in range(0, len(batch), sz):
                                        sub = batch[start : start + sz]
                                        meta_slice = work_packages_meta[start : start + sz]
                                        try:
                                            # Remove _log_counters before sending to Rails
                                            for wp in sub:
                                                wp.pop("_log_counters", None)
                                            sub_res = self.op_client.bulk_create_records(
                                                "WorkPackage",
                                                sub,
                                                timeout=900,
                                                result_basename=f"work_packages_{project_key}_sz{sz}",
                                            )
                                            if isinstance(sub_res, dict):
                                                created_list = sub_res.get("created", [])
                                                if isinstance(created_list, list) and created_list:
                                                    try:
                                                        _ = self.work_package_mapping
                                                    except Exception:
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
                                                        except Exception:
                                                            continue
                                                c = sub_res.get("created_count") or (
                                                    len(created_list) if isinstance(created_list, list) else 0
                                                )
                                                created_count += int(c or 0)
                                        except Exception as sub_e:
                                            self.logger.warning(
                                                "Sub-batch failed (%s..%s) for %s: %s",
                                                start,
                                                start + sz,
                                                project_key,
                                                sub_e,
                                            )
                                self.logger.info(
                                    "Fallback batching complete for %s; created so far: %s", project_key, created_count
                                )
                            except Exception as fb_e:
                                self.logger.warning("Fallback batching aborted for %s: %s", project_key, fb_e)
                        finally:
                            batch = []
                            work_packages_meta = []

                # Flush tail batch
                if batch:
                    # Ensure project_id is present on every record in the tail batch
                    try:
                        for _rec in batch:
                            if "project_id" not in _rec or _rec.get("project_id") in (None, 0, ""):
                                _rec["project_id"] = int(op_project_id)
                    except Exception:
                        pass

                    # Determine a fallback admin user id once (best-effort)
                    fallback_admin_user_id: int | str | None = None
                    try:
                        admin_id = self.op_client.execute_large_query_to_json_file(
                            "User.where(admin: true).limit(1).pluck(:id).first",
                            timeout=60,
                        )
                        if isinstance(admin_id, int):
                            fallback_admin_user_id = admin_id
                    except Exception:
                        fallback_admin_user_id = None
                    try:
                        _apply_required_defaults(
                            batch,
                            project_id=int(op_project_id),
                            op_client=self.op_client,
                            fallback_admin_user_id=fallback_admin_user_id,
                        )
                    except Exception as e:
                        self.logger.warning("Defaults application failed for %s: %s", project_key, e)

                    # Save batch size for tracking before processing
                    current_batch_size = len(batch)

                    try:
                        # Remove _log_counters before sending to Rails
                        for wp in batch:
                            wp.pop("_log_counters", None)
                        res = self.op_client.bulk_create_records(
                            "WorkPackage",
                            batch,
                            timeout=900,
                            result_basename=f"work_packages_{project_key}",
                        )
                        if isinstance(res, dict):
                            # Persist the bulk result for diagnostics (include paired meta)
                            try:
                                debug_path = (
                                    Path(self.data_dir)
                                    / f"bulk_result_{project_key}_{datetime.now(tz=UTC).strftime('%Y%m%d_%H%M%S')}.json"
                                )
                                with debug_path.open("w", encoding="utf-8") as f:
                                    json.dump({"result": res, "meta": work_packages_meta}, f, indent=2)
                                self.logger.info("Saved bulk result to %s", debug_path)
                            except Exception:
                                pass
                            # Always process the created list to build mapping
                            created_list = res.get("created", [])
                            if isinstance(created_list, list) and created_list:
                                try:
                                    _ = self.work_package_mapping
                                except Exception:
                                    self.work_package_mapping = {}
                                for item in created_list:
                                    try:
                                        idx = item.get("index")
                                        op_id = item.get("id")
                                        if isinstance(idx, int) and 0 <= idx < len(work_packages_meta):
                                            meta = work_packages_meta[idx]
                                            jira_id = meta.get("jira_id")
                                            if jira_id is not None:
                                                self.work_package_mapping[str(jira_id)] = {
                                                    **meta,
                                                    "openproject_id": op_id,
                                                    "openproject_project_id": int(op_project_id),
                                                }
                                    except Exception:
                                        continue
                            # Compute created count
                            c = res.get("created_count") or res.get("total_created")
                            if c is None:
                                c = len(created_list) if isinstance(created_list, list) else 0
                            created_count += int(c or 0)

                            # Track tail batch for early termination detection
                            total_attempted += current_batch_size
                            batches_processed += 1

                            # Log warning if we have systematic failure even on tail batch
                            if created_count == 0 and total_attempted >= 100:
                                self.logger.warning(
                                    "Processed %d batches (%d work packages attempted) with 0%% success rate for %s. "
                                    "All items are failing validation. Please review bulk result files in %s for error details.",
                                    batches_processed,
                                    total_attempted,
                                    project_key,
                                    self.data_dir,
                                )
                    except Exception as e:
                        self.logger.exception("Bulk create failed (final) for %s: %s", project_key, e)
                        # Final fallback for tail batch
                        try:
                            sizes = [max(1, len(batch) // 2), max(10, len(batch) // 4), 5, 1]
                            for sz in sizes:
                                if sz >= len(batch) and sz != 1:
                                    continue
                                self.logger.info(
                                    "Retrying tail %s in %s sub-batches of size %s",
                                    project_key,
                                    (len(batch) + sz - 1) // sz,
                                    sz,
                                )
                                for start in range(0, len(batch), sz):
                                    sub = batch[start : start + sz]
                                    meta_slice = work_packages_meta[start : start + sz]
                                    try:
                                        # Remove _log_counters before sending to Rails
                                        for wp in sub:
                                            wp.pop("_log_counters", None)
                                        sub_res = self.op_client.bulk_create_records(
                                            "WorkPackage",
                                            sub,
                                            timeout=900,
                                            result_basename=f"work_packages_{project_key}_sz{sz}",
                                        )
                                        if isinstance(sub_res, dict):
                                            created_list = sub_res.get("created", [])
                                            if isinstance(created_list, list) and created_list:
                                                try:
                                                    _ = self.work_package_mapping
                                                except Exception:
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
                                                    except Exception:
                                                        continue
                                            c = sub_res.get("created_count") or (
                                                len(created_list) if isinstance(created_list, list) else 0
                                            )
                                            created_count += int(c or 0)
                                    except Exception as sub_e:
                                        self.logger.warning(
                                            "Tail sub-batch failed (%s..%s) for %s: %s",
                                            start,
                                            start + sz,
                                            project_key,
                                            sub_e,
                                        )
                            self.logger.info(
                                "Tail fallback batching complete for %s; created so far: %s", project_key, created_count
                            )
                        except Exception as fb_e:
                            self.logger.warning("Tail fallback batching aborted for %s: %s", project_key, fb_e)

                # Process existing WP updates in batches
                # Batching amortizes SSH/tmux overhead across multiple WPs
                # Each batch: parallel Jira fetch, single Rails call
                WP_BATCH_SIZE = 20  # Number of WPs per batch
                if existing_wp_updates:
                    total_wps = len(existing_wp_updates)
                    num_batches = (total_wps + WP_BATCH_SIZE - 1) // WP_BATCH_SIZE
                    self.logger.info(
                        f"Processing {total_wps} existing WP updates in {num_batches} batches "
                        f"(batch size: {WP_BATCH_SIZE})",
                    )
                    total_success = 0
                    total_errors = 0

                    for batch_idx in range(num_batches):
                        start_idx = batch_idx * WP_BATCH_SIZE
                        end_idx = min(start_idx + WP_BATCH_SIZE, total_wps)
                        batch = existing_wp_updates[start_idx:end_idx]

                        self.logger.info(
                            f"Batch {batch_idx + 1}/{num_batches}: WPs {start_idx + 1}-{end_idx}",
                        )

                        try:
                            success, errors = self._update_existing_work_packages_batch(batch)
                            total_success += success
                            total_errors += errors
                        except Exception as e:
                            self.logger.warning(f"Batch {batch_idx + 1} failed: {e}")
                            total_errors += len(batch)

                        # Log progress after each batch
                        processed = end_idx
                        self.logger.info(
                            f"Batch progress: {processed}/{total_wps} WPs "
                            f"(success: {total_success}, errors: {total_errors})",
                        )

                    self.logger.info(
                        f"WP update complete: {total_success} success, {total_errors} errors",
                    )

            except Exception as e:
                self.logger.exception("Failed migrating project %s: %s", project_key, e)

            results["projects"].append({"project_key": project_key, "created": created_count, "issues": issues_seen})
            results["total_created"] += created_count
            results["total_issues"] += issues_seen

        # Save the work package mapping if available (used by time_entries)
        try:
            if getattr(self, "work_package_mapping", None):
                data_handler.save(
                    data=self.work_package_mapping,
                    filename="work_package_mapping.json",
                    directory=self.data_dir,
                )
        except Exception:
            pass

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
                error=str(e),
                timestamp=end_time.isoformat(),
                start_time=start_time.isoformat(),
                duration_seconds=duration_seconds,
            )


def _choose_default_type_id(op_client: Any) -> int:
    """Pick a default Type ID, preferring the first by position, else 1.

    This helper is isolated for testability.
    """
    try:
        type_ids = op_client.execute_large_query_to_json_file(
            "Type.order(:position).pluck(:id)",
            timeout=180,
        )
        if isinstance(type_ids, list) and type_ids:
            return int(type_ids[0])
    except Exception:
        pass
    return 1


def _apply_required_defaults(
    records: list[dict[str, Any]],
    *,
    project_id: int | None,
    op_client: Any,
    fallback_admin_user_id: int | str | None,
) -> None:
    """Fill in missing required fields on WorkPackage records.

    Sets type_id, status_id, priority_id, author_id if missing.
    """
    # Defaults via file-based queries
    default_type_id = _choose_default_type_id(op_client)

    default_status_id = 1
    try:
        status_ids = op_client.execute_large_query_to_json_file(
            "Status.order(:position).pluck(:id)",
            timeout=180,
        )
        if isinstance(status_ids, list) and status_ids:
            default_status_id = int(status_ids[0])
    except Exception:
        pass

    default_priority_id = None
    try:
        pr_ids = op_client.execute_large_query_to_json_file(
            "IssuePriority.order(:position).pluck(:id)",
            timeout=180,
        )
        if isinstance(pr_ids, list) and pr_ids:
            default_priority_id = int(pr_ids[0])
    except Exception:
        default_priority_id = None

    default_author_id = None
    if fallback_admin_user_id:
        try:
            default_author_id = int(fallback_admin_user_id)
        except Exception:
            default_author_id = fallback_admin_user_id
    if not default_author_id:
        try:
            admin_ids = op_client.execute_large_query_to_json_file(
                "User.where(admin: true).limit(1).pluck(:id)",
                timeout=180,
            )
            if isinstance(admin_ids, list) and admin_ids:
                default_author_id = int(admin_ids[0])
        except Exception:
            default_author_id = None

    for wp in records:
        if not wp.get("type_id"):
            wp["type_id"] = default_type_id
            # Also set _links.type for OpenProject API compatibility
            if "_links" not in wp:
                wp["_links"] = {}
            wp["_links"]["type"] = {"href": f"/api/v3/types/{default_type_id}"}
        # Normalize status_id: set default if missing or invalid
        if not wp.get("status_id") and default_status_id:
            wp["status_id"] = default_status_id
        else:
            try:
                sid = int(wp.get("status_id")) if wp.get("status_id") is not None else None
                if sid is not None and isinstance(status_ids, list):
                    valid_ids = {int(x) for x in status_ids if isinstance(x, (int, str)) and str(x).isdigit()}
                    if valid_ids and sid not in valid_ids and default_status_id:
                        wp["status_id"] = default_status_id
            except Exception:
                if default_status_id:
                    wp["status_id"] = default_status_id
        if not wp.get("author_id") and default_author_id:
            wp["author_id"] = default_author_id
        if not wp.get("priority_id") and default_priority_id:
            wp["priority_id"] = default_priority_id

        # Bug #10 fix: Validate date constraints - due_date must be >= start_date
        # PostgreSQL CHECK constraint: work_packages_due_larger_start_date
        start_date = wp.get("start_date")
        due_date = wp.get("due_date")
        if start_date and due_date:
            # Both dates exist, compare them
            try:
                # Handle both string and date object formats
                from datetime import date, datetime

                if isinstance(start_date, str):
                    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
                elif isinstance(start_date, date):
                    start_dt = start_date
                else:
                    start_dt = None

                if isinstance(due_date, str):
                    due_dt = datetime.strptime(due_date, "%Y-%m-%d").date()
                elif isinstance(due_date, date):
                    due_dt = due_date
                else:
                    due_dt = None

                # If due_date is before start_date, set due_date to None to avoid constraint violation
                if start_dt and due_dt and due_dt < start_dt:
                    wp["due_date"] = None
            except Exception:
                # If date parsing fails, set due_date to None to be safe
                wp["due_date"] = None
