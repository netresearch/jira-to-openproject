#!/usr/bin/env python3
"""
OpenProject Cleanup Script

This script removes all existing work packages, projects, and custom fields from an OpenProject instance.
"""

import argparse
import json
import sys
from typing import Any

import requests
from src import config
from src.clients.openproject_client import OpenProjectClient
from src.clients.openproject_rails_client import OpenProjectRailsClient
from src.display import ProgressTracker

# Set up logger
logger = config.logger


class OpenProjectCleaner:
    """
    Class to clean up an OpenProject instance by removing specified entities.
    """

    def __init__(
        self,
        entities_to_delete: list[str],
        op_client: OpenProjectClient | None = None,
        rails_client: OpenProjectRailsClient | None = None,
        dry_run: bool = False,
    ) -> None:
        """
        Initialize the OpenProject cleaner.

        Args:
            entities_to_delete: List of entity types to delete (e.g., ["work_packages", "projects"])
            op_client: Initialized OpenProject client
            rails_client: Initialized OpenProjectRailsClient instance (optional)
            dry_run: If True, simulate deletion without making changes
        """
        self.entities = entities_to_delete
        self.dry_run = dry_run
        self.rails_client = (
            rails_client or OpenProjectRailsClient()
        )  # Use provided rails client
        self.op_client = op_client or OpenProjectClient(
            rails_client=self.rails_client
        )  # Use provided or create new
        self.data_dir = config.get_path("data")

        # Ensure clients are connected
        if not self.op_client.connected:
            self.op_client.connect()
        # Note: Rails client connection is checked when needed, assuming it's managed externally

    def _ensure_rails_client(self) -> bool:
        """
        Check if the Rails client is available and connected.
        Logs an error if not available.

        Returns:
            True if the client is available and connected, False otherwise.
        """
        if not self.rails_client:
            logger.error("Rails console client was not provided during initialization.")
            return False
        if not self.rails_client.connected:
            logger.error("Provided Rails console client is not connected.")
            # Consider adding a connection attempt here if needed
            return False
        return True

    def delete_work_package(self, work_package_id: int) -> bool:
        """
        Delete a work package from OpenProject.

        Args:
            work_package_id: The ID of the work package to delete

        Returns:
            True if successful, False otherwise
        """
        try:
            if self.dry_run:
                return True

            try:
                self.op_client._request("DELETE", f"/work_packages/{work_package_id}")
                return True
            except requests.exceptions.HTTPError as e:
                logger.error(
                    f"HTTP error while deleting work package {work_package_id}: {e}",
                    extra={"markup": True},
                )
                # If we got an error response with content, log it
                if e.response.content:
                    logger.error(
                        f"Response content: {e.response.content}",
                        extra={"markup": True},
                    )
                return False
            except requests.exceptions.RequestException as e:
                logger.error(
                    f"Request error while deleting work package {work_package_id}: {e}",
                    extra={"markup": True},
                )
                return False
        except Exception as e:
            logger.error(
                f"Failed to delete work package {work_package_id}: {str(e)}",
                extra={"markup": True},
            )
            return False

    def delete_project(self, project_id: int) -> bool:
        """
        Delete a project from OpenProject.

        Args:
            project_id: The ID of the project to delete

        Returns:
            True if successful, False otherwise
        """
        try:
            if self.dry_run:
                return True

            try:
                self.op_client._request("DELETE", f"/projects/{project_id}")

                return True
            except requests.exceptions.HTTPError as e:
                logger.error(
                    f"HTTP error while deleting project {project_id}: {e}",
                    extra={"markup": True},
                )
                # If we got an error response with content, log it
                if e.response.content:
                    logger.error(
                        f"Response content: {e.response.content}",
                        extra={"markup": True},
                    )
                return False
            except requests.exceptions.RequestException as e:
                logger.error(
                    f"Request error while deleting project {project_id}: {e}",
                    extra={"markup": True},
                )
                return False
        except Exception as e:
            logger.error(
                f"Failed to delete project {project_id}: {str(e)}",
                extra={"markup": True},
            )
            return False

    def delete_custom_field(self, custom_field_id: int) -> bool:
        """
        Delete a custom field from OpenProject using Rails console.

        Args:
            custom_field_id: The ID of the custom field to delete

        Returns:
            True if successful or not found, False otherwise
        """
        if self.dry_run:
            logger.debug(
                f"DRY RUN: Would delete custom field {custom_field_id}",
                extra={"markup": True},
            )
            return True

        # Check if this is a test field ID (from the dry-run lookup)
        if isinstance(custom_field_id, str) and custom_field_id.startswith(
            "customField"
        ):
            logger.info(
                f"Skipping test custom field ID: {custom_field_id}",
                extra={"markup": True},
            )
            return True

        # Ensure Rails console is available and connected
        if not self._ensure_rails_client():
            logger.error(
                "Cannot delete custom field: Rails client not available or connected."
            )
            return False

        # Use Rails console to delete the custom field and return JSON status
        command = f"CustomField.find_by(id: {custom_field_id}).destroy"

        result = self.rails_client.execute(command)

        if result["status"] == "success":
            logger.success(
                f"Successfully deleted custom field {custom_field_id}",
                extra={"markup": True},
            )
            return True
        else:
            # Error during command execution itself (e.g., tmux issue)
            error_msg = result.get("error", "Unknown execution error")
            logger.error(
                f"Error executing Rails command for custom field {custom_field_id}: {error_msg}",
                extra={"markup": True},
            )
            return False

    def bulk_delete_work_packages(self) -> int:
        """
        Delete all work packages from OpenProject using Rails console.

        Returns:
            Number of work packages deleted
        """
        logger.info(
            "Starting bulk work package deletion via Rails console...",
            extra={"markup": True},
        )

        if not self._ensure_rails_client():
            logger.error(
                "Cannot bulk delete work packages: Rails client not available or connected."
            )
            return 0

        if self.dry_run:
            logger.info(
                "DRY RUN: Would delete all work packages via Rails console",
                extra={"markup": True},
            )
            # Get count for reporting
            result = self.rails_client.execute("WorkPackage.count")
            if result["status"] == "success":
                count = int(result.get("output", "0").strip())
                logger.info(
                    f"DRY RUN: Would delete {count} work packages",
                    extra={"markup": True},
                )
                return count
            return 0

        # First, get the count of work packages for reporting
        count_result = self.rails_client.execute("WorkPackage.count")
        if count_result["status"] != "success":
            logger.error("Failed to get work package count", extra={"markup": True})
            return 0

        wp_count = int(count_result.get("output", "0").strip())

        # Now delete all work packages
        delete_result = self.rails_client.execute("WorkPackage.delete_all")

        if delete_result["status"] == "success":
            logger.success(
                f"Successfully deleted all {wp_count} work packages",
                extra={"markup": True},
            )
            return wp_count
        else:
            error_msg = delete_result.get("error", "Unknown execution error")
            logger.error(
                f"Error executing Rails command for bulk work package deletion: {error_msg}",
                extra={"markup": True},
            )
            return 0

    def bulk_delete_projects(self) -> int:
        """
        Delete all projects from OpenProject using Rails console.

        Returns:
            Number of projects deleted
        """
        logger.info(
            "Starting bulk project deletion via Rails console...",
            extra={"markup": True},
        )

        if not self._ensure_rails_client():
            logger.error(
                "Cannot bulk delete projects: Rails client not available or connected."
            )
            return 0

        if self.dry_run:
            logger.info(
                "DRY RUN: Would delete all projects via Rails console",
                extra={"markup": True},
            )
            # Get count for reporting
            result = self.rails_client.execute("Project.count")
            if result["status"] == "success":
                count = int(result.get("output", "0").strip())
                logger.info(
                    f"DRY RUN: Would delete {count} projects", extra={"markup": True}
                )
                return count
            return 0

        # First, get the count of projects for reporting
        count_result = self.rails_client.execute("Project.count")
        if count_result["status"] != "success":
            logger.error("Failed to get project count", extra={"markup": True})
            return 0

        project_count = int(count_result.get("output", "0").strip())

        # Now delete all projects
        delete_result = self.rails_client.execute("Project.delete_all")

        if delete_result["status"] == "success":
            logger.success(
                f"Successfully deleted all {project_count} projects",
                extra={"markup": True},
            )
            return project_count
        else:
            error_msg = delete_result.get("error", "Unknown execution error")
            logger.error(
                f"Error executing Rails command for bulk project deletion: {error_msg}",
                extra={"markup": True},
            )
            return 0

    def cleanup_issue_types(self) -> int:
        """
        Clean up issue types (work package types) in OpenProject,
        preserving default ones.

        Returns:
            Number of issue types deleted
        """
        logger.info("Starting issue type cleanup...", extra={"markup": True})

        if not self._ensure_rails_client():
            logger.error(
                "Cannot clean up issue types: Rails client not available or connected."
            )
            return 0

        # Get all non-default type IDs
        # First, let's get just the IDs to avoid format issues
        command = """
        Type.where(is_default: false, is_standard: false).pluck(:id).to_json
        """

        result = self.rails_client.execute(command)

        if result["status"] != "success":
            logger.error("Failed to get issue types", extra={"markup": True})
            return 0

        try:
            type_ids = json.loads(result.get("output", "[]").strip())

            if not type_ids:
                logger.info(
                    "No non-default issue types found to delete", extra={"markup": True}
                )
                return 0

            logger.info(
                f"Found {len(type_ids)} non-default issue types to delete",
                extra={"markup": True},
            )

            if self.dry_run:
                logger.info(
                    "DRY RUN: Would delete non-default issue types",
                    extra={"markup": True},
                )
                for type_id in type_ids:
                    logger.info(
                        f"DRY RUN: Would delete issue type with ID: {type_id}",
                        extra={"markup": True},
                    )
                return len(type_ids)

            # Use bulk deletion for efficiency if there are many types
            if len(type_ids) > 20:
                logger.info(
                    f"Using bulk deletion for {len(type_ids)} issue types",
                    extra={"markup": True},
                )
                delete_cmd = """
                Type.where(is_default: false, is_standard: false).destroy_all
                """
                delete_result = self.rails_client.execute(delete_cmd)

                if delete_result["status"] == "success":
                    logger.success(
                        f"Successfully deleted all {len(type_ids)} non-default issue types",
                        extra={"markup": True},
                    )
                    return len(type_ids)
                else:
                    error_msg = delete_result.get("error", "Unknown execution error")
                    logger.error(
                        f"Error executing bulk deletion of issue types: {error_msg}",
                        extra={"markup": True},
                    )
                    return 0

            # For smaller numbers, delete individually to track progress
            deleted_count = 0
            with ProgressTracker(
                "Deleting issue types", len(type_ids), "Recent Issue Types"
            ) as tracker:
                for type_id in type_ids:
                    # Ensure type_id is properly formatted as an integer
                    try:
                        type_id_int = int(type_id)
                        # Use direct integer syntax without brackets
                        delete_cmd = f"Type.find({type_id_int}).destroy"
                        delete_result = self.rails_client.execute(delete_cmd)

                        success = delete_result["status"] == "success"

                        if success:
                            deleted_count += 1
                            tracker.add_log_item(f"Deleted: Type ID {type_id}")
                        else:
                            error_msg = delete_result.get(
                                "error", "Unknown execution error"
                            )
                            logger.error(
                                f"Error deleting issue type {type_id}: {error_msg}",
                                extra={"markup": True},
                            )
                            tracker.add_log_item(f"Failed: Type ID {type_id}")
                    except (ValueError, TypeError) as e:
                        logger.error(
                            f"Invalid type ID format: {type_id} - {str(e)}",
                            extra={"markup": True},
                        )
                        tracker.add_log_item(f"Failed (invalid ID format): {type_id}")

                    tracker.increment(
                        1, f"Deleting issue types ({deleted_count}/{len(type_ids)})"
                    )

            logger.success(
                f"Successfully deleted {deleted_count} out of {len(type_ids)} issue types",
                extra={"markup": True},
            )
            return deleted_count

        except json.JSONDecodeError:
            logger.error(
                "Failed to parse issue types from Rails output", extra={"markup": True}
            )
            return 0

    def cleanup_issue_statuses(self) -> int:
        """
        Clean up issue statuses in OpenProject,
        preserving default ones.

        Returns:
            Number of issue statuses deleted
        """
        logger.info("Starting issue status cleanup...", extra={"markup": True})

        if not self._ensure_rails_client():
            logger.error(
                "Cannot clean up issue statuses: Rails client not available or connected."
            )
            return 0

        # Get all non-default status IDs
        # We'll preserve statuses that are marked as defaults or required for workflows
        command = """
        Status.where(is_default: false).pluck(:id).to_json
        """

        result = self.rails_client.execute(command)

        if result["status"] != "success":
            logger.error("Failed to get issue statuses", extra={"markup": True})
            return 0

        try:
            status_ids = json.loads(result.get("output", "[]").strip())

            if not status_ids:
                logger.info(
                    "No non-default issue statuses found to delete",
                    extra={"markup": True},
                )
                return 0

            logger.info(
                f"Found {len(status_ids)} non-default issue statuses to delete",
                extra={"markup": True},
            )

            if self.dry_run:
                logger.info(
                    "DRY RUN: Would delete non-default issue statuses",
                    extra={"markup": True},
                )
                for status_id in status_ids:
                    logger.info(
                        f"DRY RUN: Would delete issue status with ID: {status_id}",
                        extra={"markup": True},
                    )
                return len(status_ids)

            # Use bulk deletion for efficiency if there are many statuses
            if len(status_ids) > 20:
                logger.info(
                    f"Using bulk deletion for {len(status_ids)} issue statuses",
                    extra={"markup": True},
                )
                delete_cmd = """
                Status.where(is_default: false).destroy_all
                """
                delete_result = self.rails_client.execute(delete_cmd)

                if delete_result["status"] == "success":
                    logger.success(
                        f"Successfully deleted all {len(status_ids)} non-default issue statuses",
                        extra={"markup": True},
                    )
                    return len(status_ids)
                else:
                    error_msg = delete_result.get("error", "Unknown execution error")
                    logger.error(
                        f"Error executing bulk deletion of issue statuses: {error_msg}",
                        extra={"markup": True},
                    )
                    return 0

            # For smaller numbers, delete individually to track progress
            deleted_count = 0
            with ProgressTracker(
                "Deleting issue statuses", len(status_ids), "Recent Issue Statuses"
            ) as tracker:
                for status_id in status_ids:
                    # Ensure status_id is properly formatted as an integer
                    try:
                        status_id_int = int(status_id)
                        delete_cmd = f"Status.find({status_id_int}).destroy"
                        delete_result = self.rails_client.execute(delete_cmd)

                        success = delete_result["status"] == "success"

                        if success:
                            deleted_count += 1
                            tracker.add_log_item(f"Deleted: Status ID {status_id}")
                        else:
                            error_msg = delete_result.get(
                                "error", "Unknown execution error"
                            )
                            logger.error(
                                f"Error deleting issue status {status_id}: {error_msg}",
                                extra={"markup": True},
                            )
                            tracker.add_log_item(f"Failed: Status ID {status_id}")
                    except (ValueError, TypeError) as e:
                        logger.error(
                            f"Invalid status ID format: {status_id} - {str(e)}",
                            extra={"markup": True},
                        )
                        tracker.add_log_item(f"Failed (invalid ID format): {status_id}")

                    tracker.increment(
                        1,
                        f"Deleting issue statuses ({deleted_count}/{len(status_ids)})",
                    )

            logger.success(
                f"Successfully deleted {deleted_count} out of {len(status_ids)} issue statuses",
                extra={"markup": True},
            )
            return deleted_count

        except json.JSONDecodeError:
            logger.error(
                "Failed to parse issue statuses from Rails output",
                extra={"markup": True},
            )
            return 0

    def cleanup_issue_link_types(self) -> int:
        """
        Clean up issue link types (relation types) in OpenProject,
        preserving default ones.

        Returns:
            Number of issue link types deleted
        """
        logger.info("Starting issue link type cleanup...", extra={"markup": True})

        if not self._ensure_rails_client():
            logger.error(
                "Cannot clean up issue link types: Rails client not available or connected."
            )
            return 0

        # First, check if TypedRelation exists in the system
        check_cmd = """
        begin
          TypedRelation.count
          true
        rescue NameError
          false
        end
        """
        check_result = self.rails_client.execute(check_cmd)

        if (
            check_result["status"] != "success"
            or check_result.get("output", "").strip().lower() != "true"
        ):
            logger.warning(
                "TypedRelation model not found in this OpenProject instance. Skipping relation type cleanup.",
                extra={"markup": True},
            )
            return 0

        # Use a simpler approach - get all relation types not defined in core
        # This checks for custom relation types
        command = """
        # Get all relation types that aren't default
        custom_types = []
        default_types = Relation::TYPES.keys.map(&:to_s)

        # Find TypedRelation records where name is not in default types
        TypedRelation.all.each do |rel|
          if !default_types.include?(rel.name) && !default_types.include?(rel.reverse_name)
            custom_types << {id: rel.id, name: rel.name, reverse_name: rel.reverse_name}
          end
        end

        JSON.dump(custom_types)
        """

        result = self.rails_client.execute(command)

        if result["status"] != "success":
            logger.error("Failed to get relation types", extra={"markup": True})
            return 0

        try:
            custom_link_types = json.loads(result.get("output", "[]").strip())

            if not custom_link_types:
                logger.info(
                    "No custom issue link types found to delete", extra={"markup": True}
                )
                return 0

            logger.info(
                f"Found {len(custom_link_types)} custom issue link types to delete",
                extra={"markup": True},
            )

            if self.dry_run:
                logger.info(
                    "DRY RUN: Would delete custom issue link types",
                    extra={"markup": True},
                )
                for link_type in custom_link_types:
                    link_id = link_type.get("id")
                    link_name = link_type.get("name")
                    reverse_name = link_type.get("reverse_name")
                    logger.info(
                        f"DRY RUN: Would delete issue link type: {link_name}/{reverse_name} (ID: {link_id})",
                        extra={"markup": True},
                    )
                return len(custom_link_types)

            deleted_count = 0
            with ProgressTracker(
                "Deleting issue link types",
                len(custom_link_types),
                "Recent Issue Link Types",
            ) as tracker:
                for link_type in custom_link_types:
                    link_id = link_type.get("id")
                    link_name = link_type.get("name", "Unknown")
                    reverse_name = link_type.get("reverse_name", "Unknown")

                    # Ensure link_id is properly formatted as an integer
                    try:
                        link_id_int = int(link_id)
                        delete_cmd = f"TypedRelation.find({link_id_int}).destroy"
                        delete_result = self.rails_client.execute(delete_cmd)

                        success = delete_result["status"] == "success"

                        if success:
                            deleted_count += 1
                            tracker.add_log_item(
                                f"Deleted: {link_name}/{reverse_name} (ID: {link_id})"
                            )
                        else:
                            error_msg = delete_result.get(
                                "error", "Unknown execution error"
                            )
                            logger.error(
                                f"Error deleting issue link type {link_id}: {error_msg}",
                                extra={"markup": True},
                            )
                            tracker.add_log_item(
                                f"Failed: {link_name}/{reverse_name} (ID: {link_id})"
                            )
                    except (ValueError, TypeError) as e:
                        logger.error(
                            f"Invalid link type ID format: {link_id} - {str(e)}",
                            extra={"markup": True},
                        )
                        tracker.add_log_item(f"Failed (invalid ID format): {link_id}")

                    tracker.increment(
                        1,
                        f"Deleting issue link types ({deleted_count}/{len(custom_link_types)})",
                    )

            logger.success(
                f"Successfully deleted {deleted_count} out of {len(custom_link_types)} issue link types",
                extra={"markup": True},
            )
            return deleted_count

        except json.JSONDecodeError:
            logger.error(
                "Failed to parse issue link types from Rails output",
                extra={"markup": True},
            )
            return 0

    def get_all_work_packages(self) -> list[dict[str, Any]]:
        """
        Get all work packages from OpenProject.

        Returns:
            List of work package dictionaries
        """
        try:
            # Get work packages with pagination
            all_work_packages = []
            page = 1
            page_size = 100

            logger.info(
                "Starting work package retrieval with pagination...",
                extra={"markup": True},
            )

            # Keep fetching until we get a page with no results
            while True:
                logger.info(
                    f"Fetching work packages - page {page}", extra={"markup": True}
                )

                # Try standard REST parameters first
                params = {"page": page, "per_page": page_size}

                response = self.op_client._request(
                    "GET", "/work_packages", params=params
                )

                # Get work packages from response
                work_packages = response.get("_embedded", {}).get("elements", [])
                logger.info(
                    f"Retrieved {len(work_packages)} work packages on page {page}",
                    extra={"markup": True},
                )

                # If first page, log the total count
                if page == 1 and "total" in response:
                    total = response.get("total", 0)
                    logger.info(
                        f"Found {total} work packages in total", extra={"markup": True}
                    )

                # Stop if we got zero results
                if len(work_packages) == 0:
                    logger.info(
                        "No more work packages to fetch (received zero)",
                        extra={"markup": True},
                    )
                    break

                # Add to our collection
                all_work_packages.extend(work_packages)

                # Move to next page
                page += 1

                # Safety limit to prevent infinite loops
                if page > 50:
                    logger.warning(
                        "Reached safety limit of 50 pages, stopping pagination",
                        extra={"markup": True},
                    )
                    break

            logger.success(
                f"Successfully retrieved {len(all_work_packages)} work packages in total",
                extra={"markup": True},
            )
            return all_work_packages
        except Exception as e:
            logger.error(
                f"Failed to get work packages: {str(e)}", extra={"markup": True}
            )
            return []

    def get_all_projects(self) -> list[dict[str, Any]]:
        """
        Get all projects from OpenProject with pagination.

        Returns:
            List of project dictionaries
        """
        try:
            # Get projects with pagination
            all_projects = []
            page = 1
            page_size = 1000
            has_more_data = True

            while has_more_data:
                logger.info(f"Fetching projects - page {page}", extra={"markup": True})
                params = {"pageSize": page_size, "offset": (page - 1) * page_size}

                response = self.op_client._request("GET", "/projects", params=params)

                # Update total pages on first request
                if page == 1 and "total" in response:
                    total_items = response.get("total", 0)
                    logger.info(
                        f"Found {total_items} projects in total", extra={"markup": True}
                    )

                # Get projects from response
                projects = response.get("_embedded", {}).get("elements", [])
                logger.info(
                    f"Retrieved {len(projects)} projects on page {page}",
                    extra={"markup": True},
                )

                # Add to our collection
                all_projects.extend(projects)

                # Check if we have more data
                if len(projects) < page_size:
                    has_more_data = False
                    logger.info("No more projects to fetch", extra={"markup": True})
                else:
                    page += 1

            logger.success(
                f"Successfully retrieved {len(all_projects)} projects in total",
                extra={"markup": True},
            )
            return all_projects
        except Exception as e:
            logger.error(f"Failed to get projects: {str(e)}", extra={"markup": True})
            return []

    def cleanup_work_packages(self) -> int:
        """
        Remove all work packages from OpenProject.

        Returns:
            Number of work packages deleted
        """
        logger.info("Starting work package cleanup...", extra={"markup": True})

        # Use bulk deletion via Rails console instead of API calls
        return self.bulk_delete_work_packages()

    def cleanup_projects(self) -> int:
        """
        Remove all projects from OpenProject.

        Returns:
            Number of projects deleted
        """
        logger.info("Starting project cleanup...", extra={"markup": True})

        # Use bulk deletion via Rails console instead of API calls
        return self.bulk_delete_projects()

    def cleanup_custom_fields(self) -> int:
        """
        Remove custom fields from OpenProject using bulk Rails console command.

        Returns:
            Number of custom fields deleted
        """
        logger.info(
            "Starting custom field cleanup via Rails console...", extra={"markup": True}
        )

        if not self._ensure_rails_client():
            logger.error(
                "Cannot bulk delete custom fields: Rails client not available or connected."
            )
            return 0

        if self.dry_run:
            logger.info(
                "DRY RUN: Would delete custom fields via Rails console",
                extra={"markup": True},
            )
            # Get count for reporting
            result = self.rails_client.execute("CustomField.count")
            if result["status"] == "success":
                count = int(result.get("output", "0").strip())
                logger.info(
                    f"DRY RUN: Would delete {count} custom fields",
                    extra={"markup": True},
                )
                return count
            return 0

        # First, get the count of custom fields for reporting
        count_result = self.rails_client.execute("CustomField.count")
        if count_result["status"] != "success":
            logger.error("Failed to get custom field count", extra={"markup": True})
            return 0

        cf_count = int(count_result.get("output", "0").strip())

        if cf_count == 0:
            logger.info("No custom fields found to delete", extra={"markup": True})
            return 0

        logger.info(f"Found {cf_count} custom fields to delete", extra={"markup": True})

        # Now delete all custom fields
        # Use destroy_all instead of delete_all to ensure proper cleanup of dependencies
        delete_result = self.rails_client.execute("CustomField.destroy_all")

        if delete_result["status"] == "success":
            logger.success(
                f"Successfully deleted all {cf_count} custom fields",
                extra={"markup": True},
            )
            return cf_count
        else:
            error_msg = delete_result.get("error", "Unknown execution error")
            logger.error(
                f"Error executing Rails command for bulk custom field deletion: {error_msg}",
                extra={"markup": True},
            )
            return 0

    # Placeholder for user cleanup
    def get_all_users(self) -> list[dict[str, Any]]:
        """
        Get all users from OpenProject. (Placeholder)

        Returns:
            List of user dictionaries
        """
        logger.warning("User retrieval not yet implemented.", extra={"markup": True})
        # TODO: Implement user retrieval logic using API or Rails
        return []

    def delete_user(self, user_id: int) -> bool:
        """
        Delete a user from OpenProject. (Placeholder)

        Args:
            user_id: The ID of the user to delete

        Returns:
            True if successful, False otherwise
        """
        logger.warning(
            f"User deletion for ID {user_id} not yet implemented.",
            extra={"markup": True},
        )
        # TODO: Implement user deletion logic, potentially using Rails console
        # Need to handle protected users like admin
        return False

    def cleanup_users(self) -> int:
        """
        Remove all users from OpenProject. (Placeholder)

        Returns:
            Number of users deleted
        """
        logger.info("Starting user cleanup...", extra={"markup": True})
        users = self.get_all_users()

        if not users:
            logger.info(
                "No users found or retrieval not implemented.", extra={"markup": True}
            )
            return 0

        logger.info(
            f"Found {len(users)} users to potentially delete", extra={"markup": True}
        )

        # Placeholder logic
        deleted_count = 0
        with ProgressTracker("Deleting users", len(users), "Recent Users") as tracker:
            for user in users:
                user_id = user.get("id", "N/A")
                user_name = user.get(
                    "name", "Unknown"
                )  # Adjust attribute based on actual data

                if self.dry_run:
                    logger.debug(
                        f"DRY RUN: Would delete user {user_id}: {user_name}",
                        extra={"markup": True},
                    )
                    success = True  # Assume success in dry run
                else:
                    # Add check to prevent deleting essential users (e.g., admin)
                    # success = self.delete_user(user_id) # Uncomment when implemented
                    logger.warning(
                        f"Skipping deletion of user {user_id} (implementation pending).",
                        extra={"markup": True},
                    )
                    success = False  # Mark as failed until implemented

                if success:
                    deleted_count += 1
                    tracker.add_log_item(f"Deleted: {user_name} (ID: {user_id})")
                else:
                    tracker.add_log_item(f"Failed/Skipped: {user_name} (ID: {user_id})")

                tracker.increment(
                    1, f"Processing users ({tracker.completed}/{len(users)})"
                )

        logger.success(
            f"Processed {len(users)} users. Successfully deleted: {deleted_count}",
            extra={"markup": True},
        )
        return deleted_count

    def run_cleanup(self) -> dict[str, int]:
        """
        Run the complete cleanup process for specified entities.

        Returns:
            Dictionary with cleanup statistics
        """
        logger.info("Starting OpenProject cleanup...", extra={"markup": True})

        if self.dry_run:
            logger.warning(
                "DRY RUN MODE: No actual changes will be made", extra={"markup": True}
            )

        results = {
            "work_packages_deleted": 0,
            "projects_deleted": 0,
            "custom_fields_deleted": 0,
            "users_deleted": 0,
            "issue_types_deleted": 0,
            "issue_statuses_deleted": 0,
            "issue_link_types_deleted": 0,
        }

        # Define the order of operations
        # Order is important: work packages must be deleted before projects
        # Custom fields, issue types, statuses should be cleaned after work packages
        cleanup_order = ["wp", "cf", "it", "is", "il", "p", "u"]

        for entity_type in cleanup_order:
            if entity_type in self.entities:
                if entity_type == "wp":
                    results["work_packages_deleted"] = self.cleanup_work_packages()
                elif entity_type == "p":
                    results["projects_deleted"] = self.cleanup_projects()
                elif entity_type == "cf":
                    results["custom_fields_deleted"] = self.cleanup_custom_fields()
                elif entity_type == "u":
                    results["users_deleted"] = self.cleanup_users()
                elif entity_type == "it":
                    results["issue_types_deleted"] = self.cleanup_issue_types()
                elif entity_type == "is":
                    results["issue_statuses_deleted"] = self.cleanup_issue_statuses()
                elif entity_type == "il":
                    results["issue_link_types_deleted"] = (
                        self.cleanup_issue_link_types()
                    )

        logger.success(
            "OpenProject cleanup completed for specified entities",
            extra={"markup": True},
        )
        logger.info("Summary:", extra={"markup": True})
        if "wp" in self.entities:
            logger.info(
                f"  Work packages deleted: {results['work_packages_deleted']}",
                extra={"markup": True},
            )
        if "p" in self.entities:
            logger.info(
                f"  Projects deleted: {results['projects_deleted']}",
                extra={"markup": True},
            )
        if "cf" in self.entities:
            logger.info(
                f"  Custom fields deleted: {results['custom_fields_deleted']}",
                extra={"markup": True},
            )
        if "u" in self.entities:
            logger.info(
                f"  Users deleted: {results['users_deleted']}", extra={"markup": True}
            )
        if "it" in self.entities:
            logger.info(
                f"  Issue types deleted: {results['issue_types_deleted']}",
                extra={"markup": True},
            )
        if "is" in self.entities:
            logger.info(
                f"  Issue statuses deleted: {results['issue_statuses_deleted']}",
                extra={"markup": True},
            )
        if "il" in self.entities:
            logger.info(
                f"  Issue link types deleted: {results['issue_link_types_deleted']}",
                extra={"markup": True},
            )

        return results


def main():
    """
    Main function to run the cleanup script.
    """
    parser = argparse.ArgumentParser(
        description="Clean up OpenProject by removing specified entities. Defaults to all entities if none are specified."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Run in dry-run mode (no actual changes)"
    )

    entity_choices = [
        "u",
        "users",
        "wp",
        "work_packages",
        "p",
        "projects",
        "cf",
        "custom_fields",
        "it",
        "issue_types",
        "is",
        "issue_statuses",
        "il",
        "issue_link_types",
    ]

    short_entity_map = {
        "users": "u",
        "work_packages": "wp",
        "projects": "p",
        "custom_fields": "cf",
        "issue_types": "it",
        "issue_statuses": "is",
        "issue_link_types": "il",
        # Short codes map to themselves
        "u": "u",
        "wp": "wp",
        "p": "p",
        "cf": "cf",
        "it": "it",
        "is": "is",
        "il": "il",
    }
    default_entities = [
        "u",
        "wp",
        "p",
        "cf",
        "it",
        "is",
        "il",
    ]  # Internal representation

    parser.add_argument(
        "entities",
        nargs="*",  # Optional
        choices=entity_choices,
        # Default is handled after parsing to ensure normalization
        # default=default_entities, # Cannot directly use default with normalization easily
        help="Specify the entities to delete using short codes or full names. Available entities: users (u), work_packages (wp), projects (p), custom_fields (cf), issue_types (it), issue_statuses (is), issue_link_types (il). Defaults to all if none are specified.",
    )
    args = parser.parse_args()

    # Normalize provided entities to short codes and handle default
    if not args.entities:  # No entities provided, use default
        entities_to_delete = default_entities
    else:
        # Use a set to handle duplicates automatically
        normalized_entities = {short_entity_map.get(e) for e in args.entities}
        # Filter out None in case of unexpected input, though choices should prevent this
        entities_to_delete = [e for e in normalized_entities if e]

    try:
        cleaner = OpenProjectCleaner(
            entities_to_delete=entities_to_delete, dry_run=args.dry_run
        )
        cleaner.run_cleanup()

        # Log summary (already done in run_cleanup, but keep a final message)
        logger.info("Cleanup process finished.", extra={"markup": True})
        if args.dry_run:
            logger.warning(
                "This was a dry run. No actual changes were made.",
                extra={"markup": True},
            )
    except Exception as e:
        logger.error(f"Error during cleanup: {str(e)}", extra={"markup": True})
        sys.exit(1)


if __name__ == "__main__":
    main()
