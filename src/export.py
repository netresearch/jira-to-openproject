#!/usr/bin/env python3
"""
Export work packages from Jira to JSON files for direct import into OpenProject.
This script uses the existing migration infrastructure to extract Jira issues,
but instead of creating work packages one by one via API or Rails console,
it creates JSON files that can be imported in bulk.
"""

import os
import sys
import json
import argparse
import subprocess
from typing import Dict, List, Any, Optional

from src.migrations.work_package_migration import WorkPackageMigration
from src.clients.openproject_client import OpenProjectClient
from src import config
from src.display import ProgressTracker, console
from src.config import logger, migration_config, get_path
from src.clients.jira_client import JiraClient
from src.clients.openproject_rails_client import OpenProjectRailsClient
from src.utils import load_json_file, save_json_file, sanitize_for_filename
from src.mappings.mappings import Mappings

# Get logger from config
logger = config.logger


def import_project_work_packages(export_file: str, export_dir: str, op_client: Any, container_name: str, op_server: str = None) -> Dict[str, Any]:
    """
    Import work packages for a single project from an exported JSON file.

    Args:
        export_file: Path to the exported JSON file
        export_dir: Path to the directory for storing results
        op_client: Initialized OpenProject client
        container_name: Name of the Docker container
        op_server: Optional SSH server for remote Docker

    Returns:
        Dictionary with import results
    """
    # Extract project key from filename
    file_name = os.path.basename(export_file)
    current_project_key = file_name.replace("work_packages_", "").replace(".json", "")

    logger.notice(f"=== IMPORTING WORK PACKAGES FOR PROJECT {current_project_key} ===", extra={"markup": True})

    # Read export file
    try:
        with open(export_file, "r") as f:
            work_packages_data = json.load(f)
    except Exception as e:
        logger.error(f"Error reading export file {export_file}: {str(e)}", extra={"markup": True})
        return {
            "status": "error",
            "message": f"Error reading export file: {str(e)}",
            "created_count": 0,
            "error_count": 0
        }

    if not work_packages_data:
        logger.warning(f"No work package data found in export file {export_file}", extra={"markup": True})
        return {
            "status": "warning",
            "message": "No work package data found in export file",
            "created_count": 0,
            "error_count": 0
        }

    logger.notice(f"Importing {len(work_packages_data)} work packages for project {current_project_key}", extra={"markup": True})

    # Define the path for the temporary file inside the container
    container_temp_path = f"/tmp/import_work_packages_{current_project_key}.json"

    # Copy the file to the Docker container
    try:
        if op_server:
            # If we have a server, use SSH + Docker cp
            logger.info(f"Copying file to {op_server} container {container_name}", extra={"markup": True})
            # First copy to the server
            scp_cmd = ["scp", export_file, f"{op_server}:/tmp/"]
            logger.debug(f"Running command: {' '.join(scp_cmd)}", extra={"markup": True})
            subprocess.run(scp_cmd, check=True)

            # Then copy from server into container
            ssh_cmd = ["ssh", op_server, "docker", "cp", f"/tmp/{os.path.basename(export_file)}", f"{container_name}:{container_temp_path}"]
            logger.debug(f"Running command: {' '.join(ssh_cmd)}", extra={"markup": True})
            subprocess.run(ssh_cmd, check=True)
        else:
            # Direct docker cp
            logger.info(f"Copying file to container {container_name}", extra={"markup": True})
            docker_cp_cmd = ["docker", "cp", export_file, f"{container_name}:{container_temp_path}"]
            logger.debug(f"Running command: {' '.join(docker_cp_cmd)}", extra={"markup": True})
            subprocess.run(docker_cp_cmd, check=True)

        logger.success(f"Successfully copied work packages data to container", extra={"markup": True})
    except subprocess.SubprocessError as e:
        logger.error(f"Error copying file to Docker container: {str(e)}", extra={"markup": True})
        return {
            "status": "error",
            "message": f"Error copying file to Docker container: {str(e)}",
            "created_count": 0,
            "error_count": 0
        }

    # Execute Rails code to import the work packages
    logger.notice(f"Importing {len(work_packages_data)} work packages via Rails console", extra={"markup": True})

    # Use raw string for Ruby code to avoid Python syntax issues
    rails_command = f"""
    begin
      require 'json'

      # Read the temp file
      begin
        work_packages_data = JSON.parse(File.read('{container_temp_path}'))

        # Ensure it's an array
        work_packages_data = [] unless work_packages_data.is_a?(Array)

        data_count = work_packages_data.size
      rescue
        # If any error occurs, use an empty array
        work_packages_data = []
        data_count = 0
      end

      created_work_packages = []
      errors = []

      puts "Processing " + data_count.to_s + " work packages..."

      # Process work packages
      work_packages_data.each_with_index do |wp_data, index|
        # Create work package
        wp = WorkPackage.new
        wp.project_id = wp_data['project_id']
        wp.type_id = wp_data['type_id']
        wp.subject = wp_data['subject']
        wp.description = wp_data['description']

        # Add status if present
        wp.status_id = wp_data['status_id'] if wp_data['status_id']

        # Add assignee if present
        wp.assigned_to_id = wp_data['assigned_to_id'] if wp_data['assigned_to_id']

        # Add required fields
        wp.priority = IssuePriority.default if IssuePriority.respond_to?(:default)
        wp.priority = IssuePriority.where(is_default: true).first unless wp.priority
        wp.priority = IssuePriority.first unless wp.priority

        # Set author to admin user
        wp.author = User.where(admin: true).first
        wp.author = User.find_by(id: 1) unless wp.author
        wp.author = User.first unless wp.author

        # Ensure status is set if it's missing
        if wp.status.nil?
          wp.status = Status.default if Status.respond_to?(:default)
          wp.status = Status.where(is_default: true).first unless wp.status
          wp.status = Status.first unless wp.status
        end

        if wp.save
          created_work_packages << {{
            jira_id: wp_data['jira_id'],
            jira_key: wp_data['jira_key'],
            openproject_id: wp.id,
            subject: wp.subject
          }}

          # Log progress every 10 items
          if (index + 1) % 10 == 0 || index == data_count - 1
            puts "Created " + created_work_packages.size.to_s + "/" + data_count.to_s + " work packages"
          end
        else
          # If type is not available for project, try with a default type
          if wp.errors.full_messages.any? {{ |msg| msg.include?('type') && msg.include?('not available') }}
            # Find a default type
            default_types = Type.where(is_default: true)
            if default_types.any?
              wp = WorkPackage.new
              wp.project_id = wp_data['project_id']
              wp.type_id = default_types.first.id
              wp.subject = wp_data['subject']
              wp.description = wp_data['description']

              # Add status if present
              wp.status_id = wp_data['status_id'] if wp_data['status_id']

              # Add assignee if present
              wp.assigned_to_id = wp_data['assigned_to_id'] if wp_data['assigned_to_id']

              # Add required fields (priority, author, status)
              wp.priority = IssuePriority.default if IssuePriority.respond_to?(:default)
              wp.priority = IssuePriority.where(is_default: true).first unless wp.priority
              wp.priority = IssuePriority.first unless wp.priority

              # Set author to admin user
              wp.author = User.where(admin: true).first
              wp.author = User.find_by(id: 1) unless wp.author
              wp.author = User.first unless wp.author

              # Ensure status is set if it's missing
              if wp.status.nil?
                wp.status = Status.default if Status.respond_to?(:default)
                wp.status = Status.where(is_default: true).first unless wp.status
                wp.status = Status.first unless wp.status
              end

              if wp.save
                created_work_packages << {{
                  jira_id: wp_data['jira_id'],
                  jira_key: wp_data['jira_key'],
                  openproject_id: wp.id,
                  subject: wp.subject
                }}
              else
                errors << {{
                  jira_id: wp_data['jira_id'],
                  jira_key: wp_data['jira_key'],
                  error: wp.errors.full_messages.join(', '),
                  subject: wp_data['subject']
                }}
              end
            else
              errors << {{
                jira_id: wp_data['jira_id'],
                jira_key: wp_data['jira_key'],
                error: "No default type available for project",
                subject: wp_data['subject']
              }}
            end
          else
            errors << {{
              jira_id: wp_data['jira_id'],
              jira_key: wp_data['jira_key'],
              error: wp.errors.full_messages.join(', '),
              subject: wp_data['subject']
            }}
          end
        end
      end

      # Return the results
      {{
        status: errors.empty? ? "success" : "partial",
        created_count: created_work_packages.size,
        error_count: errors.size,
        created_work_packages: created_work_packages,
        errors: errors
      }}.to_json
    rescue => e
      {{
        status: "error",
        message: e.message,
        created_count: 0,
        error_count: data_count
      }}.to_json
    end
    """

    # Execute the Rails command
    rails_client = op_rails_client or OpenProjectRailsClient()
    result = rails_client.execute_ruby(rails_command)

    # Parse the result
    try:
        result_data = json.loads(result)
        logger.success(f"Import completed: {result_data.get('created_count', 0)} work packages created, {result_data.get('error_count', 0)} errors", extra={"markup": True})

        # Write detailed report to file
        report_file = os.path.join(export_dir, f"import_report_{current_project_key}.json")
        with open(report_file, "w") as f:
            json.dump(result_data, f, indent=2)

        logger.info(f"Detailed import report saved to {report_file}", extra={"markup": True})

        return result_data
    except Exception as e:
        logger.error(f"Error parsing import result: {str(e)}", extra={"markup": True})
        logger.debug(f"Raw result: {result}", extra={"markup": True})
        return {
            "status": "error",
            "message": f"Error parsing import result: {str(e)}",
            "created_count": 0,
            "error_count": 0,
            "raw_result": result
        }


def export_work_packages(
    jira_client: JiraClient,
    op_client: OpenProjectClient,
    op_rails_client: Optional[OpenProjectRailsClient],
    dry_run: bool = False,
    force: bool = False,
    project_keys: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Export Jira issues to JSON files for bulk import into OpenProject.

    Args:
        jira_client: Initialized Jira client
        op_client: Initialized OpenProject client
        op_rails_client: Optional OpenProject Rails client
        dry_run: If True, don't create/update actual work packages
        force: If True, force re-extraction of data
        project_keys: List of Jira project keys to process (if None, process all)

    Returns:
        Dictionary with export results
    """
    try:
        # Get export directory
        export_dir = get_path("exports")
        os.makedirs(export_dir, exist_ok=True)

        # Initialize work package migration
        work_package_migration = WorkPackageMigration(jira_client, op_client, op_rails_client, data_dir=config.get_path("data"))

        # Initialize mappings
        from src.mappings.mappings import Mappings
        from src.utils import get_path
        mappings = Mappings(
            data_dir=get_path("data"),
            jira_client=jira_client,
            op_client=op_client
        )

        # Extract all necessary data (users, projects, custom fields, etc.)
        if not mappings.has_mapping("projects"):
            logger.error("Project mapping not found. Run the project migration first.", extra={"markup": True})
            return {
                "status": "error",
                "message": "Project mapping not found. Run the project migration first."
            }

        # Get project mapping
        project_mapping = mappings.get_mapping("projects")
        if not project_mapping:
            logger.error("Project mapping is empty", extra={"markup": True})
            return {
                "status": "error",
                "message": "Project mapping is empty. Run the project migration first."
            }

        logger.info(f"Found {len(project_mapping)} projects in mapping file", extra={"markup": True})

        # Filter projects if specified
        if project_keys:
            filtered_mapping = {k: v for k, v in project_mapping.items() if k in project_keys}
            if not filtered_mapping:
                logger.error(f"None of the specified projects {project_keys} found in mapping", extra={"markup": True})
                return {
                    "status": "error",
                    "message": f"None of the specified projects {project_keys} found in mapping"
                }
            project_mapping = filtered_mapping
            logger.info(f"Filtered to {len(project_mapping)} specified projects: {list(project_mapping.keys())}", extra={"markup": True})

        # Process each project
        total_projects = len(project_mapping)
        progress = ProgressTracker(total_projects, description="Exporting projects")

        total_work_packages = 0
        exported_projects = 0
        project_results = {}

        for jira_project_key, op_project_id in project_mapping.items():
            # Skip if project ID is missing
            if not op_project_id:
                logger.warning(f"OpenProject ID missing for Jira project {jira_project_key}, skipping", extra={"markup": True})
                progress.advance(1, f"Skipped {jira_project_key} (no OpenProject ID)")
                continue

            logger.notice(f"Processing project: {jira_project_key} → OpenProject ID {op_project_id}", extra={"markup": True})

            # Extract work packages for this project
            try:
                # Check if export file already exists
                export_file = os.path.join(export_dir, f"work_packages_{jira_project_key}.json")
                if os.path.exists(export_file) and not force:
                    logger.info(f"Export file already exists for {jira_project_key}. Use --force to regenerate.", extra={"markup": True})
                    # Read the existing file to count work packages
                    try:
                        with open(export_file, 'r') as f:
                            existing_data = json.load(f)
                            wp_count = len(existing_data)
                            logger.info(f"Found {wp_count} work packages in existing export", extra={"markup": True})
                            total_work_packages += wp_count
                            exported_projects += 1
                            project_results[jira_project_key] = {
                                "status": "skipped",
                                "message": "Export file already exists",
                                "work_package_count": wp_count
                            }
                            progress.advance(1, f"Skipped {jira_project_key} (already exported: {wp_count} WPs)")
                            continue
                    except (json.JSONDecodeError, FileNotFoundError) as e:
                        logger.warning(f"Error reading existing export file: {str(e)}", extra={"markup": True})
                        logger.info("Will regenerate the export file", extra={"markup": True})

                # Extract work packages
                all_issues = work_package_migration.extract_issues_for_project(jira_project_key, force=force)

                if not all_issues:
                    logger.warning(f"No issues found for project {jira_project_key}", extra={"markup": True})
                    project_results[jira_project_key] = {
                        "status": "warning",
                        "message": "No issues found",
                        "work_package_count": 0
                    }
                    progress.advance(1, f"No issues for {jira_project_key}")
                    continue

                logger.info(f"Extracted {len(all_issues)} issues for project {jira_project_key}", extra={"markup": True})

                # Map issues to work packages
                work_packages = []
                for issue in all_issues:
                    # Map the issue to a work package
                    wp_data = work_package_migration.map_issue_to_work_package(
                        issue,
                        op_project_id=op_project_id,
                        ignore_errors=True,
                        include_jira_info=True  # Include the original Jira info for reference
                    )
                    if wp_data:
                        work_packages.append(wp_data)

                logger.success(f"Mapped {len(work_packages)} issues to work packages for project {jira_project_key}", extra={"markup": True})

                # Write to export file
                with open(export_file, 'w') as f:
                    json.dump(work_packages, f, indent=2)

                logger.success(f"Exported {len(work_packages)} work packages to {export_file}", extra={"markup": True})
                total_work_packages += len(work_packages)
                exported_projects += 1

                project_results[jira_project_key] = {
                    "status": "success",
                    "message": f"Exported {len(work_packages)} work packages",
                    "work_package_count": len(work_packages)
                }

                progress.advance(1, f"Exported {jira_project_key} ({len(work_packages)} WPs)")

            except Exception as e:
                logger.error(f"Error processing project {jira_project_key}: {str(e)}", extra={"markup": True, "traceback": True})
                project_results[jira_project_key] = {
                    "status": "error",
                    "message": f"Error: {str(e)}",
                    "error": str(e)
                }
                progress.advance(1, f"Error with {jira_project_key}")

        # Write summary report
        summary = {
            "timestamp": datetime.now().isoformat(),
            "total_projects": total_projects,
            "exported_projects": exported_projects,
            "total_work_packages": total_work_packages,
            "project_results": project_results
        }

        summary_file = os.path.join(export_dir, "export_summary.json")
        with open(summary_file, 'w') as f:
            json.dump(summary, f, indent=2)

        logger.success(f"Export complete: {exported_projects}/{total_projects} projects, {total_work_packages} work packages", extra={"markup": True})
        logger.info(f"Summary report saved to {summary_file}", extra={"markup": True})

        return {
            "status": "success",
            "message": f"Export complete: {exported_projects}/{total_projects} projects, {total_work_packages} work packages",
            "exported_projects": exported_projects,
            "total_projects": total_projects,
            "total_work_packages": total_work_packages,
            "project_results": project_results
        }

    except Exception as e:
        logger.error(f"Unexpected error during export: {str(e)}", extra={"markup": True, "traceback": True})
        return {
            "status": "error",
            "message": f"Unexpected error: {str(e)}",
            "error": str(e)
        }


def import_work_packages_to_rails(export_dir: str = None, project_key: str = None) -> Dict[str, Any]:
    """
    Import work packages from exported JSON files to OpenProject via Rails console.

    Args:
        export_dir: Directory containing the exported JSON files
        project_key: If specified, only import this project

    Returns:
        Dictionary with import results
    """
    try:
        # Get export directory
        if not export_dir:
            export_dir = get_path("exports")

        if not os.path.exists(export_dir):
            logger.error(f"Export directory not found: {export_dir}", extra={"markup": True})
            return {
                "status": "error",
                "message": f"Export directory not found: {export_dir}"
            }

        # Initialize OpenProject client
        op_client = OpenProjectClient()

        # Initialize Rails client
        op_rails_client = OpenProjectRailsClient()

        # Get container name and server from config
        container_name = migration_config.get("openproject", {}).get("container", "openproject")
        op_server = migration_config.get("openproject", {}).get("server")

        # Get list of export files
        export_files = []
        if project_key:
            file_path = os.path.join(export_dir, f"work_packages_{project_key}.json")
            if os.path.exists(file_path):
                export_files.append(file_path)
            else:
                logger.error(f"Export file not found for project {project_key}", extra={"markup": True})
                return {
                    "status": "error",
                    "message": f"Export file not found for project {project_key}"
                }
        else:
            # Get all export files
            for file_name in os.listdir(export_dir):
                if file_name.startswith("work_packages_") and file_name.endswith(".json"):
                    export_files.append(os.path.join(export_dir, file_name))

        if not export_files:
            logger.error("No export files found", extra={"markup": True})
            return {
                "status": "error",
                "message": "No export files found"
            }

        logger.info(f"Found {len(export_files)} export files to process", extra={"markup": True})

        # Process each export file
        results = {}
        total_created = 0
        total_errors = 0

        for export_file in export_files:
            file_name = os.path.basename(export_file)
            logger.notice(f"Processing export file: {file_name}", extra={"markup": True})

            # Import the work packages
            import_result = import_project_work_packages(
                export_file=export_file,
                export_dir=export_dir,
                op_client=op_client,
                container_name=container_name,
                op_server=op_server
            )

            # Extract project key from filename
            current_project_key = file_name.replace("work_packages_", "").replace(".json", "")
            results[current_project_key] = import_result

            # Update totals
            total_created += import_result.get("created_count", 0)
            total_errors += import_result.get("error_count", 0)

        # Write summary report
        summary = {
            "timestamp": datetime.now().isoformat(),
            "total_files": len(export_files),
            "total_created": total_created,
            "total_errors": total_errors,
            "project_results": results
        }

        summary_file = os.path.join(export_dir, "import_summary.json")
        with open(summary_file, 'w') as f:
            json.dump(summary, f, indent=2)

        logger.success(f"Import complete: {total_created} work packages created, {total_errors} errors", extra={"markup": True})
        logger.info(f"Summary report saved to {summary_file}", extra={"markup": True})

        return {
            "status": "success" if total_errors == 0 else "partial",
            "message": f"Import complete: {total_created} work packages created, {total_errors} errors",
            "total_created": total_created,
            "total_errors": total_errors,
            "project_results": results
        }

    except Exception as e:
        logger.error(f"Unexpected error during import: {str(e)}", extra={"markup": True, "traceback": True})
        return {
            "status": "error",
            "message": f"Unexpected error: {str(e)}",
            "error": str(e)
        }


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Export Jira issues to JSON files for bulk import into OpenProject")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode (no changes to OpenProject)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-extraction of data",
    )
    parser.add_argument(
        "--projects",
        nargs="+",
        help="Specific Jira projects to process (space-separated list of keys)",
    )
    parser.add_argument(
        "--import",
        action="store_true",
        dest="import_mode",
        help="Import previously exported work packages instead of exporting",
    )
    parser.add_argument(
        "--project",
        help="When importing, only import this specific project",
    )
    parser.add_argument(
        "--export-dir",
        help="Directory for exports/imports (default: var/exports)",
    )
    return parser.parse_args()


def main():
    """Run the export tool."""
    args = parse_args()

    try:
        if args.import_mode:
            # Import mode
            result = import_work_packages_to_rails(
                export_dir=args.export_dir,
                project_key=args.project
            )
        else:
            # Export mode
            jira_client = JiraClient()
            op_client = OpenProjectClient()
            op_rails_client = OpenProjectRailsClient() if migration_config.get("use_rails_console") else None

            result = export_work_packages(
                jira_client=jira_client,
                op_client=op_client,
                op_rails_client=op_rails_client,
                dry_run=args.dry_run,
                force=args.force,
                project_keys=args.projects
            )

        # Check result status
        if result.get("status") == "success":
            logger.success("Operation completed successfully", extra={"markup": True})
            sys.exit(0)
        elif result.get("status") == "partial":
            logger.warning("Operation completed with some issues", extra={"markup": True})
            sys.exit(0)  # Still return 0 for partial success
        else:
            logger.error(f"Operation failed: {result.get('message', 'Unknown error')}", extra={"markup": True})
            sys.exit(1)

    except KeyboardInterrupt:
        print("\nOperation manually interrupted. Exiting...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", extra={"markup": True, "traceback": True})
        sys.exit(1)


if __name__ == "__main__":
    from datetime import datetime  # Import for timestamp in export_work_packages
    main()
