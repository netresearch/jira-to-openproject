"""
Workflow migration module for Jira to OpenProject migration.
Handles the migration of workflow states and their transitions from Jira to OpenProject.
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
from src import config
from src.display import ProgressTracker, console

# Get logger from config
logger = config.logger


class WorkflowMigration:
    """
    Handles the migration of workflows from Jira to OpenProject.

    This class is responsible for:
    1. Extracting workflow definitions from Jira
    2. Creating corresponding workflow states in OpenProject
    3. Mapping workflow transitions between the systems
    4. Setting up workflow configurations in OpenProject
    """

    def __init__(self, dry_run: bool = False):
        """
        Initialize the workflow migration tools.

        Args:
            dry_run: If True, no changes will be made to OpenProject
        """
        self.jira_client = JiraClient()
        self.op_client = OpenProjectClient()
        self.jira_statuses = []
        self.jira_workflows = []
        self.op_statuses = []
        self.status_mapping = {}
        self.workflow_mapping = {}
        self.dry_run = dry_run

        # Use the centralized config for var directories
        self.data_dir = config.get_path("data")

    def extract_jira_workflows(self) -> List[Dict[str, Any]]:
        """
        Extract workflow definitions from Jira.

        Returns:
            List of Jira workflow definitions
        """
        logger.info("Extracting workflows from Jira...", extra={"markup": True})

        # Connect to Jira
        if not self.jira_client.connect():
            logger.error("Failed to connect to Jira")
            return []

        # Get workflows from Jira
        self.jira_workflows = self._get_jira_workflows()

        # Log the number of workflows found
        logger.info(f"Extracted {len(self.jira_workflows)} workflows from Jira", extra={"markup": True})

        # Save workflows to file for later reference
        self._save_to_json(self.jira_workflows, "jira_workflows.json")

        return self.jira_workflows

    def _get_jira_workflows(self) -> List[Dict[str, Any]]:
        """
        Get workflow definitions from Jira.

        This method retrieves workflow definitions and their transitions.

        Returns:
            List of workflow definitions
        """
        try:
            # Get all workflows
            url = f"{self.jira_client.base_url}/rest/api/2/workflow"
            response = self.jira_client.jira._session.get(url)
            response.raise_for_status()
            workflows = response.json()

            # Enhance workflow data with additional information about transitions
            for workflow in workflows:
                # Get detailed workflow information with transitions
                workflow_name = workflow.get("name", "")
                url = f"{self.jira_client.base_url}/rest/api/2/workflow/{workflow_name}/transitions"

                try:
                    response = self.jira_client.jira._session.get(url)
                    response.raise_for_status()
                    transitions = response.json()
                    workflow["transitions"] = transitions
                except Exception as e:
                    logger.warning(
                        f"Failed to get transitions for workflow {workflow_name}: {str(e)}",
                        extra={"markup": True}
                    )
                    workflow["transitions"] = []

            return workflows
        except Exception as e:
            logger.error(f"Failed to get workflows from Jira: {str(e)}", extra={"markup": True})
            return []

    def extract_jira_statuses(self) -> List[Dict[str, Any]]:
        """
        Extract workflow statuses from Jira.

        Returns:
            List of Jira status definitions
        """
        logger.info("Extracting statuses from Jira...", extra={"markup": True})

        # Connect to Jira
        if not self.jira_client.connect():
            logger.error("Failed to connect to Jira")
            return []

        try:
            # Get all statuses
            url = f"{self.jira_client.base_url}/rest/api/2/status"
            response = self.jira_client.jira._session.get(url)
            response.raise_for_status()
            statuses = response.json()

            # Log the number of statuses found
            logger.info(f"Extracted {len(statuses)} statuses from Jira", extra={"markup": True})

            # Save statuses to file for later reference
            self.jira_statuses = statuses
            self._save_to_json(statuses, "jira_statuses.json")

            return statuses
        except Exception as e:
            logger.error(f"Failed to get statuses from Jira: {str(e)}", extra={"markup": True})
            return []

    def extract_openproject_statuses(self) -> List[Dict[str, Any]]:
        """
        Extract workflow statuses from OpenProject.

        Returns:
            List of OpenProject status definitions
        """
        logger.info("Extracting statuses from OpenProject...", extra={"markup": True})

        # Get statuses from OpenProject
        try:
            self.op_statuses = self.op_client.get_statuses()
        except Exception as e:
            logger.warning(f"Failed to get statuses from OpenProject: {str(e)}", extra={"markup": True})
            logger.warning("Using an empty list of statuses for OpenProject", extra={"markup": True})
            self.op_statuses = []

        # Log the number of statuses found
        logger.info(f"Extracted {len(self.op_statuses)} statuses from OpenProject", extra={"markup": True})

        # Save statuses to file for later reference
        self._save_to_json(self.op_statuses, "openproject_statuses.json")

        return self.op_statuses

    def create_status_mapping(self) -> Dict[str, Any]:
        """
        Create a mapping between Jira statuses and OpenProject statuses.

        This method creates a mapping based on the status names.

        Returns:
            Dictionary mapping Jira status IDs to OpenProject status data
        """
        logger.info("Creating status mapping...", extra={"markup": True})

        # Make sure we have statuses from both systems
        if not self.jira_statuses:
            self.extract_jira_statuses()

        if not self.op_statuses:
            self.extract_openproject_statuses()

        # Create lookup dictionary for OpenProject statuses by name
        op_statuses_by_name = {
            status.get("name", "").lower(): status for status in self.op_statuses
        }

        mapping = {}

        with ProgressTracker("Mapping statuses", len(self.jira_statuses), "Recent Status Mappings") as tracker:
            for jira_status in self.jira_statuses:
                jira_id = jira_status.get("id")
                jira_name = jira_status.get("name")
                jira_name_lower = jira_name.lower()

                tracker.update_description(f"Mapping status: {jira_name}")

                # Try to find a corresponding OpenProject status by name
                op_status = op_statuses_by_name.get(jira_name_lower, None)

                if op_status:
                    mapping[jira_id] = {
                        "jira_id": jira_id,
                        "jira_name": jira_name,
                        "openproject_id": op_status.get("id"),
                        "openproject_name": op_status.get("name"),
                        "is_closed": op_status.get("isClosed", False),
                        "matched_by": "name",
                    }
                    tracker.add_log_item(f"Matched by name: {jira_name} → {op_status.get('name')}")
                else:
                    # Try to find a similar name by simple normalization
                    # (e.g. "In Progress" vs "In progress")
                    match_found = False
                    for op_name, op_status in op_statuses_by_name.items():
                        if op_name.replace(" ", "").lower() == jira_name_lower.replace(
                            " ", ""
                        ):
                            mapping[jira_id] = {
                                "jira_id": jira_id,
                                "jira_name": jira_name,
                                "openproject_id": op_status.get("id"),
                                "openproject_name": op_status.get("name"),
                                "is_closed": op_status.get("isClosed", False),
                                "matched_by": "normalized_name",
                            }
                            tracker.add_log_item(f"Matched by normalized name: {jira_name} → {op_status.get('name')}")
                            match_found = True
                            break

                    if not match_found:
                        # No match found, add to mapping with empty OpenProject data
                        mapping[jira_id] = {
                            "jira_id": jira_id,
                            "jira_name": jira_name,
                            "openproject_id": None,
                            "openproject_name": None,
                            "is_closed": False,
                            "matched_by": "none",
                        }
                        tracker.add_log_item(f"No match found: {jira_name}")

                tracker.increment()

        # Save mapping to file
        self.status_mapping = mapping
        self._save_to_json(mapping, "status_mapping.json")

        # Log statistics
        total_statuses = len(mapping)
        matched_statuses = sum(
            1 for status in mapping.values() if status["matched_by"] != "none"
        )
        match_percentage = (
            (matched_statuses / total_statuses) * 100 if total_statuses > 0 else 0
        )

        logger.info(f"Status mapping created for {total_statuses} statuses", extra={"markup": True})
        logger.info(
            f"Successfully matched {matched_statuses} statuses ({match_percentage:.1f}%)",
            extra={"markup": True}
        )

        return mapping

    def create_status_in_openproject(
        self, jira_status: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Create a status in OpenProject based on a Jira status.

        Args:
            jira_status: The Jira status definition

        Returns:
            The created OpenProject status or None if creation failed
        """
        name = jira_status.get("name")

        # Get the status category to determine if it's closed
        status_category = jira_status.get("statusCategory", {})
        category_key = status_category.get("key", "undefined")
        is_closed = category_key.lower() == "done"

        # Get the color from the status category or use default
        color = status_category.get("colorName", "#1F75D3")

        logger.info(f"Creating status in OpenProject: {name} (Closed: {is_closed})", extra={"markup": True})

        if self.dry_run:
            logger.info(f"DRY RUN: Would create status: {name} (Closed: {is_closed})", extra={"markup": True})
            # Return a placeholder for dry run
            return {"id": None, "name": name, "isClosed": is_closed, "color": color}

        # Create the status in OpenProject
        try:
            result = self.op_client.create_status(
                name=name, color=color, is_closed=is_closed
            )

            if result.get("success", False):
                logger.info(f"Successfully created status: {name}", extra={"markup": True})
                return result.get("data")
            else:
                logger.error(
                    f"Failed to create status: {name} - {result.get('message', 'Unknown error')}",
                    extra={"markup": True}
                )
                return None
        except Exception as e:
            logger.error(f"Error creating status {name}: {str(e)}", extra={"markup": True})
            return None

    def migrate_statuses(self) -> Dict[str, Any]:
        """
        Migrate statuses from Jira to OpenProject.

        This method creates statuses in OpenProject based on
        Jira status definitions and updates the mapping.

        Returns:
            Updated mapping between Jira statuses and OpenProject statuses
        """
        logger.info("Starting status migration...", extra={"markup": True})

        # Make sure we have statuses from both systems
        if not self.jira_statuses:
            self.extract_jira_statuses()

        if not self.op_statuses:
            self.extract_openproject_statuses()

        # Create an initial mapping if it doesn't exist
        if not self.status_mapping:
            self.create_status_mapping()

        # Find statuses that need to be created
        statuses_to_create = [
            (jira_id, mapping)
            for jira_id, mapping in self.status_mapping.items()
            if mapping["matched_by"] == "none"
        ]

        logger.info(f"Found {len(statuses_to_create)} statuses that need to be created in OpenProject", extra={"markup": True})

        # Iterate through the mapping and create missing statuses
        with ProgressTracker("Migrating statuses", len(statuses_to_create), "Recent Statuses") as tracker:
            for i, (jira_id, mapping) in enumerate(statuses_to_create):
                # Find the Jira status definition
                jira_status = next(
                    (s for s in self.jira_statuses if s.get("id") == jira_id), None
                )

                if not jira_status:
                    logger.warning(f"Could not find Jira status definition for ID: {jira_id}", extra={"markup": True})
                    tracker.add_log_item(f"Skipped: Unknown Jira status ID {jira_id}")
                    tracker.increment()
                    continue

                status_name = jira_status.get("name", "Unknown")
                tracker.update_description(f"Creating status: {status_name}")

                # Create the status in OpenProject
                op_status = self.create_status_in_openproject(jira_status)

                if op_status:
                    # Update the mapping
                    mapping["openproject_id"] = op_status.get("id")
                    mapping["openproject_name"] = op_status.get("name")
                    mapping["is_closed"] = op_status.get("isClosed", False)
                    mapping["matched_by"] = "created"
                    tracker.add_log_item(f"Created: {status_name} (ID: {op_status.get('id')})")
                else:
                    tracker.add_log_item(f"Failed: {status_name}")

                tracker.increment()

        # Save updated mapping to file
        self._save_to_json(self.status_mapping, "status_mapping.json")

        # Log statistics
        total_statuses = len(self.status_mapping)
        matched_statuses = sum(
            1
            for status in self.status_mapping.values()
            if status["matched_by"] != "none"
        )
        created_statuses = sum(
            1
            for status in self.status_mapping.values()
            if status["matched_by"] == "created"
        )

        logger.info(f"Status migration complete for {total_statuses} statuses", extra={"markup": True})
        logger.info(
            f"Successfully matched {matched_statuses} statuses ({matched_statuses / total_statuses * 100:.1f}% of total)",
            extra={"markup": True}
        )
        logger.info(f"- Existing matches: {matched_statuses - created_statuses}", extra={"markup": True})
        logger.info(f"- Newly created: {created_statuses}", extra={"markup": True})

        return self.status_mapping

    def create_workflow_configuration(self) -> Dict[str, Any]:
        """
        Create workflow configuration in OpenProject.

        This configures which statuses are available for which work package types
        based on Jira's workflow configurations.

        Returns:
            Result of the workflow configuration
        """
        logger.info("Creating workflow configuration in OpenProject...", extra={"markup": True})

        if self.dry_run:
            logger.info("DRY RUN: Would configure workflows in OpenProject", extra={"markup": True})

            # For dry run, we'll just return a placeholder
            result = {
                "success": True,
                "message": "DRY RUN: Workflow configuration simulated",
                "details": "In dry run mode, no actual changes are made to OpenProject workflows.",
            }
        else:
            # In a real implementation, this would configure which statuses
            # are available for which work package types, based on Jira's workflows

            # For now, we'll just create a placeholder
            # OpenProject automatically makes all statuses available for all types
            result = {
                "success": True,
                "message": "Workflow configuration handled automatically by OpenProject",
                "details": "OpenProject automatically makes all statuses available for all work package types by default.",
            }

        # Save result
        self._save_to_json(result, "workflow_configuration.json")

        return result

    def analyze_status_mapping(self) -> Dict[str, Any]:
        """
        Analyze the status mapping to identify potential issues.

        Returns:
            Dictionary with analysis results
        """
        if not self.status_mapping:
            mapping_path = os.path.join(self.data_dir, "status_mapping.json")
            if os.path.exists(mapping_path):
                with open(mapping_path, "r") as f:
                    self.status_mapping = json.load(f)
            else:
                logger.error(
                    "No status mapping found. Run create_status_mapping() first.",
                    extra={"markup": True}
                )
                return {}

        # Analyze the mapping
        analysis = {
            "total_statuses": len(self.status_mapping),
            "matched_statuses": sum(
                1
                for status in self.status_mapping.values()
                if status["matched_by"] != "none"
            ),
            "matched_by_name": sum(
                1
                for status in self.status_mapping.values()
                if status["matched_by"] == "name"
            ),
            "matched_by_normalized_name": sum(
                1
                for status in self.status_mapping.values()
                if status["matched_by"] == "normalized_name"
            ),
            "matched_by_creation": sum(
                1
                for status in self.status_mapping.values()
                if status["matched_by"] == "created"
            ),
            "unmatched_statuses": sum(
                1
                for status in self.status_mapping.values()
                if status["matched_by"] == "none"
            ),
            "closed_statuses": sum(
                1
                for status in self.status_mapping.values()
                if status.get("is_closed", False)
            ),
            "unmatched_details": [
                {"jira_id": status["jira_id"], "jira_name": status["jira_name"]}
                for status in self.status_mapping.values()
                if status["matched_by"] == "none"
            ],
        }

        # Calculate percentages
        total = analysis["total_statuses"]
        if total > 0:
            analysis["match_percentage"] = (analysis["matched_statuses"] / total) * 100
        else:
            analysis["match_percentage"] = 0

        # Save analysis to file
        self._save_to_json(analysis, "status_mapping_analysis.json")

        # Log analysis summary
        logger.info(f"Status mapping analysis complete", extra={"markup": True})
        logger.info(f"Total statuses: {analysis['total_statuses']}", extra={"markup": True})
        logger.info(
            f"Matched statuses: {analysis['matched_statuses']} ({analysis['match_percentage']:.1f}%)",
            extra={"markup": True}
        )
        logger.info(f"- Matched by name: {analysis['matched_by_name']}", extra={"markup": True})
        logger.info(
            f"- Matched by normalized name: {analysis['matched_by_normalized_name']}",
            extra={"markup": True}
        )
        logger.info(f"- Created in OpenProject: {analysis['matched_by_creation']}", extra={"markup": True})
        logger.info(f"Closed statuses: {analysis['closed_statuses']}", extra={"markup": True})
        logger.info(f"Unmatched statuses: {analysis['unmatched_statuses']}", extra={"markup": True})

        return analysis

    def _save_to_json(self, data: Any, filename: str):
        """
        Save data to a JSON file.

        Args:
            data: Data to save
            filename: Name of the file to save to
        """
        filepath = os.path.join(self.data_dir, filename)
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Saved data to {filepath}", extra={"markup": True})


def run_workflow_migration(dry_run: bool = False):
    """
    Run the workflow migration as a standalone script.

    Args:
        dry_run: If True, no changes will be made to OpenProject
    """
    logger.info("Starting workflow migration", extra={"markup": True})
    migration = WorkflowMigration(dry_run=dry_run)

    # Extract workflows and statuses from both systems
    migration.extract_jira_workflows()
    migration.extract_jira_statuses()
    migration.extract_openproject_statuses()

    # Create mappings and migrate statuses
    migration.create_status_mapping()
    migration.migrate_statuses()

    # Create workflow configuration
    migration.create_workflow_configuration()

    # Analyze status mapping
    migration.analyze_status_mapping()

    logger.info("Workflow migration complete", extra={"markup": True})


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Migrate workflows from Jira to OpenProject"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode (no changes to OpenProject)",
    )
    args = parser.parse_args()

    run_workflow_migration(dry_run=args.dry_run)
