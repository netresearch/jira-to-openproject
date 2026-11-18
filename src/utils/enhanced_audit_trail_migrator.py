#!/usr/bin/env python3
# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Enhanced Audit Trail Migration for comprehensive history and activity stream preservation.

This module provides advanced audit trail migration capabilities that:
1. Extracts complete change history and activity streams from Jira issues
2. Transforms changelog entries to OpenProject audit format
3. Uses Rails console integration for inserting audit events that cannot be created via API
4. Preserves proper user attribution and timestamps
5. Handles orphaned or partial events gracefully
6. Provides comprehensive validation and reporting
"""

from pathlib import Path
from typing import Any, TypedDict

from src import config
from src.display import configure_logging
from src.infrastructure.jira.comment_audience import has_comment_audience, may_copy_history_item
from src.infrastructure.jira.jira_client import JiraClient
from src.infrastructure.openproject.openproject_client import OpenProjectClient
from src.utils.markdown_converter import MarkdownConverter

# Get logger from config
logger = configure_logging("INFO", None)


class AuditEventData(TypedDict):
    """Type hint for audit event data structure."""

    jira_issue_key: str
    jira_changelog_id: str
    openproject_work_package_id: int
    user_id: int | None
    user_name: str | None
    created_at: str
    action: str
    auditable_type: str
    auditable_id: int
    version: int
    comment: str | None
    changes: dict[str, Any]


class EnhancedAuditTrailMigrator:
    """Enhanced audit trail migrator with comprehensive changelog migration capabilities."""

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        """Initialize the Enhanced Audit Trail Migrator.

        Args:
            jira_client: JiraClient instance for accessing Jira data
            op_client: OpenProjectClient instance for creating audit events

        """
        self.jira_client = jira_client
        self.op_client = op_client
        self.logger = logger
        self.data_dir = Path(config.get_path("data"))

        # Data storage
        self.changelog_data: dict[str, list[Any]] = {}
        self.audit_events: list[AuditEventData] = []
        self.user_mapping: dict[str, Any] = {}
        self.project_mapping: dict[str, dict[str, Any]] = {}

        # Migration results
        self.migration_results = {
            "total_changelog_entries": 0,
            "processed_entries": 0,
            "successful_migrations": 0,
            "failed_migrations": 0,
            "skipped_entries": 0,
            "orphaned_events": 0,
            "user_attribution_failures": 0,
            "errors": [],
            "warnings": [],
            "comments_processed": 0,
            "rails_execution_success": False,
        }

        # Rails operations queue
        self.rails_operations: list[dict[str, Any]] = []

        # Load user mapping if available
        self._load_user_mapping()
        # Load project mapping for project move links
        self._load_project_mapping()
        # Initialize converter for mention conversion using current mapping
        self.converter = MarkdownConverter(user_mapping=self.user_mapping)

    def _load_user_mapping(self) -> None:
        """Load user mapping from previous migration results."""
        try:
            from src.utils import data_handler

            # Use data_handler.load_dict without directory - it will use config.get_path("data")
            self.user_mapping = data_handler.load_dict(
                filename=Path("user_mapping.json"),
                default={},
            )
            if self.user_mapping:
                self.logger.info(f"Loaded {len(self.user_mapping)} user mappings")
            else:
                self.logger.warning(
                    "User mapping file not found or empty - will use fallback user attribution",
                )

        except Exception as e:
            self.logger.warning(f"Failed to load user mapping: {e}")
        # Keep converter in sync with latest mapping
        try:
            if hasattr(self, "converter"):
                self.converter.user_mapping = self.user_mapping or {}
        except Exception:
            pass

    def _load_project_mapping(self) -> None:
        """Load project mapping for generating project move links."""
        try:
            from src.utils import data_handler

            self.project_mapping = data_handler.load_dict(Path("project_mapping.json"))
            self.logger.debug("Loaded %d project mappings", len(self.project_mapping))

        except Exception as e:
            self.logger.warning(f"Failed to load project mapping: {e}")

    @staticmethod
    def _history_entry(history: Any) -> dict[str, Any]:
        author = getattr(history, "author", None)
        items = []
        for raw in history.items:
            if not may_copy_history_item(raw, history):
                continue
            item = {
                key: getattr(raw, key, None)
                for key in ("field", "fieldtype", "fieldId", "from", "fromString", "to", "toString")
            }
            items.append(item)
        return {
            "id": history.id,
            "created": history.created,
            "author": {
                key: getattr(author, key, None) for key in ("accountId", "name", "key", "displayName", "emailAddress")
            },
            "items": items,
        }

    def _audit_author(self, entry: dict[str, Any], jira_issue_key: str) -> tuple[int, str | None]:
        author = entry.get("author") or {}
        keys = [author.get(key) for key in ("accountId", "name", "key", "emailAddress", "displayName")]
        names = [value for value in keys if isinstance(value, str) and value.strip()]
        for name in names:
            if (user_id := self._mapped_user_id(name)) is not None:
                return user_id, name
        name = next(iter(names), None)
        self.migration_results["user_attribution_failures"] += 1
        self.migration_results["warnings"].append(f"No user mapping found for {name} in issue {jira_issue_key}")
        return 1, name

    def _mapped_user_id(self, key: Any) -> int | None:
        if not isinstance(key, str):
            return None
        entry = self.user_mapping.get(key)
        value = entry.get("openproject_id") if isinstance(entry, dict) else entry
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            value = int(value)
        return value if type(value) is int and value > 0 else None

    def _queue_grouped_events(self, audit_events: list[AuditEventData]) -> None:
        grouped = {}
        for event in audit_events:
            changelog_id = str(event.get("jira_changelog_id", ""))
            if changelog_id not in grouped:
                grouped[changelog_id] = {**event, "changes": dict(event.get("changes") or {})}
                continue
            grouped[changelog_id]["changes"].update(event.get("changes") or {})
            comment = event.get("comment")
            if comment:
                grouped[changelog_id]["comment"] = "\n".join(
                    filter(None, (grouped[changelog_id].get("comment"), comment))
                )
        for event in grouped.values():
            self.rails_operations.append({"operation": "create_audit_event", "data": event})

    def extract_changelog_from_issue(self, jira_issue: Any) -> list[dict[str, Any]]:
        """Extract changelog data from a Jira issue.

        Args:
            jira_issue: Jira issue object with changelog expanded

        Returns:
            List of changelog entries with normalized structure

        """
        changelog_entries = []

        try:
            if not hasattr(jira_issue, "changelog") or not jira_issue.changelog:
                self.logger.debug(f"No changelog found for issue {jira_issue.key}")
                return changelog_entries

            self.logger.debug(
                f"Processing {len(jira_issue.changelog.histories)} changelog entries for {jira_issue.key}",
            )

            for history in jira_issue.changelog.histories:
                # Extract basic history information
                entry = self._history_entry(history)

                if entry["items"]:
                    changelog_entries.append(entry)

        except Exception as e:
            self.logger.exception(
                f"Error extracting changelog from issue {jira_issue.key}: {e}",
            )
            self.migration_results["errors"].append(
                f"Changelog extraction failed for {jira_issue.key}: {e}",
            )

        return changelog_entries

    def transform_changelog_to_audit_events(
        self,
        changelog_entries: list[dict[str, Any]],
        jira_issue_key_or_wp: Any,
        openproject_work_package_id: int | None = None,
    ) -> list[AuditEventData]:
        """Transform Jira changelog entries to OpenProject audit events.

        Args:
            changelog_entries: List of Jira changelog entries
            jira_issue_key_or_wp: Jira issue key, or work package ID when called with 2 args (legacy)
            openproject_work_package_id: OpenProject work package ID (optional for legacy 2-arg calls)

        Returns:
            List of audit event data structures

        """
        # Backward-compatible argument handling (some tests call with 2 args)
        if openproject_work_package_id is None:
            jira_issue_key = "UNKNOWN"
            openproject_work_package_id = int(jira_issue_key_or_wp)
        else:
            jira_issue_key = str(jira_issue_key_or_wp)

        audit_events = []

        for entry in changelog_entries:
            if has_comment_audience(entry):
                continue
            try:
                self._collect_audit_events(entry, jira_issue_key, openproject_work_package_id, audit_events)
            except Exception as e:
                self.logger.exception(
                    f"Error transforming changelog entry {entry.get('id', 'unknown')} for {jira_issue_key}: {e}",
                )
                self.migration_results["errors"].append(
                    f"Transformation failed for changelog {entry.get('id')} in {jira_issue_key}: {e}",
                )

        return audit_events

    def _collect_audit_events(
        self,
        entry: dict[str, Any],
        jira_issue_key: str,
        openproject_work_package_id: int,
        audit_events: list[AuditEventData],
    ) -> None:
        user_id, user_name = self._audit_author(entry, jira_issue_key)

        # Process each change item in the entry
        for item in entry.get("items", []) or []:
            if not may_copy_history_item(item, entry):
                continue
            # Map field changes to OpenProject audit format
            changes = self._map_field_changes(item)

            if changes:
                audit_event: AuditEventData = {
                    "jira_issue_key": jira_issue_key,
                    "jira_changelog_id": entry.get("id", "unknown"),
                    "openproject_work_package_id": openproject_work_package_id,
                    "user_id": user_id,
                    "user_name": user_name,
                    "created_at": entry.get("created", "1970-01-01T00:00:00Z"),
                    "action": "update",
                    "auditable_type": "WorkPackage",
                    "auditable_id": openproject_work_package_id,
                    "version": 1,  # Will be calculated properly during insertion
                    "comment": self._generate_audit_comment(item),
                    "changes": changes,
                }
                audit_events.append(audit_event)

    def _map_field_changes(
        self,
        change_item: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Map Jira field changes to OpenProject audit format.

        Args:
            change_item: Individual change item from Jira changelog

        Returns:
            Mapped changes in OpenProject format or None if not mappable

        """
        field = change_item.get("field")
        if not field:
            return None

        # Field mapping from Jira to OpenProject
        field_mappings = {
            "summary": "subject",
            "description": "description",
            "status": "status_id",
            "assignee": "assigned_to_id",
            "priority": "priority_id",
            "issuetype": "type_id",
            "resolution": "resolution",
            "labels": "tags",
            "fixVersion": "version",
            "component": "category",
            "reporter": "author_id",
        }

        op_field = field_mappings.get(field, field)

        # Handle different field types
        from_value = change_item.get("from") or change_item.get("fromString")
        to_value = change_item.get("to") or change_item.get("toString")

        # Filter out no-change entries to prevent phantom journals
        # "The changes were retracted" appears when journals have no actual changes
        if self._is_no_change(from_value, to_value):
            return None

        # Special handling for certain fields
        if field in ["assignee", "reporter"]:
            # Map user names to IDs
            from_id = self._mapped_user_id(from_value)
            to_id = self._mapped_user_id(to_value)
            # Filter out no-change after ID mapping
            if from_id == to_id:
                return None
            return {op_field: [from_id, to_id]}
        if field in ["status", "priority", "issuetype"]:
            # These need ID mapping which we'll handle during insertion
            return {op_field: [from_value, to_value]}
        # Generic field change
        return {op_field: [from_value, to_value]}

    def _is_no_change(self, from_value: Any, to_value: Any) -> bool:
        """Check if a change entry represents no actual change.

        Returns True if from_value equals to_value (including null-to-null),
        which would create phantom journals appearing as "The changes were retracted."

        Args:
            from_value: The original value
            to_value: The new value

        Returns:
            True if this represents no change and should be skipped

        """

        # Normalize None and empty strings for comparison
        def normalize(val: Any) -> Any:
            if val is None:
                return None
            if isinstance(val, str):
                stripped = val.strip()
                return None if stripped == "" else stripped
            return val

        normalized_from = normalize(from_value)
        normalized_to = normalize(to_value)

        # Skip if both are None/empty (null-to-null change)
        if normalized_from is None and normalized_to is None:
            return True

        # Skip if values are identical (no actual change)
        if normalized_from == normalized_to:
            return True

        return False

    def _generate_audit_comment(self, change_item: dict[str, Any]) -> str:
        """Generate a descriptive comment for the audit event.

        Args:
            change_item: Individual change item from Jira changelog

        Returns:
            Human-readable comment describing the change

        """
        field = change_item.get("field", "unknown field")
        from_value = change_item.get("fromString") or change_item.get("from") or "empty"
        to_value = change_item.get("toString") or change_item.get("to") or "empty"

        # Special handling for project moves (Key and project field changes)
        if field == "Key":
            return self._generate_key_change_comment(from_value, to_value)
        if field == "project":
            return self._generate_project_move_comment(from_value, to_value)

        return f"Changed {field} from '{from_value}' to '{to_value}'"

    def _generate_key_change_comment(self, from_key: str, to_key: str) -> str:
        """Generate a compact comment for Jira key changes (project moves).

        Format: Moved from OLDKEY (with link to project if migrated)
        """
        if not from_key or from_key == "empty":
            return f"Jira key set to {to_key}"

        # Extract project key from Jira issue key (e.g., "NRTECH" from "NRTECH-468")
        from_project_key = from_key.rsplit("-", 1)[0] if "-" in from_key else None

        if from_project_key and from_project_key in self.project_mapping:
            project_info = self.project_mapping[from_project_key]
            op_identifier = project_info.get("openproject_identifier", "").lower()
            if op_identifier:
                # Use markdown link to the project (relative URL works in OpenProject)
                return f"Moved from [{from_key}](/projects/{op_identifier}/work_packages)"
        # Fallback: just show the old key without link
        return f"Moved from {from_key}"

    def _generate_project_move_comment(self, from_project: str, to_project: str) -> str:
        """Generate a compact comment for project field changes.

        Format: Project changed: OldProject -> NewProject (with link if migrated)
        """
        if not from_project or from_project == "empty":
            return f"Assigned to project '{to_project}'"

        # Try to find the old project in mapping (match by name)
        from_project_mapping = None
        for key, info in self.project_mapping.items():
            jira_name = info.get("jira_name", "")
            if from_project in (jira_name, key):
                from_project_mapping = info
                break

        if from_project_mapping:
            op_identifier = from_project_mapping.get("openproject_identifier", "").lower()
            if op_identifier:
                return f"Moved from project [{from_project}](/projects/{op_identifier})"

        # Fallback: just show project names
        return f"Moved from project '{from_project}'"

    def migrate_audit_trail_for_issue(
        self,
        jira_issue: Any,
        openproject_work_package_id: int,
    ) -> bool:
        """Migrate complete audit trail for a single issue.

        Args:
            jira_issue: Jira issue object with changelog expanded
            openproject_work_package_id: OpenProject work package ID

        Returns:
            True when processing succeeded, including issues with no history.

        """
        issue_key = jira_issue.key
        self.logger.debug(f"Migrating audit trail for issue {issue_key}")

        try:
            # Extract changelog
            changelog_entries = self.extract_changelog_from_issue(jira_issue)
            self.migration_results["total_changelog_entries"] += len(changelog_entries)

            if not changelog_entries:
                self.migration_results["skipped_entries"] += 1
                # Ensure cache is populated for legacy tests
                self.changelog_data[issue_key] = []
                # For legacy tests, return True on success with no entries
                return True

            # Transform to audit events (do not queue here; queuing is performed in
            # process_stored_changelog_data to avoid duplication in tests)
            _ = self.transform_changelog_to_audit_events(
                changelog_entries,
                issue_key,
                openproject_work_package_id,
            )
            self.migration_results["processed_entries"] += len(changelog_entries)

        except Exception as e:
            error_msg = f"Failed to migrate audit trail for {issue_key}: {e}"
            self.logger.exception(error_msg)
            self.migration_results["errors"].append(error_msg)
            self.migration_results["failed_migrations"] += 1
            return False

        # For legacy tests, also store into changelog_data cache
        self.changelog_data[issue_key] = changelog_entries
        # Treat success path as boolean True
        return True

    def process_stored_changelog_data(
        self,
        work_package_mapping: dict[str, Any],
    ) -> bool:
        """Process all stored changelog data and create audit events.

        Args:
            work_package_mapping: Mapping of Jira IDs to OpenProject work packages.

        Returns:
            True when changelog data is queued successfully.

        """
        if not self.changelog_data:
            self.logger.info("No stored changelog data to process")
            return True

        self.logger.info(
            f"Processing {len(self.changelog_data)} stored changelog entries",
        )

        success = True
        for jira_id, changelog_info in self.changelog_data.items():
            try:
                # Find the corresponding work package ID
                work_package_info = work_package_mapping.get(jira_id)
                if not work_package_info:
                    self.logger.warning(
                        f"No work package mapping found for Jira ID {jira_id}",
                    )
                    self.migration_results["orphaned_events"] += 1
                    continue

                # Mapping may be an int (ID) or dict with openproject_id
                if isinstance(work_package_info, int):
                    openproject_work_package_id = work_package_info
                else:
                    openproject_work_package_id = work_package_info.get("openproject_id")
                if not openproject_work_package_id:
                    self.logger.warning(
                        f"No OpenProject ID found for Jira ID {jira_id}",
                    )
                    self.migration_results["orphaned_events"] += 1
                    continue

                # Transform changelog entries to audit events
                if isinstance(changelog_info, list):
                    entries = changelog_info
                    issue_key = jira_id
                else:
                    entries = changelog_info.get("changelog_entries", [])
                    issue_key = changelog_info.get("jira_issue_key", jira_id)

                audit_events = self.transform_changelog_to_audit_events(
                    entries,
                    issue_key,
                    openproject_work_package_id,
                )

                self._queue_grouped_events(audit_events)

                self.migration_results["processed_entries"] += len(entries)

            except Exception as e:
                error_msg = f"Failed to process changelog data for Jira ID {jira_id}: {e}"
                self.logger.exception(error_msg)
                self.migration_results["errors"].append(error_msg)
                self.migration_results["failed_migrations"] += 1
                success = False

        return success

    def execute_rails_audit_operations(
        self,
        work_package_mapping: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute queued Rails operations for audit event creation.

        Args:
            work_package_mapping: Mapping of Jira IDs to OpenProject work packages

        Returns:
            Summary dict with execution status

        """
        if work_package_mapping is not None:
            queued_success = self.process_stored_changelog_data(work_package_mapping)
            if not queued_success and not self.rails_operations:
                return {
                    "status": "error",
                    "processed": 0,
                    "total": 0,
                    "errors": ["Failed to queue audit trail operations from changelog data"],
                }

        if not self.rails_operations:
            self.logger.info("No audit trail Rails operations to execute")
            return {
                "status": "skipped",
                "processed": 0,
                "total": 0,
                "errors": [],
            }

        self.logger.info(
            f"Executing {len(self.rails_operations)} audit trail Rails operations",
        )

        total = len(self.rails_operations)
        try:
            activities = [self._audit_activity(op) for op in self.rails_operations]
            response = self.op_client.bulk_create_work_package_activities(activities)
            processed = int(response.get("created", 0)) + int(response.get("skipped", 0))
            errors = list(response.get("errors") or [])
            if response.get("error"):
                errors.append(str(response["error"]))
            success = bool(response.get("success") and not errors and processed == total)
        except Exception as exc:
            processed, errors, success = 0, [str(exc)], False

        self.migration_results["rails_execution_success"] = success
        if success:
            self.rails_operations.clear()
            self.migration_results["successful_migrations"] += processed
        else:
            if not errors:
                errors.append(f"OpenProject processed {processed} of {total} audit activities")
            self.migration_results["failed_migrations"] += max(0, total - processed)
            self.migration_results["errors"].extend(errors)
        return {"status": "success" if success else "error", "processed": processed, "total": total, "errors": errors}

    @staticmethod
    def _audit_activity(operation: dict[str, Any]) -> dict[str, Any]:
        """Build a repeatable, dated history note for the OpenProject activity writer."""
        import hashlib
        import json

        event = operation.get("data", operation)
        if has_comment_audience(operation) or has_comment_audience(event):
            msg = "Refusing an audit event with comment audience metadata"
            raise ValueError(msg)
        note = event.get("comment") or event.get("notes") or ""
        changes = event.get("changes") or {}
        if changes:
            details = json.dumps(changes, ensure_ascii=False, sort_keys=True)
            note = "\n\n".join(filter(None, (note, details)))
        if not note:
            msg = "Audit event is missing its history description"
            raise ValueError(msg)
        source_id = event.get("jira_changelog_id")
        # Old exports can lack a Jira changelog ID. Hash their canonical contents
        # so retrying a partially completed batch cannot duplicate history notes.
        if not str(source_id).isdecimal():
            canonical = json.dumps(event, sort_keys=True, ensure_ascii=False)
            source_id = hashlib.sha256(canonical.encode()).hexdigest()
        return {
            "work_package_id": int(event.get("work_package_id") or event["openproject_work_package_id"]),
            "user_id": event.get("user_id"),
            "created_at": event.get("created_at"),
            "comment": note,
            "jira_comment_id": f"history-{source_id}",
        }

    def generate_audit_trail_report(self) -> str:
        """Generate comprehensive audit trail migration report.

        Returns:
            Detailed report of audit trail migration results

        """
        # Older tests expect a human-readable string report using different keys
        total_issues = self.migration_results.get("total_issues_processed")
        if total_issues is not None:
            issues_with_changelog = self.migration_results.get("issues_with_changelog", 0)
            total_events = self.migration_results.get("total_audit_events_created", 0)
            user_failures = self.migration_results.get("user_attribution_failures", 0)
            rails_ok = self.migration_results.get("rails_execution_success", False)
            processing_errors = self.migration_results.get("processing_errors", [])
            lines = [
                "Audit Trail Migration Report",
                f"Total Issues Processed: {total_issues}",
                f"Issues with Changelog: {issues_with_changelog}",
                f"Total Audit Events Created: {total_events}",
                f"User Attribution Failures: {user_failures}",
                f"Rails Execution Success: {rails_ok}",
                f"Processing Errors: {len(processing_errors)}",
            ]
            return "\n".join(lines)

        # Fallback to current metrics if legacy keys are absent
        total_entries = self.migration_results.get("total_changelog_entries", 0)
        successful = self.migration_results.get("successful_migrations", 0)
        failed = self.migration_results.get("failed_migrations", 0)
        success_rate = (successful / total_entries * 100) if total_entries > 0 else 0
        lines = [
            "Audit Trail Migration Report",
            f"Total Changelog Entries: {total_entries}",
            f"Processed Entries: {self.migration_results.get('processed_entries', 0)}",
            f"Successful Migrations: {successful}",
            f"Failed Migrations: {failed}",
            f"Success Rate: {round(success_rate, 2)}%",
        ]
        return "\n".join(lines)

    def _generate_recommendations(self) -> list[str]:
        """Generate recommendations based on migration results."""
        recommendations = []

        if self.migration_results["user_attribution_failures"] > 0:
            recommendations.append(
                "Consider running user migration again to improve user attribution in audit trails",
            )

        if self.migration_results["orphaned_events"] > 0:
            recommendations.append(
                "Review orphaned audit events and manually assign to appropriate work packages",
            )

        if len(self.migration_results["errors"]) > 0:
            recommendations.append(
                "Review error logs and retry failed audit trail migrations",
            )

        success_rate = (
            self.migration_results["successful_migrations"]
            / max(self.migration_results["total_changelog_entries"], 1)
            * 100
        )

        if success_rate < 90:
            recommendations.append(
                "Audit trail migration success rate is below 90% - consider investigating common failure patterns",
            )

        return recommendations

    def save_migration_results(self) -> bool:
        """Save audit trail migration results to file."""
        from src.utils import data_handler

        try:
            data_handler.save(
                data=self.migration_results,
                filename="audit_trail_migration_results.json",
                directory=Path(self.data_dir) / "migration_data",
            )
        except Exception:
            self.logger.exception("Failed to save audit trail migration results")
            return False
        self.logger.info("Saved audit trail migration results JSON")
        return True
