#!/usr/bin/env python3
"""OpenProject Cleanup Script.

This script removes all existing work packages, projects, and custom fields from an OpenProject instance.
"""

import argparse
import sys
from typing import TYPE_CHECKING, TypeVar

from src import config
from src.clients.openproject_client import OpenProjectClient
from src.display import configure_logging

if TYPE_CHECKING:
    from pathlib import Path

# Set up logger
logger = configure_logging("INFO", None)

# Define type variable for ProgressTracker
T = TypeVar("T")


class OpenProjectCleaner:
    """Class to clean up an OpenProject instance by removing specified entities."""

    def __init__(
        self,
        entities_to_delete: list[str],
        *,
        dry_run: bool = False,
    ) -> None:
        """Initialize the OpenProject cleaner.

        Args:
            entities_to_delete: List of entity types to delete (e.g., ["work_packages", "projects"])
            op_client: Initialized OpenProject client
            dry_run: If True, simulate deletion without making changes

        """
        self.entities = entities_to_delete
        self.dry_run = dry_run

        self.op_client = OpenProjectClient()
        self.data_dir: Path = config.get_path("data")

    def cleanup_work_packages(self) -> int:
        """Remove all work packages from OpenProject.

        Returns:
            Number of work packages deleted

        """
        logger.info("Starting work package cleanup...")

        count = self.op_client.count_records("WorkPackage")
        if count <= 0:
            logger.info("No work packages found to delete")
            return 0

        if self.dry_run:
            logger.info("DRY RUN: Would delete %d work packages", count)
            return count

        return self.op_client.delete_all_work_packages()

    def cleanup_projects(self) -> int:
        """Remove all projects from OpenProject.

        Returns:
            Number of projects deleted

        """
        logger.info("Starting project cleanup...")

        count = self.op_client.count_records("Project")
        logger.debug("Count: %s", count)
        if count <= 0:
            logger.info("No projects found to delete")
            return 0

        if self.dry_run:
            logger.info("DRY RUN: Would delete %d projects", count)
            return count

        return self.op_client.delete_all_projects()

    def cleanup_custom_fields(self) -> int:
        """Remove custom fields from OpenProject using bulk operation.

        Returns:
            Number of custom fields deleted

        """
        logger.info("Starting custom field cleanup...")

        count = self.op_client.count_records("CustomField")
        if count <= 0:
            logger.info("No custom fields found to delete")
            return 0

        if self.dry_run:
            logger.info("DRY RUN: Would delete %d custom fields", count)
            return count

        return self.op_client.delete_all_custom_fields()

    def cleanup_issue_types(self) -> int:
        """Clean up issue types (work package types) in OpenProject.

        Preserve default ones.

        Returns:
            Number of issue types deleted

        """
        logger.info("Starting issue type cleanup...")

        count = self.op_client.count_records("Type")
        if count <= 1:
            logger.info("No issue types found to delete")
            return 0

        if self.dry_run:
            logger.info("DRY RUN: Would delete %d issue types", count)
            return count

        return self.op_client.delete_non_default_issue_types()

    def cleanup_issue_statuses(self) -> int:
        """Clean up issue statuses in OpenProject.

        Preserve default ones.

        Returns:
            Number of issue statuses deleted

        """
        logger.info("Starting issue status cleanup...")

        count = self.op_client.count_records("Status")
        if count <= 1:
            logger.info("No issue statuses found to delete")
            return 0

        if self.dry_run:
            logger.info("DRY RUN: Would delete %d issue statuses", count)
            return count

        return self.op_client.delete_non_default_issue_statuses()

    def cleanup_users(self) -> int:
        """Remove users from OpenProject (placeholder, not implemented).

        Users cannot be bulk deleted in OpenProject for safety reasons.

        Returns:
            Number of users deleted

        """
        logger.info("Starting user cleanup...")

        count = self.op_client.count_records("User")
        if count <= 0:
            logger.info("No users found to delete")
            return 0

        if self.dry_run:
            logger.info("DRY RUN: Would delete %d users", count)
            return count

        return 0

    def run_cleanup(self) -> dict[str, int]:  # noqa: C901, PLR0912
        """Run the complete cleanup process for specified entities.

        Returns:
            Dictionary with cleanup statistics

        """
        logger.info("Starting OpenProject cleanup...")

        if self.dry_run:
            logger.warning("DRY RUN MODE: No actual changes will be made")

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
                try:
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
                        results["issue_statuses_deleted"] = (
                            self.cleanup_issue_statuses()
                        )
                except Exception:
                    logger.exception("Error during cleanup of %s", entity_type)

        logger.success(
            "OpenProject cleanup completed for specified entities",
        )
        logger.info("Summary:")
        if "wp" in self.entities:
            logger.info("  Work packages deleted: %s", results["work_packages_deleted"])
        if "p" in self.entities:
            logger.info("  Projects deleted: %s", results["projects_deleted"])
        if "cf" in self.entities:
            logger.info("  Custom fields deleted: %s", results["custom_fields_deleted"])
        if "u" in self.entities:
            logger.info("  Users deleted: %s", results["users_deleted"])
        if "it" in self.entities:
            logger.info("  Issue types deleted: %s", results["issue_types_deleted"])
        if "is" in self.entities:
            logger.info("  Issue statuses deleted: %s", results["issue_statuses_deleted"])
        if "il" in self.entities:
            logger.info(
                "  Issue link types deleted: %s", results["issue_link_types_deleted"],
            )

        return results


def main() -> None:
    """Run the cleanup script."""
    parser = argparse.ArgumentParser(
        description="Clean up OpenProject by removing specified entities. "
        "Defaults to all entities if none are specified.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode (no actual changes)",
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
        help="Specify the entities to delete using short codes or full names. Available entities: users (u),"
        "work_packages (wp), projects (p), custom_fields (cf), issue_types (it), issue_statuses (is), "
        "issue_link_types (il). Defaults to all if none are specified.",
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
            entities_to_delete=entities_to_delete,
            dry_run=args.dry_run,
        )
        cleaner.run_cleanup()

        # Log summary (already done in run_cleanup, but keep a final message)
        logger.info("Cleanup process finished.")
        if args.dry_run:
            logger.warning("This was a dry run. No actual changes were made.")
    except Exception:
        logger.exception("Error during cleanup")
        sys.exit(1)


if __name__ == "__main__":
    main()
