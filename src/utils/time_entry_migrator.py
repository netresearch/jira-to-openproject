#!/usr/bin/env python3
"""Comprehensive Time Entry Migration for Jira to OpenProject migration.

This module provides complete time tracking data migration capabilities:
1. Extracts work logs from Jira with all metadata
2. Extracts Tempo time entries with account and billing information
3. Transforms and maps time entries to OpenProject format
4. Handles bulk migration with error handling and reporting
5. Integrates with Rails console for advanced operations
6. Provides comprehensive validation and reporting
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import configure_logging
from src.utils.time_entry_transformer import TimeEntryTransformer
from src.models.migration_error import MigrationError

# Get logger from config
logger = configure_logging("INFO", None)


class TimeEntryMigrationResult(TypedDict):
    """Type hint for time entry migration results."""

    total_work_logs_found: int
    jira_work_logs_extracted: int
    tempo_entries_extracted: int
    successful_transformations: int
    failed_transformations: int
    successful_migrations: int
    failed_migrations: int
    skipped_entries: int
    errors: list[str]
    warnings: list[str]
    processing_time_seconds: float


class TimeEntryMigrator:
    """Comprehensive time entry migrator for Jira to OpenProject migration."""

    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
        data_dir: Path | None = None,
    ) -> None:
        """Initialize the Time Entry Migrator.

        Args:
            jira_client: JiraClient instance for accessing Jira data
            op_client: OpenProjectClient instance for creating time entries
            data_dir: Directory for storing migration data files

        """
        self.jira_client = jira_client
        self.op_client = op_client
        self.logger = logger
        self.data_dir = data_dir or Path(config.get_path("data"))

        # Data storage
        self.extracted_work_logs: dict[str, list[dict[str, Any]]] = {}
        self.extracted_tempo_entries: list[dict[str, Any]] = []
        self.transformed_time_entries: list[dict[str, Any]] = []

        # Migration mappings
        self.user_mapping: dict[str, int] = {}
        self.work_package_mapping: dict[str, int] = {}
        self.activity_mapping: dict[str, int] = {}
        self.project_mapping: dict[str, int] = {}
        self.default_activity_id: int | None = None

        # Results tracking
        self.migration_results: TimeEntryMigrationResult = {
            "total_work_logs_found": 0,
            "jira_work_logs_extracted": 0,
            "tempo_entries_extracted": 0,
            "successful_transformations": 0,
            "failed_transformations": 0,
            "successful_migrations": 0,
            "failed_migrations": 0,
            "skipped_entries": 0,
            "errors": [],
            "warnings": [],
            "processing_time_seconds": 0.0,
        }

        # Initialize transformer (will be updated with mappings)
        self.transformer = TimeEntryTransformer()

        # Load mappings if available
        self._load_mappings()

    def _load_mappings(self) -> None:
        """Load existing mappings from migration data files."""
        try:
            # Load user mapping
            user_mapping_file = self.data_dir / "user_mapping.json"
            if user_mapping_file.exists():
                with open(user_mapping_file, encoding="utf-8") as f:
                    user_data = json.load(f)
                    expanded: dict[str, int] = {}
                    for entry in user_data.values():
                        op_id = entry.get("openproject_id")
                        if not op_id:
                            continue
                        # Collect potential Jira identifiers
                        jira_keys = [
                            entry.get("jira_username"),
                            entry.get("jira_name"),
                            entry.get("jira_key"),
                            entry.get("jira_email"),
                            entry.get("jira_display_name"),
                        ]
                        for key in jira_keys:
                            if key:
                                expanded[str(key)] = op_id
                    self.user_mapping = expanded
                self.logger.info(f"Loaded {len(self.user_mapping)} user mappings (expanded)")

            # Load work package mapping
            wp_mapping_file = self.data_dir / "work_package_mapping.json"
            if wp_mapping_file.exists():
                with open(wp_mapping_file, encoding="utf-8") as f:
                    wp_data = json.load(f)
                    self.work_package_mapping = {
                        entry.get("jira_key", ""): entry.get("openproject_id")
                        for entry in wp_data.values()
                        if entry.get("jira_key") and entry.get("openproject_id")
                    }
                self.logger.info(
                    f"Loaded {len(self.work_package_mapping)} work package mappings",
                )

            # Load activity mapping from OpenProject
            self._load_activity_mapping()

        except Exception as e:
            self.logger.warning(f"Failed to load some mappings: {e}")
            if config.migration_config.get("stop_on_error", False):
                raise MigrationError(f"Failed to load required mappings: {e}") from e

    def _load_activity_mapping(self) -> None:
        """Load activity mapping from OpenProject."""
        try:
            activities = self.op_client.get_time_entry_activities()
            self.activity_mapping = {
                activity.get("name", "").lower(): activity.get("id")
                for activity in activities
                if activity.get("name") and activity.get("id") is not None
            }

            # Set default activity ID (usually first one)
            if self.activity_mapping:
                self.default_activity_id = next(iter(self.activity_mapping.values()))
            else:
                self.default_activity_id = None

            self.logger.info(f"Loaded {len(self.activity_mapping)} activity mappings")

        except Exception as e:
            self.logger.warning(f"Failed to load activity mappings: {e}")
            # Honor stop-on-error if configured to avoid silent partial migrations
            try:
                if config.migration_config.get("stop_on_error", False):
                    raise MigrationError(f"Failed to load activity mappings: {e}") from e
            finally:
                self.activity_mapping = {}
                self.default_activity_id = None

    def extract_jira_work_logs_for_issues(
        self,
        issue_keys: list[str],
        save_to_file: bool = True,
    ) -> dict[str, list[dict[str, Any]]]:
        """Extract work logs from Jira for a list of issues.

        Args:
            issue_keys: List of Jira issue keys to extract work logs for
            save_to_file: Whether to save extracted data to file

        Returns:
            Dictionary mapping issue keys to their work logs

        """
        self.logger.info(f"Extracting work logs for {len(issue_keys)} Jira issues")
        start_time = datetime.now()

        extracted_logs = {}
        total_logs = 0

        for issue_key in issue_keys:
            try:
                work_logs = self.jira_client.get_work_logs_for_issue(issue_key)

                if work_logs:
                    # Add issue_key to each work log for later processing
                    for log in work_logs:
                        log["issue_key"] = issue_key

                    extracted_logs[issue_key] = work_logs
                    total_logs += len(work_logs)
                    self.logger.debug(
                        f"Extracted {len(work_logs)} work logs for {issue_key}",
                    )
                else:
                    self.logger.debug(f"No work logs found for {issue_key}")

            except Exception as e:
                error_msg = f"Failed to extract work logs for {issue_key}: {e}"
                self.logger.exception(error_msg)
                self.migration_results["errors"].append(error_msg)
                continue

        self.extracted_work_logs = extracted_logs
        self.migration_results["jira_work_logs_extracted"] = total_logs
        self.migration_results["total_work_logs_found"] += total_logs

        # Save to file if requested
        if save_to_file:
            self._save_extracted_work_logs()

        processing_time = (datetime.now() - start_time).total_seconds()
        self.logger.success(
            f"Extracted {total_logs} work logs from {len(extracted_logs)} issues in {processing_time:.2f}s",
        )

        return extracted_logs

    def extract_tempo_time_entries(
        self,
        project_keys: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        save_to_file: bool = True,
    ) -> list[dict[str, Any]]:
        """Extract Tempo time entries with enhanced metadata.

        Args:
            project_keys: List of project keys to extract entries for (None for all)
            date_from: Start date for extraction (YYYY-MM-DD format)
            date_to: End date for extraction (YYYY-MM-DD format)
            save_to_file: Whether to save extracted data to file

        Returns:
            List of Tempo time entry dictionaries

        """
        self.logger.info("Extracting Tempo time entries")
        start_time = datetime.now()

        try:
            # Check if Tempo is available
            if not hasattr(self.jira_client, "get_tempo_time_entries"):
                self.logger.warning(
                    "Tempo integration not available - skipping Tempo extraction",
                )
                return []

            # Extract Tempo entries with all metadata
            tempo_entries = self.jira_client.get_tempo_time_entries(
                project_keys=project_keys,
                date_from=date_from,
                date_to=date_to,
            )

            if tempo_entries:
                self.extracted_tempo_entries = tempo_entries
                self.migration_results["tempo_entries_extracted"] = len(tempo_entries)
                self.migration_results["total_work_logs_found"] += len(tempo_entries)

                # Save to file if requested
                if save_to_file:
                    self._save_extracted_tempo_entries()

                processing_time = (datetime.now() - start_time).total_seconds()
                self.logger.success(
                    f"Extracted {len(tempo_entries)} Tempo entries in {processing_time:.2f}s",
                )
            else:
                self.logger.info("No Tempo time entries found")

            return tempo_entries

        except Exception as e:
            error_msg = f"Failed to extract Tempo time entries: {e}"
            self.logger.exception(error_msg)
            self.migration_results["errors"].append(error_msg)
            return []

    def transform_all_time_entries(self) -> list[dict[str, Any]]:
        """Transform all extracted work logs and Tempo entries to OpenProject format.

        Returns:
            List of transformed time entries ready for migration

        """
        self.logger.info("Transforming extracted time entries to OpenProject format")
        start_time = datetime.now()

        # Update transformer with current mappings
        self.transformer = TimeEntryTransformer(
            user_mapping=self.user_mapping,
            work_package_mapping=self.work_package_mapping,
            activity_mapping=self.activity_mapping,
            default_activity_id=getattr(self, "default_activity_id", None),
        )

        transformed_entries = []

        # Transform Jira work logs
        if self.extracted_work_logs:
            jira_work_logs = []
            for work_logs in self.extracted_work_logs.values():
                jira_work_logs.extend(work_logs)

            if jira_work_logs:
                self.logger.info(f"Transforming {len(jira_work_logs)} Jira work logs")
                try:
                    jira_transformed = self.transformer.batch_transform_work_logs(
                        jira_work_logs,
                        source_type="jira",
                    )
                    transformed_entries.extend(jira_transformed)
                    self.migration_results["successful_transformations"] += len(
                        jira_transformed,
                    )

                except Exception as e:
                    error_msg = f"Failed to transform Jira work logs: {e}"
                    self.logger.exception(error_msg)
                    self.migration_results["errors"].append(error_msg)

        # Transform Tempo time entries
        if self.extracted_tempo_entries:
            self.logger.info(
                f"Transforming {len(self.extracted_tempo_entries)} Tempo entries",
            )
            try:
                tempo_transformed = self.transformer.batch_transform_work_logs(
                    self.extracted_tempo_entries,
                    source_type="tempo",
                )
                transformed_entries.extend(tempo_transformed)
                self.migration_results["successful_transformations"] += len(
                    tempo_transformed,
                )

            except Exception as e:
                error_msg = f"Failed to transform Tempo entries: {e}"
                self.logger.exception(error_msg)
                self.migration_results["errors"].append(error_msg)

        self.transformed_time_entries = transformed_entries

        # Calculate failed transformations
        total_extracted = (
            self.migration_results["jira_work_logs_extracted"]
            + self.migration_results["tempo_entries_extracted"]
        )
        self.migration_results["failed_transformations"] = (
            total_extracted - self.migration_results["successful_transformations"]
        )

        processing_time = (datetime.now() - start_time).total_seconds()
        self.logger.success(
            f"Transformed {len(transformed_entries)} time entries in {processing_time:.2f}s",
        )

        return transformed_entries

    def migrate_time_entries_to_openproject(
        self,
        time_entries: list[dict[str, Any]] | None = None,
        batch_size: int = 50,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Migrate time entries to OpenProject.

        Args:
            time_entries: Time entries to migrate (uses transformed_time_entries if None)
            batch_size: Number of entries to process in each batch
            dry_run: If True, don't actually create entries in OpenProject

        Returns:
            Migration results summary

        """
        entries_to_migrate = time_entries or self.transformed_time_entries
        if not entries_to_migrate:
            self.logger.warning("No time entries to migrate")
            return {}

        self.logger.info(
            f"Migrating {len(entries_to_migrate)} time entries to OpenProject",
        )
        start_time = datetime.now()

        migration_summary: dict[str, Any] = {
            "total_entries": len(entries_to_migrate),
            "successful_migrations": 0,
            "failed_migrations": 0,
            "skipped_entries": 0,
            "created_time_entry_ids": [],
            "errors": [],
            "warnings": [],
            "skipped_details": [],
        }

        if dry_run:
            self.logger.warning("DRY RUN mode - no time entries will be created")
            migration_summary["successful_migrations"] = len(entries_to_migrate)
            return migration_summary

        # If enabled and sufficiently large, use batch creation for performance
        use_batch = bool(config.migration_config.get("enable_time_entry_batch", False))
        if (
            use_batch
            and len(entries_to_migrate) >= max(25, batch_size)
            and hasattr(self.op_client, "batch_create_time_entries")
        ):
            try:
                batch_result = self.op_client.batch_create_time_entries(entries_to_migrate)
                created = int(batch_result.get("created", 0))
                failed = int(batch_result.get("failed", 0))
                migration_summary["successful_migrations"] += created
                migration_summary["failed_migrations"] += failed
                # Note: collect IDs if returned
                ids = [
                    r.get("id")
                    for r in batch_result.get("results", [])
                    if r.get("success") and r.get("id")
                ]
                migration_summary["created_time_entry_ids"].extend(ids)
                # Update global results and return immediately to avoid double-processing
                self.migration_results["successful_migrations"] = migration_summary[
                    "successful_migrations"
                ]
                self.migration_results["failed_migrations"] = migration_summary[
                    "failed_migrations"
                ]
                processing_time = (datetime.now() - start_time).total_seconds()
                self.migration_results["processing_time_seconds"] += processing_time
                self.logger.success(
                    f"Migration completed (batch): {migration_summary['successful_migrations']} successful, "
                    f"{migration_summary['failed_migrations']} failed in {processing_time:.2f}s",
                )
                return migration_summary
            except Exception as e:
                self.logger.warning(f"Batch create failed, falling back to per-entry: {e}")
                # fall through to per-entry loop

        # Process in batches (per-entry) when not covered by batch create
        for i in range(0, len(entries_to_migrate), batch_size):
            batch = entries_to_migrate[i : i + batch_size]
            batch_num = i // batch_size + 1

            self.logger.info(f"Processing batch {batch_num} ({len(batch)} entries)")

            for entry in batch:
                try:
                    # Validate entry has required fields
                    valid, reason = self._validate_time_entry_with_reason(entry)
                    if not valid:
                        migration_summary["skipped_entries"] += 1
                        # capture up to first 30 skipped entries for diagnostics
                        if len(migration_summary["skipped_details"]) < 30:
                            migration_summary["skipped_details"].append(
                                {
                                    "reason": reason,
                                    "hours": entry.get("hours"),
                                    "spentOn": entry.get("spentOn"),
                                    "user": entry.get("_embedded", {})
                                    .get("user", {})
                                    .get("href"),
                                    "workPackage": entry.get("_embedded", {})
                                    .get("workPackage", {})
                                    .get("href"),
                                    "meta": entry.get("_meta", {}),
                                },
                            )
                        continue

                    # Attach provenance key in meta if available
                    try:
                        if isinstance(entry, dict):
                            meta = entry.get("_meta") or {}
                            if not meta:
                                meta = {}
                                entry["_meta"] = meta
                            # Prefer worklog id if present; fallback to composite
                            meta_src = entry.get("_meta", {})
                            wl_id = (
                                meta_src.get("jira_work_log_id")
                                or meta_src.get("jira_worklog_id")
                                or entry.get("jira_work_log_id")
                                or entry.get("jira_worklog_id")
                            )
                            if wl_id and not meta.get("jira_worklog_key"):
                                meta["jira_worklog_key"] = str(wl_id)
                            elif entry.get("jira_key") and entry.get("worklog_id") and not meta.get("jira_worklog_key"):
                                meta["jira_worklog_key"] = f"{entry.get('jira_key')}:{entry.get('worklog_id')}"
                    except Exception:
                        pass

                    # Create time entry in OpenProject
                    created_entry = self.op_client.create_time_entry(entry)

                    if created_entry and created_entry.get("id"):
                        migration_summary["successful_migrations"] += 1
                        migration_summary["created_time_entry_ids"].append(
                            created_entry["id"],
                        )

                        self.logger.debug(
                            f"Created time entry {created_entry['id']} for work package "
                            f"{entry.get('_embedded', {}).get('workPackage', {}).get('href', 'unknown')}",
                        )
                    else:
                        migration_summary["failed_migrations"] += 1
                        error_msg = "Failed to create time entry: No ID returned"
                        migration_summary["errors"].append(error_msg)

                except Exception as e:
                    migration_summary["failed_migrations"] += 1
                    error_msg = f"Failed to create time entry: {e}"
                    migration_summary["errors"].append(error_msg)
                    self.logger.exception(error_msg)

        # Update global results
        self.migration_results["successful_migrations"] = migration_summary[
            "successful_migrations"
        ]
        self.migration_results["failed_migrations"] = migration_summary[
            "failed_migrations"
        ]
        self.migration_results["skipped_entries"] = migration_summary["skipped_entries"]
        self.migration_results["errors"].extend(migration_summary["errors"])

        processing_time = (datetime.now() - start_time).total_seconds()
        self.migration_results["processing_time_seconds"] += processing_time

        self.logger.success(
            f"Migration completed: {migration_summary['successful_migrations']} successful, "
            f"{migration_summary['failed_migrations']} failed, "
            f"{migration_summary['skipped_entries']} skipped in {processing_time:.2f}s",
        )

        # Persist skipped details sample for diagnostics
        try:
            if migration_summary.get("skipped_details"):
                diag_path = self.data_dir / "time_entry_skipped_samples.json"
                with open(diag_path, "w", encoding="utf-8") as f:
                    json.dump(migration_summary["skipped_details"], f, indent=2)
                self.logger.info(
                    f"Saved skipped sample diagnostics to {diag_path}",
                )
        except Exception as e:
            self.logger.warning(f"Failed to save skipped diagnostics: {e}")

        return migration_summary

    def _validate_time_entry_with_reason(self, entry: dict[str, Any]) -> tuple[bool, str]:
        embedded = entry.get("_embedded", {})
        if not embedded.get("workPackage") or not embedded.get("user"):
            self.logger.warning("Skipping entry missing workPackage or user embedding")
            return False, "missing_embedding"
        hours_value = entry.get("hours")
        if (
            not isinstance(hours_value, (int, float))
            or hours_value is None
            or hours_value <= 0
            or entry.get("spentOn") is None
        ):
            self.logger.warning("Skipping entry missing hours or spentOn")
            return False, "invalid_hours_or_date"
        return True, "ok"

    def migrate_time_entries_for_issues(
        self,
        migrated_issues: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Migrate time entries for a list of migrated work packages.

        This is the main entry point called by the migration workflow.

        Args:
            migrated_issues: List of dictionaries with jira_key, work_package_id, project_id

        Returns:
            Dictionary with migration results

        """
        self.logger.info(
            f"Starting time entry migration for {len(migrated_issues)} work packages",
        )
        overall_start_time = datetime.now()

        try:
            # Extract issue keys for processing
            issue_keys = [issue["jira_key"] for issue in migrated_issues]

            # Update work package mapping from migrated issues
            for issue in migrated_issues:
                if issue.get("jira_key") and issue.get("work_package_id"):
                    self.work_package_mapping[issue["jira_key"]] = issue[
                        "work_package_id"
                    ]

            # Run complete migration process
            migration_result = self.run_complete_migration(
                issue_keys=issue_keys,
                include_tempo=True,
                batch_size=50,
                dry_run=False,
            )

            # Compose high-level stats for component gating & reporting
            jira_discovered = self.migration_results.get("jira_work_logs_extracted", 0)
            tempo_discovered = self.migration_results.get("tempo_entries_extracted", 0)
            migrated_count = self.migration_results.get("successful_migrations", 0)
            failed_count = self.migration_results.get("failed_migrations", 0)

            return {
                "status": (
                    "success"
                    if failed_count == 0
                    else ("partial_success" if migrated_count > 0 else "failed")
                ),
                "jira_work_logs": {
                    "discovered": jira_discovered,
                },
                "tempo_time_entries": {
                    "discovered": tempo_discovered,
                },
                "total_time_entries": {
                    "migrated": migrated_count,
                    "failed": failed_count,
                },
                "time": (datetime.now() - overall_start_time).total_seconds(),
            }

        except Exception as e:
            self.logger.exception("Time entry migration failed: %s", e)
            return {
                "status": "failed",
                "error": str(e),
            }

    def run_complete_migration(
        self,
        issue_keys: list[str],
        include_tempo: bool = True,
        batch_size: int = 50,
        dry_run: bool = False,
    ) -> TimeEntryMigrationResult:
        """Run the complete time entry migration process.

        Args:
            issue_keys: List of Jira issue keys to migrate time entries for
            include_tempo: Whether to include Tempo time entries
            batch_size: Batch size for migration
            dry_run: If True, don't actually create entries in OpenProject

        Returns:
            Complete migration results

        """
        self.logger.info("Starting complete time entry migration")
        overall_start_time = datetime.now()

        try:
            # Step 1: Extract Jira work logs
            self.extract_jira_work_logs_for_issues(issue_keys)

            # Step 2: Extract Tempo entries if requested
            if include_tempo:
                self.extract_tempo_time_entries()

            # Step 3: Transform all entries
            self.transform_all_time_entries()

            # Step 4: Migrate to OpenProject
            self.migrate_time_entries_to_openproject(
                batch_size=batch_size,
                dry_run=dry_run,
            )

            # Calculate total processing time
            total_time = (datetime.now() - overall_start_time).total_seconds()
            self.migration_results["processing_time_seconds"] = total_time

            # Generate summary report
            self._generate_migration_report()

            self.logger.success(
                f"Complete time entry migration finished in {total_time:.2f}s. "
                f"Extracted: {self.migration_results['total_work_logs_found']}, "
                f"Transformed: {self.migration_results['successful_transformations']}, "
                f"Migrated: {self.migration_results['successful_migrations']}",
            )

        except Exception as e:
            error_msg = f"Complete migration failed: {e}"
            self.logger.exception(error_msg)
            self.migration_results["errors"].append(error_msg)

        return self.migration_results

    # == Helpers ==
    def _validate_time_entry(self, entry: dict[str, Any]) -> bool:
        """Validate required fields for a time entry."""
        embedded = entry.get("_embedded", {})
        if not embedded.get("workPackage") or not embedded.get("user"):
            self.logger.warning("Skipping entry missing workPackage or user embedding")
            return False
        hours_value = entry.get("hours")
        if (
            not isinstance(hours_value, (int, float))
            or hours_value is None
            or hours_value <= 0
            or entry.get("spentOn") is None
        ):
            self.logger.warning("Skipping entry missing hours or spentOn")
            return False
        return True

    def _save_extracted_work_logs(self) -> None:
        try:
            path = self.data_dir / "jira_work_logs.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.extracted_work_logs, f, indent=2)
        except Exception as e:
            self.logger.warning(f"Failed to save Jira work logs: {e}")

    def _save_extracted_tempo_entries(self) -> None:
        try:
            path = self.data_dir / "tempo_time_entries.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.extracted_tempo_entries, f, indent=2)
        except Exception as e:
            self.logger.warning(f"Failed to save Tempo time entries: {e}")

    def _generate_migration_report(self) -> None:
        """Generate a comprehensive migration report."""
        try:
            report = {
                "migration_summary": self.migration_results,
                "extraction_details": {
                    "jira_work_logs_by_issue": {
                        issue: len(logs)
                        for issue, logs in self.extracted_work_logs.items()
                    },
                    "tempo_entries_total": len(self.extracted_tempo_entries),
                },
                "mapping_statistics": {
                    "user_mappings": len(self.user_mapping),
                    "work_package_mappings": len(self.work_package_mapping),
                    "activity_mappings": len(self.activity_mapping),
                },
                "transformation_details": {
                    "total_transformed": len(self.transformed_time_entries),
                    "jira_entries": len(
                        [
                            e
                            for e in self.transformed_time_entries
                            if e.get("_meta", {}).get("jira_work_log_id")
                        ],
                    ),
                    "tempo_entries": len(
                        [
                            e
                            for e in self.transformed_time_entries
                            if e.get("_meta", {}).get("tempo_worklog_id")
                        ],
                    ),
                },
                "generated_at": datetime.now().isoformat(),
            }

            report_file = self.data_dir / "time_entry_migration_report.json"
            with open(report_file, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)

            self.logger.info(f"Generated migration report: {report_file}")

        except Exception as e:
            self.logger.warning(f"Failed to generate migration report: {e}")

    def get_migration_summary(self) -> dict[str, Any]:
        """Get a summary of the migration results.

        Returns:
            Dictionary with migration summary statistics

        """
        return {
            "total_work_logs_found": self.migration_results["total_work_logs_found"],
            "successful_transformations": self.migration_results[
                "successful_transformations"
            ],
            "successful_migrations": self.migration_results["successful_migrations"],
            "failed_migrations": self.migration_results["failed_migrations"],
            "error_count": len(self.migration_results["errors"]),
            "warning_count": len(self.migration_results["warnings"]),
            "processing_time_seconds": self.migration_results[
                "processing_time_seconds"
            ],
            "success_rate": (
                self.migration_results["successful_migrations"]
                / max(self.migration_results["total_work_logs_found"], 1)
                * 100
            ),
        }
