"""Time entry transformation utilities for Jira to OpenProject migration."""

import logging
import re
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


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
                or author.get("emailAddress")
                or "unknown"
            )

            # Parse time spent (comes as seconds in Jira)
            time_spent_seconds = work_log.get("timeSpentSeconds", 0)
            hours = time_spent_seconds / 3600.0

            # Parse date (Jira format: "2023-12-01T10:30:00.000+0000")
            started_date = work_log.get("started", "")
            spent_on = self._parse_jira_date(started_date)

            # Extract comment/description
            comment = work_log.get("comment", "")
            if isinstance(comment, dict):
                # Rich text format - extract plain text
                comment = self._extract_text_from_jira_content(comment)

            # Build OpenProject time entry
            time_entry = {
                "spentOn": spent_on,
                "hours": round(hours, 2),
                "comment": comment or f"Work log imported from Jira issue {issue_key}",
                "_embedded": {},
                "_meta": {
                    "jira_work_log_id": work_log.get("id"),
                    "jira_issue_key": issue_key,
                    "jira_author": author_username,
                    "import_timestamp": datetime.now().isoformat(),
                },
            }

            # Map user
            user_id = self._map_user_multi(author)
            if user_id:
                time_entry["_embedded"]["user"] = {"href": f"/api/v3/users/{user_id}"}

            # Map work package
            work_package_id = self._map_work_package_multi(issue_key, work_log)
            if work_package_id:
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

            return time_entry

        except Exception as e:
            logger.exception(
                "Failed to transform Jira work log %s: %s",
                work_log.get("id", "unknown"),
                e,
            )
            raise

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
                or tempo_log.get("author", {}).get("key")
                or tempo_log.get("author", {}).get("accountId")
                or tempo_log.get("author", {}).get("emailAddress")
                or "unknown"
            )
            issue_key = tempo_log.get("issue", {}).get("key", "")

            # Parse time spent (Tempo provides in seconds)
            time_spent_seconds = tempo_log.get("timeSpentSeconds", 0)
            hours = time_spent_seconds / 3600.0

            # Parse date (Tempo format: "2023-12-01")
            date_started = tempo_log.get("dateStarted", "")
            spent_on = date_started  # Already in YYYY-MM-DD format

            # Extract description/comment
            description = tempo_log.get("description", "")

            # Build OpenProject time entry
            time_entry = {
                "spentOn": spent_on,
                "hours": round(hours, 2),
                "comment": description
                or f"Tempo work log imported from issue {issue_key}",
                "_embedded": {},
                "_meta": {
                    "tempo_worklog_id": tempo_log.get("tempoWorklogId"),
                    "jira_worklog_id": tempo_log.get("jiraWorklogId"),
                    "jira_issue_key": issue_key,
                    "tempo_author": author_username,
                    "import_timestamp": datetime.now().isoformat(),
                },
            }

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

            return time_entry

        except Exception as e:
            logger.exception(
                "Failed to transform Tempo work log %s: %s",
                tempo_log.get("tempoWorklogId", "unknown"),
                e,
            )
            raise

    def batch_transform_work_logs(
        self,
        work_logs: list[dict[str, Any]],
        source_type: str = "jira",
    ) -> list[dict[str, Any]]:
        """Transform multiple work logs in batch.

        Args:
            work_logs: List of work log dictionaries
            source_type: Either "jira" or "tempo"

        Returns:
            List of transformed OpenProject time entries

        """
        transformed_entries = []
        failed_count = 0

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

                transformed_entries.append(entry)

            except Exception as e:
                logger.exception("Failed to transform work log: %s", e)
                failed_count += 1
                continue

        logger.info(
            "Transformed %d work logs, %d failed",
            len(transformed_entries),
            failed_count,
        )
        return transformed_entries

    def _parse_jira_date(self, date_string: str) -> str:
        """Parse Jira date format to OpenProject date format (YYYY-MM-DD).

        Args:
            date_string: Jira date string like "2023-12-01T10:30:00.000+0000"

        Returns:
            Date string in YYYY-MM-DD format

        """
        try:
            # Handle various Jira date formats
            if not date_string:
                return datetime.now().strftime("%Y-%m-%d")

            # Remove timezone info and parse
            date_clean = re.sub(r"[+\-]\d{4}$", "", date_string)
            date_clean = re.sub(r"\.\d{3}$", "", date_clean)

            # Parse the datetime
            dt = (
                datetime.fromisoformat(date_clean)
                if "T" in date_clean
                else datetime.strptime(date_clean, "%Y-%m-%d")
            )

            return dt.strftime("%Y-%m-%d")

        except Exception as e:
            logger.warning("Failed to parse date '%s': %s", date_string, e)
            return datetime.now().strftime("%Y-%m-%d")

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

        def extract_from_node(node) -> None:
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
        """Try multiple identifiers to map a Jira user to OpenProject ID."""
        if isinstance(author, str):
            return self.user_mapping.get(author)
        candidates = [
            author.get("name"),
            author.get("key"),
            author.get("accountId"),
            author.get("emailAddress"),
            author.get("displayName"),
        ]
        for cand in candidates:
            if cand and cand in self.user_mapping:
                return self.user_mapping[cand]
        logger.warning("No user mapping found for any identifier: %s", candidates)
        return None

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
        # Fallbacks: look in meta or variants
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
        work_attributes = tempo_log.get("workAttributes", [])
        for attr in work_attributes:
            attr_key = attr.get("key", "").lower()
            attr_value = attr.get("value", "").lower()

            # Look for activity-related attributes
            if "activity" in attr_key or "type" in attr_key:
                activity_id = self.activity_mapping.get(attr_value)
                if activity_id:
                    return activity_id

        # Fall back to comment-based detection
        description = tempo_log.get("description", "")
        return self._detect_activity(description)

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
        time_entry["_meta"]["tempo_billable"] = tempo_log.get("billableSeconds", 0) > 0
        time_entry["_meta"]["tempo_location"] = tempo_log.get("location")

        # Handle billing information
        if "billableSeconds" in tempo_log:
            billable_hours = tempo_log["billableSeconds"] / 3600.0
            time_entry["_meta"]["tempo_billable_hours"] = round(billable_hours, 2)

        # Handle work attributes
        work_attributes = tempo_log.get("workAttributes", [])
        if work_attributes:
            attributes = {}
            for attr in work_attributes:
                key = attr.get("key", "")
                value = attr.get("value", "")
                if key and value:
                    attributes[key] = value
            if attributes:
                time_entry["_meta"]["tempo_attributes"] = attributes

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
        jira_count = sum(
            1
            for entry in transformed_entries
            if entry.get("_meta", {}).get("jira_work_log_id")
        )
        tempo_count = sum(
            1
            for entry in transformed_entries
            if entry.get("_meta", {}).get("tempo_worklog_id")
        )

        # Count mapped vs unmapped users
        mapped_users = sum(
            1
            for entry in transformed_entries
            if "_embedded" in entry and "user" in entry["_embedded"]
        )

        # Count mapped vs unmapped work packages
        mapped_work_packages = sum(
            1
            for entry in transformed_entries
            if "_embedded" in entry and "workPackage" in entry["_embedded"]
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
                "users": (
                    round((mapped_users / total_entries * 100), 2)
                    if total_entries > 0
                    else 0
                ),
                "work_packages": (
                    round((mapped_work_packages / total_entries * 100), 2)
                    if total_entries > 0
                    else 0
                ),
            },
        }
