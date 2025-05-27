"""Tests for the workflow migration component."""

import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, mock_open, patch

from src.migrations.workflow_migration import WorkflowMigration


class TestWorkflowMigration(unittest.TestCase):
    """Test cases for the WorkflowMigration class."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        # Sample Jira statuses data
        self.jira_statuses = [
            {
                "id": "1",
                "name": "Open",
                "statusCategory": {"key": "new", "colorName": "#4A6785"},
            },
            {
                "id": "2",
                "name": "In Progress",
                "statusCategory": {"key": "indeterminate", "colorName": "#FFC400"},
            },
            {
                "id": "3",
                "name": "Done",
                "statusCategory": {"key": "done", "colorName": "#14892C"},
            },
            {
                "id": "4",
                "name": "Custom Status",
                "statusCategory": {"key": "indeterminate", "colorName": "#6C8CD5"},
            },
        ]

        # Sample OpenProject statuses data
        self.op_statuses = [
            {
                "id": 1,
                "name": "Open",
                "isClosed": False,
                "color": "#1A67A3",
                "position": 1,
                "_links": {"self": {"href": "/api/v3/statuses/1"}},
            },
            {
                "id": 2,
                "name": "In Progress",
                "isClosed": False,
                "color": "#F0AD4E",
                "position": 2,
                "_links": {"self": {"href": "/api/v3/statuses/2"}},
            },
            {
                "id": 3,
                "name": "Done",
                "isClosed": True,
                "color": "#5CB85C",
                "position": 3,
                "_links": {"self": {"href": "/api/v3/statuses/3"}},
            },
        ]

        # Sample Jira workflows data
        self.jira_workflows = [
            {
                "name": "Default Workflow",
                "description": "Default workflow",
                "transitions": [
                    {"from": "Open", "to": "In Progress", "name": "Start Progress"},
                    {"from": "In Progress", "to": "Done", "name": "Complete"},
                    {"from": "Done", "to": "Open", "name": "Reopen"},
                ],
            },
            {
                "name": "Bug Workflow",
                "description": "Workflow for bugs",
                "transitions": [
                    {"from": "Open", "to": "In Progress", "name": "Start Progress"},
                    {
                        "from": "In Progress",
                        "to": "Custom Status",
                        "name": "Need Testing",
                    },
                    {"from": "Custom Status", "to": "Done", "name": "Test Passed"},
                ],
            },
        ]

        # Expected status mapping
        self.expected_status_mapping = {
            "1": {
                "jira_id": "1",
                "jira_name": "Open",
                "openproject_id": 1,
                "openproject_name": "Open",
                "is_closed": False,
                "matched_by": "name",
            },
            "2": {
                "jira_id": "2",
                "jira_name": "In Progress",
                "openproject_id": 2,
                "openproject_name": "In Progress",
                "is_closed": False,
                "matched_by": "name",
            },
            "3": {
                "jira_id": "3",
                "jira_name": "Done",
                "openproject_id": 3,
                "openproject_name": "Done",
                "is_closed": True,
                "matched_by": "name",
            },
            "4": {
                "jira_id": "4",
                "jira_name": "Custom Status",
                "openproject_id": None,
                "openproject_name": None,
                "is_closed": False,
                "matched_by": "none",
            },
        }

    @patch("src.migrations.workflow_migration.JiraClient")
    @patch("src.migrations.workflow_migration.OpenProjectClient")
    @patch("src.migrations.workflow_migration.config.get_path")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_extract_jira_statuses(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the extract_jira_statuses method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_jira_instance.jira._session.get.return_value.json.return_value = self.jira_statuses
        mock_jira_instance.jira._session.get.return_value.raise_for_status = MagicMock()
        mock_jira_instance.base_url = "https://jira.local"

        mock_op_instance = mock_op_client.return_value
        mock_get_path.return_value = Path("/tmp/test_data")

        # Create instance and call method
        migration = WorkflowMigration(mock_jira_instance, mock_op_instance)
        result = migration.extract_jira_statuses()

        # Assertions
        assert result == self.jira_statuses
        mock_jira_instance.jira._session.get.assert_called_once_with("https://jira.local/rest/api/2/status")
        mock_file.assert_called_with(Path("/tmp/test_data/jira_statuses.json"), "w")
        mock_file().write.assert_called()

    @patch("src.migrations.workflow_migration.JiraClient")
    @patch("src.migrations.workflow_migration.OpenProjectClient")
    @patch("src.migrations.workflow_migration.config.get_path")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_extract_openproject_statuses(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the extract_openproject_statuses method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value
        mock_op_instance.get_statuses.return_value = self.op_statuses

        mock_get_path.return_value = Path("/tmp/test_data")

        # Create instance and call method
        migration = WorkflowMigration(mock_jira_instance, mock_op_instance)
        result = migration.extract_openproject_statuses()

        # Assertions
        assert result == self.op_statuses
        mock_op_instance.get_statuses.assert_called_once()
        mock_file.assert_called_with(Path("/tmp/test_data/openproject_statuses.json"), "w")
        mock_file().write.assert_called()

    @patch("src.migrations.workflow_migration.JiraClient")
    @patch("src.migrations.workflow_migration.OpenProjectClient")
    @patch("src.migrations.workflow_migration.config.get_path")
    @patch("src.migrations.workflow_migration.ProgressTracker")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_create_status_mapping(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_progress_tracker: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the create_status_mapping method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        # Mock tracker context
        mock_progress_tracker.return_value.__enter__.return_value

        mock_get_path.return_value = Path("/tmp/test_data")

        # Create instance and set data for the test
        migration = WorkflowMigration(mock_jira_instance, mock_op_instance)
        migration.jira_statuses = self.jira_statuses
        migration.op_statuses = self.op_statuses

        # Call method
        result = migration.create_status_mapping()

        # Assertions
        assert len(result) == 4  # One mapping entry per Jira status

        # Open, In Progress, and Done should match by name
        assert result["1"]["openproject_id"] == 1
        assert result["1"]["matched_by"] == "name"

        assert result["2"]["openproject_id"] == 2
        assert result["2"]["matched_by"] == "name"

        assert result["3"]["openproject_id"] == 3
        assert result["3"]["matched_by"] == "name"

        # Custom Status should have no match
        assert result["4"]["openproject_id"] is None
        assert result["4"]["matched_by"] == "none"

        mock_file.assert_called_with(Path("/tmp/test_data/status_mapping.json"), "w")
        mock_file().write.assert_called()

    @patch("src.migrations.workflow_migration.JiraClient")
    @patch("src.migrations.workflow_migration.OpenProjectClient")
    @patch("src.migrations.workflow_migration.config.get_path")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_create_status_in_openproject(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the create_status_in_openproject method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        # Mock successful status creation
        mock_op_instance.create_status.return_value = {
            "success": True,
            "data": {
                "id": 4,
                "name": "Custom Status",
                "isClosed": False,
                "color": "#6C8CD5",
            },
        }

        mock_get_path.return_value = Path("/tmp/test_data")

        # Create instance and call method with Custom Status
        migration = WorkflowMigration(mock_jira_instance, mock_op_instance)
        result = migration.create_status_in_openproject(self.jira_statuses[3])

        # Assertions
        assert result["id"] == 4
        assert result["name"] == "Custom Status"
        mock_op_instance.create_status.assert_called_with(name="Custom Status", color="#6C8CD5", is_closed=False)

    @patch("src.migrations.workflow_migration.JiraClient")
    @patch("src.migrations.workflow_migration.OpenProjectClient")
    @patch("src.migrations.workflow_migration.config.get_path")
    @patch("src.migrations.workflow_migration.ProgressTracker")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_migrate_statuses(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_progress_tracker: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the migrate_statuses method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        # Mock tracker context
        mock_progress_tracker.return_value.__enter__.return_value

        # Mock successful status creation for Custom Status
        mock_op_instance.create_status.return_value = {
            "success": True,
            "data": {
                "id": 4,
                "name": "Custom Status",
                "isClosed": False,
                "color": "#6C8CD5",
            },
        }

        mock_get_path.return_value = Path("/tmp/test_data")

        # Create instance and set data for the test
        migration = WorkflowMigration(mock_jira_instance, mock_op_instance)
        migration.jira_statuses = self.jira_statuses
        migration.op_statuses = self.op_statuses
        migration.status_mapping = self.expected_status_mapping

        # Call method
        result = migration.migrate_statuses()

        # Assertions
        # Only 'Custom Status' should be created as it has no mapping
        mock_op_instance.create_status.assert_called_once()
        assert result["4"]["openproject_id"] == 4
        assert result["4"]["matched_by"] == "created"

        mock_file.assert_called_with(Path("/tmp/test_data/status_mapping.json"), "w")
        mock_file().write.assert_called()

    @patch("src.migrations.workflow_migration.JiraClient")
    @patch("src.migrations.workflow_migration.OpenProjectClient")
    @patch("src.migrations.workflow_migration.config.get_path")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_create_workflow_configuration(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the create_workflow_configuration method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        mock_get_path.return_value = Path("/tmp/test_data")

        # Create instance and call method
        migration = WorkflowMigration(mock_jira_instance, mock_op_instance)
        result = migration.create_workflow_configuration()

        # Assertions
        assert result["success"]
        assert "automatically" in result["message"]
        assert "automatically" in result["details"]

        mock_file.assert_called_with(Path("/tmp/test_data/workflow_configuration.json"), "w")
        mock_file().write.assert_called()

    @patch("src.migrations.workflow_migration.JiraClient")
    @patch("src.migrations.workflow_migration.OpenProjectClient")
    @patch("src.migrations.workflow_migration.config.get_path")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_extract_jira_workflows(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the extract_jira_workflows method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        # Setup for _get_jira_workflows method
        migration = WorkflowMigration(mock_jira_instance, mock_op_instance)

        # Mock _get_jira_workflows
        with patch.object(
            migration,
            "_get_jira_workflows",
            return_value=self.jira_workflows
        ) as mock_get_jira_workflows:
            mock_get_path.return_value = Path("/tmp/test_data")

            # Call method
            result = migration.extract_jira_workflows()

            # Assertions
            assert result == self.jira_workflows
            assert mock_get_jira_workflows.call_count == 1

            # Check that we opened a file with a specific name pattern for writing
            # The exact path may vary due to mock implementation details
            assert any("/jira_workflows.json" in str(call) and "w" in str(call) for call in mock_file.call_args_list)
            mock_file().write.assert_called()


# Define testing steps for workflow migration validation


def workflow_migration_test_steps() -> Any:
    """Testing steps for workflow migration validation.

    These steps should be executed in a real environment to validate
    the workflow migration functionality:

    1. Verify status extraction from Jira:
       - Check that all Jira statuses are extracted correctly
       - Verify key attributes (name, category, color)

    2. Verify status extraction from OpenProject:
       - Check that existing OpenProject statuses are identified
       - Verify key attributes (name, color, closed flag)

    3. Test status mapping creation:
       - Check that exact matches by name are correctly mapped
       - Verify the mapping file is created with correct information

    4. Test status creation in OpenProject:
       - Identify Jira statuses that have no match in OpenProject
       - Run the migration for these statuses
       - Verify statuses are created in OpenProject with correct attributes

    5. Test the complete status migration process:
       - Run the migrate_statuses method
       - Verify that unmatched statuses are created in OpenProject
       - Check that the mapping file is updated correctly

    6. Verify workflow configuration in OpenProject:
       - Check that workflow configuration instructions are generated
       - Understand that OpenProject automatically makes all statuses available
         for all work package types by default
       - Verify that any custom workflow configurations are documented

    7. Manual configuration of workflows:
       - Using the Admin interface in OpenProject, navigate to:
         Administration > Work packages > Types
       - For each work package type, verify the available statuses
       - Configure any specific workflow rules needed based on the mapping
       - Test transitions between statuses for each work package type

    8. Test workflow usage in work package migration:
       - Create test issues in Jira with different statuses
       - Run the work package migration
       - Verify the work packages are created with correct statuses in OpenProject
       - Test status transitions for migrated work packages

    9. Verify status configuration in real projects:
        - Check status transitions in real project contexts
        - Verify that status workflows match the original Jira configuration
          as closely as possible
    """
    return "Workflow migration test steps defined"
