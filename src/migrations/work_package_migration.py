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
from datetime import datetime

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

    # Define mapping file pattern constant
    WORK_PACKAGE_MAPPING_FILE_PATTERN = "work_package_mapping_{}.json"

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
        self.issue_type_id_mapping = load_json_file(self.data_dir / "issue_type_id_mapping.json", logger) or {}
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
                    issues = self.jira_client.get_all_issues_for_project(
                        project_key, expand_changelog=True
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
                        issues = self.jira_client.get_all_issues_for_project(
                            project_key, expand_changelog=True
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
            jira_issue: The Jira issue dictionary or jira.Issue object
            project_id: The ID of the OpenProject project

        Returns:
            Dictionary with work package data
        """
        # Check if jira_issue is a Jira Issue object or a dictionary
        if hasattr(jira_issue, 'raw'):
            # Extract the necessary fields from the Jira Issue object
            issue_type_id = jira_issue.fields.issuetype.id
            issue_type_name = jira_issue.fields.issuetype.name

            status_id = None
            if hasattr(jira_issue.fields, 'status'):
                status_id = getattr(jira_issue.fields.status, 'id', None)

            assignee_name = None
            if hasattr(jira_issue.fields, 'assignee') and jira_issue.fields.assignee:
                assignee_name = getattr(jira_issue.fields.assignee, 'name', None)

            subject = jira_issue.fields.summary
            description = getattr(jira_issue.fields, 'description', '') or ''

            jira_id = jira_issue.id
            jira_key = jira_issue.key
        else:
            # It's a dictionary (as in the original implementation)
            issue_type_id = jira_issue["issue_type"]["id"]
            issue_type_name = jira_issue["issue_type"]["name"]

            status_id = None
            if "status" in jira_issue and "id" in jira_issue["status"]:
                status_id = jira_issue["status"]["id"]

            assignee_name = None
            if "assignee" in jira_issue and jira_issue["assignee"]:
                assignee_name = jira_issue["assignee"].get("name")

            subject = jira_issue["summary"]
            description = jira_issue.get("description", "")

            jira_id = jira_issue["id"]
            jira_key = jira_issue["key"]

        # Map the issue type
        type_id = None

        # First try to look up directly in issue_type_id_mapping, which is keyed by ID and has a direct OpenProject ID as value
        if self.issue_type_id_mapping and str(issue_type_id) in self.issue_type_id_mapping:
            type_id = self.issue_type_id_mapping[str(issue_type_id)]
        # Then try to look up by ID in the issue_type_mapping
        elif str(issue_type_id) in self.issue_type_mapping:
            type_id = self.issue_type_mapping[str(issue_type_id)].get('openproject_id')
        # Finally, check in mappings object if available
        elif hasattr(self, 'mappings') and self.mappings and hasattr(self.mappings, 'issue_type_id_mapping'):
            # Try to get the ID from the mappings object
            type_id = self.mappings.issue_type_id_mapping.get(str(issue_type_id))

        # Debug mapping information
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

        # Map the status
        status_op_id = None
        if status_id:
            status_op_id = self.status_mapping.get(status_id)

        # Map the assignee
        assigned_to_id = None
        if assignee_name and assignee_name in self.user_mapping:
            assigned_to_id = self.user_mapping[assignee_name]

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
            "jira_key": jira_key
        }

        # Add optional fields if available
        if status_op_id:
            work_package["status_id"] = status_op_id
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

        # Check if Rails client is available - we need it for bulk imports
        if not self.op_client.rails_client:
            logger.error("Rails client is required for work package migration. Please ensure tmux session is running.", extra={"markup": True})
            return {}

        # Get list of Jira projects to process
        jira_projects = list(set(entry.get("jira_key") for entry in self.project_mapping.values() if entry.get("jira_key")))

        if not jira_projects:
            logger.warning("No Jira projects found in mapping, nothing to migrate", extra={"markup": True})
            return {}

        # Check for migration state file to resume from last processed project
        migration_state_file = os.path.join(self.data_dir, "work_package_migration_state.json")
        processed_projects = set()
        last_processed_project = None

        if os.path.exists(migration_state_file):
            try:
                with open(migration_state_file, 'r') as f:
                    migration_state = json.load(f)
                    processed_projects = set(migration_state.get('processed_projects', []))
                    last_processed_project = migration_state.get('last_processed_project')

                logger.info(f"Found migration state - {len(processed_projects)} projects already processed", extra={"markup": True})
                if last_processed_project and last_processed_project in jira_projects:
                    logger.info(f"Last processed project was {last_processed_project} - will resume from there", extra={"markup": True})
            except Exception as e:
                logger.warning(f"Error loading migration state: {str(e)}", extra={"markup": True})

        # Filter unprocessed projects or start from the interrupted project
        remaining_projects = []
        if last_processed_project and last_processed_project in jira_projects:
            last_index = jira_projects.index(last_processed_project)
            remaining_projects = jira_projects[last_index:]
        else:
            remaining_projects = [p for p in jira_projects if p not in processed_projects]

        logger.info(f"Found {len(jira_projects)} Jira projects, will process {len(remaining_projects)} remaining projects", extra={"markup": True})

        # Initialize counters
        total_issues = 0
        total_created = 0

        # Process each project
        with ProgressTracker("Migrating projects", len(remaining_projects), "Recent Projects") as project_tracker:
            for project_key in remaining_projects:
                project_tracker.update_description(f"Processing project {project_key}")
                logger.info(f"Processing project {project_key}", extra={"markup": True})

                # Update the state file at the start of each project processing
                try:
                    with open(migration_state_file, 'w') as f:
                        json.dump({
                            'processed_projects': list(processed_projects),
                            'last_processed_project': project_key,
                            'timestamp': datetime.now().isoformat()
                        }, f, indent=2)
                except Exception as e:
                    logger.warning(f"Error saving migration state: {str(e)}", extra={"markup": True})

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
                    processed_projects.add(project_key)  # Mark as processed even if skipped
                    continue

                op_project_id = project_mapping_entry["openproject_id"]

                # Extract issues for this project
                issues = self.extract_jira_issues(project_key, project_tracker=project_tracker)
                total_issues += len(issues)

                if not issues:
                    logger.warning(f"No issues found for project {project_key}, skipping", extra={"markup": True})
                    project_tracker.add_log_item(f"Skipped: {project_key} (no issues)")
                    project_tracker.increment()
                    processed_projects.add(project_key)  # Mark as processed even if no issues
                    continue

                # Prepare work packages data
                work_packages_data = []
                logger.notice(f"Preparing {len(issues)} work packages for project {project_key}", extra={"markup": True})

                for i, issue in enumerate(issues):
                    try:
                        # Handle both dictionary and jira.Issue objects
                        if hasattr(issue, 'key'):
                            # It's a jira.Issue object
                            issue_key = issue.key
                            issue_id = issue.id
                            issue_summary = issue.fields.summary
                        else:
                            # It's a dictionary
                            issue_key = issue.get("key", "Unknown")
                            issue_id = issue.get("id", "Unknown")
                            issue_summary = issue.get("summary", "Unknown")

                        if i % 10 == 0 or i == len(issues) - 1:  # Log progress every 10 issues
                            project_tracker.update_description(f"Preparing issue {issue_key} ({i+1}/{len(issues)})")

                        if hasattr(self, 'dry_run') and self.dry_run:
                            logger.notice(f"DRY RUN: Would create work package for {issue_key}", extra={"markup": True})
                            # Add a placeholder to mapping for dry runs
                            self.work_package_mapping[issue_id] = {
                                "jira_id": issue_id,
                                "jira_key": issue_key,
                                "openproject_id": None,
                                "subject": issue_summary,
                                "dry_run": True
                            }
                            continue

                        # Prepare work package data
                        try:
                            wp_data = self.prepare_work_package(issue, op_project_id)
                            if wp_data:
                                work_packages_data.append(wp_data)
                        except Exception as e:
                            # Log the error with details about the issue
                            logger.error(f"Error preparing work package for issue {issue_key}: {str(e)}", extra={"markup": True})
                            logger.debug(f"Issue type: {type(issue)}", extra={"markup": True})
                            # Continue with the next issue
                            continue

                    except Exception as e:
                        logger.error(f"Error processing issue at index {i}: {str(e)}", extra={"markup": True})
                        continue

                if hasattr(self, 'dry_run') and self.dry_run:
                    project_tracker.add_log_item(f"DRY RUN: Would create {len(issues)} work packages for {project_key}")
                    project_tracker.increment()
                    continue

                if not work_packages_data:
                    logger.warning(f"No work package data prepared for project {project_key}, skipping", extra={"markup": True})
                    project_tracker.add_log_item(f"Skipped: {project_key} (no work packages prepared)")
                    project_tracker.increment()
                    processed_projects.add(project_key)  # Mark as processed even if no work packages
                    continue

                # --- Enable required types for the project before import ---
                required_type_ids = set(wp['type_id'] for wp in work_packages_data if 'type_id' in wp)
                if op_project_id and required_type_ids:
                    # A simpler, more direct approach with fewer Rails client calls
                    logger.info(f"Enabling types {list(required_type_ids)} for project {op_project_id}", extra={"markup": True})

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

                    # Execute once with simplified error handling
                    types_result = self.op_client.rails_client.execute(enable_types_header + enable_types_script)

                    if types_result.get('status') == 'success':
                        logger.info(f"Types setup complete for project {op_project_id}", extra={"markup": True})
                    else:
                        logger.error(f"Error enabling types: {types_result.get('error')}", extra={"markup": True})
                        # Continue despite errors - the bulk import might still work with default types

                # Bulk create work packages using Rails client
                logger.notice(f"Creating {len(work_packages_data)} work packages for project {project_key}", extra={"markup": True})

                # First, write the work packages data to a JSON file that Rails can read
                temp_file_path = os.path.join(self.data_dir, f"work_packages_{project_key}.json")
                logger.info(f"Writing {len(work_packages_data)} work packages to {temp_file_path}", extra={"markup": True})

                # Ensure each work package has all required fields
                for wp in work_packages_data:
                    # Ensure string values for certain fields
                    if 'subject' in wp:
                        wp['subject'] = str(wp['subject']).replace('"', '\\"').replace("'", "\\'")
                    if 'description' in wp:
                        wp['description'] = str(wp['description']).replace('"', '\\"').replace("'", "\\'")

                    # Store Jira IDs for mapping
                    jira_id = wp.get('jira_id')
                    jira_key = wp.get('jira_key')

                    # Remove fields not needed by OpenProject
                    wp_copy = wp.copy()
                    if 'jira_id' in wp_copy:
                        del wp_copy['jira_id']
                    if 'jira_key' in wp_copy:
                        del wp_copy['jira_key']

                    # Add to the final data
                    wp.update(wp_copy)

                # Write the JSON file
                with open(temp_file_path, "w") as f:
                    json.dump(work_packages_data, f, indent=2)

                # Get container and server info
                container_name = self.op_client.op_config.get("container")
                op_server = self.op_client.op_config.get("server")

                # Define the path for the file inside the container
                container_temp_path = f"/tmp/work_packages_{project_key}.json"

                # Copy the file to the container
                if self.op_client.rails_client.transfer_file_to_container(temp_file_path, container_temp_path):
                    logger.success(f"Successfully copied work packages data to container", extra={"markup": True})
                else:
                    logger.error(f"Failed to transfer work packages file to container", extra={"markup": True})
                    project_tracker.add_log_item(f"Error: {project_key} (file transfer failed)")
                    project_tracker.increment()
                    processed_projects.add(project_key)
                    continue

                # Create a simple Ruby script based on the example
                header_script = f"""
                # Ruby variables from Python
                wp_file_path = '{container_temp_path}'
                result_file_path = '/tmp/wp_result_{project_key}.json'
                """

                main_script = """
                begin
                  require 'json'

                  # Load the data from the JSON file
                  wp_data = JSON.parse(File.read(wp_file_path))
                  puts "Loaded #{wp_data.length} work packages from JSON file"

                  created_packages = []
                  errors = []

                  # Create each work package
                  wp_data.each do |wp_attrs|
                    begin
                      # Store Jira data for mapping
                      jira_id = wp_attrs['jira_id']
                      jira_key = wp_attrs['jira_key']

                      # Remove Jira fields not needed by OpenProject
                      wp_attrs.delete('jira_id')
                      wp_attrs.delete('jira_key')

                      # Create work package object
                      wp = WorkPackage.new(wp_attrs)

                      # Add required fields if missing
                      wp.priority = IssuePriority.default unless wp.priority_id
                      wp.author = User.where(admin: true).first unless wp.author_id
                      wp.status = Status.default unless wp.status_id

                      # Save the work package
                      if wp.save
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
                    'total' => wp_data.length
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

                # Execute the Ruby script
                result = self.op_client.rails_client.execute(header_script + main_script)

                if result.get('status') != 'success':
                    logger.error(f"Rails error during work package creation: {result.get('error', 'Unknown error')}", extra={"markup": True})
                    project_tracker.add_log_item(f"Error: {project_key} (Rails execution failed)")
                    project_tracker.increment()
                    continue

                # Try to get the result file from the container
                result_file_container = f"/tmp/wp_result_{project_key}.json"
                result_file_local = os.path.join(self.data_dir, f"wp_result_{project_key}.json")

                # Initialize variables
                created_count = 0
                errors = []

                # Try to get results from direct output first
                output = result.get('output')
                if isinstance(output, dict) and output.get('status') == 'success':
                    created_wps = output.get('created', [])
                    created_count = len(created_wps)
                    errors = output.get('errors', [])

                    # Update the mapping
                    for wp in created_wps:
                        jira_id = wp.get('jira_id')
                        if jira_id:
                            self.work_package_mapping[jira_id] = wp

                    # Handle errors
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
                else:
                    # If direct output doesn't work, try to get the result file
                    if self.op_client.rails_client.transfer_file_from_container(result_file_container, result_file_local):
                        try:
                            with open(result_file_local, 'r') as f:
                                result_data = json.load(f)

                                if result_data.get('status') == 'success':
                                    created_wps = result_data.get('created', [])
                                    created_count = len(created_wps)
                                    errors = result_data.get('errors', [])

                                    # Update the mapping
                                    for wp in created_wps:
                                        jira_id = wp.get('jira_id')
                                        if jira_id:
                                            self.work_package_mapping[jira_id] = wp

                                    # Handle errors
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
                            logger.error(f"Error processing result file: {str(e)}", extra={"markup": True})
                    else:
                        # Last resort - try to parse the console output
                        logger.warning(f"Could not get result file - parsing console output", extra={"markup": True})
                        if isinstance(output, str):
                            created_matches = re.findall(r"Created work package #(\d+): (.+?)$", output, re.MULTILINE)
                            created_count = len(created_matches)
                            logger.info(f"Found {created_count} created work packages in console output", extra={"markup": True})

                logger.success(f"Created {created_count} work packages for project {project_key} (errors: {len(errors)})", extra={"markup": True})
                total_created += created_count

                project_tracker.add_log_item(f"Completed: {project_key} ({created_count}/{len(issues)} issues)")
                project_tracker.increment()

                # Mark project as successfully processed
                processed_projects.add(project_key)

        # Save the work package mapping
        mapping_file_path = os.path.join(self.data_dir, "work_package_mapping.json")
        save_json_file(self.work_package_mapping, mapping_file_path)

        # Save final migration state
        try:
            with open(migration_state_file, 'w') as f:
                json.dump({
                    'processed_projects': list(processed_projects),
                    'last_processed_project': None,  # Reset the last processed since we're done with it
                    'timestamp': datetime.now().isoformat(),
                    'completed': True
                }, f, indent=2)
        except Exception as e:
            logger.warning(f"Error saving final migration state: {str(e)}", extra={"markup": True})

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

        # Convert Jira Issue objects to dictionaries if needed
        if isinstance(data, list) and data and hasattr(data[0], 'raw'):
            # This is a list of jira.Issue objects
            serializable_data = []
            for item in data:
                if hasattr(item, 'raw'):
                    # Convert Jira Issue to dict
                    serializable_data.append(item.raw)
                else:
                    # Skip this item if it doesn't have 'raw' attribute
                    logger.warning(f"Skipping non-serializable item in {filename}")
            data = serializable_data

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        logger.debug(f"Saved data to {filepath}", extra={"markup": True})

    def import_work_packages_direct(self, issues: List['JiraIssueType'], op_project_id: int = None) -> Dict[str, Any]:
        """
        Migrates work packages directly using API or Rails console.
        Accepts a list of jira.Issue objects.

        Args:
            issues: List of jira.Issue objects to migrate.
            op_project_id: OpenProject project ID to use for the work packages.

        Returns:
            Dictionary with migration summary (created_count, error_count).
        """
        logger.info("Starting direct work package import...", extra={"markup": True})
        created_count = 0
        error_count = 0
        processed_count = 0

        # Determine migration method (API or Rails)
        use_rails = config.migration_config.get("direct_migration", False) and self.op_rails_client
        migration_method = "Rails console" if use_rails else "API"
        logger.info(f"Using {migration_method} for direct work package creation.")

        total_issues = len(issues)
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
                    wp_payload_for_creation = self.prepare_work_package(issue, op_project_id)

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
        mapping_file_path = os.path.join(self.data_dir, "work_package_mapping.json")
        save_json_file(self.work_package_mapping, mapping_file_path) # Use the imported utility function

        logger.success(f"Finished direct work package import. Created: {created_count}, Errors/Skipped: {error_count}")
        return {
            "created_count": created_count,
            "error_count": error_count,
            "total_processed": processed_count
        }

    # --- Helper methods for direct import (Need adaptation for jira.Issue) ---

    def _create_wp_via_rails(self, wp_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Creates a work package using the Rails console client via the proper client method."""
        if not self.op_rails_client:
            logger.error("Rails client not available for direct work package creation.")
            return None

        jira_key = wp_payload.get("jira_key", "UNKNOWN")
        logger.debug(f"Attempting to create WP for {jira_key} via Rails client create_record...")

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
        success, record_data, error_message = self.op_rails_client.create_record("WorkPackage", attributes)

        if success and record_data and record_data.get("id"):
            logger.info(f"Successfully created work package {record_data['id']} for Jira issue {jira_key}")
            return {
                "id": record_data["id"],
                "_type": "WorkPackage",
                "subject": record_data.get("subject"),
            }
        else:
            logger.error(f"Failed to create WP for {jira_key} via Rails client: {error_message}")
            return None

    def run(self, dry_run: bool = False, force: bool = False, mappings=None) -> Dict[str, Any]:
        """
        Run the work package migration process.

        Args:
            dry_run: If True, don't actually create or update anything
            force: If True, force extraction of data even if it already exists
            mappings: Optional mappings object for accessing other migration results

        Returns:
            Dictionary with migration results
        """
        logger.info("Starting work package migration", extra={"markup": True})

        # Set dry_run flag
        self.dry_run = dry_run

        # Store mappings reference
        self.mappings = mappings

        # Load mappings if provided
        if mappings:
            self.project_mapping = mappings.get_mapping("project") or {}
            self.user_mapping = mappings.get_mapping("user") or {}
            self.issue_type_mapping = mappings.get_mapping("issue_type") or {}
            self.status_mapping = mappings.get_mapping("status") or {}

        # Run the migration
        return self.migrate_work_packages()
