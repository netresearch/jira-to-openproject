"""Work package migration module for Jira to OpenProject migration.
Handles the migration of issues from Jira to work packages in OpenProject.
"""

import json
import os
import shutil
import sqlite3
import time
from collections.abc import Iterator
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
from src.migrations.wp_defaults import apply_required_defaults
from src.models import ComponentResult, MigrationError
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
        # Create user mapping for markdown converter (Jira username -> OpenProject user ID)
        user_mapping = {
            username: str(user_id)
            for username, user_id in self.user_mapping.items()
            if user_id
        }

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
        )

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

        timestamp_value = (
            payload.get("last_success_at")
            or payload.get("timestamp")
            or row["updated_at"]
        )
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
        limited = [
            key
            for key in sorted({k.strip() for k in existing_keys if k and k.strip()})
            if key
        ][:900]  # Avoid overly long JQL payloads
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

    def iter_project_issues(self, project_key: str) -> Iterator[Issue]:
        """Generate issues for a project with memory-efficient pagination.

        This generator yields individual issues instead of loading all issues
        into memory at once, solving the unbounded memory growth problem.

        Args:
            project_key: The key of the Jira project

        Yields:
            Individual Jira Issue objects

        Raises:
            JiraApiError: If the API request fails after retries
            JiraResourceNotFoundError: If the project is not found

        """
        start_at = 0
        batch_size = config.migration_config.get("batch_size", 100)

        # Reset per-project tracking for latest issue timestamps
        self._project_latest_issue_ts.pop(project_key, None)

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
                if exclusion_clause:
                    jql = (
                        f'{base_condition} AND '
                        f'(updated >= "{cutoff_str}" OR {exclusion_clause}) {ordering}'
                    )
                    if len(existing_keys) > 900:
                        self.logger.debug(
                            "Truncated existing key exclusion list for %s to first 900 entries",
                            project_key,
                        )
                else:
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
        expand = "changelog"  # Include changelog for history

        logger.notice("Starting paginated fetch for project '%s'...", project_key)

        # Verify project exists first
        try:
            self.jira_client.jira.project(project_key)
        except Exception as e:
            from src.clients.jira_client import JiraResourceNotFoundError

            msg = f"Project '{project_key}' not found: {e!s}"
            raise JiraResourceNotFoundError(msg) from e

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

            logger.debug(
                "Yielded %s issues from batch (total: %s) for %s",
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
                if (
                    e.response
                    and e.response.status_code == 429
                    and attempt < max_retries
                ):
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

        Args:
            project_key: The Jira project key to extract issues from
            project_tracker: Optional project tracker for logging

        Returns:
            List of all issues from the project (as dictionaries)

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
            expand = "changelog"

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
                            current_batch = self._fetch_issues_with_retry(
                                jql=jql,
                                start_at=start_at,
                                max_results=batch_size,
                                fields=fields,
                                expand=expand,
                                project_key=project_key,
                            ) or []
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
                    current_batch = self._fetch_issues_with_retry(
                        jql=jql,
                        start_at=start_at,
                        max_results=batch_size,
                        fields=fields,
                        expand=expand,
                        project_key=project_key,
                    ) or []

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
                    f"jira_issues_{project_key}_backup_"
                    f"{datetime.now(tz=UTC).strftime('%Y%m%d_%H%M%S')}.json"
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
                                    "fields": getattr(getattr(it, "raw", {}), "get", lambda *_: {})
                                    ("fields", {}),
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
        if (
            self.issue_type_id_mapping
            and str(issue_type_id) in self.issue_type_id_mapping
        ):
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
        )

        # Log any warnings from timestamp migration
        if timestamp_result["warnings"]:
            for warning in timestamp_result["warnings"]:
                self.logger.warning("Timestamp migration: %s", warning)

        # Log any errors from timestamp migration
        if timestamp_result["errors"]:
            for error in timestamp_result["errors"]:
                self.logger.error("Timestamp migration error: %s", error)

        # Add Jira issue key to description for reference
        jira_reference = f"\n\n*Imported from Jira issue: {jira_key}*"
        if description:
            description += jira_reference
        else:
            description = jira_reference

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
                        {"id": field_id, "value": field_value}
                        for field_id, field_value in custom_field_values.items()
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
                    ("J2O Origin System", "string"),
                    ("J2O Origin ID", "string"),
                    ("J2O Origin Key", "string"),
                    ("J2O Origin URL", "string"),
                    ("J2O First Migration Date", "date"),
                    ("J2O Last Update Date", "date"),
                )
                cf_ids: dict[str, int] = {}
                for name, fmt in cf_specs:
                    try:
                        cf = self.op_client.ensure_custom_field(name, field_format=fmt, cf_type="WorkPackageCustomField")
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
                meta["jira_key"] = (d.get("key") if isinstance(d, dict) else None)
                meta["jira_id"] = (d.get("id") if isinstance(d, dict) else None)
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
                            cf = self.op_client.ensure_custom_field(n, field_format=fmt, cf_type="WorkPackageCustomField")
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
        if (
            type_id
            and self.issue_type_id_mapping
            and str(type_id) in self.issue_type_id_mapping
        ):
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
                wp["description"] = (
                    str(wp["description"]).replace('"', '\\"').replace("'", "\\'")
                )
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

            # Get ALL Jira projects first
            all_projects = self.jira_client.get_projects()
            logger.info(f"Retrieved {len(all_projects)} total Jira projects from API")

            # Filter to only configured projects from config.jira.projects
            try:
                configured_projects = config.jira_config.get("projects") or []
            except Exception:
                configured_projects = []

            if configured_projects:
                # Filter projects to only those in configuration
                projects_to_migrate = [
                    p for p in all_projects
                    if p.get("key") in configured_projects
                ]
                logger.info(
                    f"Filtered to {len(projects_to_migrate)} configured projects: {configured_projects}"
                )
            else:
                # No filter - migrate all projects
                projects_to_migrate = all_projects
                logger.warning(
                    "No projects configured in config.jira.projects - will process ALL projects"
                )

            if not projects_to_migrate:
                logger.warning(
                    f"No projects to migrate after filtering. Configured: {configured_projects}, "
                    f"Available: {[p.get('key') for p in all_projects[:10]]}"
                )
                return []

            # Process each configured project
            for project in projects_to_migrate:
                project_key = project.get("key")
                if project_key:
                    logger.info(
                        f"Starting issue fetch for project {project_key} (change detection)"
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
                        if (
                            len(all_issues)
                            % config.migration_config.get("batch_size", 100)
                            == 0
                        ):
                            logger.info(f"Processed {len(all_issues)} issues so far...")

                    logger.info(
                        f"Completed {project_key}: fetched {project_issue_count} issues for change detection"
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
            "Rebuilding custom field mapping from OpenProject metadata"
        )

        try:
            # Query all custom fields from OpenProject
            op_custom_fields = self.op_client.get_custom_fields(force_refresh=True)

            # Build name-based lookup (same logic as custom_field_migration.py)
            op_fields_by_name = {
                field.get("name", "").lower(): field
                for field in op_custom_fields
                if field.get("name")
            }

            # Load Jira custom fields to build mapping
            jira_fields_file = Path(self.data_dir) / "jira_custom_fields.json"
            if not jira_fields_file.exists():
                self.logger.warning(
                    "Jira custom fields file not found: %s. "
                    "Returning empty mapping.",
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
            jira_projects = list({
                entry.get("jira_key")
                for entry in (self.project_mapping or {}).values()
                if entry.get("jira_key")
            })
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
                    op_project_id = self.op_client.execute_large_query_to_json_file(ruby_query, timeout=30)
                    if op_project_id:
                        self.logger.info(f"Found {project_key} in OpenProject: ID {op_project_id}")
                        # Add to mapping for future use
                        if self.project_mapping is None:
                            self.project_mapping = {}
                        self.project_mapping[project_key] = {
                            "jira_key": project_key,
                            "openproject_id": int(op_project_id),
                            "openproject_identifier": identifier
                        }
                    else:
                        self.logger.warning(f"Project {project_key} not found in OpenProject (tried identifier '{identifier}'); skipping")
                        results["projects"].append({"project_key": project_key, "created": 0, "skipped": True})
                        continue
                except Exception as e:
                    self.logger.error(f"Failed to lookup OpenProject project for {project_key}: {e}")
                    results["projects"].append({"project_key": project_key, "created": 0, "skipped": True, "error": str(e)})
                    continue

            created_count = 0
            issues_seen = 0
            batch: list[dict[str, Any]] = []

            try:
                work_packages_meta: list[dict[str, Any]] = []
                for issue in self.iter_project_issues(project_key):
                    issues_seen += 1
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

                        try:
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
                        except Exception as e:
                            self.logger.exception("Bulk create failed for %s: %s", project_key, e)
                            # Fallback: adaptively reduce batch size and retry in smaller chunks
                            try:
                                sizes = [max(1, len(batch) // 2), max(10, len(batch) // 4), 5, 1]
                                for sz in sizes:
                                    if sz >= len(batch) and sz != 1:
                                        continue
                                    self.logger.info("Retrying %s in %s sub-batches of size %s", project_key, (len(batch) + sz - 1) // sz, sz)
                                    for start in range(0, len(batch), sz):
                                        sub = batch[start : start + sz]
                                        meta_slice = work_packages_meta[start : start + sz]
                                        try:
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
                                                c = sub_res.get("created_count") or (len(created_list) if isinstance(created_list, list) else 0)
                                                created_count += int(c or 0)
                                        except Exception as sub_e:
                                            self.logger.warning("Sub-batch failed (%s..%s) for %s: %s", start, start + sz, project_key, sub_e)
                                self.logger.info("Fallback batching complete for %s; created so far: %s", project_key, created_count)
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

                    try:
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
                    except Exception as e:
                        self.logger.exception("Bulk create failed (final) for %s: %s", project_key, e)
                        # Final fallback for tail batch
                        try:
                            sizes = [max(1, len(batch) // 2), max(10, len(batch) // 4), 5, 1]
                            for sz in sizes:
                                if sz >= len(batch) and sz != 1:
                                    continue
                                self.logger.info("Retrying tail %s in %s sub-batches of size %s", project_key, (len(batch) + sz - 1) // sz, sz)
                                for start in range(0, len(batch), sz):
                                    sub = batch[start : start + sz]
                                    meta_slice = work_packages_meta[start : start + sz]
                                    try:
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
                                            c = sub_res.get("created_count") or (len(created_list) if isinstance(created_list, list) else 0)
                                            created_count += int(c or 0)
                                    except Exception as sub_e:
                                        self.logger.warning("Tail sub-batch failed (%s..%s) for %s: %s", start, start + sz, project_key, sub_e)
                            self.logger.info("Tail fallback batching complete for %s; created so far: %s", project_key, created_count)
                        except Exception as fb_e:
                            self.logger.warning("Tail fallback batching aborted for %s: %s", project_key, fb_e)

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
            timeout=30,
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
            timeout=30,
        )
        if isinstance(status_ids, list) and status_ids:
            default_status_id = int(status_ids[0])
    except Exception:
        pass

    default_priority_id = None
    try:
        pr_ids = op_client.execute_large_query_to_json_file(
            "IssuePriority.order(:position).pluck(:id)",
            timeout=30,
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
                timeout=30,
            )
            if isinstance(admin_ids, list) and admin_ids:
                default_author_id = int(admin_ids[0])
        except Exception:
            default_author_id = None

    for wp in records:
        if not wp.get("type_id"):
            wp["type_id"] = default_type_id
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

