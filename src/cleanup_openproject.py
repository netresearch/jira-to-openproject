#!/usr/bin/env python3
"""
OpenProject Cleanup Script

This script removes all existing work packages, projects, and custom fields from an OpenProject instance.
"""

import os
import sys
import argparse
import logging
from typing import List, Dict, Any, Optional, TYPE_CHECKING
import requests
import subprocess
import json
import math

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.clients.openproject_client import OpenProjectClient
from src.clients.openproject_rails_client import OpenProjectRailsClient
from src import config
from src.display import ProgressTracker, console

# Set up logger
logger = config.logger

class OpenProjectCleaner:
    """
    Class to clean up an OpenProject instance by removing specified entities.
    """

    def __init__(
        self,
        entities_to_delete: List[str],
        op_client: Optional[OpenProjectClient] = None,
        rails_client: Optional['OpenProjectRailsClient'] = None,
        dry_run: bool = False
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
        self.rails_client = rails_client or OpenProjectRailsClient() # Use provided rails client
        self.op_client = op_client or OpenProjectClient(rails_client=self.rails_client) # Use provided or create new
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
                logger.error(f"HTTP error while deleting work package {work_package_id}: {e}", extra={"markup": True})
                # If we got an error response with content, log it
                if e.response.content:
                    logger.error(f"Response content: {e.response.content}", extra={"markup": True})
                return False
            except requests.exceptions.RequestException as e:
                logger.error(f"Request error while deleting work package {work_package_id}: {e}", extra={"markup": True})
                return False
        except Exception as e:
            logger.error(f"Failed to delete work package {work_package_id}: {str(e)}", extra={"markup": True})
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
                logger.error(f"HTTP error while deleting project {project_id}: {e}", extra={"markup": True})
                # If we got an error response with content, log it
                if e.response.content:
                    logger.error(f"Response content: {e.response.content}", extra={"markup": True})
                return False
            except requests.exceptions.RequestException as e:
                logger.error(f"Request error while deleting project {project_id}: {e}", extra={"markup": True})
                return False
        except Exception as e:
            logger.error(f"Failed to delete project {project_id}: {str(e)}", extra={"markup": True})
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
            logger.debug(f"DRY RUN: Would delete custom field {custom_field_id}", extra={"markup": True})
            return True

        # Check if this is a test field ID (from the dry-run lookup)
        if isinstance(custom_field_id, str) and custom_field_id.startswith('customField'):
            logger.info(f"Skipping test custom field ID: {custom_field_id}", extra={"markup": True})
            return True

        # Ensure Rails console is available and connected
        if not self._ensure_rails_client():
            logger.error("Cannot delete custom field: Rails client not available or connected.")
            return False

        # Use Rails console to delete the custom field and return JSON status
        command = f"CustomField.find_by(id: {custom_field_id}).destroy"

        result = self.rails_client.execute(command)

        if result['status'] == 'success':
            logger.success(f"Successfully deleted custom field {custom_field_id}", extra={"markup": True})
            return True
        else:
            # Error during command execution itself (e.g., tmux issue)
            error_msg = result.get('error', 'Unknown execution error')
            logger.error(f"Error executing Rails command for custom field {custom_field_id}: {error_msg}", extra={"markup": True})
            return False

    def get_all_work_packages(self) -> List[Dict[str, Any]]:
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

            logger.info("Starting work package retrieval with pagination...", extra={"markup": True})

            # Keep fetching until we get a page with no results
            while True:
                logger.info(f"Fetching work packages - page {page}", extra={"markup": True})

                # Try standard REST parameters first
                params = {
                    "page": page,
                    "per_page": page_size
                }

                response = self.op_client._request("GET", "/work_packages", params=params)

                # Get work packages from response
                work_packages = response.get("_embedded", {}).get("elements", [])
                logger.info(f"Retrieved {len(work_packages)} work packages on page {page}", extra={"markup": True})

                # If first page, log the total count
                if page == 1 and "total" in response:
                    total = response.get("total", 0)
                    logger.info(f"Found {total} work packages in total", extra={"markup": True})

                # Stop if we got zero results
                if len(work_packages) == 0:
                    logger.info("No more work packages to fetch (received zero)", extra={"markup": True})
                    break

                # Add to our collection
                all_work_packages.extend(work_packages)

                # Move to next page
                page += 1

                # Safety limit to prevent infinite loops
                if page > 50:
                    logger.warning("Reached safety limit of 50 pages, stopping pagination", extra={"markup": True})
                    break

            logger.success(f"Successfully retrieved {len(all_work_packages)} work packages in total", extra={"markup": True})
            return all_work_packages
        except Exception as e:
            logger.error(f"Failed to get work packages: {str(e)}", extra={"markup": True})
            return []

    def get_all_projects(self) -> List[Dict[str, Any]]:
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
                params = {
                    "pageSize": page_size,
                    "offset": (page - 1) * page_size
                }

                response = self.op_client._request("GET", "/projects", params=params)

                # Update total pages on first request
                if page == 1 and "total" in response:
                    total_items = response.get("total", 0)
                    logger.info(f"Found {total_items} projects in total", extra={"markup": True})

                # Get projects from response
                projects = response.get("_embedded", {}).get("elements", [])
                logger.info(f"Retrieved {len(projects)} projects on page {page}", extra={"markup": True})

                # Add to our collection
                all_projects.extend(projects)

                # Check if we have more data
                if len(projects) < page_size:
                    has_more_data = False
                    logger.info("No more projects to fetch", extra={"markup": True})
                else:
                    page += 1

            logger.success(f"Successfully retrieved {len(all_projects)} projects in total", extra={"markup": True})
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

        # Get all work packages
        work_packages = self.get_all_work_packages()

        if not work_packages:
            logger.info("No work packages found", extra={"markup": True})
            return 0

        logger.info(f"Found {len(work_packages)} work packages to delete", extra={"markup": True})

        # Delete work packages with progress bar
        deleted_count = 0
        with ProgressTracker("Deleting work packages", len(work_packages), "Recent Work Packages") as tracker:
            for wp in work_packages:
                wp_id = wp.get("id")
                wp_subject = wp.get("subject", "Unknown")

                if self.dry_run:
                    logger.debug(f"DRY RUN: Would delete work package {wp_id}: {wp_subject}", extra={"markup": True})
                    success = True
                else:
                    success = self.delete_work_package(wp_id)

                if success:
                    deleted_count += 1
                    tracker.add_log_item(f"Deleted: {wp_subject} (ID: {wp_id})")
                else:
                    tracker.add_log_item(f"Failed: {wp_subject} (ID: {wp_id})")

                tracker.increment(1, f"Deleting work packages ({deleted_count}/{len(work_packages)})")

        logger.success(f"Successfully deleted {deleted_count} out of {len(work_packages)} work packages", extra={"markup": True})
        return deleted_count

    def cleanup_projects(self) -> int:
        """
        Remove all projects from OpenProject.

        Returns:
            Number of projects deleted
        """
        logger.info("Starting project cleanup...", extra={"markup": True})

        # Get all projects with pagination
        projects = self.get_all_projects()

        if not projects:
            logger.info("No projects found", extra={"markup": True})
            return 0

        logger.info(f"Found {len(projects)} projects to delete", extra={"markup": True})

        # Delete projects with progress bar
        deleted_count = 0
        with ProgressTracker("Deleting projects", len(projects), "Recent Projects") as tracker:
            for project in projects:
                project_id = project.get("id")
                project_name = project.get("name", "Unknown")

                if self.dry_run:
                    logger.debug(f"DRY RUN: Would delete project {project_id}: {project_name}", extra={"markup": True})
                    success = True
                else:
                    success = self.delete_project(project_id)

                if success:
                    deleted_count += 1
                    tracker.add_log_item(f"Deleted: {project_name} (ID: {project_id})")
                else:
                    tracker.add_log_item(f"Failed: {project_name} (ID: {project_id})")

                tracker.increment(1, f"Deleting projects ({deleted_count}/{len(projects)})")

        logger.success(f"Successfully deleted {deleted_count} out of {len(projects)} projects", extra={"markup": True})
        return deleted_count

    def cleanup_custom_fields(self) -> int:
        """
        Remove all custom fields from OpenProject.

        Returns:
            Number of custom fields deleted
        """
        logger.info("Starting custom field cleanup...", extra={"markup": True})

        # Get all custom fields
        custom_fields = self.op_client.get_custom_fields()

        if not custom_fields:
            logger.info("No custom fields found", extra={"markup": True})
            return 0

        logger.info(f"Found {len(custom_fields)} custom fields to delete", extra={"markup": True})

        # Delete custom fields with progress bar
        deleted_count = 0
        with ProgressTracker("Deleting custom fields", len(custom_fields), "Recent Custom Fields") as tracker:
            for field in custom_fields:
                field_id = field.get("id")
                field_name = field.get("name", "Unknown")

                if self.dry_run:
                    logger.debug(f"DRY RUN: Would delete custom field {field_id}: {field_name}", extra={"markup": True})
                    success = True
                else:
                    success = self.delete_custom_field(field_id)

                if success:
                    deleted_count += 1
                    tracker.add_log_item(f"Deleted: {field_name} (ID: {field_id})")
                else:
                    tracker.add_log_item(f"Failed: {field_name} (ID: {field_id})")

                tracker.increment(1, f"Deleting custom fields ({deleted_count}/{len(custom_fields)})")

        logger.success(f"Successfully deleted {deleted_count} out of {len(custom_fields)} custom fields", extra={"markup": True})
        return deleted_count

    # Placeholder for user cleanup
    def get_all_users(self) -> List[Dict[str, Any]]:
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
        logger.warning(f"User deletion for ID {user_id} not yet implemented.", extra={"markup": True})
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
            logger.info("No users found or retrieval not implemented.", extra={"markup": True})
            return 0

        logger.info(f"Found {len(users)} users to potentially delete", extra={"markup": True})

        # Placeholder logic
        deleted_count = 0
        with ProgressTracker("Deleting users", len(users), "Recent Users") as tracker:
            for user in users:
                user_id = user.get("id", "N/A")
                user_name = user.get("name", "Unknown") # Adjust attribute based on actual data

                if self.dry_run:
                    logger.debug(f"DRY RUN: Would delete user {user_id}: {user_name}", extra={"markup": True})
                    success = True # Assume success in dry run
                else:
                    # Add check to prevent deleting essential users (e.g., admin)
                    # success = self.delete_user(user_id) # Uncomment when implemented
                    logger.warning(f"Skipping deletion of user {user_id} (implementation pending).", extra={"markup": True})
                    success = False # Mark as failed until implemented

                if success:
                    deleted_count += 1
                    tracker.add_log_item(f"Deleted: {user_name} (ID: {user_id})")
                else:
                     tracker.add_log_item(f"Failed/Skipped: {user_name} (ID: {user_id})")

                tracker.increment(1, f"Processing users ({tracker.completed}/{len(users)})")

        logger.success(f"Processed {len(users)} users. Successfully deleted: {deleted_count}", extra={"markup": True})
        return deleted_count

    def run_cleanup(self) -> Dict[str, int]:
        """
        Run the complete cleanup process for specified entities.

        Returns:
            Dictionary with cleanup statistics
        """
        logger.info("Starting OpenProject cleanup...", extra={"markup": True})

        if self.dry_run:
            logger.warning("DRY RUN MODE: No actual changes will be made", extra={"markup": True})

        results = {
            "work_packages_deleted": 0,
            "projects_deleted": 0,
            "custom_fields_deleted": 0,
            "users_deleted": 0
        }

        # Define the order of operations (e.g., delete WPs before Projects)
        cleanup_order = ['wp', 'cf', 'p', 'u'] # Custom fields before projects? Users last?

        for entity_type in cleanup_order:
            if entity_type in self.entities:
                if entity_type == 'wp':
                    results["work_packages_deleted"] = self.cleanup_work_packages()
                elif entity_type == 'p':
                    results["projects_deleted"] = self.cleanup_projects()
                elif entity_type == 'cf':
                    results["custom_fields_deleted"] = self.cleanup_custom_fields()
                elif entity_type == 'u':
                    results["users_deleted"] = self.cleanup_users() # Call the new method

        logger.success("OpenProject cleanup completed for specified entities", extra={"markup": True})
        logger.info("Summary:", extra={"markup": True})
        if 'wp' in self.entities:
            logger.info(f"  Work packages deleted: {results['work_packages_deleted']}", extra={"markup": True})
        if 'p' in self.entities:
            logger.info(f"  Projects deleted: {results['projects_deleted']}", extra={"markup": True})
        if 'cf' in self.entities:
            logger.info(f"  Custom fields deleted: {results['custom_fields_deleted']}", extra={"markup": True})
        if 'u' in self.entities:
             logger.info(f"  Users deleted: {results['users_deleted']}", extra={"markup": True})


        return results


def main():
    """
    Main function to run the cleanup script.
    """
    parser = argparse.ArgumentParser(description="Clean up OpenProject by removing specified entities. Defaults to all entities if none are specified.")
    parser.add_argument("--dry-run", action="store_true", help="Run in dry-run mode (no actual changes)")

    entity_choices = ['u', 'users', 'wp', 'work_packages', 'p', 'projects', 'cf', 'custom_fields']
    short_entity_map = {
        'users': 'u',
        'work_packages': 'wp',
        'projects': 'p',
        'custom_fields': 'cf',
        # Short codes map to themselves
        'u': 'u',
        'wp': 'wp',
        'p': 'p',
        'cf': 'cf'
    }
    default_entities = ['u', 'wp', 'p', 'cf'] # Internal representation

    parser.add_argument(
        "entities",
        nargs='*', # Optional
        choices=entity_choices,
        # Default is handled after parsing to ensure normalization
        # default=default_entities, # Cannot directly use default with normalization easily
        help="Specify the entities to delete using short codes (u, wp, p, cf) or full names (users, work_packages, projects, custom_fields). Defaults to all if none are specified."
    )
    args = parser.parse_args()

    # Normalize provided entities to short codes and handle default
    if not args.entities: # No entities provided, use default
        entities_to_delete = default_entities
    else:
        # Use a set to handle duplicates automatically
        normalized_entities = {short_entity_map.get(e) for e in args.entities}
        # Filter out None in case of unexpected input, though choices should prevent this
        entities_to_delete = [e for e in normalized_entities if e]

    try:
        cleaner = OpenProjectCleaner(
            entities_to_delete=entities_to_delete,
            dry_run=args.dry_run
        )
        result = cleaner.run_cleanup()

        # Log summary (already done in run_cleanup, but keep a final message)
        logger.info("Cleanup process finished.", extra={"markup": True})
        if args.dry_run:
            logger.warning("This was a dry run. No actual changes were made.", extra={"markup": True})
    except Exception as e:
        logger.error(f"Error during cleanup: {str(e)}", extra={"markup": True})
        sys.exit(1)


if __name__ == "__main__":
    main()
