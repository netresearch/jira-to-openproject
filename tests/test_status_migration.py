"""
Test module for StatusMigration.

This module contains test cases for validating the status migration from Jira to OpenProject.
"""

import os
import sys
import json
import unittest
from unittest.mock import MagicMock, patch

# Add the src directory to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.migrations.status_migration import StatusMigration
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.clients.openproject_rails_client import OpenProjectRailsClient
from src.mappings.mappings import Mappings
from src import config
from src.utils import load_json_file, save_json_file


class TestStatusMigration(unittest.TestCase):
    """Test cases for the StatusMigration class."""

    def setUp(self):
        """Set up the test environment."""
        # Create mock clients
        self.jira_client = MagicMock(spec=JiraClient)
        self.op_client = MagicMock(spec=OpenProjectClient)
        self.op_rails_client = MagicMock(spec=OpenProjectRailsClient)

        # Create a test data directory
        self.test_data_dir = os.path.join(os.path.dirname(__file__), 'test_data')
        os.makedirs(self.test_data_dir, exist_ok=True)

        # Create a mock mappings object
        self.mappings = MagicMock(spec=Mappings)
        self.mappings.status_mapping = {}

        # Initialize the status migration
        self.status_migration = StatusMigration(
            jira_client=self.jira_client,
            op_client=self.op_client,
            op_rails_client=self.op_rails_client,
            mappings=self.mappings,
            data_dir=self.test_data_dir,
            dry_run=True
        )

        # Create test data
        self.sample_jira_statuses = [
            {"id": "1", "name": "Open", "statusCategory": {"key": "new"}},
            {"id": "2", "name": "In Progress", "statusCategory": {"key": "indeterminate"}},
            {"id": "3", "name": "Done", "statusCategory": {"key": "done"}}
        ]

        self.sample_op_statuses = [
            {"id": 1, "name": "New", "isClosed": False},
            {"id": 2, "name": "In Progress", "isClosed": False},
            {"id": 3, "name": "Closed", "isClosed": True}
        ]

        # Set up the mock return values
        self.jira_client.get_all_statuses.return_value = self.sample_jira_statuses
        self.jira_client.get_status_categories.return_value = [
            {"id": "1", "key": "new", "name": "To Do"},
            {"id": "2", "key": "indeterminate", "name": "In Progress"},
            {"id": "3", "key": "done", "name": "Done"}
        ]
        self.op_client.get_statuses.return_value = self.sample_op_statuses

    def tearDown(self):
        """Clean up after each test."""
        # Remove test data files
        for filename in os.listdir(self.test_data_dir):
            os.remove(os.path.join(self.test_data_dir, filename))

        # Remove test data directory if empty
        if os.path.exists(self.test_data_dir) and not os.listdir(self.test_data_dir):
            os.rmdir(self.test_data_dir)

    def test_extract_jira_statuses(self):
        """Test extracting statuses from Jira."""
        statuses = self.status_migration.extract_jira_statuses(force=True)

        # Verify that get_all_statuses was called
        self.jira_client.get_all_statuses.assert_called_once()

        # Verify that the correct data was returned
        self.assertEqual(statuses, self.sample_jira_statuses)

        # Verify that the data was saved to a file
        status_file = os.path.join(self.test_data_dir, "jira_statuses.json")
        self.assertTrue(os.path.exists(status_file))

        # Verify the file content
        with open(status_file, "r") as f:
            saved_statuses = json.load(f)
        self.assertEqual(saved_statuses, self.sample_jira_statuses)

    def test_extract_status_categories(self):
        """Test extracting status categories from Jira."""
        categories = self.status_migration.extract_status_categories(force=True)

        # Verify that get_status_categories was called
        self.jira_client.get_status_categories.assert_called_once()

        # Verify that the correct data was returned
        self.assertEqual(len(categories), 3)

        # Verify that the data was saved to a file
        categories_file = os.path.join(self.test_data_dir, "jira_status_categories.json")
        self.assertTrue(os.path.exists(categories_file))

    def test_get_openproject_statuses(self):
        """Test getting statuses from OpenProject."""
        statuses = self.status_migration.get_openproject_statuses(force=True)

        # Verify that get_statuses was called
        self.op_client.get_statuses.assert_called_once()

        # Verify that the correct data was returned
        self.assertEqual(statuses, self.sample_op_statuses)

        # Verify that the data was saved to a file
        status_file = os.path.join(self.test_data_dir, "op_statuses.json")
        self.assertTrue(os.path.exists(status_file))

    def test_create_status_mapping(self):
        """Test creating a mapping between Jira and OpenProject statuses."""
        # Set up test data
        self.status_migration.jira_statuses = self.sample_jira_statuses
        self.status_migration.op_statuses = self.sample_op_statuses

        # Call the method
        mapping = self.status_migration.create_status_mapping(force=True)

        # Verify that a mapping is created
        self.assertIsInstance(mapping, dict)

        # Verify that each Jira status has a mapping
        for status in self.sample_jira_statuses:
            self.assertIn(status["id"], mapping)

        # Check specific mappings based on our sample data - updated to match the actual keys returned
        self.assertEqual(mapping["2"]["openproject_id"], 2)  # "In Progress" -> "In Progress"

        # Verify that the mapping was saved to a file
        mapping_file = os.path.join(self.test_data_dir, "status_mapping.json")
        self.assertTrue(os.path.exists(mapping_file))

    def test_migrate_statuses(self):
        """Test the status migration process."""
        # Set up test data - an unmapped Jira status that needs to be created in OpenProject
        self.status_migration.jira_statuses = self.sample_jira_statuses.copy()
        self.status_migration.jira_statuses.append({
            "id": "4",
            "name": "Pending",
            "statusCategory": {"key": "indeterminate"}
        })
        self.status_migration.op_statuses = self.sample_op_statuses

        # Create a basic mapping with a missing status
        initial_mapping = {
            "1": {"openproject_id": 1, "openproject_name": "New"},
            "2": {"openproject_id": 2, "openproject_name": "In Progress"},
            "3": {"openproject_id": 3, "openproject_name": "Closed"}
            # "4" is missing (Pending)
        }

        self.status_migration.status_mapping = initial_mapping

        # Mock the Rails client to return a successful status creation - updated to use execute instead of execute_ruby
        self.op_rails_client.execute.return_value = {
            "status": "success",
            "output": """
            SUCCESS: Status created with ID: 4
            #<Status id: 4, name: "Pending", position: 4, is_default: false, is_closed: false>
            """
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
        self.assertEqual(result["mapping"]["4"]["openproject_id"], "dry_run_4")  # In dry run mode, it uses this format
        self.assertEqual(result["mapping"]["4"]["openproject_name"], "Pending")

    def test_analyze_status_mapping(self):
        """Test the status mapping analysis."""
        # Set up test data with a complete mapping
        self.status_migration.jira_statuses = self.sample_jira_statuses
        self.status_migration.op_statuses = self.sample_op_statuses

        self.status_migration.status_mapping = {
            "1": {"openproject_id": 1, "openproject_name": "New"},
            "2": {"openproject_id": 2, "openproject_name": "In Progress"},
            "3": {"openproject_id": 3, "openproject_name": "Closed"}
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
            "openproject_name": "Pending"
        }

        # Call the analyze method again
        analysis = self.status_migration.analyze_status_mapping()

        # Verify that the unmapped status is identified - updated to match the actual key name
        self.assertEqual(len(analysis["unmapped_statuses"]), 1)
        self.assertEqual(analysis["unmapped_statuses"][0]["jira_id"], "4")


if __name__ == "__main__":
    unittest.main()
