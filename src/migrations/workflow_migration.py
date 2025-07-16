"""Workflow migration module for Jira to OpenProject migration.

Handles the migration of workflow states and their transitions from Jira to OpenProject.
"""

import json
from pathlib import Path
from typing import Any

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import ProgressTracker

# Get logger from config
logger = config.logger


class WorkflowMigration:
    """Handles the migration of workflows from Jira to OpenProject.

    This class is responsible for:
    1. Extracting workflow definitions from Jira
    2. Creating corresponding workflow states in OpenProject
    3. Mapping workflow transitions between the systems
    4. Setting up workflow configurations in OpenProject
    """

    def __init__(self, jira_client: JiraClient, op_client: OpenProjectClient) -> None:
        """Initialize the workflow migration tools.

        Args:
            jira_client: Initialized Jira client instance.
            op_client: Initialized OpenProject client instance.

        """
        self.jira_client = jira_client
        self.op_client = op_client
        self.jira_statuses: list[dict[str, Any]] = []
        self.jira_workflows: list[dict[str, Any]] = []
        self.op_statuses: list[dict[str, Any]] = []
        self.status_mapping: dict[str, Any] = {}
        self.workflow_mapping: dict[str, Any] = {}

        self.data_dir: Path = config.get_path("data")

    def extract_jira_workflows(self) -> list[dict[str, Any]]:
        """Extract workflow definitions from Jira.

        Returns:
            List of Jira workflow definitions

        """
        logger.info("Extracting workflows from Jira...")

        self.jira_workflows = self._get_jira_workflows()

        logger.info(
            f"Extracted {len(self.jira_workflows)} workflows from Jira",
        )

        self._save_to_json(self.jira_workflows, "jira_workflows.json")

        return self.jira_workflows

    def _get_jira_workflows(self) -> list[dict[str, Any]]:
        """Get workflow definitions from Jira.

        Returns:
            List of workflow definitions

        Raises:
            RuntimeError: When unable to retrieve workflows from Jira

        """
        try:
            url = f"{self.jira_client.base_url}/rest/api/2/workflow"
            response = self.jira_client.jira._session.get(url)  # noqa: SLF001
            response.raise_for_status()
            workflows = response.json()

            for workflow in workflows:
                workflow_name = workflow.get("name", "")
                url = f"{self.jira_client.base_url}/rest/api/2/workflow/{workflow_name}/transitions"

                try:
                    response = self.jira_client.jira._session.get(url)  # noqa: SLF001
                    response.raise_for_status()
                    transitions = response.json()
                    workflow["transitions"] = transitions
                except Exception as e:
                    logger.warning(
                        f"Failed to get transitions for workflow {workflow_name}: {e}",
                    )
                    workflow["transitions"] = []

            return workflows
        except Exception as e:
            logger.exception("Failed to get workflows from Jira: %s", e)
            raise RuntimeError(f"Unable to retrieve workflows from Jira: {e}") from e

    def extract_jira_statuses(self) -> list[dict[str, Any]]:
        """Extract workflow statuses from Jira.

        Returns:
            List of Jira status definitions

        Raises:
            RuntimeError: When unable to retrieve statuses from Jira

        """
        logger.info("Extracting statuses from Jira...")

        try:
            url = f"{self.jira_client.base_url}/rest/api/2/status"
            response = self.jira_client.jira._session.get(url)  # noqa: SLF001
            response.raise_for_status()
            statuses = response.json()

            logger.info("Extracted %s statuses from Jira", len(statuses))

            self.jira_statuses = statuses
            self._save_to_json(statuses, "jira_statuses.json")

            return statuses
        except Exception as e:
            logger.exception("Failed to get statuses from Jira: %s", e)
            raise RuntimeError(f"Unable to retrieve statuses from Jira: {e}") from e

    def extract_openproject_statuses(self) -> list[dict[str, Any]]:
        """Extract workflow statuses from OpenProject.

        Returns:
            List of OpenProject status definitions

        Raises:
            RuntimeError: When unable to retrieve statuses from OpenProject

        """
        logger.info("Extracting statuses from OpenProject...")

        try:
            self.op_statuses = self.op_client.get_statuses()
        except Exception as e:
            logger.exception("Failed to get statuses from OpenProject: %s", e)
            raise RuntimeError(
                f"Unable to retrieve statuses from OpenProject: {e}"
            ) from e

        logger.info(
            f"Extracted {len(self.op_statuses)} statuses from OpenProject",
        )

        self._save_to_json(self.op_statuses, "openproject_statuses.json")

        return self.op_statuses

    def create_status_mapping(self) -> dict[str, Any]:
        """Create a mapping between Jira statuses and OpenProject statuses.

        Returns:
            Dictionary mapping Jira status IDs to OpenProject status data

        """
        logger.info("Creating status mapping...")

        if not self.jira_statuses:
            self.extract_jira_statuses()

        if not self.op_statuses:
            self.extract_openproject_statuses()

        op_statuses_by_name = {
            status.get("name", "").lower(): status for status in self.op_statuses
        }

        mapping = {}

        with ProgressTracker(
            "Mapping statuses", len(self.jira_statuses), "Recent Status Mappings"
        ) as tracker:
            for jira_status in self.jira_statuses:
                jira_id = jira_status.get("id")
                jira_name = jira_status.get("name")
                jira_name_lower = jira_name.lower()

                tracker.update_description(f"Mapping status: {jira_name}")

                op_status = op_statuses_by_name.get(jira_name_lower)

                if op_status:
                    mapping[jira_id] = {
                        "jira_id": jira_id,
                        "jira_name": jira_name,
                        "openproject_id": op_status.get("id"),
                        "openproject_name": op_status.get("name"),
                        "is_closed": op_status.get("isClosed", False),
                        "matched_by": "name",
                    }
                    tracker.add_log_item(
                        f"Matched by name: {jira_name} → {op_status.get('name')}"
                    )
                else:
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
                            tracker.add_log_item(
                                f"Matched by normalized name: {jira_name} → {op_status.get('name')}"
                            )
                            match_found = True
                            break

                    if not match_found:
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

        self.status_mapping = mapping
        self._save_to_json(mapping, "status_mapping.json")

        total_statuses = len(mapping)
        matched_statuses = sum(
            1 for status in mapping.values() if status["matched_by"] != "none"
        )
        match_percentage = (
            (matched_statuses / total_statuses) * 100 if total_statuses > 0 else 0
        )

        logger.info(
            f"Status mapping created for {total_statuses} statuses",
        )
        logger.info(
            f"Successfully matched {matched_statuses} statuses ({match_percentage:.1f}%)",
        )

        return mapping

    def create_status_in_openproject(
        self, jira_status: dict[str, Any]
    ) -> dict[str, Any]:
        """Create a status in OpenProject based on a Jira status.

        Args:
            jira_status: The Jira status definition

        Returns:
            The created OpenProject status

        """
        name = jira_status.get("name")

        status_category = jira_status.get("statusCategory", {})
        category_key = status_category.get("key", "undefined")
        is_closed = category_key.lower() == "done"

        color = status_category.get("colorName", "#1F75D3")

        logger.info(
            "Creating status in OpenProject: %s (Closed: %s).",
            name,
            is_closed,
        )

        try:
            result = self.op_client.create_status(
                name=name, color=color, is_closed=is_closed
            )

            if result.get("success", False):
                logger.info("Successfully created status: %s.", name)
            else:
                message = "Failed to create status: {} - {}.".format(
                    name, result.get("message", "Unknown error")
                )
                logger.error(message)
                raise RuntimeError(message)
        except Exception:
            logger.exception("Error creating status %s.", name)
            raise

        return result.get("data")

    def migrate_statuses(self) -> dict[str, Any]:
        """Migrate statuses from Jira to OpenProject.

        Returns:
            Updated mapping between Jira statuses and OpenProject statuses

        """
        logger.info("Starting status migration...")

        if not self.jira_statuses:
            self.extract_jira_statuses()

        if not self.op_statuses:
            self.extract_openproject_statuses()

        if not self.status_mapping:
            self.create_status_mapping()

        statuses_to_create = [
            (jira_id, mapping)
            for jira_id, mapping in self.status_mapping.items()
            if mapping["matched_by"] == "none"
        ]

        logger.info(
            f"Found {len(statuses_to_create)} statuses that need to be created in OpenProject",
        )

        with ProgressTracker(
            "Migrating statuses", len(statuses_to_create), "Recent Statuses"
        ) as tracker:
            for _i, (jira_id, mapping) in enumerate(statuses_to_create):
                jira_status = next(
                    (s for s in self.jira_statuses if s.get("id") == jira_id), None
                )

                if not jira_status:
                    logger.warning(
                        f"Could not find Jira status definition for ID: {jira_id}",
                    )
                    tracker.add_log_item(f"Skipped: Unknown Jira status ID {jira_id}")
                    tracker.increment()
                    continue

                status_name = jira_status.get("name", "Unknown")
                tracker.update_description(f"Creating status: {status_name}")

                op_status = self.create_status_in_openproject(jira_status)

                if op_status:
                    mapping["openproject_id"] = op_status.get("id")
                    mapping["openproject_name"] = op_status.get("name")
                    mapping["is_closed"] = op_status.get("isClosed", False)
                    mapping["matched_by"] = "created"
                    tracker.add_log_item(
                        f"Created: {status_name} (ID: {op_status.get('id')})"
                    )
                else:
                    tracker.add_log_item(f"Failed: {status_name}")

                tracker.increment()

        self._save_to_json(self.status_mapping, "status_mapping.json")

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

        logger.info(
            f"Status migration complete for {total_statuses} statuses",
        )
        match_percentage = (
            (matched_statuses / total_statuses * 100) if total_statuses > 0 else 0
        )
        logger.info(
            f"Successfully matched {matched_statuses} statuses ({match_percentage:.1f}% of total)",
        )
        logger.info(
            f"- Existing matches: {matched_statuses - created_statuses}",
        )
        logger.info("- Newly created: %s", created_statuses)

        return self.status_mapping

    def create_workflow_configuration(self) -> dict[str, Any]:
        """Create workflow configuration in OpenProject.

        Returns:
            Result of the workflow configuration

        """
        logger.info("Creating workflow configuration in OpenProject...")

        result = {
            "success": True,
            "message": "Workflow configuration handled automatically by OpenProject",
            "details": "OpenProject automatically makes all statuses available for all work package types by default.",
        }

        self._save_to_json(result, "workflow_configuration.json")

        return result

    def _save_to_json(self, data: Any, filename: str) -> None:
        """Save data to a JSON file.

        Args:
            data: Data to save
            filename: Name of the file to save to

        """
        filepath = Path(self.data_dir) / filename
        with filepath.open("w") as f:
            json.dump(data, f, indent=2)
        logger.info("Saved data to %s", filepath)
