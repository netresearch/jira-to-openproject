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
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult
from src.utils.markdown_converter import MarkdownConverter

if TYPE_CHECKING:
    from collections.abc import Iterator

    from jira.resources import Issue


@register_entity_types("work_packages_content")
class WorkPackageContentMigration(BaseMigration):
    """Phase 2: Populate work package content with link resolution.

    This migration requires the complete work_package_mapping.json from Phase 1
    to resolve all Jira issue references to OpenProject work package links.
    """

    WORK_PACKAGE_MAPPING_FILE = "work_package_mapping.json"

    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
    ) -> None:
        """Initialize the content migration."""
        super().__init__(jira_client, op_client)

        # File paths
        self.work_package_mapping_file = self.data_dir / self.WORK_PACKAGE_MAPPING_FILE

        # Data storage
        self.work_package_mapping: dict[str, dict[str, Any]] = {}
        self.jira_key_to_wp_id: dict[str, int] = {}  # For quick link resolution

        # Load mappings
        self._load_mappings()

        # Validate mapping exists
        if not self._load_work_package_mapping():
            self.logger.error(
                "Work package mapping not found. Run work_packages_skeleton first!",
            )

        # Initialize markdown converter with complete mapping
        self._init_markdown_converter()

        self.logger.debug(
            "WorkPackageContentMigration initialized with %d mappings",
            len(self.work_package_mapping),
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
            with self.work_package_mapping_file.open("r") as f:
                self.work_package_mapping = json.load(f)

            # Build quick lookup by Jira key
            self.jira_key_to_wp_id = {
                entry.get("jira_key"): entry.get("openproject_id")
                for entry in self.work_package_mapping.values()
                if entry.get("jira_key") and entry.get("openproject_id")
            }

            self.logger.info(
                "Loaded work package mapping: %d entries, %d key lookups",
                len(self.work_package_mapping),
                len(self.jira_key_to_wp_id),
            )
            return True

        except Exception as e:
            self.logger.error("Failed to load mapping: %s", e)
            return False

    def _init_markdown_converter(self) -> None:
        """Initialize markdown converter with user and WP mappings."""
        # Build user mapping for @mentions
        user_mapping = {}
        account_id_mapping = {}
        for username, user_dict in self.user_mapping.items():
            if not user_dict:
                continue
            op_login = user_dict.get("openproject_login")
            op_id = user_dict.get("openproject_id")
            op_user = op_login if op_login else (str(op_id) if op_id else None)
            if op_user:
                user_mapping[username] = op_user
                jira_account_id = user_dict.get("jira_account_id") or user_dict.get("jira_id")
                if jira_account_id:
                    account_id_mapping[jira_account_id] = op_user

        self.markdown_converter = MarkdownConverter(
            user_mapping=user_mapping,
            work_package_mapping=self.jira_key_to_wp_id,
            account_id_mapping=account_id_mapping,
        )

    def _get_projects_to_migrate(self) -> list[dict[str, Any]]:
        """Get list of Jira projects to migrate based on filter."""
        projects = self.jira_client.get_projects()

        # CLI sets filter to jira_config["projects"] as a list
        project_filter = config.jira_config.get("projects")
        if project_filter:
            # Handle both string and list formats
            if isinstance(project_filter, str):
                filter_keys = {k.strip().upper() for k in project_filter.split(",")}
            else:
                filter_keys = {str(k).strip().upper() for k in project_filter}
            projects = [p for p in projects if p.get("key", "").upper() in filter_keys]
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
        start_at = 0
        batch_size = config.migration_config.get("batch_size", 100)

        max_issues = None
        if os.getenv("J2O_MAX_ISSUES"):
            try:
                max_issues = int(os.getenv("J2O_MAX_ISSUES"))
            except ValueError:
                pass

        jql = f'project = "{project_key}" ORDER BY created ASC'
        total_yielded = 0

        while True:
            if max_issues is not None and total_yielded >= max_issues:
                break

            issues = self._fetch_issues_batch(jql, start_at, batch_size)
            if not issues:
                break

            for issue in issues:
                if max_issues is not None and total_yielded >= max_issues:
                    break
                yield issue
                total_yielded += 1

            if len(issues) < batch_size:
                break
            start_at += batch_size

    def _fetch_issues_batch(
        self,
        jql: str,
        start_at: int,
        batch_size: int,
    ) -> list[Issue]:
        """Fetch issues with full fields for content migration."""
        try:
            # Expanded fields for content
            return self.jira_client.jira.search_issues(
                jql,
                startAt=start_at,
                maxResults=batch_size,
                expand="renderedFields,changelog",
            )
        except Exception as e:
            self.logger.error("Failed to fetch issues: %s", e)
            return []

    def _convert_jira_links(self, text: str | None) -> str:
        """Convert Jira issue references to OpenProject WP links.

        Patterns converted:
        - PROJ-123 → WP#456 or [WP#456](../work_packages/456)
        - [PROJ-123] → [WP#456]
        - {PROJ-123} → WP#456

        Args:
            text: Text containing Jira references

        Returns:
            Text with converted references

        """
        if not text:
            return ""

        # Use markdown converter for comprehensive conversion
        converted = self.markdown_converter.convert(text)

        # Additional pattern: bare Jira keys not caught by converter
        # Pattern: PROJECT-123 where PROJECT is uppercase letters
        jira_key_pattern = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")

        def replace_key(match: re.Match) -> str:
            jira_key = match.group(1)
            if jira_key in self.jira_key_to_wp_id:
                wp_id = self.jira_key_to_wp_id[jira_key]
                return f"WP#{wp_id}"
            return jira_key  # Keep original if not found

        converted = jira_key_pattern.sub(replace_key, converted)
        return converted

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

        # Convert description with link resolution
        converted_description = self._convert_jira_links(description)

        try:
            self.op_client.update_work_package(
                wp_id,
                {"description": {"raw": converted_description}},
            )
            return True
        except Exception as e:
            self.logger.debug("Failed to update description for WP#%d: %s", wp_id, e)
            return False

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
        raw_fields = getattr(jira_issue, "raw", {}).get("fields", {})
        updates = {}

        for jira_field_id, value in raw_fields.items():
            if not jira_field_id.startswith("customfield_"):
                continue
            if value is None:
                continue

            # Check if we have a mapping for this field
            if jira_field_id in self.custom_field_mapping:
                mapping = self.custom_field_mapping[jira_field_id]
                op_cf_id = mapping.get("openproject_id") if isinstance(mapping, dict) else mapping
                if op_cf_id:
                    # Convert value if it's text (may contain links)
                    if isinstance(value, str):
                        value = self._convert_jira_links(value)
                    updates[f"customField{op_cf_id}"] = value

        if updates:
            try:
                self.op_client.update_work_package(wp_id, updates)
                return True
            except Exception as e:
                self.logger.debug("Failed to update custom fields for WP#%d: %s", wp_id, e)
                return False

        return True

    def _populate_comments(
        self,
        jira_issue: Issue,
        wp_id: int,
    ) -> int:
        """Populate work package comments/journals with link resolution.

        Args:
            jira_issue: The Jira issue
            wp_id: OpenProject work package ID

        Returns:
            Number of comments migrated

        """
        migrated = 0

        try:
            # Get comments from Jira
            comments = self.jira_client.jira.comments(jira_issue.key)
        except Exception as e:
            self.logger.debug("Failed to fetch comments for %s: %s", jira_issue.key, e)
            return 0

        for comment in comments:
            body = getattr(comment, "body", None)
            if not body:
                continue

            # Convert comment body with link resolution
            converted_body = self._convert_jira_links(body)

            try:
                # Create activity/journal in OpenProject
                self.op_client.create_work_package_activity(
                    wp_id,
                    {"comment": {"raw": converted_body}},
                )
                migrated += 1
            except Exception as e:
                self.logger.debug(
                    "Failed to create comment for WP#%d: %s",
                    wp_id,
                    e,
                )

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
        except Exception:
            return 0

        for watcher in watchers:
            # Get OpenProject user ID
            jira_username = watcher.get("name") or watcher.get("accountId")
            if jira_username and jira_username in self.user_mapping:
                op_user_id = self.user_mapping[jira_username].get("openproject_id")
                if op_user_id:
                    try:
                        self.op_client.add_watcher(wp_id, op_user_id)
                        added += 1
                    except Exception:
                        pass

        return added

    def _populate_content(
        self,
        jira_issue: Issue,
        wp_id: int,
    ) -> dict[str, Any]:
        """Populate all content for a work package.

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
        """Migrate content for all work packages.

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
            "custom_fields_updated": 0,
            "comments_migrated": 0,
            "watchers_added": 0,
            "projects": {},
        }

        projects = self._get_projects_to_migrate()
        self.logger.info("Migrating content for %d projects", len(projects))

        for project in projects:
            project_key = project.get("key")
            project_results = {
                "processed": 0,
                "updated": 0,
                "skipped": 0,
                "failed": 0,
            }

            self.logger.info("Processing content for project %s", project_key)

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

                # Populate content
                content_results = self._populate_content(issue, wp_id)

                if content_results["description"] or content_results["custom_fields"]:
                    project_results["updated"] += 1
                    if content_results["description"]:
                        results["descriptions_updated"] += 1
                    if content_results["custom_fields"]:
                        results["custom_fields_updated"] += 1
                    results["comments_migrated"] += content_results["comments_migrated"]
                    results["watchers_added"] += content_results["watchers_added"]
                else:
                    project_results["failed"] += 1

                if project_results["updated"] % 100 == 0 and project_results["updated"] > 0:
                    self.logger.info(
                        "  Updated %d work packages for %s",
                        project_results["updated"],
                        project_key,
                    )

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

            return ComponentResult(
                status="success",
                success=True,
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
                },
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
