"""
Project migration module for Jira to OpenProject migration.
Handles the migration of projects and their hierarchies from Jira to OpenProject.
"""

import os
import sys
import json
import re
import requests
import time
from typing import Dict, List, Any, Optional
from collections import deque

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src import config
from src.display import ProgressTracker, process_with_progress, console, print_info

# Get logger from config
logger = config.logger


class ProjectMigration:
    """
    Handles the migration of projects from Jira to OpenProject.

    This class is responsible for:
    1. Extracting projects from Jira
    2. Creating corresponding projects in OpenProject
    3. Setting account information as custom field values
    4. Creating a mapping between Jira and OpenProject project IDs for later use

    The structure created in OpenProject is:
    - Top-level projects representing Tempo companies (created by company_migration.py)
    - Projects with account information stored in custom fields
    """

    def __init__(self, dry_run: bool = False):
        """
        Initialize the project migration tools.

        Args:
            dry_run: If True, no changes will be made to OpenProject
        """
        self.jira_client = JiraClient()
        self.op_client = OpenProjectClient()
        self.jira_projects = []
        self.op_projects = []
        self.project_mapping = {}
        self.account_mapping = {}
        self.project_account_mapping = {}
        self.account_custom_field_id = None

        # Use the centralized config for var directories
        self.data_dir = config.get_path("data")
        self.dry_run = dry_run

        # Base Tempo API URL - typically {JIRA_URL}/rest/tempo-accounts/1 for Server
        self.tempo_api_base = f"{config.jira_config.get('url', '').rstrip('/')}/rest/tempo-accounts/1"

        # Setup auth for Tempo API
        self.tempo_auth_headers = {
            "Authorization": f"Bearer {config.jira_config.get('api_token', '')}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def extract_jira_projects(self) -> List[Dict[str, Any]]:
        """
        Extract projects from Jira.

        Returns:
            List of Jira projects
        """
        logger.info("Extracting projects from Jira...")

        # Connect to Jira
        if not self.jira_client.connect():
            logger.error("Failed to connect to Jira")
            return []

        # Get all projects from Jira
        self.jira_projects = self.jira_client.get_projects()

        # Log the number of projects found
        logger.info(f"Extracted {len(self.jira_projects)} projects from Jira")

        # Save projects to file for later reference
        self._save_to_json(self.jira_projects, "jira_projects.json")

        return self.jira_projects

    def extract_openproject_projects(self) -> List[Dict[str, Any]]:
        """
        Extract projects from OpenProject.

        Returns:
            List of OpenProject project dictionaries
        """
        logger.info("Extracting projects from OpenProject...")

        # Get all projects from OpenProject
        self.op_projects = self.op_client.get_projects()

        # Log the number of projects found
        logger.info(f"Extracted {len(self.op_projects)} projects from OpenProject")

        # Save projects to file for later reference
        self._save_to_json(self.op_projects, "openproject_projects.json")

        return self.op_projects

    def load_account_mapping(self) -> Dict[str, Any]:
        """
        Load the account mapping created by the account migration.

        Returns:
            Dictionary mapping Tempo account IDs to OpenProject custom field data
        """
        mapping_path = os.path.join(self.data_dir, "account_mapping.json")

        if os.path.exists(mapping_path):
            with open(mapping_path, "r") as f:
                self.account_mapping = json.load(f)

            # Extract the custom field ID from the mapping
            for account in self.account_mapping.values():
                if account.get("custom_field_id"):
                    self.account_custom_field_id = account.get("custom_field_id")
                    break

            logger.info(f"Loaded account mapping with {len(self.account_mapping)} entries")
            # Clean any potential console output from the custom_field_id
            if self.account_custom_field_id and isinstance(self.account_custom_field_id, str):
                # Remove any console output that might be in the string
                self.account_custom_field_id = self.account_custom_field_id.split()[0] if ' ' in self.account_custom_field_id else self.account_custom_field_id
            logger.info(f"Account custom field ID: {self.account_custom_field_id}")
            return self.account_mapping
        else:
            logger.warning("No account mapping found. Account information won't be migrated.")
            return {}

    def extract_project_account_mapping(self) -> Dict[str, Any]:
        """
        Extract mapping between Jira projects and Tempo accounts.

        This method uses the Tempo API to find the default account for each project.

        Returns:
            Dictionary mapping Jira project keys to Tempo account IDs
        """
        logger.info("Extracting project-account mapping from Tempo...")

        if not self.jira_projects:
            self.extract_jira_projects()

        # Create a lookup dictionary for Jira project IDs to keys
        jira_projects_by_id = {
            project.get("id"): project.get("key")
            for project in self.jira_projects
        }

        try:
            # Call the Tempo API to get project-account associations
            account_endpoint = f"{self.tempo_api_base}/account"

            response = requests.get(
                account_endpoint,
                headers=self.tempo_auth_headers,
                verify=config.migration_config.get("ssl_verify", True),
            )

            if response.status_code == 200:
                accounts = response.json()
                logger.info(f"Retrieved {len(accounts)} accounts from Tempo")

                # Create mapping from project key to default account ID
                # In Tempo, each project can have multiple accounts, but one is designated as the default
                mapping = {}
                for account in accounts:
                    # Each account may have multiple associated projects
                    projects = account.get("projects", [])
                    account_id = account.get("id")

                    for project in projects:
                        project_id = project.get("projectId")
                        # Check if this is a default project-account association
                        is_default = project.get("default", False)

                        if project_id in jira_projects_by_id and account_id:
                            project_key = jira_projects_by_id[project_id]
                            # If this is the default account or we haven't found any account yet
                            if is_default or project_key not in mapping:
                                mapping[project_key] = account_id

                # Save to file
                self.project_account_mapping = mapping
                self._save_to_json(mapping, "project_account_mapping.json")

                logger.info(f"Created project-account mapping with {len(mapping)} entries")
                return mapping
            else:
                logger.error(
                    f"Failed to get accounts. Status code: {response.status_code}, Response: {response.text}"
                )

                return {}

        except Exception as e:
            logger.error(f"Failed to extract project-account mapping: {str(e)}")
            return {}

    def create_project_in_openproject(
        self, jira_project: Dict[str, Any], account_id: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Create a project in OpenProject based on a Jira project.

        Args:
            jira_project: Jira project data
            account_id: Optional ID of the associated Tempo account

        Returns:
            OpenProject project data or None if creation failed
        """
        # Extract project information from Jira project
        jira_key = jira_project.get("key", "")
        jira_name = jira_project.get("name", "")
        jira_description = jira_project.get("description", "")

        # Create a valid OpenProject identifier from the Jira key
        # OpenProject identifiers must be lowercase alphanumeric with dashes only
        identifier = re.sub(r"[^a-zA-Z0-9]", "-", jira_key.lower())
        # Ensure it starts with a letter and limit to 100 chars
        if not identifier[0].isalpha():
            identifier = "p-" + identifier
        identifier = identifier[:100]

        # Get account information if available
        account_name = None
        if account_id and str(account_id) in self.account_mapping:
            account_name = self.account_mapping[str(account_id)].get("tempo_name")

        # Since we're now using a progress bar, we'll avoid excessive logging
        if self.dry_run:
            return {
                "id": None,
                "name": jira_name,
                "identifier": identifier,
                "jira_key": jira_key,
                "account_name": account_name,
            }

        # Create the project in OpenProject
        try:
            # First create the project - unpack the tuple to get the project object and creation status
            project_result, was_created = self.op_client.create_project(
                name=jira_name,
                identifier=identifier,
                description=jira_description
            )

            if not project_result:
                logger.error(f"Failed to create project: {jira_name}")
                return None

            # Log appropriate message based on whether project was created or found
            if was_created:
                logger.info(f"Created new project: {jira_name} with identifier {identifier}")
            else:
                logger.info(f"Using existing project: {jira_name} with identifier {identifier}")

            # If we have account information and a custom field, update the project with account data
            if account_name and self.account_custom_field_id:
                # Set the account custom field for this project
                cf_success = self.op_client.set_project_custom_field(
                    project_id=project_result.get("id"),
                    custom_field_id=self.account_custom_field_id,
                    value=account_name
                )

            # Add the account name to the result for reference
            if account_name:
                project_result["account_name"] = account_name

            return project_result
        except Exception as e:
            error_msg = str(e)
            if "422 Client Error" in error_msg and "Unprocessable Entity" in error_msg:
                logger.warning(f"Project {jira_name} may already exist with a different identifier")
            else:
                logger.error(f"Error creating project {jira_name}: {error_msg}")
            return None

    def migrate_projects(self) -> Dict[str, Any]:
        """
        Migrate projects from Jira to OpenProject.

        Returns:
            Dictionary mapping Jira project keys to OpenProject project IDs
        """
        logger.info("Starting project migration...")

        # Make sure we have Jira projects
        if not self.jira_projects:
            self.extract_jira_projects()

        # Get existing OpenProject projects to avoid duplicates
        if not self.op_projects:
            self.extract_openproject_projects()

        # Load account mapping to get the custom field ID and account data
        if not self.account_mapping:
            self.load_account_mapping()

        # Load project-account mapping to associate projects with accounts
        if not self.project_account_mapping:
            self.extract_project_account_mapping()

        # Create lookup dictionaries for OpenProject projects
        op_projects_by_name = {
            project.get("name", "").lower(): project for project in self.op_projects
        }
        op_projects_by_identifier = {
            project.get("identifier", "").lower(): project
            for project in self.op_projects
        }

        # Initialize the mapping dictionary
        mapping = {}

        # Define the function to process each project
        def process_project(jira_project, context):
            jira_key = jira_project.get("key")
            jira_name = jira_project.get("name", "")

            # Create a potential identifier for checking if project already exists
            potential_identifier = re.sub(r"[^a-zA-Z0-9]", "-", jira_key.lower())
            if not potential_identifier[0].isalpha():
                potential_identifier = "p-" + potential_identifier
            potential_identifier = potential_identifier[:100]

            # Check if project already exists in OpenProject
            op_project = None
            if jira_name.lower() in op_projects_by_name:
                op_project = op_projects_by_name[jira_name.lower()]
            elif potential_identifier in op_projects_by_identifier:
                op_project = op_projects_by_identifier[potential_identifier]
            else:
                # Check if this project has an account association
                account_id = None
                if jira_key in self.project_account_mapping:
                    account_id = self.project_account_mapping[jira_key]

                # Create the project in OpenProject
                op_project = self.create_project_in_openproject(jira_project, account_id)

            if op_project:
                # Add to mapping
                mapping[jira_key] = {
                    "jira_key": jira_key,
                    "jira_name": jira_project.get("name"),
                    "openproject_id": op_project.get("id"),
                    "openproject_identifier": op_project.get("identifier"),
                    "openproject_name": op_project.get("name"),
                    "account_id": self.project_account_mapping.get(jira_key),
                    "account_name": op_project.get("account_name"),
                    "created_new": op_project.get("id")
                    not in [p.get("id") for p in self.op_projects],
                }

                # Return the project name if it was newly created
                if op_project.get("id") not in [p.get("id") for p in self.op_projects]:
                    return jira_name
            else:
                # Add to mapping as failed
                mapping[jira_key] = {
                    "jira_key": jira_key,
                    "jira_name": jira_project.get("name"),
                    "openproject_id": None,
                    "openproject_identifier": None,
                    "openproject_name": None,
                    "account_id": self.project_account_mapping.get(jira_key),
                    "account_name": None,
                    "created_new": False,
                    "failed": True,
                }

            return None  # Return None if not a newly created project

        # Process all projects with progress tracking
        process_with_progress(
            items=self.jira_projects,
            process_func=process_project,
            description="Migrating projects",
            log_title="Projects Being Created",
            item_name_func=lambda project: project.get("name", "Unknown")
        )

        # Save mapping to file
        self.project_mapping = mapping
        self._save_to_json(mapping, "project_mapping.json")

        # Analyze the mapping
        analysis = self.analyze_project_mapping()

        if self.dry_run:
            logger.info("DRY RUN: No projects were actually created in OpenProject")

        return mapping

    def analyze_project_mapping(self) -> Dict[str, Any]:
        """
        Analyze the project mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        if not self.project_mapping:
            if os.path.exists(os.path.join(self.data_dir, "project_mapping.json")):
                with open(
                    os.path.join(self.data_dir, "project_mapping.json"), "r"
                ) as f:
                    self.project_mapping = json.load(f)
            else:
                logger.error("No project mapping found. Run migrate_projects() first.")
                return {}

        # Analyze the mapping
        analysis = {
            "total_projects": len(self.project_mapping),
            "migrated_projects": sum(
                1
                for p in self.project_mapping.values()
                if p.get("openproject_id") is not None
            ),
            "new_projects": sum(
                1 for p in self.project_mapping.values() if p.get("created_new", False)
            ),
            "existing_projects": sum(
                1
                for p in self.project_mapping.values()
                if p.get("openproject_id") is not None
                and not p.get("created_new", False)
            ),
            "projects_with_accounts": sum(
                1
                for p in self.project_mapping.values()
                if p.get("account_name") is not None
            ),
            "failed_projects": sum(
                1 for p in self.project_mapping.values() if p.get("status") == "failed"
            ),
            "failed_details": [
                {"jira_key": p.get("jira_key"), "jira_name": p.get("jira_name")}
                for p in self.project_mapping.values()
                if p.get("status") == "failed"
            ],
        }

        # Calculate percentage
        total = analysis["total_projects"]
        if total > 0:
            analysis["migration_percentage"] = (
                analysis["migrated_projects"] / total
            ) * 100
        else:
            analysis["migration_percentage"] = 0

        # Save analysis to file
        self._save_to_json(analysis, "project_mapping_analysis.json")

        # Log analysis summary
        logger.info(f"Project mapping analysis complete")
        logger.info(f"Total projects: {analysis['total_projects']}")
        logger.info(
            f"Migrated projects: {analysis['migrated_projects']} ({analysis['migration_percentage']:.1f}%)"
        )
        logger.info(f"- Newly created: {analysis['new_projects']}")
        logger.info(f"- Already existing: {analysis['existing_projects']}")
        logger.info(f"- With account information: {analysis['projects_with_accounts']}")
        logger.info(f"Failed projects: {analysis['failed_projects']}")

        return analysis

    def _save_to_json(self, data: Any, filename: str):
        """
        Save data to a JSON file in the data directory.

        Args:
            data: Data to save
            filename: Name of the file to save to
        """
        file_path = os.path.join(self.data_dir, filename)
        with open(file_path, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Saved data to {file_path}")


def run_project_migration(dry_run: bool = False):
    """
    Run the project migration as a standalone script.

    Args:
        dry_run: If True, no changes will be made to OpenProject
    """
    logger.info("Starting project migration")
    migration = ProjectMigration(dry_run=dry_run)

    # Extract projects from both systems
    migration.extract_jira_projects()
    migration.extract_openproject_projects()

    # Load account mapping and create project-account mapping
    migration.load_account_mapping()
    migration.extract_project_account_mapping()

    # Migrate and analyze the projects
    migration.migrate_projects()
    migration.analyze_project_mapping()

    logger.info("Project migration complete")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Migrate projects from Jira to OpenProject"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode (no changes to OpenProject)",
    )
    args = parser.parse_args()

    run_project_migration(dry_run=args.dry_run)
