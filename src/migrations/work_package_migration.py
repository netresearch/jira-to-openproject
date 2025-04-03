"""
Work package migration module for Jira to OpenProject migration.
Handles the migration of Jira issues to OpenProject work packages.
"""

import os
import sys
import json
import re
from typing import Dict, List, Any, Optional

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.models.mapping import JiraToOPMapping
from src import config
from src.display import ProgressTracker, console

# Get logger from config
logger = config.logger


class WorkPackageMigration:
    """
    Handles the migration of issues from Jira to work packages in OpenProject.

    This class is responsible for:
    1. Extracting issues from Jira projects
    2. Creating corresponding work packages in OpenProject
    3. Mapping issues between the systems
    4. Handling attachments, comments, and relationships
    """

    def __init__(self, dry_run: bool = False):
        """
        Initialize the work package migration tools.

        Args:
            dry_run: If True, no changes will be made to OpenProject
        """
        self.jira_client = JiraClient()
        self.op_client = OpenProjectClient()
        self.dry_run = dry_run

        # Use the centralized config for var directories
        self.data_dir = config.get_path("data")

        # Initialize mappings
        self.project_mapping = {}
        self.type_mapping = {}
        self.status_mapping = {}
        self.user_mapping = {}
        self.work_package_mapping = {}

    def load_mappings(self) -> bool:
        """
        Load required mappings for the migration.

        Returns:
            True if all required mappings were loaded successfully, False otherwise
        """
        logger.info("Loading required mappings...", extra={"markup": True})

        # Load project mapping
        try:
            with open(os.path.join(self.data_dir, "project_mapping.json"), "r") as f:
                self.project_mapping = json.load(f)
            logger.notice(f"Loaded project mapping with {len(self.project_mapping)} projects", extra={"markup": True})
        except Exception as e:
            logger.error(f"Failed to load project mapping: {str(e)}", extra={"markup": True})
            return False

        # Load type mapping
        try:
            with open(os.path.join(self.data_dir, "issue_type_mapping.json"), "r") as f:
                type_mapping_data = json.load(f)

                # Convert to format needed for migration (Jira ID -> OpenProject ID)
                self.type_mapping = {}
                for jira_type, mapping in type_mapping_data.items():
                    if "jira_id" in mapping and "openproject_id" in mapping:
                        self.type_mapping[mapping["jira_id"]] = mapping["openproject_id"]

            logger.notice(f"Loaded type mapping with {len(self.type_mapping)} types", extra={"markup": True})
        except Exception as e:
            logger.error(f"Failed to load type mapping: {str(e)}", extra={"markup": True})
            return False

        # Load status mapping
        status_mapping_path = os.path.join(self.data_dir, "status_mapping.json")
        try:
            # Create an empty status mapping file if it doesn't exist
            if not os.path.exists(status_mapping_path):
                logger.warning("Status mapping file not found, creating an empty one", extra={"markup": True})
                with open(status_mapping_path, "w") as f:
                    json.dump({}, f)
                self.status_mapping = {}
            else:
                with open(status_mapping_path, "r") as f:
                    status_mapping_data = json.load(f)

                    # Convert to format needed for migration (Jira ID -> OpenProject ID)
                    self.status_mapping = {}
                    for jira_status, mapping in status_mapping_data.items():
                        if "jira_id" in mapping and "openproject_id" in mapping:
                            self.status_mapping[mapping["jira_id"]] = mapping["openproject_id"]

                logger.notice(f"Loaded status mapping with {len(self.status_mapping)} statuses", extra={"markup": True})
        except Exception as e:
            logger.warning(f"Failed to load status mapping: {str(e)}", extra={"markup": True})
            logger.warning("Continuing without status mapping - default status will be used for work packages", extra={"markup": True})
            self.status_mapping = {}

        # Load user mapping
        try:
            with open(os.path.join(self.data_dir, "user_mapping.json"), "r") as f:
                user_mapping_data = json.load(f)

                # Convert to format needed for migration (Jira username -> OpenProject ID)
                self.user_mapping = {}
                for jira_user, mapping in user_mapping_data.items():
                    if "openproject_id" in mapping:
                        self.user_mapping[jira_user] = mapping["openproject_id"]

            logger.notice(f"Loaded user mapping with {len(self.user_mapping)} users", extra={"markup": True})
        except Exception as e:
            logger.warning(f"Failed to load user mapping: {str(e)}", extra={"markup": True})
            logger.warning("Continuing without user mapping - users will not be assigned to work packages", extra={"markup": True})

        return True

    def extract_jira_issues(self, project_key: str, batch_size: int = 100, project_tracker: ProgressTracker = None) -> List[Dict[str, Any]]:
        """
        Extract issues from a Jira project.

        Args:
            project_key: The key of the Jira project to extract issues from
            batch_size: Number of issues to retrieve in each batch
            project_tracker: Optional parent progress tracker to update

        Returns:
            List of Jira issue dictionaries
        """
        logger.info(f"Extracting issues from Jira project {project_key}...", extra={"markup": True})

        # Connect to Jira
        if not self.jira_client.connect():
            logger.error("Failed to connect to Jira", extra={"markup": True})
            return []

        try:
            # First, get the total number of issues for this project to set up progress bar
            total_issues = self.jira_client.get_issue_count(project_key)
            if total_issues <= 0:
                logger.warning(f"No issues found for project {project_key}", extra={"markup": True})
                return []

            logger.info(f"Found {total_issues} issues to extract from project {project_key}", extra={"markup": True})

            # Get issues in batches with progress tracking
            all_issues = []
            start_at = 0

            # If we have a parent tracker, update its description
            if project_tracker:
                project_tracker.update_description(f"Fetching issues from {project_key} (0/{total_issues})")
                current_batch = 0

                # Using the parent tracker instead of creating a new one
                while start_at < total_issues:
                    # Update progress description
                    current_batch += 1
                    progress_desc = f"Fetching {project_key} issues {start_at+1}-{min(start_at+batch_size, total_issues)}/{total_issues}"
                    project_tracker.update_description(progress_desc)

                    # Fetch a batch of issues
                    issues = self.jira_client.get_issues(
                        project_key, start_at=start_at, max_results=batch_size
                    )

                    if not issues:
                        break

                    # Add to overall list
                    all_issues.extend(issues)

                    # Update trackers
                    retrieved_count = len(issues)
                    project_tracker.add_log_item(f"Retrieved {retrieved_count} issues from {project_key} (batch #{current_batch})")

                    # Only log at the NOTICE level for large projects with multiple batches
                    if total_issues > batch_size:
                        logger.notice(f"Retrieved {retrieved_count} issues (total: {len(all_issues)}/{total_issues})", extra={"markup": True})

                    if len(issues) < batch_size:
                        # We got fewer issues than requested, so we're done
                        break

                    start_at += batch_size
            else:
                # Create a new progress tracker when not running inside another one
                with ProgressTracker(f"Fetching issues from {project_key}", total_issues, "Recent Batches") as tracker:
                    while start_at < total_issues:
                        # Update progress description
                        tracker.update_description(f"Fetching {project_key} issues {start_at+1}-{min(start_at+batch_size, total_issues)}/{total_issues}")

                        # Fetch a batch of issues
                        issues = self.jira_client.get_issues(
                            project_key, start_at=start_at, max_results=batch_size
                        )

                        if not issues:
                            break

                        # Add to overall list
                        all_issues.extend(issues)

                        # Update trackers
                        retrieved_count = len(issues)
                        tracker.add_log_item(f"Retrieved {retrieved_count} issues (batch #{start_at//batch_size+1})")
                        tracker.increment(retrieved_count)

                        # Only log at the NOTICE level for large projects with multiple batches
                        if total_issues > batch_size:
                            logger.notice(f"Retrieved {retrieved_count} issues (total: {len(all_issues)}/{total_issues})", extra={"markup": True})

                        if len(issues) < batch_size:
                            # We got fewer issues than requested, so we're done
                            break

                        start_at += batch_size

            # Save issues to file for later reference
            self._save_to_json(all_issues, f"jira_issues_{project_key}.json")

            logger.info(f"Extracted {len(all_issues)} issues from project {project_key}", extra={"markup": True})
            return all_issues

        except Exception as e:
            logger.error(f"Failed to extract issues from project {project_key}: {str(e)}", extra={"markup": True})
            return []

    def create_work_package(self, jira_issue: Dict[str, Any], project_id: int) -> Optional[Dict[str, Any]]:
        """
        Create a work package in OpenProject based on a Jira issue.

        Args:
            jira_issue: The Jira issue dictionary
            project_id: The ID of the OpenProject project to create the work package in

        Returns:
            The created OpenProject work package or None if creation failed
        """
        # Map the Jira issue to an OpenProject work package
        issue_type_id = jira_issue["issue_type"]["id"]
        type_id = self.type_mapping.get(issue_type_id)

        status_id = None
        if "status" in jira_issue and "id" in jira_issue["status"]:
            status_id = self.status_mapping.get(jira_issue["status"]["id"])

        # Get assignee if available
        assigned_to_id = None
        if "assignee" in jira_issue and jira_issue["assignee"]:
            assignee_name = jira_issue["assignee"].get("name")
            if assignee_name in self.user_mapping:
                assigned_to_id = self.user_mapping[assignee_name]

        # Create the work package
        subject = jira_issue["summary"]
        description = jira_issue.get("description", "")

        # Add Jira issue key to description for reference
        jira_reference = f"\n\n*Imported from Jira issue: {jira_issue['key']}*"
        if description:
            description += jira_reference
        else:
            description = jira_reference

        logger.notice(f"Creating work package in OpenProject: '{subject}' (from Jira issue {jira_issue['key']})", extra={"markup": True})

        if self.dry_run:
            logger.notice(f"DRY RUN: Would create work package: {subject}", extra={"markup": True})
            # Return a placeholder for dry run
            return {
                "id": None,
                "subject": subject,
                "jira_key": jira_issue["key"],
                "jira_id": jira_issue["id"]
            }

        # Create the work package in OpenProject
        try:
            wp = self.op_client.create_work_package(
                project_id=project_id,
                type_id=type_id,
                subject=subject,
                description=description,
                status_id=status_id,
                assigned_to_id=assigned_to_id
            )

            if wp and "id" in wp:
                logger.info(f"Successfully created work package: {subject} (ID: {wp['id']})", extra={"markup": True})

                # Add to mapping
                self.work_package_mapping[jira_issue["id"]] = {
                    "jira_id": jira_issue["id"],
                    "jira_key": jira_issue["key"],
                    "openproject_id": wp["id"],
                    "subject": subject
                }

                return wp
            else:
                logger.error(f"Failed to create work package: {subject}", extra={"markup": True})
                return None

        except Exception as e:
            logger.error(f"Error creating work package {subject}: {str(e)}", extra={"markup": True})
            return None

    def migrate_work_packages(self) -> Dict[str, Any]:
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
        logger.info("Starting work package migration...", extra={"markup": True})

        # Load mappings
        self.load_mappings()

        # Get list of Jira projects to process
        jira_projects = list(set(entry.get("jira_key") for entry in self.project_mapping.values() if entry.get("jira_key")))

        if not jira_projects:
            logger.warning("No Jira projects found in mapping, nothing to migrate", extra={"markup": True})
            return {}

        logger.info(f"Found {len(jira_projects)} Jira projects to process", extra={"markup": True})

        # Initialize counters
        total_issues = 0
        total_created = 0

        # Process each project
        with ProgressTracker("Migrating projects", len(jira_projects), "Recent Projects") as project_tracker:
            for project_key in jira_projects:
                project_tracker.update_description(f"Processing project {project_key}")
                logger.notice(f"Processing project {project_key}", extra={"markup": True})

                # Find corresponding OpenProject project ID
                project_mapping_entry = None
                for key, entry in self.project_mapping.items():
                    if entry.get("jira_key") == project_key and entry.get("openproject_id"):
                        project_mapping_entry = entry
                        break

                if not project_mapping_entry:
                    logger.warning(f"No OpenProject project mapping found for Jira project {project_key}, skipping", extra={"markup": True})
                    project_tracker.add_log_item(f"Skipped: {project_key} (no mapping)")
                    project_tracker.increment()
                    continue

                op_project_id = project_mapping_entry["openproject_id"]

                # Extract issues for this project
                issues = self.extract_jira_issues(project_key, project_tracker=project_tracker)
                total_issues += len(issues)

                if not issues:
                    logger.warning(f"No issues found for project {project_key}, skipping", extra={"markup": True})
                    project_tracker.add_log_item(f"Skipped: {project_key} (no issues)")
                    project_tracker.increment()
                    continue

                # Create work packages for each issue
                created_count = 0

                # Process issues without nested progress tracker
                logger.notice(f"Processing {len(issues)} issues for project {project_key}", extra={"markup": True})
                for i, issue in enumerate(issues):
                    issue_key = issue.get("key", "Unknown")
                    if i % 10 == 0 or i == len(issues) - 1:  # Log progress every 10 issues
                        project_tracker.update_description(f"Processing issue {issue_key} ({i+1}/{len(issues)})")

                    work_package = self.create_work_package(issue, op_project_id)
                    if work_package:
                        created_count += 1
                        if created_count % 10 == 0 or created_count == len(issues):  # Log every 10 created
                            project_tracker.add_log_item(f"Created: {created_count}/{len(issues)} for {project_key}")

                logger.success(f"Created {created_count} work packages for project {project_key}", extra={"markup": True})
                project_tracker.add_log_item(f"Completed: {project_key} ({created_count}/{len(issues)} issues)")
                project_tracker.increment()
                total_created += created_count

        # Save the work package mapping
        self._save_to_json(self.work_package_mapping, "work_package_mapping.json")

        logger.success(f"Work package migration completed", extra={"markup": True})
        logger.info(f"Total issues processed: {total_issues}", extra={"markup": True})
        logger.info(f"Total work packages created: {total_created}", extra={"markup": True})

        return self.work_package_mapping

    def analyze_work_package_mapping(self) -> Dict[str, Any]:
        """
        Analyze the work package mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        logger.info("Analyzing work package mapping...", extra={"markup": True})

        if not self.work_package_mapping:
            try:
                with open(os.path.join(self.data_dir, "work_package_mapping.json"), "r") as f:
                    self.work_package_mapping = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load work package mapping: {str(e)}", extra={"markup": True})
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

        # Check for failed work package creations
        failed_creations = []
        for wp_id, wp_data in self.work_package_mapping.items():
            if not wp_data.get("openproject_id"):
                failed_creations.append(wp_data.get("jira_key", wp_id))

        if failed_creations:
            potential_issues.append(
                {
                    "issue": "failed_creations",
                    "description": f"{len(failed_creations)} work packages failed to be created",
                    "affected_items": failed_creations[:10],  # Limit to first 10
                    "count": len(failed_creations),
                }
            )

        # Prepare analysis results
        return {
            "status": "success",
            "work_packages_count": total_issues,
            "projects_migrated": len(projects_count),
            "work_packages_by_project": projects_count,
            "potential_issues": potential_issues,
        }

    def _save_to_json(self, data: Any, filename: str):
        """
        Save data to a JSON file in the data directory.

        Args:
            data: The data to save
            filename: The name of the file to save to
        """
        filepath = os.path.join(self.data_dir, filename)
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        logger.debug(f"Saved data to {filepath}", extra={"markup": True})


def run_work_package_migration(dry_run: bool = False, force: bool = False, direct_migration: bool = False):
    """
    Run the work package migration.

    Args:
        dry_run: If True, no changes will be made to OpenProject
        force: If True, force extraction of data even if files exist
        direct_migration: If True, use direct Rails console execution for components that support it

    Returns:
        Dictionary with migration results
    """
    logger.info(f"Starting work package migration (dry_run={dry_run}, force={force})", extra={"markup": True})

    migration = WorkPackageMigration(dry_run=dry_run)
    mapping = migration.migrate_work_packages()
    analysis = migration.analyze_work_package_mapping()

    logger.success(f"Work package migration completed", extra={"markup": True})
    return analysis


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run work package migration")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode (no changes to OpenProject)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force extraction of data even if files exist",
    )
    parser.add_argument(
        "--direct-migration",
        action="store_true",
        help="Use direct Rails console execution for components that support it",
    )
    args = parser.parse_args()

    run_work_package_migration(dry_run=args.dry_run, force=args.force, direct_migration=args.direct_migration)
