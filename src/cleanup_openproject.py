#!/usr/bin/env python3
"""
OpenProject Cleanup Script

This script removes all existing work packages, projects, and custom fields from an OpenProject instance.
"""

import argparse
import sys
from typing import Any, Dict, TypeVar

from src import config
from src.clients.openproject_client import OpenProjectClient

# Set up logger
logger = config.logger

# Define type variable for ProgressTracker
T = TypeVar('T')


class OpenProjectCleaner:
    """
    Class to clean up an OpenProject instance by removing specified entities.
    """

    def __init__(
        self,
        entities_to_delete: list[str],
        op_client: OpenProjectClient | None = None,
        dry_run: bool = False,
    ) -> None:
        """
        Initialize the OpenProject cleaner.

        Args:
            entities_to_delete: List of entity types to delete (e.g., ["work_packages", "projects"])
            op_client: Initialized OpenProject client
            dry_run: If True, simulate deletion without making changes
        """
        self.entities = entities_to_delete
        self.dry_run = dry_run

        try:
            self.op_client = op_client or OpenProjectClient()
            self.data_dir = config.get_path("data")
        except Exception as e:
            logger.error(
                f"Error initializing OpenProject client: {str(e)}",
                extra={"markup": True},
            )
            self.op_client = None

    def _ensure_client(self) -> bool:
        """
        Check if the OpenProject client is available.
        Logs an error if not available.

        Returns:
            True if the client is available, False otherwise.
        """
        if not self.op_client:
            logger.error(
                "OpenProject client was not provided during initialization or failed to initialize.",
                extra={"markup": True},
            )
            return False

        # Try a basic command that should always work if connection is good
        try:
            # Use a simple method that doesn't make API calls if possible
            if hasattr(self.op_client, 'is_connected'):
                return self.op_client.is_connected()

            # Otherwise try a simple count operation
            self.op_client.count_records("Project")
            return True
        except Exception as e:
            # Log the specific exception for debugging
            logger.error(
                f"OpenProject client connection failed: {str(e)}",
                extra={"markup": True},
            )
            return False

    def _count_records_direct(self, model: str) -> int:
        """
        Count records for a model using direct Rails console access.
        This avoids the file transfer issues in dry-run mode.

        Args:
            model: Model name (e.g., "WorkPackage", "Project")

        Returns:
            Number of records or -1 if error
        """
        if not self._ensure_client():
            return -1

        try:
            # Execute count command directly in Rails console
            command = f"{model}.count"
            result = self.op_client.rails_client.execute(command)

            if result.get("status") == "success":
                output = result.get("output", "")
                if isinstance(output, str):
                    # Try to extract numeric count from the output
                    # Remove any Rails console prompts or whitespace
                    output = output.strip()
                    # Extract the first number we can find
                    import re
                    numbers = re.findall(r'\d+', output)
                    if numbers:
                        return int(numbers[0])
                else:
                    # If it's already a number, just return it
                    try:
                        return int(output)
                    except (ValueError, TypeError):
                        pass

            # If we couldn't get a valid count
            return -1

        except Exception as e:
            logger.error(f"Error counting {model} records: {str(e)}", extra={"markup": True})
            return -1

    def cleanup_work_packages(self) -> int:
        """
        Remove all work packages from OpenProject.

        Returns:
            Number of work packages deleted
        """
        logger.info("Starting work package cleanup...", extra={"markup": True})

        if not self._ensure_client():
            logger.error(
                "Cannot delete work packages: OpenProject client not available.",
                extra={"markup": True},
            )
            return 0

        if self.dry_run:
            logger.info(
                "DRY RUN: Would delete all work packages",
                extra={"markup": True},
            )
            # Get count for reporting using direct method
            wp_count = self._count_records_direct("WorkPackage")
            logger.info(
                f"DRY RUN: Would delete {wp_count} work packages",
                extra={"markup": True},
            )
            return wp_count

        # Get count before deletion
        try:
            wp_count_before = self._count_records_direct("WorkPackage")

            if wp_count_before <= 0:
                logger.info(
                    "No work packages found to delete",
                    extra={"markup": True}
                )
                return 0

            # Execute deletion command directly using Rails console client
            command = "WorkPackage.delete_all; puts 'SUCCESS'"
            result = self.op_client.rails_client.execute(command)

            success = result.get("status") == "success"

            if success:
                # Get count after deletion to determine actual number deleted
                wp_count_after = self._count_records_direct("WorkPackage")
                actual_deleted = max(0, wp_count_before - wp_count_after)

                logger.success(
                    f"Successfully deleted {actual_deleted} work packages",
                    extra={"markup": True}
                )
                return actual_deleted
            else:
                logger.error(
                    f"Failed to delete work packages: {result.get('error', 'Unknown error')}",
                    extra={"markup": True},
                )
                return 0

        except Exception as e:
            logger.error(
                f"Failed to delete work packages: {str(e)}",
                extra={"markup": True},
            )
            return 0

    def cleanup_projects(self) -> int:
        """
        Remove all projects from OpenProject.

        Returns:
            Number of projects deleted
        """
        logger.info("Starting project cleanup...", extra={"markup": True})

        if not self._ensure_client():
            logger.error(
                "Cannot delete projects: OpenProject client not available.",
                extra={"markup": True},
            )
            return 0

        if self.dry_run:
            logger.info(
                "DRY RUN: Would delete all projects",
                extra={"markup": True},
            )
            # Get count for reporting using direct method
            project_count = self._count_records_direct("Project")
            logger.info(
                f"DRY RUN: Would delete {project_count} projects",
                extra={"markup": True}
            )
            return project_count

        # Get count before deletion
        try:
            project_count_before = self._count_records_direct("Project")

            if project_count_before <= 0:
                logger.info(
                    "No projects found to delete",
                    extra={"markup": True}
                )
                return 0

            # Execute deletion command directly using Rails console client
            command = "Project.delete_all; puts 'SUCCESS'"
            result = self.op_client.rails_client.execute(command)

            success = result.get("status") == "success"

            if success:
                # Get count after deletion to determine actual number deleted
                project_count_after = self._count_records_direct("Project")
                actual_deleted = max(0, project_count_before - project_count_after)

                logger.success(
                    f"Successfully deleted {actual_deleted} projects",
                    extra={"markup": True}
                )
                return actual_deleted
            else:
                logger.error(
                    f"Failed to delete projects: {result.get('error', 'Unknown error')}",
                    extra={"markup": True},
                )
                return 0

        except Exception as e:
            logger.error(
                f"Failed to delete projects: {str(e)}",
                extra={"markup": True},
            )
            return 0

    def cleanup_custom_fields(self) -> int:
        """
        Remove custom fields from OpenProject using bulk operation.

        Returns:
            Number of custom fields deleted
        """
        logger.info(
            "Starting custom field cleanup...", extra={"markup": True}
        )

        if not self._ensure_client():
            logger.error(
                "Cannot delete custom fields: OpenProject client not available.",
                extra={"markup": True},
            )
            return 0

        if self.dry_run:
            logger.info(
                "DRY RUN: Would delete all custom fields",
                extra={"markup": True},
            )
            # Get count for reporting using direct method
            cf_count = self._count_records_direct("CustomField")
            logger.info(
                f"DRY RUN: Would delete {cf_count} custom fields",
                extra={"markup": True},
            )
            return cf_count

        # Get count first for reporting using direct method
        try:
            cf_count_before = self._count_records_direct("CustomField")

            if cf_count_before == 0:
                logger.info("No custom fields found to delete", extra={"markup": True})
                return 0

            logger.info(f"Found {cf_count_before} custom fields to delete", extra={"markup": True})

            # Try a more direct approach for custom fields
            # Get IDs first, then delete one by one in batches to avoid timeouts
            logger.info("Using batch deletion approach for custom fields", extra={"markup": True})

            # Get IDs of all custom fields
            command = "puts CustomField.pluck(:id).join(',')"
            result = self.op_client.rails_client.execute(command)

            if result.get("status") == "success":
                output = result.get("output", "")
                if isinstance(output, str) and "," in output:
                    try:
                        # Extract comma-separated IDs
                        ids_output = output.strip().split("\n")[0]  # Take first line in case of multiple lines
                        ids = [int(id_str) for id_str in ids_output.split(",") if id_str.strip().isdigit()]

                        if not ids:
                            logger.warning("No custom field IDs found to delete", extra={"markup": True})
                            return 0

                        total_ids = len(ids)
                        logger.info(f"Processing {total_ids} custom field IDs in batches", extra={"markup": True})

                        # Process in batches of 100
                        batch_size = 100
                        deleted_count = 0

                        for i in range(0, total_ids, batch_size):
                            batch = ids[i:i+batch_size]
                            batch_command = f"CustomField.where(id: {batch}).destroy_all"
                            batch_result = self.op_client.rails_client.execute(batch_command)

                            if batch_result.get("status") != "error":
                                logger.info(
                                    f"Processed batch {i//batch_size + 1}/"
                                    f"{(total_ids + batch_size - 1)//batch_size}",
                                    extra={"markup": True}
                                )
                                deleted_count += len(batch)
                            else:
                                logger.error(
                                    f"Error processing batch {i//batch_size + 1}: "
                                    f"{batch_result.get('error')}",
                                    extra={"markup": True}
                                )

                        # Verify deletion
                        cf_count_after = self._count_records_direct("CustomField")
                        if cf_count_after < cf_count_before:
                            logger.success(
                                f"Successfully deleted {cf_count_before - cf_count_after} custom fields",
                                extra={"markup": True}
                            )
                            return cf_count_before - cf_count_after
                        else:
                            logger.warning(
                                f"Expected custom field count to decrease but it didn't: {cf_count_before} -> {cf_count_after}",
                                extra={"markup": True}
                            )
                            return 0
                    except Exception as e:
                        logger.error(f"Error processing custom field IDs: {str(e)}", extra={"markup": True})
                else:
                    logger.error("Failed to get custom field IDs", extra={"markup": True})
            else:
                logger.error(
                    f"Failed to get custom field IDs: {result.get('error', 'Unknown error')}",
                    extra={"markup": True}
                )

            # Fallback to direct approach if the batch method failed
            logger.info("Falling back to direct deletion", extra={"markup": True})
            command = "CustomField.destroy_all"
            result = self.op_client.rails_client.execute(command)

            if result.get("status") != "error":
                # Check count after deletion to verify success
                cf_count_after = self._count_records_direct("CustomField")
                if cf_count_after < cf_count_before:
                    deleted_count = cf_count_before - cf_count_after
                    logger.success(
                        f"Successfully deleted {deleted_count} custom fields",
                        extra={"markup": True},
                    )
                    return deleted_count
                else:
                    logger.warning(
                        f"Failed to delete custom fields (count before: {cf_count_before}, after: {cf_count_after})",
                        extra={"markup": True},
                    )
                    return 0
            else:
                logger.error(
                    f"Failed to delete custom fields: {result.get('error', 'Unknown error')}",
                    extra={"markup": True},
                )
                return 0

        except Exception as e:
            logger.error(
                f"Failed to delete custom fields: {str(e)}",
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

        if not self._ensure_client():
            logger.error(
                "Cannot clean up issue types: OpenProject client not available.",
                extra={"markup": True},
            )
            return 0

        try:
            # Execute deletion command directly using Rails console client
            command = """
            non_default_types = Type.where(is_default: false, is_standard: false)
            count = non_default_types.count
            if count > 0
              non_default_types.destroy_all
              puts "DELETED #{count} non-default issue types"
            else
              puts "No non-default issue types found to delete"
            end
            puts "END_MARKER"
            """
            result = self.op_client.rails_client.execute(command)

            if result.get("status") == "success":
                output = result.get("output", "")
                if isinstance(output, str):
                    # Check if we have a success indicator
                    if "DELETED" in output:
                        # Try to extract the count using regex
                        import re
                        match = re.search(r"DELETED\s+(\d+)", output)
                        if match:
                            count = int(match.group(1))
                            logger.success(
                                f"Successfully deleted {count} non-default issue types",
                                extra={"markup": True},
                            )
                            return count

                    # If we can't extract a number but we see no types to delete
                    if "No non-default issue types found to delete" in output:
                        logger.info(
                            "No non-default issue types found to delete",
                            extra={"markup": True}
                        )
                        return 0

            logger.error(
                "Failed to clean up issue types",
                extra={"markup": True},
            )
            return 0

        except Exception as e:
            logger.error(
                f"Failed to clean up issue types: {str(e)}",
                extra={"markup": True},
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

        if not self._ensure_client():
            logger.error(
                "Cannot clean up issue statuses: OpenProject client not available.",
                extra={"markup": True},
            )
            return 0

        try:
            # Execute deletion command directly using Rails console client
            command = """
            non_default_statuses = Status.where(is_default: false)
            count = non_default_statuses.count
            if count > 0
              non_default_statuses.destroy_all
              puts "DELETED #{count} non-default issue statuses"
            else
              puts "No non-default issue statuses found to delete"
            end
            puts "END_MARKER"
            """
            result = self.op_client.rails_client.execute(command)

            if result.get("status") == "success":
                output = result.get("output", "")
                if isinstance(output, str):
                    # Check if we have a success indicator
                    if "DELETED" in output:
                        # Try to extract the count using regex
                        import re
                        match = re.search(r"DELETED\s+(\d+)", output)
                        if match:
                            count = int(match.group(1))
                            logger.success(
                                f"Successfully deleted {count} non-default issue statuses",
                                extra={"markup": True},
                            )
                            return count

                    # If we can't extract a number but we see no statuses to delete
                    if "No non-default issue statuses found to delete" in output:
                        logger.info(
                            "No non-default issue statuses found to delete",
                            extra={"markup": True}
                        )
                        return 0

            logger.error(
                "Failed to clean up issue statuses",
                extra={"markup": True},
            )
            return 0

        except Exception as e:
            logger.error(
                f"Failed to clean up issue statuses: {str(e)}",
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

        if not self._ensure_client():
            logger.error(
                "Cannot clean up issue link types: OpenProject client not available.",
                extra={"markup": True},
            )
            return 0

        try:
            # Execute deletion command directly using Rails console client
            command = """
            begin
              # Check if TypedRelation exists in the system
              if !defined?(TypedRelation)
                puts "TypedRelation model not found in OpenProject"
              else
                # Get all relation types that aren't default
                custom_types = []
                default_types = Relation::TYPES.keys.map(&:to_s)

                # Find TypedRelation records where name is not in default types
                TypedRelation.all.each do |rel|
                  if !default_types.include?(rel.name) && !default_types.include?(rel.reverse_name)
                    custom_types << rel
                  end
                end

                count = custom_types.length
                if count > 0
                  custom_types.each(&:destroy)
                  puts "DELETED #{count} custom issue link types"
                else
                  puts "No custom issue link types found to delete"
                end
              end
              puts "END_MARKER"
            rescue => e
              puts "ERROR: #{e.message}"
            end
            """
            result = self.op_client.rails_client.execute(command)

            if result.get("status") == "success":
                output = result.get("output", "")
                if isinstance(output, str):
                    if "TypedRelation model not found" in output:
                        logger.warning(
                            "TypedRelation model not found in OpenProject. Skipping.",
                            extra={"markup": True},
                        )
                        return 0
                    # Check if we have a success indicator
                    elif "DELETED" in output:
                        # Try to extract the count using regex
                        import re
                        match = re.search(r"DELETED\s+(\d+)", output)
                        if match:
                            count = int(match.group(1))
                            logger.success(
                                f"Successfully deleted {count} custom issue link types",
                                extra={"markup": True},
                            )
                            return count
                    # If we can't extract a number but we see no types to delete
                    elif "No custom issue link types found" in output:
                        logger.info(
                            "No custom issue link types found to delete",
                            extra={"markup": True}
                        )
                        return 0
                    elif "ERROR:" in output:
                        error_msg = output.split("ERROR:")[1].strip()
                        logger.error(
                            f"Error in issue link type cleanup: {error_msg}",
                            extra={"markup": True}
                        )
                        return 0

            logger.error(
                "Failed to clean up issue link types",
                extra={"markup": True},
            )
            return 0

        except Exception as e:
            logger.error(
                f"Failed to clean up issue link types: {str(e)}",
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
            logger.info(
                "Starting work package retrieval...",
                extra={"markup": True},
            )

            if not self._ensure_client():
                logger.error(
                    "Cannot retrieve work packages: OpenProject client not available.",
                    extra={"markup": True},
                )
                return []

            # Use find_all_records method from the client
            work_packages = self.op_client.find_all_records("WorkPackage")

            logger.success(
                f"Successfully retrieved {len(work_packages)} work packages in total",
                extra={"markup": True},
            )
            return work_packages
        except Exception as e:
            logger.error(
                f"Failed to get work packages: {str(e)}", extra={"markup": True}
            )
            return []

    def get_all_projects(self) -> list[dict[str, Any]]:
        """
        Get all projects from OpenProject.

        Returns:
            List of project dictionaries
        """
        try:
            logger.info("Fetching projects...", extra={"markup": True})

            if not self._ensure_client():
                logger.error(
                    "Cannot retrieve projects: OpenProject client not available.",
                    extra={"markup": True},
                )
                return []

            # Use get_projects method from the client
            projects = self.op_client.get_projects()

            logger.success(
                f"Successfully retrieved {len(projects)} projects in total",
                extra={"markup": True},
            )
            return projects
        except Exception as e:
            logger.error(f"Failed to get projects: {str(e)}", extra={"markup": True})
            return []

    def cleanup_users(self) -> int:
        """
        Remove users from OpenProject (placeholder, not implemented).
        Users cannot be bulk deleted in OpenProject for safety reasons.

        Returns:
            Number of users deleted
        """
        logger.info("Starting user cleanup...", extra={"markup": True})
        logger.warning(
            "User deletion is not implemented as a bulk operation for safety reasons.",
            extra={"markup": True},
        )
        return 0

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

        # Check if client is available
        if not self._ensure_client():
            logger.error(
                "OpenProject client not available. Cannot proceed with cleanup.",
                extra={"markup": True},
            )
            # Use very short lines
            logger.info("Connection troubleshooting:", extra={"markup": True})
            logger.info("1. Verify SSH access", extra={"markup": True})
            logger.info("2. Check Rails console", extra={"markup": True})
            logger.info("3. Confirm tmux session", extra={"markup": True})

            return {
                "work_packages_deleted": 0,
                "projects_deleted": 0,
                "custom_fields_deleted": 0,
                "users_deleted": 0,
                "issue_types_deleted": 0,
                "issue_statuses_deleted": 0,
                "issue_link_types_deleted": 0,
            }

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
                        results["issue_statuses_deleted"] = self.cleanup_issue_statuses()
                    elif entity_type == "il":
                        results["issue_link_types_deleted"] = (
                            self.cleanup_issue_link_types()
                        )
                except Exception as e:
                    logger.error(
                        f"Error during cleanup of {entity_type}: {str(e)}",
                        extra={"markup": True},
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


def main() -> None:
    """
    Main function to run the cleanup script.
    """
    parser = argparse.ArgumentParser(
        description="Clean up OpenProject by removing specified entities. "
        "Defaults to all entities if none are specified."
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
