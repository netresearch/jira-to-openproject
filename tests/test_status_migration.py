"""
Test module for StatusMigration.

This module contains test cases for validating the status migration from Jira to OpenProject.
"""

import json
import os
import unittest
from unittest.mock import MagicMock, patch
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.mappings.mappings import Mappings
from src.migrations.status_migration import StatusMigration


class TestStatusMigration(unittest.TestCase):
    """Test cases for the StatusMigration class."""

    def setUp(self) -> None:
        """Set up the test environment."""
        # Create mock clients
        self.jira_client = MagicMock(spec=JiraClient)

        # Add jira attribute to JiraClient mock
        self.jira_client.jira = MagicMock()

        self.op_client = MagicMock(spec=OpenProjectClient)

        # Create a test data directory
        self.test_data_dir = os.path.join(os.path.dirname(__file__), "test_data")
        os.makedirs(self.test_data_dir, exist_ok=True)

        # Create a mock mappings object
        self.mappings = MagicMock(spec=Mappings)
        self.mappings.status_mapping = {}

        # Mock the config
        self.config_patcher = patch("src.migrations.status_migration.config")
        self.mock_config = self.config_patcher.start()
        self.mock_config.migration_config.get.side_effect = lambda key, default=None: {
            "dry_run": True,
            "force": False,
        }.get(key, default)

        # Initialize the status migration
        self.status_migration = StatusMigration(
            jira_client=self.jira_client,
            op_client=self.op_client,
            mappings=self.mappings,
            data_dir=self.test_data_dir,
        )

        # Create test data
        self.sample_jira_statuses = [
            {"id": "1", "name": "Open", "statusCategory": {"key": "new"}},
            {
                "id": "2",
                "name": "In Progress",
                "statusCategory": {"key": "indeterminate"},
            },
            {"id": "3", "name": "Done", "statusCategory": {"key": "done"}},
        ]

        self.sample_op_statuses = [
            {"id": 1, "name": "New", "isClosed": False},
            {"id": 2, "name": "In Progress", "isClosed": False},
            {"id": 3, "name": "Closed", "isClosed": True},
        ]

        # Set up the mock return values
        self.jira_client.jira._get_json.return_value = self.sample_jira_statuses
        self.jira_client.get_status_categories.return_value = [
            {"id": "1", "key": "new", "name": "To Do"},
            {"id": "2", "key": "indeterminate", "name": "In Progress"},
            {"id": "3", "key": "done", "name": "Done"},
        ]
        self.op_client.get_statuses.return_value = self.sample_op_statuses

    def tearDown(self) -> None:
        """Clean up after each test."""
        # Stop config patcher
        self.config_patcher.stop()

        # Remove test data files
        for filename in os.listdir(self.test_data_dir):
            os.remove(os.path.join(self.test_data_dir, filename))

        # Remove test data directory if empty
        if os.path.exists(self.test_data_dir) and not os.listdir(self.test_data_dir):
            os.rmdir(self.test_data_dir)

    def test_extract_jira_statuses(self) -> None:
        """Test extracting statuses from Jira."""
        # Temporarily override force to True
        self.mock_config.migration_config.get.side_effect = lambda key, default=None: {
            "dry_run": True,
            "force": True,
        }.get(key, default)

        # Mock the Jira API response for statuses
        self.jira_client.jira._get_json.return_value = self.sample_jira_statuses

        statuses = self.status_migration.extract_jira_statuses()

        # Verify that _get_json was called with 'status'
        self.jira_client.jira._get_json.assert_called_once_with('status')

        # Verify that the correct data was returned
        self.assertEqual(statuses, self.sample_jira_statuses)

        # Verify that the data was saved to a file
        status_file = os.path.join(self.test_data_dir, "jira_statuses.json")
        self.assertTrue(os.path.exists(status_file))

        # Verify the file content
        with open(status_file) as f:
            saved_statuses = json.load(f)
        self.assertEqual(saved_statuses, self.sample_jira_statuses)

    def test_extract_status_categories(self) -> None:
        """Test extracting status categories from Jira."""
        # Temporarily override force to True
        self.mock_config.migration_config.get.side_effect = lambda key, default=None: {
            "dry_run": True,
            "force": True,
        }.get(key, default)

        categories = self.status_migration.extract_status_categories()

        # Verify that get_status_categories was called
        self.jira_client.get_status_categories.assert_called_once()

        # Verify that the correct data was returned
        self.assertEqual(len(categories), 3)

        # Verify that the data was saved to a file
        categories_file = os.path.join(
            self.test_data_dir, "jira_status_categories.json"
        )
        self.assertTrue(os.path.exists(categories_file))

    def test_get_openproject_statuses(self) -> None:
        """Test getting statuses from OpenProject."""
        # Temporarily override force to True
        self.mock_config.migration_config.get.side_effect = lambda key, default=None: {
            "dry_run": True,
            "force": True,
        }.get(key, default)

        statuses = self.status_migration.get_openproject_statuses()

        # Verify that get_statuses was called
        self.op_client.get_statuses.assert_called_once()

        # Verify that the correct data was returned
        self.assertEqual(statuses, self.sample_op_statuses)

        # Verify that the data was saved to a file
        status_file = os.path.join(self.test_data_dir, "op_statuses.json")
        self.assertTrue(os.path.exists(status_file))

    def test_create_status_mapping(self) -> None:
        """Test creating a mapping between Jira and OpenProject statuses."""
        # Set up test data
        self.status_migration.jira_statuses = self.sample_jira_statuses
        self.status_migration.op_statuses = self.sample_op_statuses

        # Temporarily override force to True
        self.mock_config.migration_config.get.side_effect = lambda key, default=None: {
            "dry_run": True,
            "force": True,
        }.get(key, default)

        # Call the method
        mapping = self.status_migration.create_status_mapping()

        # Verify that a mapping is created
        self.assertIsInstance(mapping, dict)

        # Verify that each Jira status has a mapping
        for status in self.sample_jira_statuses:
            self.assertIn(status["id"], mapping)

        # Check specific mappings based on our sample data - updated to match the actual keys returned
        self.assertEqual(
            mapping["2"]["openproject_id"], 2
        )  # "In Progress" -> "In Progress"

        # Verify that the mapping was saved to a file
        mapping_file = os.path.join(self.test_data_dir, "status_mapping.json")
        self.assertTrue(os.path.exists(mapping_file))

    def test_migrate_statuses(self) -> None:
        """Test the status migration process."""
        # Set up test data - an unmapped Jira status that needs to be created in OpenProject
        self.status_migration.jira_statuses = self.sample_jira_statuses.copy()
        self.status_migration.jira_statuses.append(
            {"id": "4", "name": "Pending", "statusCategory": {"key": "indeterminate"}}
        )
        self.status_migration.op_statuses = self.sample_op_statuses

        # Create a basic mapping with a missing status
        initial_mapping = {
            "1": {"openproject_id": 1, "openproject_name": "New"},
            "2": {"openproject_id": 2, "openproject_name": "In Progress"},
            "3": {"openproject_id": 3, "openproject_name": "Closed"},
            # "4" is missing (Pending)
        }

        self.status_migration.status_mapping = initial_mapping

        # Mock the OpenProject client to return success for execute_query instead of execute
        self.op_client.execute_query.return_value = {
            "status": "success",
            "output": """
            Processing status 'Pending' (Jira ID: 4)...
            SUCCESS: Created status 'Pending' with ID: 4
            RESULTS_JSON_START
            {"4":{"id":4,"name":"Pending","is_closed":false,"already_existed":false}}
            RESULTS_JSON_END
            Bulk status creation completed.
            """,
        }

        # Mock the OpenProject client to return updated statuses including the new one
        updated_op_statuses = self.sample_op_statuses.copy()
        updated_op_statuses.append({"id": 4, "name": "Pending", "isClosed": False})
        self.op_client.get_statuses.return_value = updated_op_statuses

        # Call the migrate method
        result = self.status_migration.migrate_statuses()

        # Verify that the migration was successful
        self.assertEqual(result["status"], "success")

        # Verify that the result contains the expected mapping
        self.assertIn("4", result["mapping"])
        self.assertEqual(
            result["mapping"]["4"]["openproject_id"], "dry_run_4"
        )  # In dry run mode, it uses this format
        self.assertEqual(result["mapping"]["4"]["openproject_name"], "Pending")

    def test_analyze_status_mapping(self) -> None:
        """Test the status mapping analysis."""
        # Set up test data with a complete mapping
        self.status_migration.jira_statuses = self.sample_jira_statuses
        self.status_migration.op_statuses = self.sample_op_statuses

        self.status_migration.status_mapping = {
            "1": {"openproject_id": 1, "openproject_name": "New"},
            "2": {"openproject_id": 2, "openproject_name": "In Progress"},
            "3": {"openproject_id": 3, "openproject_name": "Closed"},
        }

        # Call the analyze method
        analysis = self.status_migration.analyze_status_mapping()

        # Verify that the analysis was successful
        self.assertEqual(analysis["status"], "success")

        # Verify that all statuses are mapped - updated to match the actual key in analyze_status_mapping
        self.assertEqual(analysis["unmapped_statuses"], [])

        # Now test with an incomplete mapping by adding a status with no OpenProject ID
        self.status_migration.status_mapping["4"] = {
            "openproject_id": None,
            "openproject_name": "Pending",
        }

        # Call the analyze method again
        analysis = self.status_migration.analyze_status_mapping()

        # Verify that the analysis contains the unmapped status
        self.assertEqual(len(analysis["unmapped_statuses"]), 1)
        # Check that the first unmapped status has a jira_id of '4'
        self.assertEqual(analysis["unmapped_statuses"][0]["jira_id"], "4")


if __name__ == "__main__":
    unittest.main()
