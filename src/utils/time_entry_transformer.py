# Copyright (c) 2024-2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT

"""Time entry transformation utilities for Jira to OpenProject migration."""

import logging
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.infrastructure.jira.comment_audience import has_comment_audience

logger = logging.getLogger(__name__)


def _worklog_hours(log: dict[str, Any]) -> float:
    seconds = log.get("time_spent_seconds", log.get("timeSpentSeconds"))
    if type(seconds) is not int or seconds <= 0:
        msg = "The work log does not have a positive integer duration in seconds."
        raise ValueError(msg)
    return seconds / 3600


def _worklog_comment(value: str) -> str:
    if len(value) > 1000:
        msg = "The work log comment exceeds the target limit of 1000 characters."
        raise ValueError(msg)
    return value


def _worklog_start(value: str, zone: str | None) -> tuple[str, dict[str, Any]]:
    """Keep the original instant when OpenProject's wall clock cannot encode it."""
    stamp = datetime.fromisoformat(value)
    local = stamp.astimezone(ZoneInfo(zone)) if zone and stamp.tzinfo else stamp
    details: dict[str, Any] = {"jira_started": value}
    if zone and stamp.tzinfo:
        details["jira_time_zone"] = zone
        if not (local.fold or local.second or local.microsecond):
            details["jira_start_minute"] = local.hour * 60 + local.minute
    return local.date().isoformat(), details


class TimeEntryTransformer:
    """Transform Jira and Tempo work logs to OpenProject time entries."""

    def __init__(
        self,
        user_mapping: dict[str, int] | None = None,
        work_package_mapping: dict[str, int] | None = None,
        activity_mapping: dict[str, int] | None = None,
        default_activity_id: int | None = None,
    ) -> None:
        """Initialize the transformer with necessary mappings.

        Args:
            user_mapping: Map Jira username to OpenProject user ID
            work_package_mapping: Map Jira issue key to OpenProject work package ID
            activity_mapping: Map activity names to OpenProject activity IDs
            default_activity_id: Default activity ID if mapping not found

        """
        self.user_mapping = user_mapping or {}
        self.work_package_mapping = work_package_mapping or {}
        self.activity_mapping = activity_mapping or {}
        self.default_activity_id = default_activity_id
        # Count of entries dropped in the last batch_transform_work_logs call
        # because neither user nor work package could be resolved.  Callers
        # (TimeEntryMigrator) read this after the call to surface the count.
        self.last_unmappable_count: int = 0

        # Common activity name mappings
        self.default_activity_mappings = {
            "development": "Development",
            "coding": "Development",
            "programming": "Development",
            "bug fixing": "Bug fixing",
            "bugfix": "Bug fixing",
            "testing": "Testing",
            "qa": "Testing",
            "review": "Code review",
            "code review": "Code review",
            "meeting": "Meeting",
            "planning": "Planning",
            "documentation": "Documentation",
            "docs": "Documentation",
            "research": "Research",
            "analysis": "Analysis",
            "design": "Design",
            "deployment": "Deployment",
            "support": "Support",
            "maintenance": "Maintenance",
        }

    def transform_jira_work_log(
        self,
        work_log: dict[str, Any],
        issue_key: str,
        custom_field_mapping: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Transform a single Jira work log to OpenProject time entry format.

        Args:
            work_log: Jira work log data
            issue_key: The Jira issue key this work log belongs to
            custom_field_mapping: Map Jira custom field names to OpenProject fields

        Returns:
            OpenProject time entry data structure

        """
        try:
            # Extract basic information
            author = work_log.get("author", {})
            author_username = (
                author.get("name")
                or author.get("key")
                or author.get("accountId")
                or author.get("account_id")
                or author.get("emailAddress")
                or "unknown"
            )

            if has_comment_audience(work_log):
                msg = "Work log audience cannot be represented on a native time entry."
                raise ValueError(msg)
            final_hours = _worklog_hours(work_log)
            zone = author.get("timeZone") or author.get("time_zone")
            spent_on, start = _worklog_start(work_log.get("started", ""), zone)

            # Extract comment/description
            comment = work_log.get("comment", "")
            if isinstance(comment, dict):
                # Rich text format - extract plain text
                comment = self._extract_text_from_jira_content(comment)

            # Build OpenProject time entry
            time_entry = {
                "spentOn": spent_on,
                "hours": final_hours,
                "comment": _worklog_comment(comment or ""),
                "_embedded": {},
                "_meta": {
                    "jira_work_log_id": work_log.get("id"),
                    "jira_issue_key": issue_key,
                    "jira_author": author_username,
                    "import_timestamp": datetime.now(tz=UTC).isoformat(),
                    "jira_worklog_key": f"{issue_key}:{work_log.get('id')}" if work_log.get("id") is not None else None,
                    # Carry source timestamps for fast-forward/upsert logic
                    **start,
                    **self._worklog_editor(work_log),
                    "jira_created_at": work_log.get("created"),
                    "jira_updated_at": work_log.get("updated"),
                },
            }

            # Map user
            user_id = self._map_user_multi(author)
            if user_id is not None:
                time_entry["_embedded"]["user"] = {"href": f"/api/v3/users/{user_id}"}

            # Map work package
            work_package_id = self._map_work_package_multi(issue_key, work_log)
            if work_package_id is not None:
                time_entry["_embedded"]["workPackage"] = {
                    "href": f"/api/v3/work_packages/{work_package_id}",
                }

            # Map activity (try to detect from comment or use default)
            activity_id = self._detect_activity(comment)
            if activity_id:
                time_entry["_embedded"]["activity"] = {
                    "href": f"/api/v3/time_entries/activities/{activity_id}",
                }

            # Handle custom fields if mapping provided
            if custom_field_mapping:
                self._map_custom_fields(work_log, time_entry, custom_field_mapping)

            result_entry = time_entry

        except Exception:
            logger.exception(
                "Failed to transform Jira work log %s",
                work_log.get("id", "unknown"),
            )
            raise
        else:
            return result_entry

    def _worklog_editor(self, work_log: dict[str, Any]) -> dict[str, int]:
        editor = work_log.get("update_author", work_log.get("updateAuthor"))
        if editor is None:
            return {}
        identifier = self._map_user_multi(editor)
        if identifier is None:
            msg = "The work log update author has no unambiguous target identity."
            raise ValueError(msg)
        return {"jira_update_author_id": identifier}

    @staticmethod
    def _tempo_worklog_key(tempo_log: dict[str, Any], issue_key: str | None) -> str | None:
        if (jira_id := tempo_log.get("jiraWorklogId", tempo_log.get("jira_worklog_id"))) is not None:
            return f"{issue_key}:{jira_id}"
        if (tempo_id := tempo_log.get("tempoWorklogId", tempo_log.get("tempo_worklog_id"))) is not None:
            return f"tempo:{tempo_id}"
        return None

    def _link_tempo_principals(self, tempo_log: dict[str, Any], issue_key: str, time_entry: dict[str, Any]) -> None:
        # Map user
        user_id = self._map_user_multi(tempo_log.get("author", {}))
        if user_id:
            time_entry["_embedded"]["user"] = {"href": f"/api/v3/users/{user_id}"}

        # Map work package
        work_package_id = self._map_work_package_multi(issue_key, tempo_log)
        if work_package_id:
            time_entry["_embedded"]["workPackage"] = {
                "href": f"/api/v3/work_packages/{work_package_id}",
            }

    @staticmethod
    def _tempo_worklog_start(log: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if log.get("jira_started"):
            return _worklog_start(log["jira_started"], log.get("jira_author_time_zone"))
        return datetime.fromisoformat(log.get("dateStarted", log.get("date_started", ""))).date().isoformat(), {}

    def transform_tempo_work_log(
        self,
        tempo_log: dict[str, Any],
        custom_field_mapping: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Transform a Tempo work log to OpenProject time entry format.

        Args:
            tempo_log: Tempo work log data with enhanced metadata
            custom_field_mapping: Map Tempo custom field names to OpenProject fields

        Returns:
            OpenProject time entry data structure

        """
        try:
            # Extract basic information
            author_username = (
                tempo_log.get("author", {}).get("name")
                or tempo_log.get("author", {}).get("username")
                or tempo_log.get("author", {}).get("key")
                or tempo_log.get("author", {}).get("accountId")
                or tempo_log.get("author", {}).get("emailAddress")
                or "unknown"
            )
            issue_key = tempo_log.get("issue_key") or tempo_log.get("issue", {}).get("key", "")

            if has_comment_audience(tempo_log):
                msg = "Work log audience cannot be represented on a native time entry."
                raise ValueError(msg)
            final_hours = _worklog_hours(tempo_log)
            spent_on, start = self._tempo_worklog_start(tempo_log)
            description = _worklog_comment(tempo_log.get("description", tempo_log.get("comment")) or "")

            # Build OpenProject time entry
            time_entry = {
                "spentOn": spent_on,
                "hours": final_hours,
                "comment": description,
                "_embedded": {},
                "_meta": {
                    "tempo_worklog_id": tempo_log.get("tempoWorklogId", tempo_log.get("tempo_worklog_id")),
                    "jira_worklog_id": tempo_log.get("jiraWorklogId", tempo_log.get("jira_worklog_id")),
                    "jira_issue_key": issue_key,
                    "tempo_author": author_username,
                    **start,
                    "import_timestamp": datetime.now(tz=UTC).isoformat(),
                    "jira_worklog_key": self._tempo_worklog_key(tempo_log, issue_key),
                },
            }

            self._link_tempo_principals(tempo_log, issue_key, time_entry)
            # Handle Tempo-specific fields
            self._handle_tempo_specific_fields(tempo_log, time_entry)

            # Map activity (enhanced with Tempo attributes)
            activity_id = self._detect_activity_from_tempo(tempo_log)
            if activity_id:
                time_entry["_embedded"]["activity"] = {
                    "href": f"/api/v3/time_entries/activities/{activity_id}",
                }

            # Handle custom fields if mapping provided
            if custom_field_mapping:
                self._map_custom_fields(tempo_log, time_entry, custom_field_mapping)

            result_entry = time_entry

        except Exception:
            logger.exception(
                "Failed to transform Tempo work log %s",
                tempo_log.get("tempoWorklogId", "unknown"),
            )
            raise
        else:
            return result_entry

    def batch_transform_work_logs(
        self,
        work_logs: list[dict[str, Any]],
        source_type: str = "jira",
    ) -> list[dict[str, Any]]:
        """Transform multiple work logs in batch.

        Entries where BOTH the user and the work package could not be resolved
        from the mapping tables are classified as *unmappable* and dropped
        before returning.  They are never passed to the migrator, so they do
        not cause batch-create failures.  The count of dropped entries is
        stored in ``self.last_unmappable_count`` for the caller to read.

        Args:
            work_logs: List of work log dictionaries
            source_type: Either "jira" or "tempo"

        Returns:
            List of transformed OpenProject time entries (unmappable entries
            excluded).

        """
        transformed_entries = []
        failed_count = 0
        unmappable_count = 0

        for work_log in work_logs:
            try:
                if source_type == "tempo":
                    entry = self.transform_tempo_work_log(work_log)
                else:
                    # For Jira work logs, need issue key
                    issue_key = work_log.get("issue_key", "")
                    if not issue_key:
                        logger.warning(
                            "Skipping work log without issue key: %s",
                            work_log.get("id"),
                        )
                        failed_count += 1
                        continue
                    entry = self.transform_jira_work_log(work_log, issue_key)

                # Drop entries that are missing ANY required ID embedding.
                # batch_create_time_entries treats a missing workPackage OR a
                # missing user as a per-entry failure, so an entry with only one
                # of the two resolved would still fail inside the migrator and
                # trigger the zero-created gate on otherwise clean re-runs.
                # Use OR so that a partial resolve also results in a drop here.
                embedded = entry.get("_embedded", {})
                if not embedded.get("user") or not embedded.get("workPackage"):
                    logger.warning(
                        "Dropping unmappable %s entry (missing user or work package): worklog_id=%s issue_key=%s",
                        source_type,
                        entry.get("_meta", {}).get(
                            "tempo_worklog_id" if source_type == "tempo" else "jira_work_log_id"
                        ),
                        entry.get("_meta", {}).get("jira_issue_key"),
                    )
                    unmappable_count += 1
                    continue

                transformed_entries.append(entry)

            except Exception:
                logger.exception("Failed to transform work log")
                failed_count += 1
                continue

        self.last_unmappable_count = unmappable_count
        logger.info(
            "Transformed %d work logs, %d failed, %d unmappable",
            len(transformed_entries),
            failed_count,
            unmappable_count,
        )
        return transformed_entries

    @staticmethod
    def _parse_jira_date(date_string: str, time_zone: str | None = None) -> str:
        """Use the source actor's local date when the API supplied a zoned timestamp."""
        day, _ = _worklog_start(date_string, time_zone)
        return day

    def _extract_text_from_jira_content(self, content: dict[str, Any]) -> str:
        """Extract plain text from Jira rich text content.

        Args:
            content: Jira content structure with type and content

        Returns:
            Plain text string

        """
        try:
            if isinstance(content, str):
                return content

            if isinstance(content, dict):
                # Handle Atlassian Document Format (ADF)
                if content.get("type") == "doc":
                    return self._extract_text_from_adf(content)

                # Handle simple text content
                if "text" in content:
                    return content["text"]

            return str(content)

        except Exception as e:
            logger.warning("Failed to extract text from content: %s", e)
            return str(content)

    def _extract_text_from_adf(self, doc: dict[str, Any]) -> str:
        """Extract text from Atlassian Document Format.

        Args:
            doc: ADF document structure

        Returns:
            Plain text string

        """
        text_parts = []

        def extract_from_node(node: object) -> None:
            if isinstance(node, dict):
                if node.get("type") == "text":
                    text_parts.append(node.get("text", ""))
                elif "content" in node:
                    for child in node["content"]:
                        extract_from_node(child)

        extract_from_node(doc)
        # Join and clean up extra spaces
        return " ".join(text_parts).strip().replace("  ", " ")

    def _map_user(self, username: str) -> int | None:
        """Map Jira username to OpenProject user ID.

        Args:
            username: Jira username

        Returns:
            OpenProject user ID or None if not found

        """
        user_id = self.user_mapping.get(username)
        if not user_id:
            logger.warning("No user mapping found for username: %s", username)
        return user_id

    def _map_user_multi(self, author: dict[str, Any] | str) -> int | None:
        """Resolve stable source identities and decline conflicting aliases."""
        if isinstance(author, str):
            return self.user_mapping.get(author)
        stable = {author[key] for key in ("key", "accountId", "account_id") if author.get(key)}
        if stable and not stable.intersection(self.user_mapping):
            return None
        aliases = stable.union((author.get("name"), author.get("username")))
        matches = {self.user_mapping[key] for key in aliases if key in self.user_mapping}
        return next(iter(matches)) if len(matches) == 1 else None

    def _map_work_package(self, issue_key: str) -> int | None:
        """Map Jira issue key to OpenProject work package ID.

        Args:
            issue_key: Jira issue key like "TEST-123"

        Returns:
            OpenProject work package ID or None if not found

        """
        work_package_id = self.work_package_mapping.get(issue_key)
        if not work_package_id:
            logger.warning("No work package mapping found for issue: %s", issue_key)
        return work_package_id

    def _map_work_package_multi(self, issue_key: str, log: dict[str, Any]) -> int | None:
        """Map using issue_key or any alternative identifiers if available."""
        # Primary: issue key
        wp_id = self.work_package_mapping.get(issue_key)
        if wp_id:
            return wp_id
        # Look in meta or variants
        meta = log.get("_meta", {}) if isinstance(log, dict) else {}
        candidates = [
            issue_key,
            meta.get("jira_issue_key"),
            meta.get("issueKey"),
        ]
        for key in candidates:
            if key and key in self.work_package_mapping:
                return self.work_package_mapping[key]
        logger.warning("No work package mapping found for identifiers: %s", candidates)
        return None

    def _detect_activity(self, comment: str) -> int | None:
        """Detect activity type from work log comment.

        Args:
            comment: Work log comment/description

        Returns:
            OpenProject activity ID or None

        """
        if not comment:
            return self.default_activity_id

        comment_lower = comment.lower()

        # Check for activity keywords in comment
        for keyword, activity_name in self.default_activity_mappings.items():
            if keyword in comment_lower:
                activity_id = self.activity_mapping.get(activity_name)
                if activity_id:
                    return activity_id

        return self.default_activity_id

    def _detect_activity_from_tempo(self, tempo_log: dict[str, Any]) -> int | None:
        """Detect activity from Tempo work log with enhanced metadata.

        Args:
            tempo_log: Tempo work log with attributes

        Returns:
            OpenProject activity ID or None

        """
        # Check Tempo work attributes first
        work_attributes = tempo_log.get("work_attributes", tempo_log.get("workAttributes", []))
        for attr in work_attributes:
            attr_key = attr.get("key", "").lower()
            attr_value = str(attr.get("value", "")).lower()

            # Look for activity-related attributes
            if "activity" in attr_key or "type" in attr_key:
                activity_id = self.activity_mapping.get(attr_value)
                if activity_id:
                    return activity_id

        # Fall back to comment-based detection
        description = tempo_log.get("description", tempo_log.get("comment")) or ""
        return self._detect_activity(description)

    @classmethod
    def tempo_billing(cls, tempo_log: dict[str, Any]) -> dict[str, Any]:
        """Billable hours and approval status of a Tempo work log for the time entry writer (#490).

        Four decimals keep a single billable minute.
        """
        billing: dict[str, Any] = {}
        billable = tempo_log.get("billable_seconds", tempo_log.get("billableSeconds"))
        if billable is not None:
            billing["tempo_billable_hours"] = round(billable / 3600.0, 4)
        if approval := cls._tempo_approval_status(tempo_log):
            billing["tempo_approval_status"] = approval
        return billing

    @staticmethod
    def _tempo_approval_status(tempo_log: dict[str, Any]) -> str | None:
        """Tempo's approval status as a string, or None when the work log has none (#490)."""
        approval = tempo_log.get("approval_status", tempo_log.get("approvalStatus"))
        if isinstance(approval, dict):
            approval = approval.get("status") or approval.get("name")
        return approval.strip() or None if isinstance(approval, str) else None

    def _handle_tempo_specific_fields(
        self,
        tempo_log: dict[str, Any],
        time_entry: dict[str, Any],
    ) -> None:
        """Handle Tempo-specific fields and attributes.

        Args:
            tempo_log: Tempo work log data
            time_entry: OpenProject time entry being built

        """
        # Add Tempo metadata
        billable = tempo_log.get("billable_seconds", tempo_log.get("billableSeconds"))
        time_entry["_meta"]["tempo_billable"] = billable is not None and billable > 0
        time_entry["_meta"]["tempo_location"] = tempo_log.get("location")

        time_entry["_meta"].update(self.tempo_billing(tempo_log))

        if attributes := self._tempo_work_attributes(tempo_log):
            time_entry["_meta"]["tempo_attributes"] = attributes

    @staticmethod
    def _tempo_work_attributes(tempo_log: dict[str, Any]) -> dict[str, Any]:
        """The work attribute values of a Tempo work log by key; a repeated key is refused."""
        attributes: dict[str, Any] = {}
        for attr in tempo_log.get("work_attributes", tempo_log.get("workAttributes", [])) or []:
            key = attr.get("key", "")
            value = attr.get("value", "")
            if key and key in attributes:
                msg = "Tempo contains duplicate work attribute keys."
                raise ValueError(msg)
            if key and value is not None:
                attributes[key] = value
        return attributes

    def _map_custom_fields(
        self,
        source_log: dict[str, Any],
        time_entry: dict[str, Any],
        field_mapping: dict[str, str],
    ) -> None:
        """Map custom fields from source to OpenProject time entry.

        Args:
            source_log: Source work log data
            time_entry: Target OpenProject time entry
            field_mapping: Map source field names to OpenProject fields

        """
        custom_fields = {}

        for source_field, target_field in field_mapping.items():
            if source_field in source_log:
                value = source_log[source_field]
                custom_fields[target_field] = value

        if custom_fields:
            if "_meta" not in time_entry:
                time_entry["_meta"] = {}
            time_entry["_meta"]["custom_fields"] = custom_fields

    def get_transformation_stats(
        self,
        transformed_entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Get statistics about the transformation results.

        Args:
            transformed_entries: List of transformed time entries

        Returns:
            Statistics dictionary

        """
        total_entries = len(transformed_entries)
        total_hours = sum(entry.get("hours", 0) for entry in transformed_entries)

        # Count entries by source
        jira_count = sum(1 for entry in transformed_entries if entry.get("_meta", {}).get("jira_work_log_id"))
        tempo_count = sum(1 for entry in transformed_entries if entry.get("_meta", {}).get("tempo_worklog_id"))

        # Count mapped vs unmapped users
        mapped_users = sum(1 for entry in transformed_entries if "_embedded" in entry and "user" in entry["_embedded"])

        # Count mapped vs unmapped work packages
        mapped_work_packages = sum(
            1 for entry in transformed_entries if "_embedded" in entry and "workPackage" in entry["_embedded"]
        )

        return {
            "total_entries": total_entries,
            "total_hours": round(total_hours, 2),
            "jira_entries": jira_count,
            "tempo_entries": tempo_count,
            "mapped_users": mapped_users,
            "unmapped_users": total_entries - mapped_users,
            "mapped_work_packages": mapped_work_packages,
            "unmapped_work_packages": total_entries - mapped_work_packages,
            "mapping_success_rate": {
                "users": (round((mapped_users / total_entries * 100), 2) if total_entries > 0 else 0),
                "work_packages": (round((mapped_work_packages / total_entries * 100), 2) if total_entries > 0 else 0),
            },
        }
