#!/usr/bin/env python3
"""
Test suite for the custom field migration component.
"""

import os
import sys
import json
import unittest
from unittest.mock import Mock, patch, MagicMock, call, ANY
from typing import Dict, List, Any
import tempfile

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

    def test_migrate_custom_fields(self):
        """Test migrating custom fields using the JSON-based approach."""
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

        # Mock the file transfer and execute methods
        self.mock_rails_client.transfer_file_to_container = Mock(return_value=True)
        self.mock_rails_client.transfer_file_from_container = Mock(return_value=True)

        # Mock the execute method to return success
        self.mock_rails_client.execute = Mock(return_value={
            "status": "success",
            "output": {
                "status": "success",
                "created": [
                    {
                        "name": "Test Text Field",
                        "status": "created",
                        "id": 3,
                        "jira_id": "customfield_10001"
                    }
                ],
                "created_count": 1,
                "existing_count": 0,
                "error_count": 0
            }
        })

        # Mock the open function to return a mock file for the result json
        mock_json_content = json.dumps({
            "status": "success",
            "created": [
                {
                    "name": "Test Text Field",
                    "status": "created",
                    "id": 3,
                    "jira_id": "customfield_10001"
                }
            ],
            "created_count": 1,
            "existing_count": 0,
            "error_count": 0
        })
        mock_file = Mock()
        mock_file.__enter__ = Mock(return_value=mock_file)
        mock_file.__exit__ = Mock(return_value=None)
        mock_file.read = Mock(return_value=mock_json_content)
        self.mock_open.return_value = mock_file

        # Mock analyze_custom_field_mapping to return successful analysis
        with patch.object(self.migration, 'analyze_custom_field_mapping', return_value={"status": "success"}):
            # Run the migration
            result = self.migration.migrate_custom_fields()

            # Verify the migration
            self.assertTrue(result)

            # Verify that file transfer methods and execute were called
            self.mock_rails_client.transfer_file_to_container.assert_called_once()
            self.mock_rails_client.execute.assert_called_once()

            # Check the arguments - needs to contain the data_file_path which should
            # start with "/tmp/custom_fields_batch_"
            args, kwargs = self.mock_rails_client.execute.call_args
            script_content = args[0] if args else ""
            self.assertIn("Ruby variables from Python", script_content)
            self.assertIn("data_file_path", script_content)
            self.assertIn("/tmp/custom_fields_batch_", script_content)

    def test_migrate_custom_fields_with_error(self):
        """Test migrating custom fields when an error occurs."""
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

        # Mock the file transfer methods
        self.mock_rails_client.transfer_file_to_container = Mock(return_value=True)

        # Mock the execute method to return an error
        self.mock_rails_client.execute = Mock(return_value={
            "status": "error",
            "error": "Test error message"
        })

        # Mock analyze_custom_field_mapping to return successful analysis
        with patch.object(self.migration, 'analyze_custom_field_mapping', return_value={"status": "success"}):
            # Run the migration
            result = self.migration.migrate_custom_fields()

            # Verify the migration failed
            self.assertFalse(result)

            # Verify that transfer and execute were called
            self.mock_rails_client.transfer_file_to_container.assert_called_once()
            self.mock_rails_client.execute.assert_called_once()

    def test_json_file_handling(self):
        """Test the handling of JSON files for custom field migration."""
        # Set up sample data for the test
        test_mapping = {
            "customfield_10001": {
                "jira_id": "customfield_10001",
                "jira_name": "Test Field",
                "openproject_name": "Test Field",
                "openproject_type": "text",
                "matched_by": "create"
            }
        }

        # Replace json.dump to capture the data being written
        json_dump_mock = Mock()
        with patch('json.dump', json_dump_mock):
            # Replace tempfile.NamedTemporaryFile to return a controlled temp file
            temp_file_mock = Mock()
            temp_file_mock.name = "/tmp/test_json_file.json"

            with patch('tempfile.NamedTemporaryFile', return_value=temp_file_mock):
                # Mock the file transfer and execute methods
                self.mock_rails_client.transfer_file_to_container = Mock(return_value=True)
                self.mock_rails_client.execute = Mock(return_value={"status": "success"})

                # Create a version of migrate_custom_fields_via_json that doesn't need full setup
                with patch.object(self.migration, 'mapping', test_mapping):
                    with patch.object(self.migration, 'analyze_custom_field_mapping', return_value={"status": "success"}):
                        # Call the method
                        self.migration.migrate_custom_fields()

                        # Verify json.dump was called with the correct data
                        json_dump_mock.assert_called_once()
                        args, _ = json_dump_mock.call_args

                        # Check that data is a list of fields
                        self.assertTrue(isinstance(args[0], list), "First argument to json.dump should be a list")
                        self.assertEqual(len(args[0]), 1, "Expected one field in the data")

                        # Check the field properties
                        field = args[0][0]
                        self.assertEqual(field["jira_id"], "customfield_10001")
                        self.assertEqual(field["name"], "Test Field")

                        # Verify that transfer_file_to_container was called at least once
                        self.mock_rails_client.transfer_file_to_container.assert_called_once()
                        # Verify the execute method was called
                        self.mock_rails_client.execute.assert_called_once()

    def test_container_file_transfer_failure(self):
        """Test handling of container file transfer failures."""
        # Set up sample data
        test_mapping = {
            "customfield_10001": {
                "jira_id": "customfield_10001",
                "jira_name": "Test Field",
                "openproject_name": "Test Field",
                "openproject_type": "text",
                "matched_by": "create"
            }
        }

        # Mock the transfer_file_to_container to simulate failure
        self.mock_rails_client.transfer_file_to_container = Mock(return_value=False)

        # Mock analyze_custom_field_mapping to return successful analysis
        with patch.object(self.migration, 'mapping', test_mapping):
            with patch.object(self.migration, 'analyze_custom_field_mapping', return_value={"status": "success"}):
                # Run the migration - should fail due to transfer failure
                result = self.migration.migrate_custom_fields()

                # Verify the migration failed
                self.assertFalse(result)

                # Verify transfer was attempted
                self.mock_rails_client.transfer_file_to_container.assert_called_once()

                # Verify execute was not called (since transfer failed)
                self.mock_rails_client.execute.assert_not_called()

    def test_ruby_script_generation(self):
        """Test the generation of Ruby script with proper structure."""
        # Set up sample data
        test_mapping = {
            "customfield_10001": {
                "jira_id": "customfield_10001",
                "jira_name": "Test Field",
                "openproject_name": "Test Field",
                "openproject_type": "text",
                "matched_by": "create"
            }
        }

        # Mock file operations
        self.mock_rails_client.transfer_file_to_container = Mock(return_value=True)
        self.mock_rails_client.execute = Mock(return_value={"status": "success"})

        # Mock analyze_custom_field_mapping to return successful analysis
        with patch.object(self.migration, 'mapping', test_mapping):
            with patch.object(self.migration, 'analyze_custom_field_mapping', return_value={"status": "success"}):
                # Run the migration
                self.migration.migrate_custom_fields()

                # Get the script content from the execute call
                args, _ = self.mock_rails_client.execute.call_args
                script_content = args[0]

                # Verify script structure - header with Python variables
                self.assertIn("# Ruby variables from Python", script_content)

                # Verify main Ruby code section
                self.assertIn("begin", script_content)
                self.assertIn("rescue Exception => e", script_content)

                # Check for proper error handling
                self.assertIn("error_result", script_content)
                self.assertIn("message", script_content)
                self.assertIn("backtrace", script_content)

                # Check for handling of list field possible values
                self.assertIn("possible_values", script_content)
                self.assertIn("value.to_s.strip", script_content)

    def test_handle_list_field_possible_values(self):
        """Test that list field possible values are properly handled."""
        # Create a mock for execute that captures the script
        self.mock_rails_client.execute = Mock(return_value={"status": "success"})
        self.mock_rails_client.transfer_file_to_container = Mock(return_value=True)

        # Set up sample data with a list field
        test_mapping = {
            "customfield_10002": {
                "jira_id": "customfield_10002",
                "jira_name": "Test Select Field",
                "openproject_name": "Test Select Field",
                "openproject_type": "list",
                "matched_by": "create",
                "allowed_values": ["Option 1", "Option 2", "Option 3"]
            }
        }

        # Mock analyze_custom_field_mapping to return successful analysis
        with patch.object(self.migration, 'mapping', test_mapping):
            with patch.object(self.migration, 'analyze_custom_field_mapping', return_value={"status": "success"}):
                # Run the migration
                self.migration.migrate_custom_fields()

                # Get the script content
                args, _ = self.mock_rails_client.execute.call_args
                script_content = args[0]

                # Check for proper handling of values.map call with to_s.strip
                self.assertIn("map { |value| value.to_s.strip }", script_content)
                # Check for conversion of list field values
                self.assertIn("values.map { |value| value.to_s.strip }", script_content)


if __name__ == '__main__':
    unittest.main()
