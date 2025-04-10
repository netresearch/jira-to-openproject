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

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "./")))

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

              wp.author = User.where(admin: true).first
              wp.author = User.find_by(id: 1) unless wp.author
              wp.author = User.first unless wp.author

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
                  subject: wp.subject,
                  used_fallback_type: true,
                  original_type_id: wp_data['type_id'],
                  used_type_id: default_types.first.id
                }}

                # Log progress
                if (index + 1) % 10 == 0 || index == data_count - 1
                  puts "Created " + created_work_packages.size.to_s + "/" + data_count.to_s + " work packages (with fallback type)"
                end
              else
                errors << {{
                  jira_id: wp_data['jira_id'],
                  jira_key: wp_data['jira_key'],
                  subject: wp_data['subject'],
                  errors: wp.errors.full_messages,
                  error_type: 'validation_error_with_fallback'
                }}
              end
            else
              errors << {{
                jira_id: wp_data['jira_id'],
                jira_key: wp_data['jira_key'],
                subject: wp_data['subject'],
                errors: wp.errors.full_messages,
                error_type: 'no_default_type'
              }}
            end
          else
            errors << {{
              jira_id: wp_data['jira_id'],
              jira_key: wp_data['jira_key'],
              subject: wp_data['subject'],
              errors: wp.errors.full_messages,
              error_type: 'validation_error'
            }}
          end
        end
      end

      # Write results to files
      File.write('{container_temp_path}.result', JSON.generate({{
        created: created_work_packages,
        errors: errors,
        total: data_count,
        created_count: created_work_packages.size,
        error_count: errors.size
      }}))

      # Return summary
      {{
        status: 'success',
        message: "Processed " + data_count.to_s + " work packages: Created " + created_work_packages.size.to_s + ", Failed " + errors.size.to_s,
        created_count: created_work_packages.size,
        error_count: errors.size,
        result_file: '{container_temp_path}.result'
      }}
    rescue => e
      {{ status: 'error', message: e.message, backtrace: e.backtrace }}
    end
    """

    # Execute the Rails command
    result = op_client.rails_client.execute(rails_command)

    if result.get('status') == 'success':
        created_count = result.get('created_count', 0)
        error_count = result.get('error_count', 0)
        logger.success(f"Created {created_count} work packages for project {current_project_key} (errors: {error_count})", extra={"markup": True})

        # Retrieve the result file
        result_file_container = result.get('result_file')
        result_file_local = os.path.join(export_dir, f"import_result_{current_project_key}.json")

        try:
            if op_server:
                # If we have a server, use SSH + Docker cp
                docker_cp_cmd = ["ssh", op_server, "docker", "cp", f"{container_name}:{result_file_container}", "/tmp/"]
                subprocess.run(docker_cp_cmd, check=True)

                scp_cmd = ["scp", f"{op_server}:/tmp/{os.path.basename(result_file_container)}", result_file_local]
                subprocess.run(scp_cmd, check=True)
            else:
                # Direct docker cp
                docker_cp_cmd = ["docker", "cp", f"{container_name}:{result_file_container}", result_file_local]
                subprocess.run(docker_cp_cmd, check=True)

            # Read the result file
            with open(result_file_local, "r") as f:
                import_result = json.load(f)

                # Create a mapping file for this project
                mapping_file = os.path.join(export_dir, f"work_package_mapping_{current_project_key}.json")

                # Prepare mapping dictionary
                mapping = {}
                created_wps = import_result.get('created', [])
                for wp in created_wps:
                    jira_id = wp.get('jira_id')
                    if jira_id:
                        mapping[jira_id] = wp

                # Add errors to mapping
                errors = import_result.get('errors', [])
                for error in errors:
                    jira_id = error.get('jira_id')
                    if jira_id:
                        mapping[jira_id] = {
                            "jira_id": jira_id,
                            "jira_key": error.get('jira_key'),
                            "openproject_id": None,
                            "subject": error.get('subject'),
                            "error": ', '.join(error.get('errors', [])),
                            "error_type": error.get('error_type')
                        }

                # Save mapping
                with open(mapping_file, "w") as mf:
                    json.dump(mapping, mf, indent=2)

                logger.info(f"Saved mapping to {mapping_file}", extra={"markup": True})

                return {
                    "status": "success",
                    "created_count": created_count,
                    "error_count": error_count,
                    "result_file": result_file_local,
                    "mapping_file": mapping_file
                }

        except Exception as e:
            logger.error(f"Error retrieving result file: {str(e)}", extra={"markup": True})
            return {
                "status": "partial",
                "message": f"Error retrieving result file: {str(e)}",
                "created_count": created_count,
                "error_count": error_count
            }
    else:
        logger.error(f"Rails error: {result.get('message', 'Unknown error')}", extra={"markup": True})
        if 'backtrace' in result:
            logger.error(f"Backtrace: {result['backtrace'][:3]}", extra={"markup": True})  # Just first 3 lines

        return {
            "status": "error",
            "message": result.get('message', 'Unknown error'),
            "created_count": 0,
            "error_count": len(work_packages_data)
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
    Exports work packages (issues) from Jira projects and imports them into OpenProject.

    Args:
        jira_client: The JiraClient instance.
        op_client: The OpenProjectClient instance.
        op_rails_client: The optional OpenProjectRailsClient instance.
        dry_run: If True, performs extraction but no import/creation.
        force: If True, re-extracts Jira issues even if JSON files exist.
        project_keys: Optional list of project keys to process.

    Returns:
        Dictionary with export results
    """
    logger.info(f"Starting work package export (dry_run={dry_run}, force={force})", extra={"markup": True})

    # Load mappings (ensure paths are correct)
    mappings = Mappings(
        data_dir=get_path("data"),
        jira_client=jira_client,
        op_client=op_client
    )

    # Get list of Jira projects to process
    jira_projects = list(set(entry.get("jira_key") for entry in mappings.project_mapping.values() if entry.get("jira_key")))

    if not jira_projects:
        logger.warning("No Jira projects found in mapping, nothing to export", extra={"markup": True})
        return {"status": "warning", "message": "No Jira projects found"}

    logger.info(f"Found {len(jira_projects)} Jira projects to process", extra={"markup": True})

    # Create export directory
    export_dir = os.path.join(mappings.data_dir, "exports")
    os.makedirs(export_dir, exist_ok=True)

    # Initialize counters
    total_issues = 0
    total_exported = 0
    total_created = 0
    total_errors = 0
    results = {}

    # Initialize the OpenProject client for immediate imports
    op_client = None
    container_name = None
    op_server = None

    # Check if we need to do immediate imports (not dry run)
    if not dry_run:
        try:
            # Initialize the OpenProject client
            from src.clients.openproject_client import OpenProjectClient
            op_client = OpenProjectClient()

            # Check if Rails client is available
            if not hasattr(op_client, 'rails_client') or not op_client.rails_client:
                logger.error("Rails client is required for work package import. Please ensure tmux session is running.", extra={"markup": True})
                op_client = None
            else:
                # Get Docker container and server info from config for file transfers
                container_name = op_client.op_config.get("container")
                op_server = op_client.op_config.get("server")

                if not container_name:
                    logger.error("Docker container name must be configured for bulk import", extra={"markup": True})
                    op_client = None
        except Exception as e:
            logger.error(f"Error initializing OpenProject client: {str(e)}", extra={"markup": True})
            op_client = None

    # Process each project
    with ProgressTracker("Exporting projects", len(jira_projects), "Recent Projects") as project_tracker:
        for project_key in jira_projects:
            project_tracker.update_description(f"Processing project {project_key}")
            logger.info(f"Processing project {project_key}", extra={"markup": True})

            # Find corresponding OpenProject project ID
            project_mapping_entry = None
            for key, entry in mappings.project_mapping.items():
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
            issues = mappings.extract_jira_issues(project_key, project_tracker=project_tracker)
            total_issues += len(issues)

            if not issues:
                logger.warning(f"No issues found for project {project_key}, skipping", extra={"markup": True})
                project_tracker.add_log_item(f"Skipped: {project_key} (no issues)")
                project_tracker.increment()
                continue

            # Prepare work packages data
            work_packages_data = []
            logger.notice(f"Preparing {len(issues)} work packages for project {project_key}", extra={"markup": True})

            # Initialize counters for the current project
            exported_count_in_project = 0
            error_count_in_project = 0

            for i, issue in enumerate(issues):
                # Use attribute access for jira.Issue object
                try:
                    issue_key = issue.key
                except AttributeError:
                    logger.warning(f"Skipping issue: Missing key attribute. Issue data: {issue}")
                    continue # Skip this issue if it doesn't have a key

                if i % 10 == 0 or i == len(issues) - 1:  # Log progress every 10 issues
                    project_tracker.update_description(f"Preparing issue {issue_key} ({i+1}/{len(issues)}) for export")

                # Use the function's dry_run argument, not self.dry_run
                if dry_run:
                    logger.notice(f"DRY RUN: Would prepare work package data for {issue_key}", extra={"markup": True})
                    # Add a placeholder to mapping for dry runs if necessary for other logic,
                    # but the main goal here is the JSON export file.
                    # Example placeholder if needed later:
                    # work_package_mapping_placeholder[issue.id] = {
                    #     "jira_id": issue.id,
                    #     "jira_key": issue.key,
                    #     "openproject_id": None,
                    #     "subject": getattr(issue.fields, 'summary', 'N/A'),
                    #     "dry_run": True
                    # }
                    exported_count_in_project += 1 # Count as processed for summary
                    continue # Don't add to work_packages_data in dry run

                # Prepare work package data using the mappings object
                # The mappings.prepare_work_package method already handles jira.Issue objects
                wp_data = mappings.prepare_work_package(issue, op_project_id)
                if wp_data:
                    work_packages_data.append(wp_data)
                    exported_count_in_project += 1
                else:
                    # Log if prepare_work_package failed (it logs internally too)
                    logger.warning(f"Failed to prepare work package data for {issue_key}. Skipping.")
                    error_count_in_project += 1

            # --- After loop for the project ---
            total_exported += exported_count_in_project
            total_errors += error_count_in_project

            if not dry_run and work_packages_data:
                # Save work packages data to export file
                export_file_path = os.path.join(export_dir, f"work_packages_{project_key}.json")
                logger.info(f"Saving {len(work_packages_data)} prepared work packages to {export_file_path}", extra={"markup": True})
                if save_json_file(work_packages_data, export_file_path): # Use util function
                     project_tracker.add_log_item(f"Exported {len(work_packages_data)} WPs for {project_key} to JSON")
                else:
                     project_tracker.add_log_item(f"[ERROR] Failed to save export file for {project_key}")
                     total_errors += 1 # Count save failure as an error

            elif dry_run:
                 project_tracker.add_log_item(f"[DRY RUN] Would export {exported_count_in_project} WPs for {project_key}")
            else: # Not dry run, but no data prepared
                 project_tracker.add_log_item(f"Skipped export for {project_key} (no data prepared/errors occurred)")

            # --- Direct Import Logic (Conditional) ---
            # This part seems separate from the primary JSON export goal of this script.
            # It should likely only run if a specific flag (like direct_work_package_creation)
            # is set, and it needs its own error handling.
            # Assuming direct creation is tied to the `direct_migration` flag for now.
            if config.migration_config.get("direct_work_package_creation", False):
                 logger.notice(f"Attempting direct import for {project_key}...", extra={"markup": True})
                 # Instantiate WorkPackageMigration for this specific project
                 wp_migration = WorkPackageMigration(
                    jira_client=jira_client,
                    op_client=op_client,
                    op_rails_client=op_rails_client,
                    mappings=mappings,
                    project_key=project_key,
                    op_project_id=op_project_id,
                    data_dir=get_path("data"), # Use config to get data dir
                    tracker=project_tracker, # Pass the existing tracker
                    dry_run=dry_run
                 )
                 # Call the direct import method, passing the list of jira.Issue objects
                 import_summary = wp_migration.import_work_packages_direct(issues) # Pass the original list
                 # Accumulate direct import counts
                 created_count_direct = import_summary.get("created_count", 0)
                 error_count_direct = import_summary.get("error_count", 0)
            # --- End Direct Import Logic ---

            project_tracker.increment()

    # Final Summary (combine export and direct import results)

    # Save summary of exports
    summary_file = os.path.join(export_dir, "export_summary.json")
    summary = {
        "total_issues": total_issues,
        "total_exported": total_exported,
        "total_created": total_created,
        "total_errors": total_errors,
        "projects": results
    }

    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2)

    logger.success(f"Work package export completed", extra={"markup": True})
    if total_created > 0:
        logger.success(f"Work packages imported: {total_created} created, {total_errors} errors", extra={"markup": True})
    logger.info(f"Total issues processed: {total_issues}", extra={"markup": True})
    logger.info(f"Total work packages exported: {total_exported}", extra={"markup": True})
    logger.info(f"Export files saved to: {export_dir}", extra={"markup": True})

    return {
        "status": "success",
        "total_issues": total_issues,
        "total_exported": total_exported,
        "total_created": total_created,
        "total_errors": total_errors,
        "export_dir": export_dir,
        "summary_file": summary_file,
        "projects": results
    }


def import_work_packages_to_rails(export_dir: str = None, project_key: str = None) -> Dict[str, Any]:
    """
    Import work packages from exported JSON files to OpenProject via Rails console.

    Args:
        export_dir: Path to directory containing exported JSON files
        project_key: Optional project key to import only a specific project

    Returns:
        Dictionary with import results
    """
    logger.info("Starting work package import to Rails console", extra={"markup": True})

    # Get export directory if not provided
    if not export_dir:
        export_dir = os.path.join(config.get_path("data"), "exports")

    if not os.path.exists(export_dir):
        logger.error(f"Export directory not found: {export_dir}", extra={"markup": True})
        return {"status": "error", "message": "Export directory not found"}

    # Initialize the OpenProject client
    op_client = OpenProjectClient()

    # Check if Rails client is available
    if not hasattr(op_client, 'rails_client') or not op_client.rails_client:
        logger.error("Rails client is required for work package import. Please ensure tmux session is running.", extra={"markup": True})
        return {"status": "error", "message": "Rails client not available"}

    # Get Docker container and server info from config for file transfers
    container_name = op_client.op_config.get("container")
    op_server = op_client.op_config.get("server")

    if not container_name:
        logger.error("Docker container name must be configured for bulk import", extra={"markup": True})
        return {"status": "error", "message": "Docker container name not configured"}

    # Get list of export files to process
    if project_key:
        export_files = [os.path.join(export_dir, f"work_packages_{project_key}.json")]
        if not os.path.exists(export_files[0]):
            logger.error(f"Export file not found for project {project_key}", extra={"markup": True})
            return {"status": "error", "message": f"Export file not found for project {project_key}"}
    else:
        # Get all export files
        export_files = [os.path.join(export_dir, f) for f in os.listdir(export_dir)
                       if f.startswith("work_packages_") and f.endswith(".json")]

    if not export_files:
        logger.warning("No export files found to import", extra={"markup": True})
        return {"status": "warning", "message": "No export files found"}

    logger.info(f"Found {len(export_files)} export files to process", extra={"markup": True})

    # Initialize results
    results = {}
    total_created = 0
    total_errors = 0

    # Process each export file
    with ProgressTracker("Importing projects", len(export_files), "Recent Projects") as import_tracker:
        for export_file in export_files:
            # Extract project key from filename
            file_name = os.path.basename(export_file)
            current_project_key = file_name.replace("work_packages_", "").replace(".json", "")

            import_tracker.update_description(f"Processing project {current_project_key}")
            logger.info(f"Processing project {current_project_key}", extra={"markup": True})

            # Read export file
            try:
                with open(export_file, "r") as f:
                    work_packages_data = json.load(f)
            except Exception as e:
                logger.error(f"Error reading export file {export_file}: {str(e)}", extra={"markup": True})
                import_tracker.add_log_item(f"Error: {current_project_key} (file read failed)")
                import_tracker.increment()
                results[current_project_key] = {
                    "status": "error",
                    "message": f"Error reading export file: {str(e)}",
                    "created_count": 0,
                    "error_count": 0
                }
                continue

            if not work_packages_data:
                logger.warning(f"No work package data found in export file {export_file}", extra={"markup": True})
                import_tracker.add_log_item(f"Skipped: {current_project_key} (no data)")
                import_tracker.increment()
                results[current_project_key] = {
                    "status": "warning",
                    "message": "No work package data found in export file",
                    "created_count": 0,
                    "error_count": 0
                }
                continue

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
                import_tracker.add_log_item(f"Error: {current_project_key} (file transfer failed)")
                import_tracker.increment()
                results[current_project_key] = {
                    "status": "error",
                    "message": f"Error copying file to Docker container: {str(e)}",
                    "created_count": 0,
                    "error_count": 0
                }
                continue

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

                      wp.author = User.where(admin: true).first
                      wp.author = User.find_by(id: 1) unless wp.author
                      wp.author = User.first unless wp.author

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
                          subject: wp.subject,
                          used_fallback_type: true,
                          original_type_id: wp_data['type_id'],
                          used_type_id: default_types.first.id
                        }}

                        # Log progress
                        if (index + 1) % 10 == 0 || index == data_count - 1
                          puts "Created " + created_work_packages.size.to_s + "/" + data_count.to_s + " work packages (with fallback type)"
                        end
                      else
                        errors << {{
                          jira_id: wp_data['jira_id'],
                          jira_key: wp_data['jira_key'],
                          subject: wp_data['subject'],
                          errors: wp.errors.full_messages,
                          error_type: 'validation_error_with_fallback'
                        }}
                      end
                    else
                      errors << {{
                        jira_id: wp_data['jira_id'],
                        jira_key: wp_data['jira_key'],
                        subject: wp_data['subject'],
                        errors: wp.errors.full_messages,
                        error_type: 'no_default_type'
                      }}
                    end
                  else
                    errors << {{
                      jira_id: wp_data['jira_id'],
                      jira_key: wp_data['jira_key'],
                      subject: wp_data['subject'],
                      errors: wp.errors.full_messages,
                      error_type: 'validation_error'
                    }}
                  end
                end
              end

              # Write results to files
              File.write('{container_temp_path}.result', JSON.generate({{
                created: created_work_packages,
                errors: errors,
                total: data_count,
                created_count: created_work_packages.size,
                error_count: errors.size
              }}))

              # Return summary
              {{
                status: 'success',
                message: "Processed " + data_count.to_s + " work packages: Created " + created_work_packages.size.to_s + ", Failed " + errors.size.to_s,
                created_count: created_work_packages.size,
                error_count: errors.size,
                result_file: '{container_temp_path}.result'
              }}
            rescue => e
              {{ status: 'error', message: e.message, backtrace: e.backtrace }}
            end
            """

            # Execute the Rails command
            result = op_client.rails_client.execute(rails_command)

            if result.get('status') == 'success':
                created_count = result.get('created_count', 0)
                error_count = result.get('error_count', 0)
                logger.success(f"Created {created_count} work packages for project {current_project_key} (errors: {error_count})", extra={"markup": True})

                # Retrieve the result file
                result_file_container = result.get('result_file')
                result_file_local = os.path.join(export_dir, f"import_result_{current_project_key}.json")

                try:
                    if op_server:
                        # If we have a server, use SSH + Docker cp
                        docker_cp_cmd = ["ssh", op_server, "docker", "cp", f"{container_name}:{result_file_container}", "/tmp/"]
                        subprocess.run(docker_cp_cmd, check=True)

                        scp_cmd = ["scp", f"{op_server}:/tmp/{os.path.basename(result_file_container)}", result_file_local]
                        subprocess.run(scp_cmd, check=True)
                    else:
                        # Direct docker cp
                        docker_cp_cmd = ["docker", "cp", f"{container_name}:{result_file_container}", result_file_local]
                        subprocess.run(docker_cp_cmd, check=True)

                    # Read the result file
                    with open(result_file_local, "r") as f:
                        import_result = json.load(f)

                        # Update work package mapping
                        # Create a mapping file for this project
                        mapping_file = os.path.join(export_dir, f"work_package_mapping_{current_project_key}.json")

                        # Prepare mapping dictionary
                        mapping = {}
                        created_wps = import_result.get('created', [])
                        for wp in created_wps:
                            jira_id = wp.get('jira_id')
                            if jira_id:
                                mapping[jira_id] = wp

                        # Add errors to mapping
                        errors = import_result.get('errors', [])
                        for error in errors:
                            jira_id = error.get('jira_id')
                            if jira_id:
                                mapping[jira_id] = {
                                    "jira_id": jira_id,
                                    "jira_key": error.get('jira_key'),
                                    "openproject_id": None,
                                    "subject": error.get('subject'),
                                    "error": ', '.join(error.get('errors', [])),
                                    "error_type": error.get('error_type')
                                }

                        # Save mapping
                        with open(mapping_file, "w") as mf:
                            json.dump(mapping, mf, indent=2)

                        # Log results
                        logger.info(f"Saved mapping to {mapping_file}", extra={"markup": True})

                        # Store results
                        results[current_project_key] = {
                            "status": "success",
                            "created_count": created_count,
                            "error_count": error_count,
                            "result_file": result_file_local,
                            "mapping_file": mapping_file
                        }

                        # Update counters
                        total_created += created_count
                        total_errors += error_count

                except Exception as e:
                    logger.error(f"Error retrieving result file: {str(e)}", extra={"markup": True})
                    results[current_project_key] = {
                        "status": "partial",
                        "message": f"Error retrieving result file: {str(e)}",
                        "created_count": created_count,
                        "error_count": error_count
                    }
            else:
                logger.error(f"Rails error: {result.get('message', 'Unknown error')}", extra={"markup": True})
                if 'backtrace' in result:
                    logger.error(f"Backtrace: {result['backtrace'][:3]}", extra={"markup": True})  # Just first 3 lines

                results[current_project_key] = {
                    "status": "error",
                    "message": result.get('message', 'Unknown error'),
                    "created_count": 0,
                    "error_count": len(work_packages_data)
                }
                total_errors += len(work_packages_data)

            import_tracker.add_log_item(f"Completed: {current_project_key} ({created_count}/{len(work_packages_data)} issues)")
            import_tracker.increment()

    # Save overall import summary
    summary_file = os.path.join(export_dir, "import_summary.json")
    summary = {
        "total_created": total_created,
        "total_errors": total_errors,
        "projects": results
    }

    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2)

    logger.success(f"Work package import completed", extra={"markup": True})
    logger.info(f"Total work packages created: {total_created}", extra={"markup": True})
    logger.info(f"Total errors: {total_errors}", extra={"markup": True})
    logger.info(f"Import summary saved to: {summary_file}", extra={"markup": True})

    return {
        "status": "success",
        "total_created": total_created,
        "total_errors": total_errors,
        "summary_file": summary_file,
        "projects": results
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export and import work packages using bulk JSON files")

    # Create subparsers for export and import commands
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Export command
    export_parser = subparsers.add_parser("export", help="Export work packages from Jira to JSON files")
    export_parser.add_argument("--dry-run", action="store_true", help="Run in dry-run mode (no files created)")
    export_parser.add_argument("--force", action="store_true", help="Force extraction of data even if files exist")

    # Import command
    import_parser = subparsers.add_parser("import", help="Import work packages from JSON files to OpenProject")
    import_parser.add_argument("--export-dir", help="Directory containing exported JSON files")
    import_parser.add_argument("--project", help="Import a specific project only")

    # Parse args
    args = parser.parse_args()

    if args.command == "export":
        export_work_packages(dry_run=args.dry_run, force=args.force)
    elif args.command == "import":
        import_work_packages_to_rails(export_dir=args.export_dir, project_key=args.project)
    else:
        parser.print_help()
