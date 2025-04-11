"""
Work package migration module for Jira to OpenProject migration.
Handles the migration of Jira issues to OpenProject work packages.
"""

import os
import sys
import json
import re
from typing import Dict, List, Any, Optional
from pathlib import Path

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.clients.openproject_rails_client import OpenProjectRailsClient
from src import config
from src.display import ProgressTracker
from src.utils import save_json_file, load_json_file
from src.config import logger

from jira import Issue as JiraIssueType


class WorkPackageMigration:
    """
    Handles the migration of issues from Jira to work packages in OpenProject.

    This class is responsible for:
    1. Extracting issues from Jira projects
    2. Creating corresponding work packages in OpenProject
    3. Mapping issues between the systems
    4. Handling attachments, comments, and relationships
    """

    def __init__(
        self,
        jira_client: JiraClient,
        op_client: OpenProjectClient,
        op_rails_client: Optional[OpenProjectRailsClient] = None,
        data_dir: str = None
    ):
        """
        Initialize the work package migration.

        Args:
            jira_client: JiraClient instance.
            op_client: OpenProjectClient instance.
            op_rails_client: Optional OpenProjectRailsClient instance.
            data_dir: Path to data directory for storing mappings.
        """
        self.jira_client = jira_client
        self.op_client = op_client
        self.op_rails_client = op_rails_client

        # Configure paths
        self.data_dir = Path(data_dir or config.get_path("data"))
        os.makedirs(self.data_dir, exist_ok=True)

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
        logger.debug(f"WorkPackageMigration initialized with data dir: {self.data_dir}")

    def _load_mappings(self):
        """Load all required mappings from files."""
        # Load mappings from disk
        self.project_mapping = load_json_file(self.data_dir / "project_mapping.json", logger) or {}
        self.user_mapping = load_json_file(self.data_dir / "user_mapping.json", logger) or {}
        self.issue_type_mapping = load_json_file(self.data_dir / "issue_type_mapping.json", logger) or {}
        self.status_mapping = load_json_file(self.data_dir / "status_mapping.json", logger) or {}

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

        try:
            # First, get the total number of issues for this project to set up progress bar
            total_issues = self.jira_client.get_issue_count(project_key)
            if total_issues <= 0:
                logger.warning(f"No issues found for project {project_key}", extra={"markup": True})
                return []

            logger.info(f"Found {total_issues} issues to extract from project {project_key}", extra={"markup": True})

            total_issues = min(10, total_issues);
            batch_size = min(batch_size, total_issues);

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
        type_id = self.issue_type_mapping.get(issue_type_id)

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
        self.mappings.load_mappings()

        # Check if Rails client is available - we need it for bulk imports
        if not hasattr(self.op_client, 'rails_client') or not self.op_client.rails_client:
            logger.error("Rails client is required for work package migration. Please ensure tmux session is running.", extra={"markup": True})
            return {}

        # Get list of Jira projects to process
        jira_projects = list(set(entry.get("jira_key") for entry in self.mappings.project_mapping.values() if entry.get("jira_key")))

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
                logger.info(f"Processing project {project_key}", extra={"markup": True})

                # Find corresponding OpenProject project ID
                project_mapping_entry = None
                for key, entry in self.mappings.project_mapping.items():
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
        mapping_file_path = os.path.join(self.data_dir, self.mappings.WORK_PACKAGE_MAPPING_FILE_PATTERN.format(self.project_key))
        save_json_file(self.work_package_mapping, mapping_file_path) # Use the imported utility function

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
                with open(os.path.join(self.data_dir, self.mappings.WORK_PACKAGE_MAPPING_FILE_PATTERN.format(self.project_key)), "r") as f:
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

    def import_work_packages_direct(self, issues: List['JiraIssueType']) -> Dict[str, Any]:
        """
        Migrates work packages directly using API or Rails console.
        Accepts a list of jira.Issue objects.

        Args:
            issues: List of jira.Issue objects to migrate.

        Returns:
            Dictionary with migration summary (created_count, error_count).
        """
        logger.info(f"Starting direct work package import for project {self.project_key} (OP ID: {self.op_project_id})...")
        created_count = 0
        error_count = 0
        processed_count = 0

        # Determine migration method (API or Rails)
        use_rails = config.migration_config.get("direct_migration", False) and self.op_rails_client
        migration_method = "Rails console" if use_rails else "API"
        logger.info(f"Using {migration_method} for direct work package creation.")

        total_issues = len(issues)
        # Use self.tracker if it exists, otherwise iterate without a specific tracker for this inner loop
        # (Outer loop in export_work_packages already tracks overall progress)
        # Removed internal: with ProgressTracker(...) as tracker:
        for issue in issues:
            if self.tracker:
                self.tracker.update_description(f"Importing {issue.key} via {migration_method} ({processed_count+1}/{total_issues})")
            else: # Log progress if no tracker available
                 if processed_count % 50 == 0: # Log every 50 issues
                      logger.debug(f"Processing issue {processed_count+1}/{total_issues}: {issue.key}")

            jira_key = issue.key

            # Check if already migrated in this run or previous runs
            if jira_key in self.work_package_mapping and self.work_package_mapping[jira_key].get("openproject_id"):
                logger.debug(f"Skipping issue {jira_key} - already mapped to OP ID {self.work_package_mapping[jira_key]['openproject_id']}")
                if self.tracker: self.tracker.increment() # Increment progress even if skipped
                processed_count += 1
                continue

            if self.dry_run:
                logger.info(f"[DRY RUN] Would attempt to create work package for Jira issue {jira_key}")
                # Simulate success for dry run
                self.work_package_mapping[jira_key] = {
                     "jira_id": issue.id,
                     "openproject_id": f"dry_run_{issue.id}",
                     "status": "dry_run_skipped"
                }
                created_count += 1
            else:
                # --- Create Work Package ---
                created_wp = None
                try:
                    # Prepare payload using the Mappings class method
                    # This method already handles jira.Issue objects
                    wp_payload_for_creation = self.mappings.prepare_work_package(issue, self.op_project_id)

                    if not wp_payload_for_creation:
                         logger.warning(f"Failed to prepare payload for issue {jira_key}. Skipping.")
                         error_count += 1
                    else:
                        # Add the Jira status name to the payload for status creation
                        if hasattr(issue, 'fields') and hasattr(issue.fields, 'status') and hasattr(issue.fields.status, 'name'):
                            wp_payload_for_creation['jira_status_name'] = issue.fields.status.name
                            logger.debug(f"Added Jira status name '{issue.fields.status.name}' to work package payload")

                        if use_rails:
                            # Use the _create_wp_via_rails method with the enhanced payload
                            created_wp = self._create_wp_via_rails(wp_payload_for_creation)
                        else:
                            # Use API client
                            created_wp = self.op_client.create_work_package(wp_payload_for_creation)

                        if created_wp and created_wp.get("id"):
                            op_wp_id = created_wp.get("id")
                            logger.info(f"Successfully created work package {op_wp_id} for Jira issue {jira_key}")
                            self.work_package_mapping[jira_key] = {
                                "jira_id": issue.id,
                                "openproject_id": op_wp_id,
                                "status": "created"
                            }
                            created_count += 1

                            # --- Migrate Comments, Attachments, Relations (Optional) ---
                            # These methods also need adapting to take jira.Issue object
                            # self._migrate_comments(issue, op_wp_id)
                            # self._migrate_attachments(issue, op_wp_id)
                            # self._create_relations(issue, op_wp_id)

                        else:
                            # Log failure if Rails creation didn't return expected dict or API failed
                            log_message = f"Failed to create work package for Jira issue {jira_key}."
                            if use_rails and created_wp is None:
                                log_message += " (Rails creation failed or returned None)"
                            elif not use_rails and created_wp is None:
                                log_message += " (API creation failed or returned None)"
                            elif created_wp is not None and not created_wp.get("id"):
                                log_message += f" (Creation method returned unexpected result: {created_wp})"

                            logger.error(log_message)
                            self.work_package_mapping[jira_key] = {"jira_id": issue.id, "status": "creation_failed"}
                            error_count += 1

                except Exception as e:
                     logger.error(f"Error migrating Jira issue {jira_key}: {str(e)}", exc_info=True)
                     self.work_package_mapping[jira_key] = {"jira_id": issue.id, "status": "error"}
                     error_count += 1

            if self.tracker: self.tracker.increment()
            processed_count += 1

        # Save the updated mapping for this project
        mapping_file_path = os.path.join(self.data_dir, self.mappings.WORK_PACKAGE_MAPPING_FILE_PATTERN.format(self.project_key))
        save_json_file(self.work_package_mapping, mapping_file_path) # Use the imported utility function

        logger.success(f"Finished direct work package import for {self.project_key}. Created: {created_count}, Errors/Skipped: {error_count}")
        return {
            "created_count": created_count,
            "error_count": error_count,
            "total_processed": processed_count
        }

    # --- Helper methods for direct import (Need adaptation for jira.Issue) ---

    def _create_wp_via_rails(self, wp_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Creates a work package using the Rails console client."""
        if not self.op_rails_client:
            logger.error("Rails client not available for direct work package creation.")
            return None

        jira_key = wp_payload.get("jira_key", "UNKNOWN")
        jira_status_name = wp_payload.get("jira_status_name")
        logger.debug(f"Attempting to create WP for {jira_key} via Rails...")

        # Get status ID from mapping if available
        status_id = wp_payload.get('status_id')
        if not status_id and jira_status_name:
            # First attempt to get status ID from status mapping in memory
            for status_name, status_info in self.mappings.status_mapping.items():
                if status_name == jira_status_name and 'openproject_id' in status_info:
                    status_id = status_info['openproject_id']
                    logger.debug(f"Found status ID {status_id} for '{jira_status_name}' in mapping")
                    break

            # If still no status ID, use default 'New' status
            if not status_id:
                status_id = 2083761  # Default to 'New' status ID
                logger.debug(f"Using default 'New' status ID {status_id} for '{jira_status_name}'")

        # Get basic required fields
        project_id = wp_payload.get('project_id')
        type_id = wp_payload.get('type_id')

        # Escape single quotes in subject and description
        safe_subject = (wp_payload.get('subject', '') or '').replace("'", "\\'")
        safe_description = (wp_payload.get('description', '') or '').replace("'", "\\'").replace("\n", "\\n")

        # Header section with Python f-string variables
        header_script = f"""
        # Work Package configuration variables
        project_id_var = {str(project_id)}
        type_id_var = {str(type_id)}
        subject_var = '{safe_subject}'
        description_var = '{safe_description}'
        status_id_var = {str(status_id or 2083761)}
        """

        # Add assignee if available
        if wp_payload.get('assigned_to_id'):
            header_script += f"""
        assignee_id_var = {str(wp_payload.get('assigned_to_id'))}
            """

        # Main Ruby section without f-strings
        main_script = """
        begin
          puts "Starting command execution..."
          project = Project.find(project_id_var)
          type_id = type_id_var

          # Check if the type is enabled for the project, if not use the first available type
          unless project.types.map(&:id).include?(type_id)
            puts "Type #{type_id} not available in project, using the first available type"
            if project.types.any?
              type_id = project.types.first.id
            else
              puts "No types available for project, using system default"
              type_id = Type.first.id
            end
          end

          # Create the work package with all required attributes
          wp = WorkPackage.new(
            project_id: project.id,
            type_id: type_id,
            subject: subject_var,
            description: description_var,
            status_id: status_id_var,
            author_id: (User.find_by(admin: true)&.id || User.find_by(id: 1)&.id || User.first&.id),
            priority_id: (IssuePriority.default&.id || IssuePriority.find_by(is_default: true)&.id || IssuePriority.first&.id)
          )
        """

        # Add assignee to main script if available
        if wp_payload.get('assigned_to_id'):
            main_script += """
          wp.assigned_to_id = assignee_id_var
            """

        # Complete the main script
        main_script += """
          if wp.save
            puts "SUCCESS: Work package created with ID: #{wp.id}"
            wp
          else
            puts "ERROR: Failed to save work package. Validation errors:"
            wp.errors.full_messages.each do |msg|
              puts "  - #{msg}"
            end
            puts "Trying to debug missing associations:"
            puts "  - Project exists? #{Project.exists?(wp.project_id)}"
            puts "  - Type exists? #{Type.exists?(wp.type_id)}"
            puts "  - Type available in project? #{Project.find_by(id: wp.project_id)&.types&.map(&:id)&.include?(wp.type_id)}"
            puts "  - Status? #{wp.status_id || 'nil'}"
            puts "  - Priority? #{wp.priority_id || 'nil'}"
            nil
          end
        rescue => e
          puts "EXCEPTION: #{e.class.name}: #{e.message}"
          nil
        end
        """

        # Combine the scripts
        command = header_script + main_script

        try:
            # Corrected method name: execute
            result = self.op_rails_client.execute(command, timeout=180) # Use execute, maybe add timeout

            if result and result.get("status") == "success":
                output_str = result.get("output", "")

                # Check for success message with ID
                id_match = re.search(r"SUCCESS: Work package created with ID: (\d+)", output_str)
                if id_match:
                    wp_id = int(id_match.group(1))
                    logger.debug(f"Rails successfully created WP for {jira_key} with ID: {wp_id}")

                    # Check if we created a new status that needs to be added to the mapping
                    status_mapping_match = re.search(r"STATUS_MAPPING:([^:]+):(\d+)", output_str)
                    if status_mapping_match:
                        status_name = status_mapping_match.group(1)
                        status_id = int(status_mapping_match.group(2))
                        logger.info(f"Created new status mapping: '{status_name}' -> {status_id}")

                        # Update status mapping dynamically
                        if hasattr(self.mappings, 'status_mapping_by_name') and jira_status_name:
                            self.mappings.status_mapping_by_name[jira_status_name] = status_id
                            logger.debug(f"Updated status mapping with new entry: {jira_status_name} -> {status_id}")

                            # Save mapping to file to persist for future runs
                            # (Optional, depends on how mappings are persisted in your system)
                            self.mappings.save_mappings()

                    return {
                        "id": wp_id,
                        "_type": "WorkPackage",
                        "subject": wp_payload.get('subject'),
                    }

                # Log detailed error messages
                if "ERROR:" in output_str:
                    # Extract and log all error lines
                    error_lines = re.findall(r"ERROR:.*|  - .*", output_str)
                    logger.error(f"Failed to create WP for {jira_key}. Errors:")
                    for error in error_lines:
                        logger.error(f"  {error.strip()}")

                    # Extract debug info
                    debug_lines = re.findall(r"Trying to debug.*|  - .*", output_str)
                    if debug_lines:
                        logger.debug("Debug information:")
                        for debug in debug_lines:
                            logger.debug(f"  {debug.strip()}")

                # Check for exception
                if "EXCEPTION:" in output_str:
                    exception_match = re.search(r"EXCEPTION: (.*)", output_str)
                    if exception_match:
                        logger.error(f"Exception in Rails: {exception_match.group(1)}")

                # If we still can't determine what happened, log the raw output
                if not id_match and "ERROR:" not in output_str and "EXCEPTION:" not in output_str:
                    logger.error(f"Unexpected output from Rails for {jira_key}. Output: {output_str}")

                return None
            else:
                logger.error(f"Rails command execution failed for {jira_key}. Status: {result.get('status')}, Error: {result.get('error')}")
                if result and result.get("raw_output"):
                    logger.debug(f"Raw output: {result.get('raw_output')[:500]}")  # Log first 500 chars of raw output
                return None
        except Exception as e:
            logger.error(f"Exception during Rails execution for {jira_key}: {str(e)}", exc_info=True)
            return None
