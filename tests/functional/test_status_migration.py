"""Test module for StatusMigration.

This module contains test cases for validating the status migration from Jira to OpenProject.
"""

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.migrations.status_migration import StatusMigration


class TestStatusMigration(unittest.TestCase):
    """Test cases for the StatusMigration class."""

    def setUp(self) -> None:
        """Set up the test environment."""
        # Create mock clients
        self.jira_client = MagicMock(spec=JiraClient)
        self.op_client = MagicMock(spec=OpenProjectClient)

        # Create a test data directory path
        self.test_data_dir = Path(__file__).parent / "test_data"
        self.test_data_dir.mkdir(parents=True, exist_ok=True)

        # Mock config.get_path to return the test data directory
        patcher = patch("src.config.get_path")
        mock_get_path = patcher.start()
        mock_get_path.return_value = self.test_data_dir
        self.addCleanup(patcher.stop)

        # Mock config.migration_config
        patcher_config = patch("src.config.migration_config")
        self.mock_config = patcher_config.start()
        self.mock_config.get.return_value = False  # Default for dry_run, force, etc.
        self.addCleanup(patcher_config.stop)

        # Setup sample data
        self.sample_jira_statuses = [
            {
                "id": "1",
                "name": "To Do",
                "statusCategory": {"id": "2", "name": "To Do"},
            },
            {
                "id": "2",
                "name": "In Progress",
                "statusCategory": {"id": "4", "name": "In Progress"},
            },
            {
                "id": "3",
                "name": "Done",
                "statusCategory": {"id": "3", "name": "Done"},
            },
        ]

        self.sample_op_statuses = [
            {"id": 1, "name": "New", "is_closed": False},
            {"id": 2, "name": "In Progress", "is_closed": False},
            {"id": 3, "name": "Closed", "is_closed": True},
        ]

        # Prepare custom field mock
        self.op_client.get_statuses.return_value = self.sample_op_statuses
        self.jira_client.get_status_categories.return_value = [
            {"id": "2", "name": "To Do"},
            {"id": "3", "name": "Done"},
            {"id": "4", "name": "In Progress"},
        ]

        # Initialize the migration with the mocked clients
        self.status_migration = StatusMigration(
            jira_client=self.jira_client,
            op_client=self.op_client
        )

    def tearDown(self) -> None:
        """Clean up after each test."""
        # Remove test data files if the directory exists
        if self.test_data_dir.exists():
            for filename in Path(self.test_data_dir).iterdir():
                try:
                    if filename.is_file():
                        filename.unlink()
                except Exception as e:
                    print(f"Error removing {filename}: {e}")

    def test_extract_jira_statuses(self) -> None:
        """Test extracting statuses from Jira."""
        # Set up the jira client to return our sample data
        jira_mock = MagicMock()
        jira_mock._get_json.return_value = self.sample_jira_statuses
        self.jira_client.jira = jira_mock

        # Temporarily override force to True
        self.mock_config.get.side_effect = lambda key, default=None: {
            "dry_run": True,
            "force": True,
        }.get(key, default)

        # Mock the _save_to_json method to verify it's called with the right arguments
        with patch.object(self.status_migration, "_save_to_json") as mock_save:
            # Call the method
            statuses = self.status_migration.extract_jira_statuses()

            # Verify that _save_to_json was called with the right arguments
            assert mock_save.call_count == 1
            args = mock_save.call_args[0]
            assert args[0] == self.sample_jira_statuses
            assert str(args[1]) == "jira_statuses.json"

        # Verify that _get_json was called with 'status'
        self.jira_client.jira._get_json.assert_called_once_with("status")

        # Verify that the correct data was returned
        assert statuses == self.sample_jira_statuses

    def test_extract_status_categories(self) -> None:
        """Test extracting status categories from Jira."""
        # Temporarily override force to True
        self.mock_config.get.side_effect = lambda key, default=None: {
            "dry_run": True,
            "force": True,
        }.get(key, default)

        # Mock the _save_to_json method
        with patch.object(self.status_migration, "_save_to_json") as mock_save:
            # Call the method
            categories = self.status_migration.extract_status_categories()

            # Verify that _save_to_json was called correctly
            assert mock_save.call_count == 1
            args = mock_save.call_args[0]
            assert args[0] == categories
            assert str(args[1]) == "jira_status_categories.json"

        # Verify that get_status_categories was called
        self.jira_client.get_status_categories.assert_called_once()

        # Verify that the correct data was returned
        assert len(categories) == 3

    def test_get_openproject_statuses(self) -> None:
        """Test getting statuses from OpenProject."""
        # Temporarily override force to True
        self.mock_config.get.side_effect = lambda key, default=None: {
            "dry_run": True,
            "force": True,
        }.get(key, default)

        # Mock the _save_to_json method
        with patch.object(self.status_migration, "_save_to_json") as mock_save:
            # Call the method
            statuses = self.status_migration.get_openproject_statuses()

            # Verify that _save_to_json was called correctly
            assert mock_save.call_count == 1
            args = mock_save.call_args[0]
            assert args[0] == statuses
            assert str(args[1]) == "op_statuses.json"

        # Verify that get_statuses was called
        self.op_client.get_statuses.assert_called_once()

        # Verify that the correct data was returned
        assert statuses == self.sample_op_statuses

    def test_create_status_mapping(self) -> None:
        """Test creating a mapping between Jira and OpenProject statuses."""
        # Set up test data
        self.status_migration.jira_statuses = self.sample_jira_statuses
        self.status_migration.op_statuses = self.sample_op_statuses

        # Temporarily override force to True
        self.mock_config.get.side_effect = lambda key, default=None: {
            "dry_run": True,
            "force": True,
        }.get(key, default)

        # Mock the _save_to_json method
        with patch.object(self.status_migration, "_save_to_json") as mock_save:
            # Call the method
            mapping = self.status_migration.create_status_mapping()

            # Verify that _save_to_json was called correctly
            mock_save.assert_called_once()
            args = mock_save.call_args[0]
            assert str(args[1]) == "status_mapping.json"

        # Verify that a mapping is created
        assert isinstance(mapping, dict)

        # Verify that each Jira status has a mapping
        for status in self.sample_jira_statuses:
            assert status["id"] in mapping

        # Check specific mappings based on our sample data - updated to match the actual keys returned
        assert mapping["2"]["openproject_id"] == 2  # "In Progress" -> "In Progress"

    def test_migrate_statuses(self) -> None:
        """Test the status migration process."""
        # Set up test data
        self.status_migration.jira_statuses = self.sample_jira_statuses
        self.status_migration.op_statuses = self.sample_op_statuses

        # Mock mappings object for the migration
        mock_mappings = MagicMock()
        mock_mappings.status_mapping = {}
        self.status_migration.mappings = mock_mappings

        # Force dry run mode to skip actual Rails console commands
        self.mock_config.get.side_effect = lambda key, default=None: {
            "dry_run": True,
            "force": True,
        }.get(key, default)

        # Mock the _save_to_json method
        with patch.object(self.status_migration, "_save_to_json"):
            # Call the method
            result = self.status_migration.migrate_statuses()

        # Verify that the result is as expected
        assert result["status"] == "success"

        # Verify that every Jira status has a mapping
        for status in self.sample_jira_statuses:
            assert status["id"] in result["mapping"]

        # Verify that similar names are mapped correctly
        assert result["mapping"]["2"]["openproject_id"] == 2  # "In Progress" -> "In Progress"

    def test_analyze_status_mapping(self) -> None:
        """Test analyzing the status mapping."""
        # Set up test data
        self.status_migration.jira_statuses = self.sample_jira_statuses
        self.status_migration.op_statuses = self.sample_op_statuses

        # Create a test mapping
        status_mapping = {
            "1": {"openproject_id": 1, "openproject_name": "New"},
            "2": {"openproject_id": 2, "openproject_name": "In Progress"},
            "3": {"openproject_id": 3, "openproject_name": "Closed"},
        }
        self.status_migration.status_mapping = status_mapping

        # Call the method
        result = self.status_migration.analyze_status_mapping()

        # Verify that the result is as expected
        assert result["status"] == "success"
        assert result["statuses_count"] == 3
        assert result["mapped_count"] == 3
        assert result["unmapped_count"] == 0
        assert result["unmapped_statuses"] == []  # No unmapped statuses


if __name__ == "__main__":
    unittest.main()
