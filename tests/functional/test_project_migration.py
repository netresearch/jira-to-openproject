"""Tests for the project migration component."""

import json
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, mock_open, patch

from src import config
from src.migrations.project_migration import ProjectMigration


class TestProjectMigration(unittest.TestCase):
    """Test cases for the ProjectMigration class."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        # Sample Jira projects data
        self.jira_projects = [
            {
                "id": "10001",
                "key": "PROJ1",
                "name": "Project One",
                "description": "First test project",
            },
            {
                "id": "10002",
                "key": "PROJ2",
                "name": "Project Two",
                "description": "Second test project",
            },
            {
                "id": "10003",
                "key": "PROJ3",
                "name": "Project Three",
                "description": "Third test project with no account",
            },
        ]

        # Sample OpenProject projects data
        self.op_projects = [
            {
                "id": 1,
                "name": "Project One",
                "identifier": "proj1",
                "description": {"raw": "First test project"},
            },
            {
                "id": 3,
                "name": "Existing Project",
                "identifier": "existing-project",
                "description": {"raw": "This already exists"},
            },
        ]

        # Sample project account mapping
        self.project_account_mapping = {
            "PROJ1": 101,
            "PROJ2": 102,
        }

        # Sample account mapping
        self.account_mapping = {
            "101": {"tempo_id": "101", "tempo_name": "Account One"},
            "102": {"tempo_id": "102", "tempo_name": "Account Two"},
        }

        # Expected project mapping
        self.expected_mapping = {
            "PROJ1": {
                "jira_key": "PROJ1",
                "jira_name": "Project One",
                "openproject_id": 1,
                "openproject_identifier": "proj1",
                "openproject_name": "Project One",
                "account_id": 101,
                "account_name": "Account One",
                "created_new": False,
            },
            "PROJ2": {
                "jira_key": "PROJ2",
                "jira_name": "Project Two",
                "openproject_id": 2,
                "openproject_identifier": "proj2",
                "openproject_name": "Project Two",
                "account_id": 102,
                "account_name": "Account Two",
                "created_new": True,
            },
            "PROJ3": {
                "jira_key": "PROJ3",
                "jira_name": "Project Three",
                "openproject_id": 4,
                "openproject_identifier": "proj3",
                "openproject_name": "Project Three",
                "account_id": None,
                "account_name": None,
                "created_new": True,
            },
        }

    @patch("src.clients.jira_client.JiraClient")
    @patch("src.clients.openproject_client.OpenProjectClient")
    @patch("src.migrations.project_migration.config.get_path")
    @patch("src.migrations.project_migration.config.migration_config")
    @patch("os.path.exists")
    def test_extract_jira_projects(
        self,
        mock_exists: MagicMock,
        mock_migration_config: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test extracting projects from Jira."""
        # Create instance with mocked clients
        jira_client = mock_jira_client.return_value

        # Setup the mock return value
        jira_client.get_projects = MagicMock(return_value=self.jira_projects)

        # Mock migration config to force extraction
        mock_migration_config.get.return_value = True  # Force extraction
        mock_exists.return_value = False  # No cached file exists

        # Create the migration instance
        migration = ProjectMigration(jira_client, mock_op_client.return_value)

        # We'll directly patch the _save_to_json method to avoid serialization issues
        with patch.object(migration, "_save_to_json"):
            # Call the method
            result = migration.extract_jira_projects()

            # Assertions - we can't use direct equality because the mock has changed
            assert len(result) == len(self.jira_projects)
            for i, project in enumerate(result):
                assert project["id"] == self.jira_projects[i]["id"]
                assert project["key"] == self.jira_projects[i]["key"]
                assert project["name"] == self.jira_projects[i]["name"]

            # Verify the right method was called
            jira_client.get_projects.assert_called_once()

    @patch("src.clients.jira_client.JiraClient")
    @patch("src.clients.openproject_client.OpenProjectClient")
    @patch("src.migrations.project_migration.config.get_path")
    @patch("src.migrations.project_migration.config.migration_config")
    @patch("os.path.exists")
    def test_extract_openproject_projects(
        self,
        mock_exists: MagicMock,
        mock_migration_config: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test extracting projects from OpenProject."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value
        mock_op_instance.get_projects.return_value = self.op_projects

        mock_get_path.return_value = Path("/tmp/test_data")
        mock_exists.return_value = False

        # Mock the config to return force=True
        mock_migration_config.get.side_effect = lambda key, default=None: True if key == "force" else default

        # Create instance and patch the _save_to_json method to avoid serialization issues
        migration = ProjectMigration(mock_jira_instance, mock_op_instance)
        with patch.object(migration, "_save_to_json") as mock_save_to_json:
            # Call method
            result = migration.extract_openproject_projects()

            # Assertions
            assert result == self.op_projects
            assert mock_op_instance.get_projects.call_count == 1
            assert mock_save_to_json.call_count == 1

    @patch("src.clients.jira_client.JiraClient")
    @patch("src.clients.openproject_client.OpenProjectClient")
    @patch("src.migrations.project_migration.config.get_path")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_analyze_project_mapping(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the analyze_project_mapping method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        mock_get_path.return_value = Path("/tmp/test_data")
        mock_exists.return_value = True

        # Mock file reads
        mock_file.return_value.__enter__.return_value.read.return_value = json.dumps(self.expected_mapping)

        # Create instance
        migration = ProjectMigration(mock_jira_instance, mock_op_instance)
        migration.project_mapping = self.expected_mapping

        # Call method
        result = migration.analyze_project_mapping()

        # Assertions
        assert result["total_projects"] == 3
        assert result["migrated_projects"] == 3
        assert result["new_projects"] == 2  # PROJ2 and PROJ3 are new
        assert result["existing_projects"] == 1  # PROJ1 already existed
        assert result["projects_with_accounts"] == 2  # PROJ1 and PROJ2 have accounts

    def test_find_parent_company_for_project(self) -> None:
        """Test that we resolve parent company via default Tempo account."""
        migration = ProjectMigration(MagicMock(), MagicMock())
        # Stub mappings
        migration.project_account_mapping = {"ACMEWEB": [{"id": "42", "key": "ACC-42", "name": "Q1 Review"}]}
        migration.account_mapping = {"42": {"tempo_id": "42", "company_id": "7", "tempo_name": "Account42"}}
        migration.company_mapping = {
            "7": {
                "tempo_id": "7",
                "openproject_id": 123,
                "tempo_key": "CUST7",
                "tempo_name": "AcmeCorp",
            },
        }
        parent = migration.find_parent_company_for_project({"key": "ACMEWEB"})
        assert parent is not None
        assert parent.get("openproject_id") == 123
        assert parent.get("tempo_name") == "AcmeCorp"

    def test_find_parent_company_warns_on_missing(self) -> None:
        """Test that missing mappings return None and log a warning."""
        migration = ProjectMigration(MagicMock(), MagicMock())
        migration.project_account_mapping = {}
        # Make sure we can capture warnings
        with self.assertLogs(config.logger.name, level="DEBUG") as cm:
            parent = migration.find_parent_company_for_project({"key": "UNKNOWN"})
        assert parent is None
        # Should log a debug about missing account mapping
        assert any("No account mapping found for project UNKNOWN" in msg for msg in cm.output)


# Define testing steps for project migration validation


def project_migration_test_steps() -> Any:
    """Testing steps for project migration validation.

    These steps should be executed in a real environment to validate
    the project migration functionality:

    1. Verify project extraction from Jira:
       - Check that all expected Jira projects are extracted
       - Verify key project attributes (key, name, description)

    2. Verify project extraction from OpenProject:
       - Check that existing OpenProject projects are correctly identified
       - Verify key project attributes

    3. Test project creation:
       - Create a new test Jira project
       - Run the migration for just this project
       - Verify the project is created in OpenProject with correct attributes
       - Check that the project identifier follows naming conventions

    4. Test project mapping:
       - Verify projects with the same name are correctly mapped
       - Verify the mapping file contains correct information
       - Check that account associations are correctly maintained

    5. Test project hierarchy (if applicable):
       - Create test Jira projects with parent-child relationships
       - Run the migration
       - Verify the hierarchy is preserved in OpenProject

    6. Test project with custom fields:
       - Verify custom fields like 'Tempo Account' are correctly set on projects
       - Test projects with and without account associations

    7. Test the analysis functionality:
       - Run the analyze_project_mapping method
       - Verify it correctly reports on new vs. existing projects
       - Check it accurately reports on account associations

    8. Test idempotency:
       - Run the migration twice
       - Verify no duplicate projects are created
       - Check that the mapping is correctly updated

    9. Test edge cases:
       - Project with very long name/identifier
       - Project with special characters in name
       - Project with no description
    """
    return "Project migration test steps defined"
