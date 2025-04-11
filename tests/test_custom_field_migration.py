#!/usr/bin/env python3
"""
Test suite for the custom field migration component.
"""

import os
import sys
import json
import unittest
from unittest.mock import Mock, patch, MagicMock, call
from typing import Dict, List, Any

# Add src directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Import the CustomFieldMigration class
from src.migrations.custom_field_migration import CustomFieldMigration
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.clients.openproject_rails_client import OpenProjectRailsClient


class TestCustomFieldMigration(unittest.TestCase):
    """Test cases for the CustomFieldMigration class."""

    def setUp(self):
        """Set up test fixtures."""
        # Create mock clients
        self.mock_jira_client = Mock(spec=JiraClient)
        self.mock_op_client = Mock(spec=OpenProjectClient)
        self.mock_rails_client = Mock(spec=OpenProjectRailsClient)

        # Create a mock JIRA instance to mock the jira attribute
        self.mock_jira = Mock()
        self.mock_jira_client.jira = self.mock_jira

        # Mock the fields method
        self.mock_jira.fields = Mock(return_value=[
            {
                "id": "customfield_10001",
                "name": "Test Text Field",
                "custom": True,
                "schema": {
                    "type": "string",
                    "custom": "com.atlassian.jira.plugin.system.customfieldtypes:textfield"
                }
            },
            {
                "id": "customfield_10002",
                "name": "Test Select Field",
                "custom": True,
                "schema": {
                    "type": "option",
                    "custom": "com.atlassian.jira.plugin.system.customfieldtypes:select"
                }
            },
            {
                "id": "customfield_10003",
                "name": "Test Date Field",
                "custom": True,
                "schema": {
                    "type": "date",
                    "custom": "com.atlassian.jira.plugin.system.customfieldtypes:datepicker"
                }
            }
        ])

        # Mock get_field_metadata for the select field
        self.mock_jira_client.get_field_metadata = Mock(return_value={
            "allowedValues": [
                {"value": "Option 1"},
                {"value": "Option 2"},
                {"value": "Option 3"}
            ]
        })

        # Mock OpenProject custom fields
        self.mock_op_client.get_custom_fields = Mock(return_value=[
            {
                "id": 1,
                "name": "Existing Text Field",
                "field_format": "text",
            },
            {
                "id": 2,
                "name": "Existing List Field",
                "field_format": "list",
                "possible_values": ["Value 1", "Value 2"]
            }
        ])

        # Create a patch for os.path.exists to avoid reading real files
        self.path_exists_patcher = patch('os.path.exists')
        self.mock_path_exists = self.path_exists_patcher.start()
        self.mock_path_exists.return_value = False  # Default to files not existing

        # Create patches for open() using context manager to mock file operations
        self.open_patcher = patch('builtins.open', create=True)
        self.mock_open = self.open_patcher.start()

        # Mock json.load and json.dump
        self.json_load_patcher = patch('json.load')
        self.mock_json_load = self.json_load_patcher.start()
        self.mock_json_load.return_value = {}

        self.json_dump_patcher = patch('json.dump')
        self.mock_json_dump = self.json_dump_patcher.start()

        # Create the migration instance with mocked clients
        self.migration = CustomFieldMigration(
            jira_client=self.mock_jira_client,
            op_client=self.mock_op_client,
            rails_console=self.mock_rails_client
        )

    def tearDown(self):
        """Tear down test fixtures."""
        # Stop all patchers
        self.path_exists_patcher.stop()
        self.open_patcher.stop()
        self.json_load_patcher.stop()
        self.json_dump_patcher.stop()

    def test_extract_jira_custom_fields(self):
        """Test extracting custom fields from Jira."""
        # Run the extraction
        fields = self.migration.extract_jira_custom_fields(force=True)

        # Verify the extraction
        self.assertEqual(len(fields), 3)
        self.assertEqual(fields[0]["name"], "Test Text Field")
        self.assertEqual(fields[1]["name"], "Test Select Field")

        # Verify that get_field_metadata was called for the select field
        self.mock_jira_client.get_field_metadata.assert_called_once()

        # Verify that the select field has allowed values
        self.assertIn("allowed_values", fields[1])
        self.assertEqual(len(fields[1]["allowed_values"]), 3)
        self.assertEqual(fields[1]["allowed_values"][0], "Option 1")

    def test_extract_openproject_custom_fields(self):
        """Test extracting custom fields from OpenProject."""
        # Run the extraction
        fields = self.migration.extract_openproject_custom_fields(force=True)

        # Verify the extraction
        self.assertEqual(len(fields), 2)
        self.assertEqual(fields[0]["name"], "Existing Text Field")
        self.assertEqual(fields[1]["name"], "Existing List Field")

        # Verify that the OpenProject client method was called
        self.mock_op_client.get_custom_fields.assert_called_once_with(force_refresh=True)

    def test_map_jira_field_to_openproject_format(self):
        """Test mapping Jira field types to OpenProject field formats."""
        # Test text field mapping
        text_field = {
            "name": "Text Field",
            "schema": {"type": "string", "custom": "textfield"}
        }
        self.assertEqual(self.migration.map_jira_field_to_openproject_format(text_field), "text")

        # Test select field mapping
        select_field = {
            "name": "Select Field",
            "schema": {"type": "option", "custom": "select"}
        }
        self.assertEqual(self.migration.map_jira_field_to_openproject_format(select_field), "list")

        # Test date field mapping
        date_field = {
            "name": "Date Field",
            "schema": {"type": "date", "custom": "datepicker"}
        }
        self.assertEqual(self.migration.map_jira_field_to_openproject_format(date_field), "date")

    def test_migrate_custom_fields_via_rails(self):
        """Test migrating custom fields via Rails console."""
        # Set up mocks for existing mapping and field data
        field_data = {
            "jira_id": "customfield_10001",
            "jira_name": "Test Text Field",
            "openproject_name": "Test Text Field",
            "openproject_type": "text",
            "matched_by": "create"
        }

        # Mock mapping with a field that needs to be created
        self.migration.mapping = {
            "customfield_10001": field_data
        }

        # Mock the rails client to return success for create_custom_field_via_rails
        self.mock_rails_client.execute = Mock(return_value={
            "status": "success",
            "id": 3,
            "name": "Test Text Field"
        })

        # Mock analyze_custom_field_mapping to return our field
        analyze_result = {
            "fields_to_migrate": [field_data]
        }
        with patch.object(self.migration, 'analyze_custom_field_mapping', return_value=analyze_result):
            # Run the migration
            result = self.migration.migrate_custom_fields_via_rails()

            # Verify the migration
            self.assertTrue(result)

            # Verify that execute was called with a command that includes our field name
            called_with = self.mock_rails_client.execute.call_args[0][0]
            self.assertIn("Test Text Field", called_with)


if __name__ == '__main__':
    unittest.main()
