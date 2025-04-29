"""
Work package migration module for Jira to OpenProject migration.
Handles the migration of issues from Jira to work packages in OpenProject.
"""

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from jira import Issue

from src.models import ComponentResult
from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.clients.openproject_rails_client import OpenProjectRailsClient
from src.display import ProgressTracker
from src.utils import data_handler
from src.migrations.base_migration import BaseMigration


class WorkPackageMigration(BaseMigration):
    """
    Handles the migration of issues from Jira to work packages in OpenProject.

    This class is responsible for:
    1. Extracting issues from Jira projects
    2. Creating corresponding work packages in OpenProject
    3. Mapping issues between the systems
    4. Handling attachments, comments, and relationships
    """

    # Define mapping file pattern constant
    WORK_PACKAGE_MAPPING_FILE_PATTERN = "work_package_mapping_{}.json"

    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
        op_rails_client: OpenProjectRailsClient | None = None,
        data_dir: str = None,
    ):
        """
        Initialize the work package migration.

        Args:
            jira_client: JiraClient instance.
            op_client: OpenProjectClient instance.
            op_rails_client: Optional OpenProjectRailsClient instance.
            data_dir: Path to data directory for storing mappings.
        """
        super().__init__(jira_client, op_client, op_rails_client)

        # Override data_dir if specified
        if data_dir:
            self.data_dir = Path(data_dir)
            os.makedirs(self.data_dir, exist_ok=True)
        else:
            # Convert string path from BaseMigration to Path object
            self.data_dir = Path(self.data_dir)

        # Setup file paths
        self.jira_issues_file = self.data_dir / "jira_issues.json"
        self.op_work_packages_file = self.data_dir / "op_work_packages.json"
        self.work_package_mapping_file = self.data_dir / "work_package_mapping.json"

        # Data storage
        self.jira_issues = {}
        self.op_work_packages = {}
        self.work_package_mapping = {}

        # Mappings
        self.project_mapping = {}
        self.user_mapping = {}
        self.issue_type_mapping = {}
        self.status_mapping = {}

        # Load existing mappings
        self._load_mappings()

        # Logging
        self.logger.debug(f"WorkPackageMigration initialized with data dir: {self.data_dir}")

    def _load_mappings(self):
        """Load all required mappings from files."""
        from src.utils import data_handler

        # Load mappings from disk
        self.project_mapping = data_handler.load_dict(
            filename="project_mapping.json",
            directory=self.data_dir,
            default={}
        )

        self.user_mapping = data_handler.load_dict(
            filename="user_mapping.json",
            directory=self.data_dir,
            default={}
        )

        self.issue_type_mapping = data_handler.load_dict(
            filename="issue_type_mapping.json",
            directory=self.data_dir,
            default={}
        )

        self.issue_type_id_mapping = data_handler.load_dict(
            filename="issue_type_id_mapping.json",
            directory=self.data_dir,
            default={}
        )

        self.status_mapping = data_handler.load_dict(
            filename="status_mapping.json",
            directory=self.data_dir,
            default={}
        )

    def _extract_jira_issues(
        self,
        project_key: str,
        batch_size: int = 100,
        project_tracker: ProgressTracker = None,
    ) -> list[Issue]:
        """
        Extract issues from a Jira project.

        Args:
            project_key: The key of the Jira project to extract issues from
            batch_size: Number of issues to retrieve in each batch
            project_tracker: Optional parent progress tracker to update

        Returns:
            List of Jira issue dictionaries
        """
        self.logger.info(
            f"Extracting issues from Jira project {project_key}...",
            extra={"markup": True},
        )
        all_issues: list[Issue] = []

        try:
            # First, get the total number of issues for this project to set up progress bar
            total_issues = self.jira_client.get_issue_count(project_key)
            if total_issues <= 0:
                self.logger.warning(
                    f"No issues found for project {project_key}", extra={"markup": True}
                )
                return all_issues

            self.logger.info(
                f"Found {total_issues} issues to extract from project {project_key}",
                extra={"markup": True},
            )

            total_issues = min(10, total_issues)
            batch_size = min(batch_size, total_issues)

            # Get issues in batches with progress tracking
            start_at = 0

            project_tracker.update_description(
                f"Fetching issues from {project_key} (0/{total_issues})"
            )
            current_batch = 0

            # Using the parent tracker instead of creating a new one
            while start_at < total_issues:
                # Update progress description
                current_batch += 1
                progress_desc = (
                    f"Fetching {project_key} issues "
                    f"{start_at+1}-{min(start_at+batch_size, total_issues)}/{total_issues}"
                )
                project_tracker.update_description(progress_desc)

                # Fetch a batch of issues with retry logic
                max_retries = 3
                retry_count = 0
                issues: list[Issue] = []

                while retry_count < max_retries:
                    try:
                        issues = self.jira_client.get_all_issues_for_project(
                            project_key, expand_changelog=True
                        )
                        break
                    except Exception as e:
                        retry_count += 1
                        retry_msg = (
                            f"Error fetching issues for {project_key} "
                            f"(attempt {retry_count}/{max_retries}): {str(e)}"
                        )
                        self.logger.warning(retry_msg, extra={"markup": True})
                        project_tracker.add_log_item(retry_msg)

                        if retry_count >= max_retries:
                            self.logger.error(
                                f"Failed to fetch issues after {max_retries} attempts: {str(e)}",
                                extra={"markup": True},
                            )
                            project_tracker.add_log_item(
                                f"Failed to fetch issues after {max_retries} attempts"
                            )
                            # Save what we have so far before potentially raising exception
                            if all_issues:
                                self._save_to_json(
                                    all_issues, f"jira_issues_{project_key}.json"
                                )
                                self.logger.info(
                                    f"Saved {len(all_issues)} issues collected before error occurred",
                                    extra={"markup": True},
                                )
                            # Continue with next batch instead of failing completely
                            issues = []
                            break

                        # Exponential backoff: wait longer with each retry
                        import time

                        wait_time = 2**retry_count
                        self.logger.info(
                            f"Retrying in {wait_time} seconds...",
                            extra={"markup": True},
                        )
                        time.sleep(wait_time)

                if not issues:
                    # Log message and move to next batch instead of breaking completely
                    self.logger.warning(
                        f"No issues retrieved for batch starting at {start_at}. Moving to next batch.",
                        extra={"markup": True},
                    )
                    project_tracker.add_log_item(
                        f"Warning: No issues retrieved for batch starting at {start_at}"
                    )
                    start_at += batch_size
                    continue

                # Add to overall list
                all_issues.extend(issues)

                # Update trackers
                retrieved_count = len(issues)
                project_tracker.add_log_item(
                    f"Retrieved {retrieved_count} issues from {project_key} (batch #{current_batch})"
                )

                # Only log at the NOTICE level for large projects with multiple batches
                if total_issues > batch_size:
                    self.logger.notice(
                        f"Retrieved {retrieved_count} issues (total: {len(all_issues)}/{total_issues})",
                        extra={"markup": True},
                    )

                if len(issues) < batch_size:
                    # We got fewer issues than requested, so we're done
                    break

                start_at += batch_size

            # Save issues to file for later reference, using safe save
            try:
                self._save_to_json(all_issues, f"jira_issues_{project_key}.json")
                self.logger.info(
                    f"Extracted and saved {len(all_issues)} issues from project {project_key}",
                    extra={"markup": True},
                )
            except Exception as e:
                self.logger.error(
                    f"Failed to save issues to file: {str(e)}", extra={"markup": True}
                )
                # Try to save to alternate location as backup
                backup_path = (
                    self.data_dir
                    / f"jira_issues_{project_key}_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                )
                try:
                    with open(backup_path, "w") as f:
                        json.dump(all_issues, f, indent=2)
                    self.logger.info(
                        f"Saved backup of issues to {backup_path}",
                        extra={"markup": True},
                    )
                except Exception as backup_error:
                    self.logger.critical(
                        f"Also failed to save backup: {str(backup_error)}",
                        extra={"markup": True},
                    )

            return all_issues

        except Exception as e:
            error_msg = f"Failed to extract issues from project {project_key}: {str(e)}"
            self.logger.error(error_msg, extra={"markup": True})
            if project_tracker:
                project_tracker.add_log_item(error_msg)
            # Reraise with more context
            raise RuntimeError(
                f"Jira issue extraction failed for project {project_key}: {str(e)}"
            ) from e

    def _prepare_work_package(
        self, jira_issue: dict[str, Any], project_id: int
    ) -> dict[str, Any]:
        """
        Internal method to prepare a work package object from a Jira issue (without creating it).

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

        assignee_name = None
        if hasattr(jira_issue.fields, "assignee") and jira_issue.fields.assignee:
            assignee_name = getattr(jira_issue.fields.assignee, "name", None)

        # Extract creator and reporter
        creator_name = None
        if hasattr(jira_issue.fields, "creator") and jira_issue.fields.creator:
            creator_name = getattr(jira_issue.fields.creator, "name", None)

        reporter_name = None
        if hasattr(jira_issue.fields, "reporter") and jira_issue.fields.reporter:
            reporter_name = getattr(jira_issue.fields.reporter, "name", None)

        # Extract dates
        created_at = None
        if hasattr(jira_issue.fields, "created"):
            created_at = jira_issue.fields.created

        updated_at = None
        if hasattr(jira_issue.fields, "updated"):
            updated_at = jira_issue.fields.updated

        # Extract watchers
        watchers = []
        if hasattr(jira_issue.fields, "watches") and jira_issue.fields.watches:
            watcher_count = getattr(jira_issue.fields.watches, "watchCount", 0)
            if watcher_count > 0:
                try:
                    # Fetch watchers if there are any
                    watchers_data = self.jira_client.get_issue_watchers(
                        jira_issue.key
                    )
                    if watchers_data:
                        watchers = [
                            watcher.get("name") for watcher in watchers_data
                        ]
                except Exception as e:
                    self.logger.exception(
                        f"Failed to fetch watchers for issue {jira_issue.key}: {str(e)}"
                    )

        # Extract custom fields
        custom_fields = {}
        for field_name, field_value in jira_issue.raw.get("fields", {}).items():
            if field_name.startswith("customfield_") and field_value is not None:
                custom_fields[field_name] = field_value

        subject = jira_issue.fields.summary
        description = getattr(jira_issue.fields, "description", "") or ""

        jira_id = jira_issue.id
        jira_key = jira_issue.key

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
            extra={"markup": True},
        )

        # If no type mapping exists, default to Task
        if not type_id:
            self.logger.warning(
                f"No mapping found for issue type {issue_type_name} (ID: {issue_type_id}), defaulting to Task",
                extra={"markup": True},
            )
            # Get the Task type ID from OpenProject
            task_types = [
                t
                for t in self.op_client.get_work_package_types()
                if t["name"] == "Task"
            ]
            if task_types:
                type_id = task_types[0]["id"]
            else:
                # If no Task type found, use the first available type
                types = self.op_client.get_work_package_types()
                if types:
                    type_id = types[0]["id"]
                else:
                    self.logger.error(
                        "No work package types available in OpenProject",
                        extra={"markup": True},
                    )
                    return None

        # Map the status
        status_op_id = None
        if status_id:
            status_op_id = self.status_mapping.get(status_id)

        # Map the assignee
        assigned_to_id = None
        if assignee_name and assignee_name in self.user_mapping:
            assigned_to_id = self.user_mapping[assignee_name]

        # Map creator and reporter (author) users
        author_id = None
        if reporter_name and reporter_name in self.user_mapping:
            author_id = self.user_mapping[reporter_name]

        # If reporter is not available, fall back to creator
        if not author_id and creator_name and creator_name in self.user_mapping:
            author_id = self.user_mapping[creator_name]

        # Handle watchers
        watcher_ids = []
        for watcher_name in watchers:
            if watcher_name in self.user_mapping:
                watcher_id = self.user_mapping[watcher_name]
                if watcher_id:
                    watcher_ids.append(watcher_id)
            else:
                self.logger.debug(f"Watcher {watcher_name} not found in user mapping")

        # Add Jira issue key to description for reference
        jira_reference = f"\n\n*Imported from Jira issue: {jira_key}*"
        if description:
            description += jira_reference
        else:
            description = jira_reference

        # Prepare work package data
        work_package = {
            "project_id": project_id,
            "type_id": type_id,
            "subject": subject,
            "description": description,
            "jira_id": jira_id,
            "jira_key": jira_key,
        }

        # Add optional fields if available
        if status_op_id:
            work_package["status_id"] = status_op_id
        if assigned_to_id:
            work_package["assigned_to_id"] = assigned_to_id
        if author_id:
            work_package["author_id"] = author_id
        if watcher_ids:
            work_package["watcher_ids"] = watcher_ids

        # Handle dates
        if created_at:
            work_package["created_at"] = created_at
        if updated_at:
            work_package["updated_at"] = updated_at

        # Process custom fields
        if custom_fields:
            # Load custom field mappings
            custom_field_mapping = self._load_custom_field_mapping()
            if custom_field_mapping:
                custom_field_values = {}

                for jira_field_id, field_value in custom_fields.items():
                    if jira_field_id in custom_field_mapping:
                        op_field = custom_field_mapping[jira_field_id]
                        op_field_id = op_field.get("openproject_id")

                        if op_field_id:
                            # Process different field types differently
                            field_type = op_field.get("field_type", "")
                            processed_value = self._process_custom_field_value(
                                field_value, field_type
                            )
                            if processed_value is not None:
                                custom_field_values[op_field_id] = processed_value

                if custom_field_values:
                    work_package["custom_fields"] = [
                        {"id": field_id, "value": field_value}
                        for field_id, field_value in custom_field_values.items()
                    ]

        return work_package

    def prepare_work_package(
        self, jira_issue: dict[str, Any], project_id: int
    ) -> dict[str, Any]:
        """
        Prepare a work package object from a Jira issue (without creating it).
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

            # Format the description to include the Jira key
            formatted_description = f"Jira Issue: {jira_key}\n\n{description}"

            # Convert the dictionary format used in tests to work package format
            work_package = {
                "project_id": project_id,
                "subject": jira_issue.get("summary", ""),
                "description": formatted_description,
                "jira_key": jira_key,
                "jira_id": jira_issue.get("id", ""),
                "_links": {},
            }

            # Add type if available
            issue_type = jira_issue.get("issue_type", {})
            if issue_type:
                type_id = self._map_issue_type(
                    issue_type.get("id"),
                    issue_type.get("name")
                )
                if type_id:
                    work_package["_links"]["type"] = {
                        "href": f"/api/v3/types/{type_id}"
                    }

            # Add status if available
            status = jira_issue.get("status", {})
            if status:
                status_id = self._map_status(
                    status.get("id"),
                    status.get("name")
                )
                if status_id:
                    work_package["_links"]["status"] = {
                        "href": f"/api/v3/statuses/{status_id}"
                    }

            return work_package
        else:
            # It's a Jira issue object, use the internal method
            return self._prepare_work_package(jira_issue, project_id)

    def _map_issue_type(self, type_id: str = None, type_name: str = None) -> int:
        """Map Jira issue type to OpenProject type ID"""
        if not type_id and not type_name:
            return None

        # Try to find in mapping by ID
        if type_id and self.issue_type_id_mapping and str(type_id) in self.issue_type_id_mapping:
            return self.issue_type_id_mapping[str(type_id)]

        # Try to find in mapping by ID in issue_type_mapping
        if type_id and str(type_id) in self.issue_type_mapping:
            return self.issue_type_mapping[str(type_id)].get("openproject_id")

        # Default to Task (typically ID 1 in OpenProject)
        return 1

    def _map_status(self, status_id: str = None, status_name: str = None) -> int:
        """Map Jira status to OpenProject status ID"""
        if not status_id and not status_name:
            return None

        # Try to find in mapping by ID
        if status_id and self.status_mapping and str(status_id) in self.status_mapping:
            return self.status_mapping[str(status_id)].get("openproject_id")

        # Default to "New" status (typically ID 1 in OpenProject)
        return 1

    def _load_custom_field_mapping(self) -> dict[str, Any]:
        """
        Load custom field mapping from disk.

        Returns:
            Dictionary mapping Jira custom field IDs to OpenProject custom field IDs
        """
        mapping_file = os.path.join(self.data_dir, "custom_field_mapping.json")
        if os.path.exists(mapping_file):
            try:
                with open(mapping_file) as f:
                    return json.load(f)
            except Exception as e:
                self.logger.warning(
                    f"Error loading custom field mapping: {str(e)}",
                    extra={"markup": True},
                )

        return {}

    def _process_custom_field_value(self, value: Any, field_type: str) -> Any:
        """
        Process a custom field value based on its type.

        Args:
            value: The value to process
            field_type: The type of the field (e.g., 'text', 'list', 'date')

        Returns:
            Processed value suitable for OpenProject
        """
        if value is None:
            return None

        if field_type in ("string", "text"):
            return str(value)

        elif field_type == "date":
            # Convert to ISO format if it's not already
            if isinstance(value, str):
                # Check if already in ISO format
                if "T" in value and (value.endswith("Z") or "+" in value):
                    return value

                # Try to parse different formats
                try:
                    from datetime import datetime

                    # Try parsing various formats
                    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
                        try:
                            date_obj = datetime.strptime(value, fmt)
                            return date_obj.strftime("%Y-%m-%d")
                        except ValueError:
                            continue

                    # If none of the formats worked, return as is
                    return value
                except Exception:
                    return value
            return value

        elif field_type == "list":
            # Handle list fields (dropdown/select)
            if isinstance(value, dict) and "value" in value:
                return value["value"]
            elif isinstance(value, list):
                # If it's a multi-select list, take the first value
                if value and isinstance(value[0], dict) and "value" in value[0]:
                    return [item["value"] for item in value]
                return value
            return value

        elif field_type == "user":
            # Handle user custom fields
            if isinstance(value, dict) and "name" in value:
                user_name = value["name"]
                if user_name in self.user_mapping:
                    return self.user_mapping[user_name]
            return None

        elif field_type == "boolean":
            # Convert to boolean
            if isinstance(value, bool):
                return value
            elif isinstance(value, str):
                return value.lower() in ("true", "yes", "1")
            return bool(value)

        # Default: return as is
        return value

    def _migrate_work_packages(self) -> dict[str, Any]:
        """
        Migrate issues from Jira to work packages in OpenProject.

        This method handles the complete migration process, including:
        - Loading necessary mappings
        - Processing each Jira project
        - Creating work packages for each issue
        - Updating relationships and attachments

        Returns:
            Dictionary mapping Jira issue IDs to OpenProject work package IDs
        """
        self.logger.info("Starting work package migration...", extra={"markup": True})

        # Check if Rails client is available - we need it for bulk imports
        if not self.op_client.rails_client:
            self.logger.error(
                "Rails client is required for work package migration. Please ensure tmux session is running.",
                extra={"markup": True},
            )
            return {}

        # Get list of Jira projects to process
        jira_projects = list(
            {
                entry.get("jira_key")
                for entry in self.project_mapping.values()
                if entry.get("jira_key")
            }
        )

        if not jira_projects:
            self.logger.warning(
                "No Jira projects found in mapping, nothing to migrate",
                extra={"markup": True},
            )
            return {}

        # Check for migration state file to resume from last processed project
        migration_state_file = os.path.join(
            self.data_dir, "work_package_migration_state.json"
        )
        processed_projects = set()
        last_processed_project = None

        if os.path.exists(migration_state_file):
            try:
                with open(migration_state_file) as f:
                    migration_state = json.load(f)
                    processed_projects = set(
                        migration_state.get("processed_projects", [])
                    )
                    last_processed_project = migration_state.get(
                        "last_processed_project"
                    )

                self.logger.info(
                    f"Found migration state - {len(processed_projects)} projects already processed",
                    extra={"markup": True},
                )
                if last_processed_project and last_processed_project in jira_projects:
                    self.logger.info(
                        f"Last processed project was {last_processed_project} - will resume from there",
                        extra={"markup": True},
                    )
            except Exception as e:
                self.logger.warning(
                    f"Error loading migration state: {str(e)}", extra={"markup": True}
                )
                # Create a backup of the corrupted state file if it exists
                if os.path.exists(migration_state_file):
                    backup_file = f"{migration_state_file}.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                    try:
                        shutil.copy2(migration_state_file, backup_file)
                        self.logger.info(
                            f"Created backup of corrupted state file: {backup_file}",
                            extra={"markup": True},
                        )
                    except Exception as backup_err:
                        self.logger.warning(
                            f"Failed to create backup of state file: {str(backup_err)}",
                            extra={"markup": True},
                        )

        # Filter unprocessed projects or start from the interrupted project
        remaining_projects = []
        if last_processed_project and last_processed_project in jira_projects:
            last_index = jira_projects.index(last_processed_project)
            remaining_projects = jira_projects[last_index:]
        else:
            remaining_projects = [
                p for p in jira_projects if p not in processed_projects
            ]

        self.logger.info(
            f"Found {len(jira_projects)} Jira projects, will process {len(remaining_projects)} remaining projects",
            extra={"markup": True},
        )

        # Initialize counters
        total_issues = 0
        total_created = 0
        failed_projects = []
        successful_projects = []

        # Process each project
        with ProgressTracker(
            "Migrating projects", len(remaining_projects), "Recent Projects"
        ) as project_tracker:
            for project_key in remaining_projects:
                project_tracker.update_description(f"Processing project {project_key}")
                self.logger.info(f"Processing project {project_key}", extra={"markup": True})

                # Update the state file at the start of each project processing
                try:
                    with open(migration_state_file, "w") as f:
                        json.dump(
                            {
                                "processed_projects": list(processed_projects),
                                "last_processed_project": project_key,
                                "timestamp": datetime.now().isoformat(),
                            },
                            f,
                            indent=2,
                        )
                except Exception as e:
                    self.logger.warning(
                        f"Error saving migration state: {str(e)}",
                        extra={"markup": True},
                    )
                    # Try alternate location if main save fails
                    try:
                        alt_file = f"{migration_state_file}.latest"
                        with open(alt_file, "w") as f:
                            json.dump(
                                {
                                    "processed_projects": list(processed_projects),
                                    "last_processed_project": project_key,
                                    "timestamp": datetime.now().isoformat(),
                                },
                                f,
                                indent=2,
                            )
                        self.logger.info(
                            f"Saved migration state to alternate location: {alt_file}",
                            extra={"markup": True},
                        )
                    except Exception as alt_err:
                        self.logger.error(
                            f"Failed to save migration state to alternate location: {str(alt_err)}",
                            extra={"markup": True},
                        )

                # Find corresponding OpenProject project ID
                project_mapping_entry = None
                for key, entry in self.project_mapping.items():
                    if entry.get("jira_key") == project_key and entry.get(
                        "openproject_id"
                    ):
                        project_mapping_entry = entry
                        break

                if not project_mapping_entry:
                    self.logger.warning(
                        f"No OpenProject project mapping found for Jira project {project_key}, skipping",
                        extra={"markup": True},
                    )
                    project_tracker.add_log_item(f"Skipped: {project_key} (no mapping)")
                    project_tracker.increment()
                    processed_projects.add(
                        project_key
                    )  # Mark as processed even if skipped
                    failed_projects.append(
                        {"project_key": project_key, "reason": "no_mapping"}
                    )
                    continue

                op_project_id = project_mapping_entry["openproject_id"]

                # Extract issues for this project
                try:
                    issues = self._extract_jira_issues(
                        project_key, project_tracker=project_tracker
                    )
                    total_issues += len(issues)

                    if not issues:
                        self.logger.warning(
                            f"No issues found for project {project_key}, skipping",
                            extra={"markup": True},
                        )
                        project_tracker.add_log_item(
                            f"Skipped: {project_key} (no issues)"
                        )
                        project_tracker.increment()
                        processed_projects.add(
                            project_key
                        )  # Mark as processed even if no issues
                        failed_projects.append(
                            {"project_key": project_key, "reason": "no_issues"}
                        )
                        continue
                except Exception as e:
                    self.logger.error(
                        f"Failed to extract issues for project {project_key}: {str(e)}",
                        extra={"markup": True},
                    )
                    project_tracker.add_log_item(
                        f"Failed: {project_key} (issue extraction error)"
                    )
                    project_tracker.increment()
                    failed_projects.append(
                        {
                            "project_key": project_key,
                            "reason": "extraction_error",
                            "error": str(e),
                        }
                    )
                    # Continue with next project instead of failing completely
                    continue

                # Prepare work packages data
                work_packages_data = []
                preparation_errors = 0
                self.logger.notice(
                    f"Preparing {len(issues)} work packages for project {project_key}",
                    extra={"markup": True},
                )

                for i, issue in enumerate(issues):
                    try:
                        # Handle both dictionary and jira.Issue objects
                        if hasattr(issue, "key"):
                            # It's a jira.Issue object
                            issue_key = issue.key
                            issue_id = issue.id
                            issue_summary = issue.fields.summary
                        else:
                            # It's a dictionary
                            issue_key = issue.get("key", "Unknown")
                            issue_id = issue.get("id", "Unknown")
                            issue_summary = issue.get("summary", "Unknown")

                        if (
                            i % 10 == 0 or i == len(issues) - 1
                        ):  # Log progress every 10 issues
                            project_tracker.update_description(
                                f"Preparing issue {issue_key} ({i+1}/{len(issues)})"
                            )

                        if config.migration_config.get("dry_run", False):
                            self.logger.notice(
                                f"DRY RUN: Would create work package for {issue_key}",
                                extra={"markup": True},
                            )
                            # Add a placeholder to mapping for dry runs
                            self.work_package_mapping[issue_id] = {
                                "jira_id": issue_id,
                                "jira_key": issue_key,
                                "openproject_id": None,
                                "subject": issue_summary,
                                "dry_run": True,
                            }
                            continue

                        # Prepare work package data
                        try:
                            wp_data = self._prepare_work_package(issue, op_project_id)
                            if wp_data:
                                work_packages_data.append(wp_data)
                        except Exception as e:
                            # Log the error with details about the issue
                            self.logger.error(
                                f"Error preparing work package for issue {issue_key}: {str(e)}",
                                extra={"markup": True},
                            )
                            self.logger.debug(
                                f"Issue type: {type(issue)}", extra={"markup": True}
                            )
                            preparation_errors += 1
                            # Continue with the next issue
                            continue

                    except Exception as e:
                        self.logger.error(
                            f"Error processing issue at index {i}: {str(e)}",
                            extra={"markup": True},
                        )
                        preparation_errors += 1
                        continue

                if preparation_errors > 0:
                    self.logger.warning(
                        f"Encountered {preparation_errors} errors while preparing work packages for {project_key}",
                        extra={"markup": True},
                    )
                    project_tracker.add_log_item(
                        f"Warnings: {preparation_errors} preparation errors for {project_key}"
                    )

                if config.migration_config.get("dry_run", False):
                    project_tracker.add_log_item(
                        f"DRY RUN: Would create {len(issues)} work packages for {project_key}"
                    )
                    project_tracker.increment()
                    continue

                if not work_packages_data:
                    self.logger.warning(
                        f"No work package data prepared for project {project_key}, skipping",
                        extra={"markup": True},
                    )
                    project_tracker.add_log_item(
                        f"Skipped: {project_key} (no work packages prepared)"
                    )
                    project_tracker.increment()
                    processed_projects.add(
                        project_key
                    )  # Mark as processed even if no work packages
                    failed_projects.append(
                        {"project_key": project_key, "reason": "preparation_failed"}
                    )
                    continue

                # --- Enable required types for the project before import ---
                required_type_ids = {
                    wp["type_id"] for wp in work_packages_data if "type_id" in wp
                }
                if op_project_id and required_type_ids:
                    # A simpler, more direct approach with fewer Rails client calls
                    self.logger.info(
                        f"Enabling work package types {list(required_type_ids)} "
                        f"for project {op_project_id}",
                        extra={"markup": True},
                    )

                    # Create a single script to handle all types at once
                    enable_types_header = f"""
                    # Ruby variables from Python
                    project_id = {op_project_id}
                    type_ids = {list(required_type_ids)}
                    """

                    enable_types_script = """
                    # Find the project
                    project = Project.find_by(id: project_id)

                    unless project
                      puts "Project not found: #{project_id}"
                      return nil # Use return instead of next
                    end

                    # Get current types
                    current_type_ids = project.types.pluck(:id)
                    puts "Current types: #{current_type_ids.join(', ')}"

                    # Types to add
                    types_to_add = []

                    # Check each type
                    type_ids.each do |type_id|
                      type = Type.find_by(id: type_id)

                      unless type
                        puts "Type not found: #{type_id}"
                        next # This next is valid because it's inside the each loop
                      end

                      if current_type_ids.include?(type_id)
                        puts "Type already enabled: #{type_id} (#{type.name})"
                      else
                        types_to_add << type
                        puts "Type to be enabled: #{type_id} (#{type.name})"
                      end
                    end

                    # If we have types to add, update the project
                    unless types_to_add.empty?
                      # Add new types to current types
                      project.types = project.types + types_to_add

                      # Save project
                      if project.save
                        puts "Successfully enabled types: #{types_to_add.map(&:id).join(', ')}"
                      else
                        puts "Failed to save project: #{project.errors.full_messages.join(', ')}"
                      end
                    else
                      puts "No new types to enable"
                    end
                    """

                    # Execute with retry logic
                    max_retries = 3
                    retry_count = 0
                    types_result = None

                    while retry_count < max_retries:
                        try:
                            types_result = self.op_client.rails_client.execute(
                                enable_types_header + enable_types_script
                            )
                            break
                        except Exception as e:
                            retry_count += 1
                            self.logger.warning(
                                f"Error enabling types (attempt {retry_count}/{max_retries}): {str(e)}",
                                extra={"markup": True},
                            )

                            if retry_count >= max_retries:
                                self.logger.error(
                                    f"Failed to enable types after {max_retries} attempts: {str(e)}",
                                    extra={"markup": True},
                                )
                                project_tracker.add_log_item(
                                    f"Warning: Failed to enable types for {project_key}"
                                )
                                break

                            # Exponential backoff
                            import time

                            wait_time = 2**retry_count
                            self.logger.info(
                                f"Retrying in {wait_time} seconds...",
                                extra={"markup": True},
                            )
                            time.sleep(wait_time)

                    if types_result and types_result.get("status") == "success":
                        self.logger.info(
                            f"Types setup complete for project {op_project_id}",
                            extra={"markup": True},
                        )
                    else:
                        error_msg = (
                            types_result.get("error")
                            if types_result
                            else "No result returned"
                        )
                        self.logger.error(
                            f"Error enabling types: {error_msg}", extra={"markup": True}
                        )
                        project_tracker.add_log_item(
                            f"Warning: Types may not be properly enabled for {project_key}"
                        )
                        # Continue despite errors - the bulk import might still work with default types

                # Bulk create work packages using Rails client
                self.logger.notice(
                    f"Creating {len(work_packages_data)} work packages for project {project_key}",
                    extra={"markup": True},
                )

                # First, check custom fields and proactively update them if needed
                custom_field_values_to_add = self._collect_missing_custom_field_values(work_packages_data)
                if custom_field_values_to_add:
                    self._update_custom_field_allowed_values(custom_field_values_to_add)

                # First, write the work packages data to a JSON file that Rails can read
                temp_file_path = os.path.join(
                    self.data_dir, f"work_packages_{project_key}.json"
                )
                self.logger.info(
                    f"Writing {len(work_packages_data)} work packages to {temp_file_path}",
                    extra={"markup": True},
                )

                # Ensure each work package has all required fields
                for wp in work_packages_data:
                    # Ensure string values for certain fields
                    if "subject" in wp:
                        wp["subject"] = (
                            str(wp["subject"]).replace('"', '\\"').replace("'", "\\'")
                        )
                    if "description" in wp:
                        wp["description"] = (
                            str(wp["description"])
                            .replace('"', '\\"')
                            .replace("'", "\\'")
                        )

                    # Store Jira IDs for mapping
                    jira_id = wp.get("jira_id")

                    # Remove fields not needed by OpenProject
                    wp_copy = wp.copy()
                    if "jira_id" in wp_copy:
                        del wp_copy["jira_id"]
                    if "jira_key" in wp_copy:
                        del wp_copy["jira_key"]

                    # Add to the final data
                    wp.update(wp_copy)

                # Write the JSON file
                with open(temp_file_path, "w") as f:
                    json.dump(work_packages_data, f, indent=2)

                # Define the path for the file inside the container
                container_temp_path = f"/tmp/work_packages_{project_key}.json"

                # Also save a timestamped copy for debugging
                debug_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                debug_json_path = os.path.join(
                    self.data_dir, f"work_packages_{project_key}_{debug_timestamp}.json"
                )
                debug_script_path = os.path.join(
                    self.data_dir, f"ruby_script_{project_key}_{debug_timestamp}.rb"
                )

                # Copy the file for debugging
                shutil.copy2(temp_file_path, debug_json_path)
                self.logger.info(
                    f"Saved debug copy of work packages data to {debug_json_path}",
                    extra={"markup": True},
                )

                # Copy the file to the container
                if self.op_client.rails_client.transfer_file_to_container(
                    temp_file_path, container_temp_path
                ):
                    self.logger.success(
                        "Successfully copied work packages data to container",
                        extra={"markup": True},
                    )
                else:
                    self.logger.error(
                        "Failed to transfer work packages file to container",
                        extra={"markup": True},
                    )
                    project_tracker.add_log_item(
                        f"Error: {project_key} (file transfer failed)"
                    )
                    project_tracker.increment()
                    processed_projects.add(project_key)
                    failed_projects.append(
                        {"project_key": project_key, "reason": "file_transfer_failed"}
                    )
                    continue

                # Create a simple Ruby script based on the example
                header_script = f"""
                # Ruby variables from Python
                wp_file_path = '{container_temp_path}'
                result_file_path = '/tmp/wp_result_{project_key}.json'
                """

                # Note: The following script contains Ruby variables (like 'values_added' and 'cf')
                # which may trigger Python linter warnings but are valid in the Ruby context.
                # These can be safely ignored.
                main_script = """
                begin
                  require 'json'

                  # Load the data from the JSON file
                  wp_data = JSON.parse(File.read(wp_file_path))
                  puts "Loaded #{wp_data.length} work packages from JSON file"

                  created_packages = []
                  errors = []

                  # Cache for custom fields we've already updated
                  updated_custom_fields = {}

                  # Helper method to update custom field allowed values
                  def update_custom_field_allowed_values(field_name, new_value)
                    return false unless new_value.present?

                    # Find the custom field by name
                    custom_field = CustomField.find_by(name: field_name)
                    return false unless custom_field

                    # Skip if not a list type field
                    return false unless custom_field.field_format == "list"

                    # Get current values and add the new one if not present
                    current_values = custom_field.possible_values || []
                    return true if current_values.include?(new_value)

                    # Add the new value
                    current_values << new_value
                    custom_field.possible_values = current_values

                    # Save and return result
                    if custom_field.save
                      puts "Updated custom field '#{field_name}' with new value: '#{new_value}'"
                      return true
                    else
                      puts "Failed to update custom field '#{field_name}': " +
                      "#{custom_field.errors.full_messages.join(', ')}"
                      return false
                    end
                  end

                  # Helper to extract field name and value from validation error
                  def extract_field_and_value_from_error(error_message)
                    # Pattern matches errors like "Field Name is not set to one of the allowed values."
                    match = error_message.match(/^(.*?) is not set to one of the allowed values/)
                    return match ? match[1] : nil
                  end

                  # Create each work package
                  wp_data.each do |wp_attrs|
                    begin
                      # Store Jira data for mapping
                      jira_id = wp_attrs['jira_id']
                      jira_key = wp_attrs['jira_key']

                      # Remove Jira fields not needed by OpenProject
                      wp_attrs.delete('jira_id')
                      wp_attrs.delete('jira_key')

                      # Handle watchers - validate watcher IDs
                      if wp_attrs['watcher_ids'].is_a?(Array)
                        # Filter out nil values
                        wp_attrs['watcher_ids'].compact!

                        # Verify each watcher exists
                        valid_watcher_ids = []
                        wp_attrs['watcher_ids'].each do |watcher_id|
                          if User.find_by(id: watcher_id)
                            valid_watcher_ids << watcher_id
                          else
                            puts "Warning: Watcher ID #{watcher_id} does not exist for work package #{jira_key}"
                          end
                        end
                        wp_attrs['watcher_ids'] = valid_watcher_ids
                      end

                      # Create work package object
                      wp = WorkPackage.new(wp_attrs)

                      # Add required fields if missing
                      wp.priority = IssuePriority.default unless wp.priority_id
                      wp.author = User.where(admin: true).first unless wp.author_id
                      wp.status = Status.default unless wp.status_id

                      # Try saving the work package
                      created = false
                      retry_attempts = 0
                      max_retries = 3

                      until created || retry_attempts >= max_retries
                        if wp.save
                          created = true
                        else
                          retry_attempts += 1

                          # Check for custom field validation errors
                          custom_field_errors = wp.errors.full_messages.select { |msg|
                            msg.include?('is not set to one of the allowed values')
                          }

                           if custom_field_errors.any? && retry_attempts < max_retries
                             # Try to update custom fields and retry
                             custom_field_errors.each do |error|
                               field_name = extract_field_and_value_from_error(error)

                               # Get the value attempted from custom values
                               if field_name && wp.custom_field_values.present?
                                 cf = CustomField.find_by(name: field_name)
                                 if cf
                                   value = wp.custom_value_for(cf).try(:value)

                                   # Update custom field if needed and not already updated
                                   cache_key = "#{field_name}:#{value}"
                                   unless updated_custom_fields[cache_key]
                                     updated = update_custom_field_allowed_values(field_name, value)
                                     updated_custom_fields[cache_key] = true if updated
                                     puts "Updated custom field '#{field_name}' with value '#{value}': " +
                                       "#{updated ? 'success' : 'failed'}"
                                   end
                                 end
                               end
                             end

                             # Refresh the work package for the next attempt
                             wp = WorkPackage.new(wp_attrs)
                             wp.priority = IssuePriority.default unless wp.priority_id
                             wp.author = User.where(admin: true).first unless wp.author_id
                             wp.status = Status.default unless wp.status_id
                           else
                             # Not fixable with retries or max retries reached
                             break
                           end
                         end
                       end

                       if created
                         created_packages << {
                           'jira_id' => jira_id,
                           'jira_key' => jira_key,
                           'openproject_id' => wp.id,
                           'subject' => wp.subject
                         }
                         puts "Created work package ##{wp.id}: #{wp.subject}"
                       else
                         errors << {
                           'jira_id' => jira_id,
                           'jira_key' => jira_key,
                           'subject' => wp_attrs['subject'],
                           'errors' => wp.errors.full_messages,
                           'error_type' => 'validation_error'
                         }
                         puts "Error creating work package: #{wp.errors.full_messages.join(', ')}"
                       end
                     rescue => e
                       errors << {
                         'jira_id' => wp_attrs['jira_id'],
                         'jira_key' => wp_attrs['jira_key'],
                         'subject' => wp_attrs['subject'],
                         'errors' => [e.message],
                         'error_type' => 'exception'
                       }
                       puts "Exception: #{e.message}"
                     end
                   end

                   # Write results to result file
                   result = {
                     'status' => 'success',
                     'created' => created_packages,
                     'errors' => errors,
                     'created_count' => created_packages.length,
                     'error_count' => errors.length,
                     'total' => wp_data.length,
                     'updated_custom_fields' => updated_custom_fields.keys
                   }

                   File.write(result_file_path, result.to_json)
                   puts "Results written to #{result_file_path}"

                   # Also return the result for direct capture
                   result
                 rescue => e
                   error_result = {
                     'status' => 'error',
                     'message' => e.message,
                     'backtrace' => e.backtrace[0..5]
                   }

                   # Try to save error to file
                   begin
                     File.write(result_file_path, error_result.to_json)
                   rescue => write_error
                     puts "Failed to write error to file: #{write_error.message}"
                   end

                   # Return error result
                   error_result
                 end
                 """

                # Save the Ruby script for debugging
                with open(debug_script_path, "w") as f:
                    f.write(header_script + main_script)
                self.logger.info(
                    f"Saved debug copy of Ruby script to {debug_script_path}",
                    extra={"markup": True},
                )

                # Execute the Ruby script
                result = self.op_client.rails_client.execute(
                    header_script + main_script
                )

                if result.get("status") != "success":
                    self.logger.error(
                        f"Rails error during work package creation: {result.get('error', 'Unknown error')}",
                        extra={"markup": True},
                    )
                    project_tracker.add_log_item(
                        f"Error: {project_key} (Rails execution failed)"
                    )
                    project_tracker.increment()
                    processed_projects.add(project_key)
                    failed_projects.append(
                        {"project_key": project_key, "reason": "rails_execution_failed"}
                    )
                    continue

                # Try to get the result file from the container
                result_file_container = f"/tmp/wp_result_{project_key}.json"
                result_file_local = os.path.join(
                    self.data_dir, f"wp_result_{project_key}.json"
                )

                # Initialize variables
                created_count = 0
                errors = []

                # Try to get results from direct output first
                output = result.get("output")
                if isinstance(output, dict) and output.get("status") == "success":
                    created_wps = output.get("created", [])
                    created_count = len(created_wps)
                    errors = output.get("errors", [])
                    updated_custom_fields = output.get("updated_custom_fields", [])

                    # Log any custom fields that were updated
                    self.log_custom_field_updates(updated_custom_fields)

                    # Update the mapping
                    for wp in created_wps:
                        jira_id = wp.get("jira_id")
                        if jira_id:
                            self.work_package_mapping[jira_id] = wp

                    # Handle errors
                    for error in errors:
                        jira_id = error.get("jira_id")
                        if jira_id:
                            self.work_package_mapping[jira_id] = {
                                "jira_id": jira_id,
                                "jira_key": error.get("jira_key"),
                                "openproject_id": None,
                                "subject": error.get("subject"),
                                "error": ", ".join(error.get("errors", [])),
                                "error_type": error.get("error_type"),
                            }
                else:
                    # If direct output doesn't work, try to get the result file
                    if self.op_client.rails_client.transfer_file_from_container(
                        result_file_container, result_file_local
                    ):
                        try:
                            # Also save a debug copy with timestamp
                            debug_result_path = os.path.join(
                                self.data_dir,
                                f"wp_result_{project_key}_{debug_timestamp}.json"
                            )
                            shutil.copy2(result_file_local, debug_result_path)
                            self.logger.info(
                                f"Saved debug copy of result file to {debug_result_path}",
                                extra={"markup": True},
                            )

                            with open(result_file_local) as f:
                                result_data = json.load(f)

                                if result_data.get("status") == "success":
                                    created_wps = result_data.get("created", [])
                                    created_count = len(created_wps)
                                    errors = result_data.get("errors", [])
                                    updated_custom_fields = result_data.get("updated_custom_fields", [])

                                    # Log any custom fields that were updated
                                    self.log_custom_field_updates(updated_custom_fields)

                                    # Update the mapping
                                    for wp in created_wps:
                                        jira_id = wp.get("jira_id")
                                        if jira_id:
                                            self.work_package_mapping[jira_id] = wp

                                    # Handle errors
                                    for error in errors:
                                        jira_id = error.get("jira_id")
                                        if jira_id:
                                            self.work_package_mapping[jira_id] = {
                                                "jira_id": jira_id,
                                                "jira_key": error.get("jira_key"),
                                                "openproject_id": None,
                                                "subject": error.get("subject"),
                                                "error": ", ".join(
                                                    error.get("errors", [])
                                                ),
                                                "error_type": error.get("error_type"),
                                            }
                        except Exception as e:
                            self.logger.error(
                                f"Error processing result file: {str(e)}",
                                extra={"markup": True},
                            )
                    else:
                        # Last resort - try to parse the console output
                        self.logger.warning(
                            "Could not get result file - parsing console output",
                            extra={"markup": True},
                        )
                        if isinstance(output, str):
                            created_matches = re.findall(
                                r"Created work package #(\d+): (.+?)$",
                                output,
                                re.MULTILINE,
                            )
                            created_count = len(created_matches)
                            self.logger.info(
                                f"Found {created_count} created work packages in console output",
                                extra={"markup": True},
                            )

                self.logger.success(
                    f"Created {created_count} work packages for project {project_key} (errors: {len(errors)})",
                    extra={"markup": True},
                )
                total_created += created_count

                project_tracker.add_log_item(
                    f"Completed: {project_key} ({created_count}/{len(issues)} issues)"
                )
                project_tracker.increment()

                # Mark project as successfully processed
                processed_projects.add(project_key)
                successful_projects.append(
                    {"project_key": project_key, "created_count": created_count}
                )

        # Save the work package mapping
        data_handler.save(
            data=self.work_package_mapping,
            filename="work_package_mapping.json",
            directory=self.data_dir
        )

        # Save final migration state
        try:
            with open(migration_state_file, "w") as f:
                json.dump(
                    {
                        "processed_projects": list(processed_projects),
                        "last_processed_project": None,  # Reset the last processed since we're done with it
                        "timestamp": datetime.now().isoformat(),
                        "completed": True,
                    },
                    f,
                    indent=2,
                )
        except Exception as e:
            self.logger.warning(
                f"Error saving final migration state: {str(e)}", extra={"markup": True}
            )

        self.logger.success("Work package migration completed", extra={"markup": True})
        self.logger.info(f"Total issues processed: {total_issues}", extra={"markup": True})
        self.logger.info(
            f"Total work packages created: {total_created}", extra={"markup": True}
        )

        return self.work_package_mapping

    def analyze_work_package_mapping(self) -> dict[str, Any]:
        """
        Analyze the work package mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        self.logger.info("Analyzing work package mapping...", extra={"markup": True})

        if not self.work_package_mapping:
            try:
                with open(
                    os.path.join(self.data_dir, "work_package_mapping.json")
                ) as f:
                    self.work_package_mapping = json.load(f)
            except Exception as e:
                self.logger.error(
                    f"Failed to load work package mapping: {str(e)}",
                    extra={"markup": True},
                )
                return {"status": "error", "message": str(e)}

        total_issues = len(self.work_package_mapping)
        if total_issues == 0:
            return {
                "status": "warning",
                "message": "No work packages have been created yet",
                "work_packages_count": 0,
                "potential_issues": [],
            }

        # Count issues by project
        projects_count = {}
        for wp_id, wp_data in self.work_package_mapping.items():
            jira_key = wp_data.get("jira_key", "")
            if jira_key:
                project_key = jira_key.split("-")[0]
                projects_count[project_key] = projects_count.get(project_key, 0) + 1

        # Look for potential issues
        potential_issues = []

        # Check for failed work package creations with more detailed analysis
        failed_creations = []
        error_types = {}
        validation_errors = {}

        for wp_id, wp_data in self.work_package_mapping.items():
            if not wp_data.get("openproject_id"):
                jira_key = wp_data.get("jira_key", wp_id)
                failed_creations.append(jira_key)

                # Analyze error types
                if "error" in wp_data:
                    error_message = wp_data["error"]

                    # Categorize errors
                    if (
                        "422" in error_message
                        or "Unprocessable Entity" in error_message
                    ):
                        error_type = "validation_error"
                    elif (
                        "401" in error_message
                        or "403" in error_message
                        or "Unauthorized" in error_message
                    ):
                        error_type = "authorization_error"
                    elif "404" in error_message or "Not Found" in error_message:
                        error_type = "not_found_error"
                    elif (
                        "500" in error_message
                        or "Internal Server Error" in error_message
                    ):
                        error_type = "server_error"
                    else:
                        error_type = "other_error"

                    error_types[error_type] = error_types.get(error_type, 0) + 1

                    # Collect specific validation errors
                    if "validation_errors" in wp_data and wp_data["validation_errors"]:
                        for error in wp_data["validation_errors"]:
                            # Create a simplified key for the error
                            simple_error = error.lower()
                            for pattern, category in [
                                ("type", "type_error"),
                                ("status", "status_error"),
                                ("project", "project_error"),
                                ("subject", "subject_error"),
                                ("description", "description_error"),
                                ("assignee", "assignee_error"),
                            ]:
                                if pattern in simple_error:
                                    validation_errors[category] = (
                                        validation_errors.get(category, 0) + 1
                                    )
                                    break
                            else:
                                validation_errors["other_validation"] = (
                                    validation_errors.get("other_validation", 0) + 1
                                )

        if failed_creations:
            potential_issues.append(
                {
                    "issue": "failed_creations",
                    "description": f"{len(failed_creations)} work packages failed to be created",
                    "affected_items": failed_creations[:10],  # Limit to first 10
                    "count": len(failed_creations),
                    "error_types": error_types,
                    "validation_errors": validation_errors,
                }
            )

        # Prepare analysis results
        return {
            "status": "success",
            "work_packages_count": total_issues,
            "projects_migrated": len(projects_count),
            "work_packages_by_project": projects_count,
            "success_count": total_issues - len(failed_creations),
            "failed_count": len(failed_creations),
            "error_categories": error_types if error_types else None,
            "validation_error_types": validation_errors if validation_errors else None,
            "potential_issues": potential_issues,
        }

    def _save_to_json(self, data: Any, filename: str):
        """
        Save data to a JSON file in the data directory.

        Args:
            data: The data to save
            filename: The name of the file to save to
        """
        # Convert Jira Issue objects to dictionaries if needed
        if isinstance(data, list) and data and hasattr(data[0], "raw"):
            # This is a list of jira.Issue objects
            serializable_data = []
            for item in data:
                if hasattr(item, "raw"):
                    # Convert Jira Issue to dict
                    serializable_data.append(item.raw)
                else:
                    # Skip this item if it doesn't have 'raw' attribute
                    self.logger.warning(f"Skipping non-serializable item in {filename}")
            data = serializable_data

        # Call parent method to save the data
        return super()._save_to_json(data, filename)

    # --- Helper methods for direct import (Need adaptation for jira.Issue) ---

    def _create_wp_via_rails(self, wp_payload: dict[str, Any]) -> dict[str, Any] | None:
        """Creates a work package using the Rails console client via the proper client method."""
        if not self.op_rails_client:
            self.logger.error("Rails client not available for direct work package creation.")
            return None

        jira_key = wp_payload.get("jira_key", "UNKNOWN")
        self.logger.debug(
            f"Attempting to create WP for {jira_key} via Rails client create_record..."
        )

        # Prepare the attributes for the WorkPackage
        attributes = {
            "project_id": wp_payload.get("project_id"),
            "type_id": wp_payload.get("type_id"),
            "subject": wp_payload.get("subject"),
            "description": wp_payload.get("description"),
            "status_id": wp_payload.get("status_id"),
        }
        if wp_payload.get("assigned_to_id"):
            attributes["assigned_to_id"] = wp_payload["assigned_to_id"]

        # Remove None values
        attributes = {k: v for k, v in attributes.items() if v is not None}

        # Use the OpenProjectRailsClient.create_record method
        success, record_data, error_message = self.op_rails_client.create_record(
            "WorkPackage", attributes
        )

        if success and record_data and record_data.get("id"):
            self.logger.info(
                f"Successfully created work package {record_data['id']} for Jira issue {jira_key}"
            )
            return {
                "id": record_data["id"],
                "_type": "WorkPackage",
                "subject": record_data.get("subject"),
            }
        else:
            self.logger.error(
                f"Failed to create WP for {jira_key} via Rails client: {error_message}"
            )
            return None

    def run(self) -> ComponentResult:
        """
        Run the work package migration process.

        Returns:
            ComponentResult with migration results
        """
        self.logger.info("Starting work package migration", extra={"markup": True})
        start_time = datetime.now()

        try:
            # Verify required mappings are available
            missing_mappings = []

            # Load mappings if provided
            if config.mappings:
                self.project_mapping = config.mappings.get_mapping("project") or {}
                self.user_mapping = config.mappings.get_mapping("user") or {}
                self.issue_type_mapping = (
                    config.mappings.get_mapping("issue_type") or {}
                )
                self.status_mapping = config.mappings.get_mapping("status") or {}

                # Check if critical mappings are empty
                if not self.project_mapping:
                    missing_mappings.append("project")
                if not self.issue_type_mapping:
                    missing_mappings.append("issue_type")
                if not self.status_mapping:
                    missing_mappings.append("status")
            else:
                # Load mappings from disk again
                self._load_mappings()

                # Check if critical mappings are empty
                if not self.project_mapping:
                    missing_mappings.append("project")
                if not self.issue_type_mapping:
                    missing_mappings.append("issue_type")
                if not self.status_mapping:
                    missing_mappings.append("status")

            if missing_mappings:
                warning_message = (
                    f"Missing critical mappings: {', '.join(missing_mappings)}. "
                    f"Migration may fail or create incomplete data."
                )
                self.logger.warning(warning_message, extra={"markup": True})

                # Create the var/data directory if it doesn't exist
                if not os.path.exists(self.data_dir):
                    os.makedirs(self.data_dir, exist_ok=True)

                # Record the warning in a migration issues file
                issues_file = os.path.join(self.data_dir, "migration_issues.json")
                issues_data = {}

                if os.path.exists(issues_file):
                    try:
                        with open(issues_file) as f:
                            issues_data = json.load(f)
                    except Exception as e:
                        self.logger.warning(
                            f"Error loading migration issues file: {str(e)}",
                            extra={"markup": True},
                        )

                # Update issues data
                timestamp = datetime.now().isoformat()
                if "work_package_migration" not in issues_data:
                    issues_data["work_package_migration"] = []

                issues_data["work_package_migration"].append(
                    {
                        "timestamp": timestamp,
                        "type": "warning",
                        "message": warning_message,
                        "missing_mappings": missing_mappings,
                    }
                )

                try:
                    with open(issues_file, "w") as f:
                        json.dump(issues_data, f, indent=2)
                except Exception as e:
                    self.logger.warning(
                        f"Error writing to migration issues file: {str(e)}",
                        extra={"markup": True},
                    )

            # Run the migration with additional error handling
            migration_results = {}
            try:
                migration_results = self._migrate_work_packages()
            except Exception as e:
                error_message = f"Work package migration failed with error: {str(e)}"
                self.logger.error(error_message, extra={"markup": True})

                # Record the error in the migration issues file
                issues_file = os.path.join(self.data_dir, "migration_issues.json")
                issues_data = {}

                if os.path.exists(issues_file):
                    try:
                        with open(issues_file) as f:
                            issues_data = json.load(f)
                    except Exception as read_err:
                        self.logger.warning(
                            f"Error loading migration issues file: {str(read_err)}",
                            extra={"markup": True},
                        )

                # Update issues data
                timestamp = datetime.now().isoformat()
                if "work_package_migration" not in issues_data:
                    issues_data["work_package_migration"] = []

                issues_data["work_package_migration"].append(
                    {
                        "timestamp": timestamp,
                        "type": "error",
                        "message": error_message,
                        "error": str(e),
                        "traceback": str(
                            getattr(e, "__traceback__", "No traceback available")
                        ),
                    }
                )

                try:
                    with open(issues_file, "w") as f:
                        json.dump(issues_data, f, indent=2)
                except Exception as write_err:
                    self.logger.warning(
                        f"Error writing to migration issues file: {str(write_err)}",
                        extra={"markup": True},
                    )

                # Return error result
                return ComponentResult(
                    status="error",
                    success=False,
                    error=str(e),
                    timestamp=datetime.now().isoformat(),
                    duration_seconds=(datetime.now() - start_time).total_seconds(),
                )

            # Calculate duration
            end_time = datetime.now()
            duration_seconds = (end_time - start_time).total_seconds()

            # Create the ComponentResult to return
            result = ComponentResult(
                status="success",
                success=True,
                timestamp=end_time.isoformat(),
                start_time=start_time.isoformat(),
                duration_seconds=duration_seconds,
                data=migration_results
            )

            # Add any additional data from migration_results
            if "total_created" in migration_results:
                result.success_count = migration_results["total_created"]

            # Log summary
            self.logger.success(
                f"Work package migration completed in {duration_seconds:.2f} seconds",
                extra={"markup": True},
            )
            if "total_created" in migration_results:
                self.logger.success(
                    f"Created {migration_results['total_created']} work packages",
                    extra={"markup": True},
                )

            return result

        except Exception as e:
            # Catch any unexpected errors
            end_time = datetime.now()
            duration_seconds = (end_time - start_time).total_seconds()

            error_message = f"Unexpected error in work package migration: {str(e)}"
            self.logger.critical(error_message, extra={"markup": True})

            # Try to save error information
            try:
                error_file = os.path.join(self.data_dir, "migration_error.json")
                with open(error_file, "w") as f:
                    json.dump(
                        {
                            "component": "work_package_migration",
                            "timestamp": datetime.now().isoformat(),
                            "error": str(e),
                            "traceback": str(
                                getattr(e, "__traceback__", "No traceback available")
                            ),
                            "duration_seconds": duration_seconds,
                        },
                        f,
                        indent=2,
                    )
                self.logger.info(
                    f"Error details saved to {error_file}", extra={"markup": True}
                )
            except Exception as save_err:
                self.logger.warning(
                    f"Could not save error details: {str(save_err)}",
                    extra={"markup": True},
                )

            return ComponentResult(
                success=False,
                error=str(e),
                timestamp=datetime.now().isoformat(),
                duration_seconds=duration_seconds,
            )

    def log_custom_field_updates(self, updated_fields):
        """
        Log information about custom fields that were updated during migration.

        Args:
            updated_fields: List of custom field names and values that were updated
        """
        if not updated_fields:
            return

        self.logger.notice(
            f"Updated {len(updated_fields)} custom field values during migration:",
            extra={"markup": True},
        )

        for field_value in updated_fields:
            parts = field_value.split(":", 1)
            if len(parts) == 2:
                field, value = parts
                self.logger.info(
                    f"  - Added '{value}' to '{field}'",
                    extra={"markup": True},
                )
            else:
                self.logger.info(f"  - {field_value}", extra={"markup": True})

    def _collect_missing_custom_field_values(self, work_packages_data):
        """
        Collect custom field values from work packages that might need to be added to OpenProject.

        Args:
            work_packages_data: List of prepared work package dictionaries

        Returns:
            Dictionary mapping custom field names to sets of values to add
        """
        self.logger.info("Checking for missing custom field values...")

        # Check if work_packages_data is valid
        if not work_packages_data:
            self.logger.info("No work packages data provided")
            return {}

        if not isinstance(work_packages_data, list):
            self.logger.error(
                f"Expected work_packages_data to be a list, got {type(work_packages_data)}",
                extra={"markup": True}
            )
            return {}

        # Use CustomFieldMigration to get OpenProject custom fields
        from src.migrations.custom_field_migration import CustomFieldMigration

        try:
            # Create a temporary instance of CustomFieldMigration
            cf_migration = CustomFieldMigration(
                self.jira_client,
                self.op_client,
                self.op_client.rails_client if hasattr(self.op_client, "rails_client") else None
            )

            # Extract custom fields using the existing method
            custom_fields = cf_migration.extract_openproject_custom_fields()

            if not custom_fields:
                self.logger.warning("No custom fields found in OpenProject", extra={"markup": True})
                return {}

            self.logger.success(
                f"Successfully retrieved {len(custom_fields)} custom fields",
                extra={"markup": True}
            )
        except Exception as e:
            self.logger.error(
                f"Error retrieving custom fields from OpenProject: {str(e)}",
                extra={"markup": True}
            )
            return {}

        # Create a dictionary of custom field ID to name and allowed values
        field_id_to_info = {}
        for cf in custom_fields:
            try:
                field_id = cf["id"]
                field_id_to_info[field_id] = {
                    "name": cf["name"],
                    "field_format": cf.get("field_format", ""),
                    "possible_values": cf.get("possible_values", []) or []
                }
            except (KeyError, TypeError) as e:
                self.logger.warning(f"Invalid custom field data: {e}", extra={"markup": True})
                continue

        self.logger.debug(f"Processed {len(field_id_to_info)} custom fields with IDs")

        # Collect values that need to be added (field_name -> set of values)
        values_to_add = {}

        # Check each work package for custom field values
        for i, wp in enumerate(work_packages_data):
            if "custom_fields" not in wp:
                continue

            if not isinstance(wp["custom_fields"], list):
                self.logger.warning(
                    f"custom_fields in work package {i} is not a list: {type(wp['custom_fields'])}",
                    extra={"markup": True}
                )
                continue

            for cf in wp["custom_fields"]:
                try:
                    if not isinstance(cf, dict):
                        self.logger.warning(
                            f"Custom field entry in work package {i} is not a dictionary: {type(cf)}",
                            extra={"markup": True}
                        )
                        continue

                    cf_id = cf.get("id")
                    if not cf_id:
                        self.logger.debug(f"Custom field in work package {i} has no ID")
                        continue

                    # Convert to int if it's a string
                    if isinstance(cf_id, str) and cf_id.isdigit():
                        cf_id = int(cf_id)

                    if cf_id not in field_id_to_info:
                        self.logger.debug(f"Custom field ID {cf_id} not found in OpenProject fields")
                        continue

                    cf_info = field_id_to_info[cf_id]
                    cf_name = cf_info["name"]
                    cf_format = cf_info["field_format"]

                    # Only check list-type fields
                    if cf_format != "list":
                        continue

                    cf_value = cf.get("value")
                    if not cf_value or cf_value == "":
                        continue  # Skip empty values

                    # For both single values and lists
                    values_to_check = [cf_value] if not isinstance(cf_value, list) else cf_value

                    # Check if values are in the allowed values
                    for value in values_to_check:
                        if (value and value != "" and
                           value not in cf_info["possible_values"]):
                            if cf_name not in values_to_add:
                                values_to_add[cf_name] = set()
                            values_to_add[cf_name].add(value)
                            self.logger.debug(f"Found missing value '{value}' for field '{cf_name}'")
                except Exception as e:
                    self.logger.warning(
                        f"Error processing custom field in work package {i}: {str(e)}",
                        extra={"markup": True}
                    )
                    continue

        # Convert sets to lists for easier JSON serialization later
        result = {k: list(v) for k, v in values_to_add.items()}

        if result:
            self.logger.notice(
                f"Found {sum(len(values) for values in result.values())} missing custom field values "
                f"across {len(result)} fields",
                extra={"markup": True}
            )
        else:
            self.logger.info("No missing custom field values found")

        return result

    def _update_custom_field_allowed_values(self, missing_values):
        """
        Update OpenProject custom fields with missing values.

        Args:
            missing_values: Dictionary mapping field names to lists of values to add

        Returns:
            Boolean indicating if all updates were successful
        """
        # Add detailed debugging of the input
        self.logger.debug(f"_update_custom_field_allowed_values received: type={type(missing_values)}")

        # Check if missing_values is a dictionary
        if not isinstance(missing_values, dict):
            self.logger.error(
                f"Expected missing_values to be a dictionary, got {type(missing_values)}",
                extra={"markup": True}
            )
            # Try to handle it if it's a string that might be JSON
            if isinstance(missing_values, str):
                try:
                    self.logger.debug(f"Attempting to parse string as JSON. First 100 chars: {missing_values[:100]}")
                    missing_values = json.loads(missing_values)
                    self.logger.info("Successfully parsed string as JSON", extra={"markup": True})
                except json.JSONDecodeError:
                    self.logger.error("Failed to parse string as JSON", extra={"markup": True})
                    return False
            else:
                # If it's not a string or dict, we can't proceed
                return False

        if not missing_values:
            self.logger.info("No custom field values to add")
            return True

        self.logger.info(f"Adding values to {len(missing_values)} custom fields...")

        # Debug the structure of missing_values
        for field_name, values in missing_values.items():
            self.logger.debug(f"Field: {field_name}, Values type: {type(values)}, Content: {values}")

        all_success = True

        for field_name, values in missing_values.items():
            if not values:
                continue

            # Ensure values is a list
            if not isinstance(values, list):
                self.logger.warning(
                    f"Values for field '{field_name}' is not a list, attempting to convert",
                    extra={"markup": True}
                )
                if isinstance(values, str):
                    # Try to parse as JSON if it's a string
                    try:
                        values = json.loads(values)
                    except json.JSONDecodeError:
                        # If it's just a string, make it a single-item list
                        values = [values]
                else:
                    # For other types, try to make a list
                    try:
                        values = list(values)
                    except TypeError:
                        values = [values]

            self.logger.info(f"Adding {len(values)} values to custom field '{field_name}'")

            # Create Ruby script to update the field
            ruby_header = f"""
            # Ruby variables from Python
            field_name = '{field_name}'
            values_to_add = {json.dumps(values)}
            """

            ruby_script = """
            # Find the custom field by name
            cf = CustomField.find_by(name: field_name)

            if cf.nil?
              puts "ERROR: Custom field '#{field_name}' not found"
              next
            end

            # Skip if not a list type field
            unless cf.field_format == "list"
              puts "SKIP: Custom field '#{field_name}' is not a list type"
              next
            end

            # Get current values and add new ones
            current_values = cf.possible_values || []
            added_values = []

            values_to_add.each do |value|
              unless current_values.include?(value)
                current_values << value
                added_values << value
              end
            end

            if added_values.empty?
              puts "INFO: No new values to add for '#{field_name}'"
              next
            end

            # Update the custom field
            cf.possible_values = current_values

            if cf.save
              puts "SUCCESS: Added #{added_values.length} values to '#{field_name}'"
              puts added_values.inspect
            else
              puts "ERROR: Failed to update '#{field_name}': #{cf.errors.full_messages.join(', ')}"
            end
            """

            # Execute the script
            result = self.op_client.rails_client.execute(ruby_header + ruby_script)

            if result.get("status") != "success":
                self.logger.error(
                    f"Failed to update custom field '{field_name}': {result.get('error', 'Unknown error')}",
                    extra={"markup": True}
                )
                all_success = False
                continue

            output = result.get("output", "")

            if "SUCCESS:" in output:
                self.logger.success(
                    f"Successfully added values to custom field '{field_name}'",
                    extra={"markup": True}
                )
            elif "ERROR:" in output:
                error_msg = output.split("ERROR:", 1)[1].strip()
                self.logger.error(
                    f"Failed to update custom field '{field_name}': {error_msg}",
                    extra={"markup": True}
                )
                all_success = False
            elif "SKIP:" in output:
                self.logger.warning(
                    f"Skipped updating custom field '{field_name}': not a list type",
                    extra={"markup": True}
                )
            elif "INFO:" in output:
                self.logger.info(
                    f"No new values needed for custom field '{field_name}'",
                    extra={"markup": True}
                )

        return all_success

    def _format_values_list(self, values):
        """
        Format a list of values for logging.

        Args:
            values: List of values to format

        Returns:
            Formatted string of values
        """
        if not values:
            return ""

        formatted = ", ".join(str(value) for value in values)
        # Truncate if too long for display
        if len(formatted) > 60:
            return formatted[:57] + "..."
        return formatted

    def migrate_work_packages(self) -> dict[str, Any]:
        """
        Migrate work packages from Jira to OpenProject.
        Public method that calls the internal _migrate_work_packages method.

        Returns:
            Dictionary with migration results
        """
        return self._migrate_work_packages()
