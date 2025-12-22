"""Work Package Skeleton Migration - Phase 1 of two-phase migration.

This migration creates work package skeletons with minimal data to establish
a complete Jira→OpenProject mapping BEFORE any content with cross-references
is migrated. This solves the chicken-and-egg problem where descriptions and
comments contain links to other issues that may not exist yet.

Phase 1 creates:
- Work package with type, status, subject, project assignment
- J2O Origin Key custom field (for traceability)
- Complete work_package_mapping.json for Phase 2

Phase 1 does NOT create:
- Descriptions (deferred to Phase 2 for link resolution)
- Custom field values (deferred)
- Journals/comments (deferred)
- Attachments (deferred)
- Watchers (deferred)

Usage:
    python -m src.main migrate --components work_packages_skeleton
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.migrations.base_migration import BaseMigration, register_entity_types
from src.models import ComponentResult

if TYPE_CHECKING:
    from collections.abc import Iterator

    from jira.resources import Issue


@register_entity_types("work_packages_skeleton")
class WorkPackageSkeletonMigration(BaseMigration):
    """Phase 1: Create work package skeletons and establish complete mapping.

    This migration creates minimal work packages across ALL projects to establish
    a complete Jira→OpenProject ID mapping before any content migration.
    """

    WORK_PACKAGE_MAPPING_FILE = "work_package_mapping.json"

    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
    ) -> None:
        """Initialize the skeleton migration."""
        super().__init__(jira_client, op_client)

        # File paths
        self.work_package_mapping_file = self.data_dir / self.WORK_PACKAGE_MAPPING_FILE

        # Data storage
        self.work_package_mapping: dict[str, dict[str, Any]] = {}

        # Load mappings
        self._load_mappings()

        # Load existing work package mapping if available (for incremental runs)
        self._load_existing_mapping()

        # Cache for OpenProject types, statuses, priorities (lazy loaded)
        self._cached_types: list[dict[str, Any]] | None = None
        self._cached_statuses: list[dict[str, Any]] | None = None
        self._cached_priorities: list[dict[str, Any]] | None = None
        self._j2o_origin_key_cf_id: int | None = None

        # Batch processing configuration
        self.batch_size = config.migration_config.get("skeleton_batch_size", 50)

        self.logger.debug(
            "WorkPackageSkeletonMigration initialized with data dir: %s",
            self.data_dir,
        )

    def _load_mappings(self) -> None:
        """Load required mappings from config."""
        try:
            self.project_mapping = config.mappings.get_mapping("project") or {}
            self.issue_type_mapping = config.mappings.get_mapping("issue_type") or {}
            self.issue_type_id_mapping = config.mappings.get_mapping("issue_type_id") or {}
            self.status_mapping = config.mappings.get_mapping("status") or {}
        except Exception as e:
            self.logger.warning("Failed to load mappings via config: %s", e)
            self.project_mapping = {}
            self.issue_type_mapping = {}
            self.issue_type_id_mapping = {}
            self.status_mapping = {}

    def _load_existing_mapping(self) -> None:
        """Load existing work package mapping for incremental migration."""
        if self.work_package_mapping_file.exists():
            try:
                with self.work_package_mapping_file.open("r") as f:
                    self.work_package_mapping = json.load(f)
                self.logger.info(
                    "Loaded existing work package mapping with %d entries",
                    len(self.work_package_mapping),
                )
            except Exception as e:
                self.logger.warning("Failed to load existing mapping: %s", e)
                self.work_package_mapping = {}
        else:
            self.work_package_mapping = {}

    def _save_mapping(self) -> None:
        """Save the work package mapping to disk."""
        try:
            with self.work_package_mapping_file.open("w") as f:
                json.dump(self.work_package_mapping, f, indent=2)
            self.logger.debug("Saved work package mapping to %s", self.work_package_mapping_file)
        except Exception as e:
            self.logger.error("Failed to save mapping: %s", e)

    def _get_projects_to_migrate(self) -> list[dict[str, Any]]:
        """Get list of Jira projects to migrate based on filter."""
        projects = self.jira_client.get_projects()

        # Apply project filter if configured
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
        """Generate issues for a project with pagination.

        Args:
            project_key: The Jira project key

        Yields:
            Individual Jira Issue objects

        """
        start_at = 0
        batch_size = config.migration_config.get("batch_size", 100)

        # Check for test issue limiting
        max_issues = None
        if os.getenv("J2O_MAX_ISSUES"):
            try:
                max_issues = int(os.getenv("J2O_MAX_ISSUES"))
                self.logger.info(f"Limiting to {max_issues} issues (J2O_MAX_ISSUES)")
            except ValueError:
                pass

        jql = f'project = "{project_key}" ORDER BY created ASC'
        total_yielded = 0

        while True:
            # Check limit
            if max_issues is not None and total_yielded >= max_issues:
                self.logger.info(f"Reached max issues limit ({max_issues})")
                break

            # Fetch batch - minimal fields for skeleton
            issues = self._fetch_issues_batch(jql, start_at, batch_size)

            if not issues:
                break

            for issue in issues:
                if max_issues is not None and total_yielded >= max_issues:
                    break
                yield issue
                total_yielded += 1

            # Check if we got a full batch
            if len(issues) < batch_size:
                break

            start_at += batch_size

    def _fetch_issues_batch(
        self,
        jql: str,
        start_at: int,
        batch_size: int,
    ) -> list[Issue]:
        """Fetch a batch of issues from Jira.

        Args:
            jql: The JQL query
            start_at: Starting index
            batch_size: Number of issues to fetch

        Returns:
            List of Jira Issue objects

        """
        try:
            # Minimal fields for skeleton creation
            fields = "summary,issuetype,status,project"
            return self.jira_client.jira.search_issues(
                jql,
                startAt=start_at,
                maxResults=batch_size,
                fields=fields,
            )
        except Exception as e:
            self.logger.error("Failed to fetch issues: %s", e)
            return []

    def _get_openproject_type_id(self, jira_issue: Issue) -> int | None:
        """Get OpenProject type ID for a Jira issue type.

        Args:
            jira_issue: The Jira issue

        Returns:
            OpenProject type ID or None

        """
        issue_type_id = str(jira_issue.fields.issuetype.id)

        # Try issue_type_id mapping first
        if issue_type_id in self.issue_type_id_mapping:
            return self.issue_type_id_mapping[issue_type_id]

        # Try issue_type mapping by name
        issue_type_name = jira_issue.fields.issuetype.name
        if issue_type_name in self.issue_type_mapping:
            mapping = self.issue_type_mapping[issue_type_name]
            if isinstance(mapping, dict):
                return mapping.get("openproject_id")
            return mapping

        # Fallback to default type
        return self._get_default_type_id()

    def _get_default_type_id(self) -> int:
        """Get default OpenProject type ID (cached)."""
        if self._cached_types is None:
            try:
                self._cached_types = self.op_client.get_work_package_types()
                self.logger.info("Cached %d work package types", len(self._cached_types or []))
            except Exception:
                self._cached_types = []
        if self._cached_types:
            return self._cached_types[0].get("id", 1)
        return 1

    def _get_openproject_status_id(self, jira_issue: Issue) -> int | None:
        """Get OpenProject status ID for a Jira status.

        Args:
            jira_issue: The Jira issue

        Returns:
            OpenProject status ID or None

        """
        status_id = str(jira_issue.fields.status.id)

        if status_id in self.status_mapping:
            mapping = self.status_mapping[status_id]
            if isinstance(mapping, dict):
                return mapping.get("openproject_id")
            return mapping

        # Fallback to default status
        return self._get_default_status_id()

    def _get_default_status_id(self) -> int:
        """Get default OpenProject status ID (cached)."""
        if self._cached_statuses is None:
            try:
                self._cached_statuses = self.op_client.get_statuses()
                self.logger.info("Cached %d statuses", len(self._cached_statuses or []))
            except Exception:
                self._cached_statuses = []
        if self._cached_statuses:
            return self._cached_statuses[0].get("id", 1)
        return 1

    def _get_default_priority_id(self) -> int:
        """Get default OpenProject priority ID (Normal priority, cached)."""
        if self._cached_priorities is None:
            try:
                self._cached_priorities = self.op_client.get_issue_priorities()
                self.logger.info("Cached %d priorities", len(self._cached_priorities or []))
            except Exception:
                self._cached_priorities = []
        if self._cached_priorities:
            # Try to find "Normal" priority, otherwise use first
            for p in self._cached_priorities:
                if p.get("name", "").lower() == "normal":
                    return p.get("id", 1)
            return self._cached_priorities[0].get("id", 1)
        return 1

    def _get_default_author_id(self) -> int:
        """Get default author ID (admin user)."""
        # Use admin user (ID 1) as default author for skeleton creation
        return 1

    def _get_j2o_origin_key_cf_id(self) -> int | None:
        """Get or create the J2O Origin Key custom field ID (cached)."""
        if self._j2o_origin_key_cf_id is not None:
            return self._j2o_origin_key_cf_id

        try:
            cf_name = "J2O Origin Key"
            cf = self.op_client.ensure_custom_field(
                name=cf_name,
                field_format="string",
                type_ids=[],  # All types
                is_for_all=True,
            )
            if cf and cf.get("id"):
                self._j2o_origin_key_cf_id = cf["id"]
                self.logger.info("J2O Origin Key custom field ID: %d", self._j2o_origin_key_cf_id)
        except Exception as e:
            self.logger.warning("Failed to get/create J2O Origin Key custom field: %s", e)

        return self._j2o_origin_key_cf_id

    def _get_openproject_project_id(self, jira_project_key: str) -> int | None:
        """Get OpenProject project ID for a Jira project.

        Args:
            jira_project_key: The Jira project key

        Returns:
            OpenProject project ID or None

        """
        if jira_project_key in self.project_mapping:
            mapping = self.project_mapping[jira_project_key]
            if isinstance(mapping, dict):
                return mapping.get("openproject_id")
            return mapping
        return None

    def _build_skeleton_payload(
        self,
        jira_issue: Issue,
        project_id: int,
        j2o_cf_id: int | None,
    ) -> dict[str, Any] | None:
        """Build a skeleton payload for batch creation.

        Args:
            jira_issue: The Jira issue
            project_id: OpenProject project ID
            j2o_cf_id: J2O Origin Key custom field ID

        Returns:
            Payload dict or None if missing mappings

        """
        type_id = self._get_openproject_type_id(jira_issue)
        status_id = self._get_openproject_status_id(jira_issue)

        if not type_id or not status_id:
            self.logger.warning(
                "Missing type or status mapping for %s",
                jira_issue.key,
            )
            return None

        # Build minimal work package payload
        payload: dict[str, Any] = {
            "subject": jira_issue.fields.summary[:255],  # Truncate if needed
            "project_id": project_id,
            "type_id": type_id,
            "status_id": status_id,
            "priority_id": self._get_default_priority_id(),
            "author_id": self._get_default_author_id(),
            # Include jira_key for result matching
            "_jira_key": jira_issue.key,
            "_jira_id": str(jira_issue.id),
        }

        # Add J2O Origin Key custom field
        if j2o_cf_id:
            payload["custom_fields"] = [{"id": j2o_cf_id, "value": jira_issue.key}]

        return payload

    def _create_skeletons_batch(
        self,
        payloads: list[dict[str, Any]],
        project_key: str,
    ) -> tuple[int, int, list[tuple[str, str, int]]]:
        """Create a batch of work package skeletons.

        Args:
            payloads: List of skeleton payloads
            project_key: Project key for logging

        Returns:
            Tuple of (created_count, failed_count, list of (jira_id, jira_key, wp_id))

        """
        if not payloads:
            return 0, 0, []

        # Extract jira info before sending to batch (we remove _ prefixed keys)
        jira_info = [(p["_jira_id"], p["_jira_key"]) for p in payloads]
        clean_payloads = [
            {k: v for k, v in p.items() if not k.startswith("_")}
            for p in payloads
        ]

        try:
            # Call _create_work_packages_batch directly (returns dict, not list)
            result = self.op_client._create_work_packages_batch(clean_payloads)
            created = result.get("created", 0)
            failed = result.get("failed", 0)
            results_list = result.get("results", [])

            # Match results back to Jira issues by index
            mappings: list[tuple[str, str, int]] = []
            for i, res in enumerate(results_list):
                if res.get("status") == "created" and res.get("id"):
                    jira_id, jira_key = jira_info[i]
                    mappings.append((jira_id, jira_key, res["id"]))

            self.logger.info(
                "  Batch result: %d created, %d failed in %s",
                created,
                failed,
                project_key,
            )
            return created, failed, mappings

        except Exception as e:
            self.logger.error("Batch creation failed for %s: %s", project_key, e)
            return 0, len(payloads), []

    def _update_mapping(
        self,
        jira_issue: Issue,
        wp_result: dict[str, Any],
    ) -> None:
        """Update the work package mapping.

        Args:
            jira_issue: The Jira issue
            wp_result: The created work package

        """
        jira_id = str(jira_issue.id)
        jira_key = jira_issue.key
        project_key = jira_issue.fields.project.key if hasattr(jira_issue.fields, "project") else ""

        self.work_package_mapping[jira_id] = {
            "jira_key": jira_key,
            "openproject_id": wp_result["id"],
            "project_key": project_key,
        }

    def _migrate_skeletons(self) -> dict[str, Any]:
        """Migrate work package skeletons for all projects using batch processing.

        Returns:
            Migration results dictionary

        """
        results = {
            "total_processed": 0,
            "total_created": 0,
            "total_skipped": 0,
            "total_failed": 0,
            "projects": {},
        }

        # Get J2O Origin Key custom field ID once upfront
        j2o_cf_id = self._get_j2o_origin_key_cf_id()

        projects = self._get_projects_to_migrate()
        self.logger.info(
            "Migrating skeletons for %d projects (batch_size=%d)",
            len(projects),
            self.batch_size,
        )

        for project in projects:
            project_key = project.get("key")
            project_id = self._get_openproject_project_id(project_key)

            if not project_id:
                self.logger.warning(
                    "No OpenProject mapping for project %s, skipping",
                    project_key,
                )
                continue

            project_results = {
                "processed": 0,
                "created": 0,
                "skipped": 0,
                "failed": 0,
            }

            self.logger.info("Processing project %s", project_key)

            # Collect issues into batches
            batch_payloads: list[dict[str, Any]] = []
            batch_issues: list[Issue] = []

            for issue in self.iter_project_issues(project_key):
                project_results["processed"] += 1
                jira_id = str(issue.id)

                # Skip if already migrated
                if jira_id in self.work_package_mapping:
                    project_results["skipped"] += 1
                    continue

                # Build payload for batch
                payload = self._build_skeleton_payload(issue, project_id, j2o_cf_id)
                if payload:
                    batch_payloads.append(payload)
                    batch_issues.append(issue)
                else:
                    project_results["failed"] += 1

                # Process batch when full
                if len(batch_payloads) >= self.batch_size:
                    created, failed, mappings = self._create_skeletons_batch(
                        batch_payloads, project_key
                    )
                    project_results["created"] += created
                    project_results["failed"] += failed

                    # Update mappings
                    for jira_id, jira_key, wp_id in mappings:
                        pkey = issue.fields.project.key if hasattr(issue.fields, "project") else project_key
                        self.work_package_mapping[jira_id] = {
                            "jira_key": jira_key,
                            "openproject_id": wp_id,
                            "project_key": pkey,
                        }

                    self.logger.info(
                        "  Created %d skeletons for %s (batch)",
                        project_results["created"],
                        project_key,
                    )
                    self._save_mapping()

                    # Reset batch
                    batch_payloads = []
                    batch_issues = []

            # Process remaining items in last batch
            if batch_payloads:
                created, failed, mappings = self._create_skeletons_batch(
                    batch_payloads, project_key
                )
                project_results["created"] += created
                project_results["failed"] += failed

                # Update mappings
                for jira_id, jira_key, wp_id in mappings:
                    self.work_package_mapping[jira_id] = {
                        "jira_key": jira_key,
                        "openproject_id": wp_id,
                        "project_key": project_key,
                    }
                self._save_mapping()

            # Aggregate results
            results["projects"][project_key] = project_results
            results["total_processed"] += project_results["processed"]
            results["total_created"] += project_results["created"]
            results["total_skipped"] += project_results["skipped"]
            results["total_failed"] += project_results["failed"]

            self.logger.info(
                "Project %s: %d processed, %d created, %d skipped, %d failed",
                project_key,
                project_results["processed"],
                project_results["created"],
                project_results["skipped"],
                project_results["failed"],
            )

        # Final save
        self._save_mapping()

        self.logger.success(
            "Skeleton migration complete: %d created, %d skipped, %d failed",
            results["total_created"],
            results["total_skipped"],
            results["total_failed"],
        )

        return results

    def run(self) -> ComponentResult:
        """Run the skeleton migration.

        Returns:
            ComponentResult with migration status

        """
        start_time = datetime.now(tz=UTC)
        try:
            migration_results = self._migrate_skeletons()
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
                    "total_created": migration_results["total_created"],
                    "total_skipped": migration_results["total_skipped"],
                    "total_failed": migration_results["total_failed"],
                    "mapping_file": str(self.work_package_mapping_file),
                },
            )
        except Exception as e:
            end_time = datetime.now(tz=UTC)
            duration_seconds = (end_time - start_time).total_seconds()
            self.logger.exception("Skeleton migration failed: %s", e)
            return ComponentResult(
                status="error",
                success=False,
                error=str(e),
                timestamp=end_time.isoformat(),
                start_time=start_time.isoformat(),
                duration_seconds=duration_seconds,
            )
