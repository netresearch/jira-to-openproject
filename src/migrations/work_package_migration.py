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

    def prepare_work_package(self, jira_issue: Dict[str, Any], project_id: int) -> Dict[str, Any]:
        """
        Prepare a work package object from a Jira issue (without creating it).

        Args:
            jira_issue: The Jira issue dictionary
            project_id: The ID of the OpenProject project

        Returns:
            Dictionary with work package data
        """
        # Map the Jira issue to an OpenProject work package
        issue_type_id = jira_issue["issue_type"]["id"]
        issue_type_name = jira_issue["issue_type"]["name"]
        type_id = self.type_mapping.get(issue_type_id)

        # Log detailed type mapping information for debugging
        logger.debug(f"Mapping issue type: {issue_type_name} (ID: {issue_type_id}) -> OpenProject type ID: {type_id}", extra={"markup": True})

        # If no type mapping exists, default to Task
        if not type_id:
            logger.warning(f"No mapping found for issue type {issue_type_name} (ID: {issue_type_id}), defaulting to Task", extra={"markup": True})
            # Get the Task type ID from OpenProject
            task_types = [t for t in self.op_client.get_work_package_types() if t["name"] == "Task"]
            if task_types:
                type_id = task_types[0]["id"]
            else:
                # If no Task type found, use the first available type
                types = self.op_client.get_work_package_types()
                if types:
                    type_id = types[0]["id"]
                else:
                    logger.error("No work package types available in OpenProject", extra={"markup": True})
                    return None

        status_id = None
        if "status" in jira_issue and "id" in jira_issue["status"]:
            status_id = self.status_mapping.get(jira_issue["status"]["id"])

        # Get assignee if available
        assigned_to_id = None
        if "assignee" in jira_issue and jira_issue["assignee"]:
            assignee_name = jira_issue["assignee"].get("name")
            if assignee_name in self.user_mapping:
                assigned_to_id = self.user_mapping[assignee_name]

        # Create the work package data
        subject = jira_issue["summary"]
        description = jira_issue.get("description", "")

        # Add Jira issue key to description for reference
        jira_reference = f"\n\n*Imported from Jira issue: {jira_issue['key']}*"
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
            "jira_id": jira_issue["id"],
            "jira_key": jira_issue["key"]
        }

        # Add optional fields if available
        if status_id:
            work_package["status_id"] = status_id
        if assigned_to_id:
            work_package["assigned_to_id"] = assigned_to_id

        return work_package

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

        # Check if Rails client is available - we need it for bulk imports
        if not hasattr(self.op_client, 'rails_client') or not self.op_client.rails_client:
            logger.error("Rails client is required for work package migration. Please ensure tmux session is running.", extra={"markup": True})
            return {}

        # Get list of Jira projects to process
        jira_projects = list(set(entry.get("jira_key") for entry in self.project_mapping.values() if entry.get("jira_key")))

        if not jira_projects:
            logger.warning("No Jira projects found in mapping, nothing to migrate", extra={"markup": True})
            return {}

        logger.info(f"Found {len(jira_projects)} Jira projects to process", extra={"markup": True})

        # Initialize counters
        total_issues = 0
        total_created = 0

        # Get Docker container and server info from config for file transfers
        container_name = self.op_client.op_config.get("container")
        op_server = self.op_client.op_config.get("server")

        if not container_name:
            logger.error("Docker container name must be configured for bulk import", extra={"markup": True})
            return {}

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

                # Prepare work packages data
                work_packages_data = []
                logger.notice(f"Preparing {len(issues)} work packages for project {project_key}", extra={"markup": True})

                for i, issue in enumerate(issues):
                    issue_key = issue.get("key", "Unknown")
                    if i % 10 == 0 or i == len(issues) - 1:  # Log progress every 10 issues
                        project_tracker.update_description(f"Preparing issue {issue_key} ({i+1}/{len(issues)})")

                    if self.dry_run:
                        logger.notice(f"DRY RUN: Would create work package for {issue_key}", extra={"markup": True})
                        # Add a placeholder to mapping for dry runs
                        self.work_package_mapping[issue["id"]] = {
                            "jira_id": issue["id"],
                            "jira_key": issue["key"],
                            "openproject_id": None,
                            "subject": issue["summary"],
                            "dry_run": True
                        }
                        continue

                    # Prepare work package data
                    wp_data = self.prepare_work_package(issue, op_project_id)
                    if wp_data:
                        work_packages_data.append(wp_data)

                if self.dry_run:
                    project_tracker.add_log_item(f"DRY RUN: Would create {len(issues)} work packages for {project_key}")
                    project_tracker.increment()
                    continue

                if not work_packages_data:
                    logger.warning(f"No work package data prepared for project {project_key}, skipping", extra={"markup": True})
                    project_tracker.add_log_item(f"Skipped: {project_key} (no work packages prepared)")
                    project_tracker.increment()
                    continue

                # Save work packages data to temp file
                temp_file_path = os.path.join(self.data_dir, f"work_packages_{project_key}.json")
                logger.info(f"Saving {len(work_packages_data)} work packages to {temp_file_path}", extra={"markup": True})

                with open(temp_file_path, "w") as f:
                    json.dump(work_packages_data, f, indent=2)

                # Define the path for the temporary file inside the container
                container_temp_path = f"/tmp/work_packages_{project_key}.json"

                # Copy the file to the Docker container
                try:
                    if op_server:
                        # If we have a server, use SSH + Docker cp
                        logger.info(f"Copying file to {op_server} container {container_name}", extra={"markup": True})
                        import subprocess
                        # First copy to the server
                        scp_cmd = ["scp", temp_file_path, f"{op_server}:/tmp/"]
                        logger.debug(f"Running command: {' '.join(scp_cmd)}", extra={"markup": True})
                        subprocess.run(scp_cmd, check=True)

                        # Then copy from server into container
                        ssh_cmd = ["ssh", op_server, "docker", "cp", f"/tmp/{os.path.basename(temp_file_path)}", f"{container_name}:{container_temp_path}"]
                        logger.debug(f"Running command: {' '.join(ssh_cmd)}", extra={"markup": True})
                        subprocess.run(ssh_cmd, check=True)
                    else:
                        # Direct docker cp
                        logger.info(f"Copying file to container {container_name}", extra={"markup": True})
                        import subprocess
                        docker_cp_cmd = ["docker", "cp", temp_file_path, f"{container_name}:{container_temp_path}"]
                        logger.debug(f"Running command: {' '.join(docker_cp_cmd)}", extra={"markup": True})
                        subprocess.run(docker_cp_cmd, check=True)

                    logger.success(f"Successfully copied work packages data to container", extra={"markup": True})
                except subprocess.SubprocessError as e:
                    logger.error(f"Error copying file to Docker container: {str(e)}", extra={"markup": True})
                    project_tracker.add_log_item(f"Error: {project_key} (file transfer failed)")
                    project_tracker.increment()
                    continue

                # Now execute Rails code to import the work packages
                logger.notice(f"Importing {len(work_packages_data)} work packages via Rails console", extra={"markup": True})

                rails_command = f"""
                begin
                  require 'json'

                  # Read the temp file
                  work_packages_data = JSON.parse(File.read('{container_temp_path}'))
                  created_work_packages = []
                  errors = []

                  puts "Processing #{work_packages_data.size} work packages..."

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
                      if (index + 1) % 10 == 0 || index == work_packages_data.size - 1
                        puts "Created #{created_work_packages.size}/#{work_packages_data.size} work packages"
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
                            if (index + 1) % 10 == 0 || index == work_packages_data.size - 1
                              puts "Created #{created_work_packages.size}/#{work_packages_data.size} work packages (with fallback type)"
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
                    total: work_packages_data.size,
                    created_count: created_work_packages.size,
                    error_count: errors.size
                  }}))

                  # Return summary
                  {{
                    status: 'success',
                    message: "Processed #{work_packages_data.size} work packages: Created #{created_work_packages.size}, Failed #{errors.size}",
                    created_count: created_work_packages.size,
                    error_count: errors.size,
                    result_file: '{container_temp_path}.result'
                  }}
                rescue => e
                  {{ status: 'error', message: e.message, backtrace: e.backtrace }}
                end
                """

                # Execute the Rails command
                result = self.op_client.rails_client.execute(rails_command)

                if result.get('status') == 'success':
                    created_count = result.get('created_count', 0)
                    error_count = result.get('error_count', 0)
                    logger.success(f"Created {created_count} work packages for project {project_key} (errors: {error_count})", extra={"markup": True})

                    # Retrieve the result file
                    result_file_container = result.get('result_file')
                    result_file_local = os.path.join(self.data_dir, f"work_packages_{project_key}_result.json")

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

                            # Update our mapping with created work packages
                            created_wps = import_result.get('created', [])
                            for wp in created_wps:
                                jira_id = wp.get('jira_id')
                                if jira_id:
                                    self.work_package_mapping[jira_id] = wp

                            # Log error details
                            errors = import_result.get('errors', [])
                            if errors:
                                logger.warning(f"Failed to create {len(errors)} work packages", extra={"markup": True})
                                for error in errors[:5]:  # Log first 5 errors
                                    logger.warning(f"Error for {error.get('jira_key')}: {', '.join(error.get('errors', []))}", extra={"markup": True})

                                # Add errors to mapping
                                for error in errors:
                                    jira_id = error.get('jira_id')
                                    if jira_id:
                                        self.work_package_mapping[jira_id] = {
                                            "jira_id": jira_id,
                                            "jira_key": error.get('jira_key'),
                                            "openproject_id": None,
                                            "subject": error.get('subject'),
                                            "error": ', '.join(error.get('errors', [])),
                                            "error_type": error.get('error_type')
                                        }
                    except Exception as e:
                        logger.error(f"Error retrieving result file: {str(e)}", extra={"markup": True})
                else:
                    logger.error(f"Rails error: {result.get('message', 'Unknown error')}", extra={"markup": True})
                    if 'backtrace' in result:
                        logger.error(f"Backtrace: {result['backtrace'][:3]}", extra={"markup": True})  # Just first 3 lines

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
                    if "422" in error_message or "Unprocessable Entity" in error_message:
                        error_type = "validation_error"
                    elif "401" in error_message or "403" in error_message or "Unauthorized" in error_message:
                        error_type = "authorization_error"
                    elif "404" in error_message or "Not Found" in error_message:
                        error_type = "not_found_error"
                    elif "500" in error_message or "Internal Server Error" in error_message:
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
                                ("assignee", "assignee_error")
                            ]:
                                if pattern in simple_error:
                                    validation_errors[category] = validation_errors.get(category, 0) + 1
                                    break
                            else:
                                validation_errors["other_validation"] = validation_errors.get("other_validation", 0) + 1

        if failed_creations:
            potential_issues.append(
                {
                    "issue": "failed_creations",
                    "description": f"{len(failed_creations)} work packages failed to be created",
                    "affected_items": failed_creations[:10],  # Limit to first 10
                    "count": len(failed_creations),
                    "error_types": error_types,
                    "validation_errors": validation_errors
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
        filepath = os.path.join(self.data_dir, filename)
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        logger.debug(f"Saved data to {filepath}", extra={"markup": True})
