#!/usr/bin/env python3
"""Test suite for the custom field migration component."""

import unittest
from unittest.mock import Mock, patch

from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.migrations.custom_field_migration import CustomFieldMigration


class TestCustomFieldMigration(unittest.TestCase):
    """Test cases for the CustomFieldMigration class."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        # Create mock clients
        self.mock_jira_client = Mock(spec=JiraClient)
        self.mock_op_client = Mock()  # Remove spec for more flexible mocking
        self.mock_rails_client = Mock(spec=OpenProjectClient)

        # Set up the rails client reference on the op_client
        self.mock_op_client.rails_client = self.mock_rails_client

        # Explicitly mock the get_custom_fields method
        self.mock_op_client.get_custom_fields = Mock()

        # Add ScriptRunner attributes to the mock JiraClient
        self.mock_jira_client.scriptrunner_enabled = False
        self.mock_jira_client.scriptrunner_custom_field_options_endpoint = None
        self.mock_jira_client._make_request = Mock()

        # Add methods for the new client
        self.mock_rails_client.execute_query = Mock()
        self.mock_rails_client.transfer_file_from_container = Mock()
        self.mock_rails_client.execute = Mock(return_value={
            "status": "success",
            "output": {
                "status": "success",
                "created": [],
                "existing": [],
                "error": [],
                "created_count": 0,
                "existing_count": 0,
                "error_count": 0,
            },
        })
        self.mock_rails_client.transfer_file_to_container = Mock(return_value=True)

        # Add execute and transfer_file_to_container methods to op_client
        self.mock_op_client.execute = Mock(return_value={
            "status": "success",
            "output": {
                "status": "success",
                "created": [],
                "existing": [],
                "error": [],
                "created_count": 0,
                "existing_count": 0,
                "error_count": 0,
            },
        })
        self.mock_op_client.transfer_file_to_container = Mock()

        # Create a mock JIRA instance to mock the jira attribute
        self.mock_jira = Mock()
        self.mock_jira_client.jira = self.mock_jira

        # Mock the fields method
        self.mock_jira.fields = Mock(
            return_value=[
                {
                    "id": "customfield_10001",
                    "name": "Test Text Field",
                    "custom": True,
                    "schema": {
                        "type": "string",
                        "custom": "com.atlassian.jira.plugin.system.customfieldtypes:textfield",
                    },
                },
                {
                    "id": "customfield_10002",
                    "name": "Test Select Field",
                    "custom": True,
                    "schema": {
                        "type": "option",
                        "custom": "com.atlassian.jira.plugin.system.customfieldtypes:select",
                    },
                },
                {
                    "id": "customfield_10003",
                    "name": "Test Date Field",
                    "custom": True,
                    "schema": {
                        "type": "date",
                        "custom": "com.atlassian.jira.plugin.system.customfieldtypes:datepicker",
                    },
                },
            ],
        )

        # Mock get_field_metadata for the select field
        self.mock_jira_client.get_field_metadata = Mock(
            return_value={
                "allowedValues": [
                    {"value": "Option 1"},
                    {"value": "Option 2"},
                    {"value": "Option 3"},
                ],
            },
        )

        # Mock OpenProject custom fields
        self.mock_op_client.get_custom_fields = Mock(
            return_value=[
                {
                    "id": 1,
                    "name": "Existing Text Field",
                    "field_format": "text",
                },
                {
                    "id": 2,
                    "name": "Existing List Field",
                    "field_format": "list",
                    "possible_values": ["Value 1", "Value 2"],
                },
            ],
        )

        # Create a patch for os.path.exists to avoid reading real files
        self.path_exists_patcher = patch("os.path.exists")
        self.mock_path_exists = self.path_exists_patcher.start()
        self.mock_path_exists.return_value = False  # Default to files not existing

        # Create patches for open() using context manager to mock file operations
        self.open_patcher = patch("builtins.open", create=True)
        self.mock_open = self.open_patcher.start()

        # Mock json.load and json.dump
        self.json_load_patcher = patch("json.load")
        self.mock_json_load = self.json_load_patcher.start()
        self.mock_json_load.return_value = {}

        self.json_dump_patcher = patch("json.dump")
        self.mock_json_dump = self.json_dump_patcher.start()

        # Create the migration instance with mocked clients
        self.migration = CustomFieldMigration(
            jira_client=self.mock_jira_client,
            op_client=self.mock_op_client,
            rails_console=self.mock_rails_client,
        )

    def tearDown(self) -> None:
        """Tear down test fixtures."""
        # Stop all patchers
        self.path_exists_patcher.stop()
        self.open_patcher.stop()
        self.json_load_patcher.stop()
        self.json_dump_patcher.stop()

    def test_extract_jira_custom_fields(self) -> None:
        """Test the extraction of Jira custom fields."""
        # Reset the mock to clear any previous calls
        self.mock_jira.fields.reset_mock()

        # Force the file to not exist so we extract from the API
        self.mock_path_exists.return_value = False

        # Also force extraction even if file exists
        with patch("src.config.migration_config", {"force": True}):
            # Call the method
            fields = self.migration.extract_jira_custom_fields()

            # Verify
            assert len(fields) == 3
            assert fields[0]["name"] == "Test Text Field"
            assert fields[1]["name"] == "Test Select Field"

            # Verify that get_field_metadata was called for the select field
            self.mock_jira_client.get_field_metadata.assert_called_once()

            # Verify that the select field has allowed values
            assert "allowed_values" in fields[1]
            assert len(fields[1]["allowed_values"]) == 3
            assert fields[1]["allowed_values"][0] == "Option 1"

    def test_extract_openproject_custom_fields(self) -> None:
        """Test the extraction of OpenProject custom fields."""
        # Setup: Define the expected fields that should be returned
        op_fields = [
            {
                "id": 1,
                "name": "Existing Text Field",
                "field_format": "text",
            },
            {
                "id": 2,
                "name": "Existing List Field",
                "field_format": "list",
                "possible_values": ["Value 1", "Value 2"],
            },
        ]

        # Case 1: When file exists and force=False - should read from file
        with patch("src.config.migration_config", {"force": False}):
            # Mock path.exists to always return True for this test
            self.mock_path_exists.return_value = True

            # Mock json.load to return our expected fields data
            self.mock_json_load.return_value = op_fields

            # Call the method
            fields = self.migration.extract_openproject_custom_fields()

            # Verify that we got the expected fields
            assert len(fields) == 2
            assert fields[0]["name"] == "Existing Text Field"
            assert fields[1]["name"] == "Existing List Field"

        # Case 2: When force=True - should call the API even if file exists
        with patch("src.config.migration_config", {"force": True}):
            # File exists but should be ignored due to force=True
            self.mock_path_exists.return_value = True

            # Mock the op_client.get_custom_fields method to return our test data
            self.mock_op_client.get_custom_fields.return_value = op_fields

            # Call the method again
            fields = self.migration.extract_openproject_custom_fields()

            # Verify the results
            assert len(fields) == 2
            assert fields[0]["name"] == "Existing Text Field"
            assert fields[1]["name"] == "Existing List Field"

        # Case 3: When file doesn't exist - should call the API
        with patch("src.config.migration_config", {"force": False}):
            # No file exists
            self.mock_path_exists.return_value = False

            # Reset the get_custom_fields mock
            self.mock_op_client.get_custom_fields.reset_mock()
            self.mock_op_client.get_custom_fields.return_value = op_fields

            # Call the method again
            fields = self.migration.extract_openproject_custom_fields()

            # Verify the results match what we expect
            assert len(fields) == 2
            assert fields[0]["name"] == "Existing Text Field"
            assert fields[1]["name"] == "Existing List Field"

    def test_map_jira_field_to_openproject_format(self) -> None:
        """Test mapping Jira field types to OpenProject field formats."""
        # Test text field mapping
        text_field = {
            "name": "Text Field",
            "schema": {"type": "string", "custom": "textfield"},
        }
        assert self.migration.map_jira_field_to_openproject_format(text_field) == "text"

        # Test select field mapping
        select_field = {
            "name": "Select Field",
            "schema": {"type": "option", "custom": "select"},
        }
        assert self.migration.map_jira_field_to_openproject_format(select_field) == "list"

        # Test date field mapping
        date_field = {
            "name": "Date Field",
            "schema": {"type": "date", "custom": "datepicker"},
        }
        assert self.migration.map_jira_field_to_openproject_format(date_field) == "date"

    def test_handle_list_field_possible_values(self) -> None:
        """Test the handling of possible values for list-type custom fields."""
        # Create a test field with list type
        test_field = {
            "jira_id": "customfield_10002",
            "jira_name": "Test List Field",
            "openproject_name": "Test List Field",
            "openproject_type": "list",
            "matched_by": "create",
            "possible_values": ["Value 1", "Value 2", "Value 3"],
        }

        # Ensure execute is called with proper values
        self.mock_op_client.execute = Mock(return_value={"status": "success"})

        # Call the method directly to ensure the mock gets called
        self.migration.create_custom_field_via_rails(test_field)

        # Verify execute was called
        self.mock_op_client.execute.assert_called_once()

        # Check that the script contains the correct possible values
        args, _ = self.mock_op_client.execute.call_args
        script_content = args[0]

        # Check for expected values in the script
        assert "field_format" in script_content
        assert "list" in script_content
        assert "possible_values" in script_content
        assert "Value 1" in script_content
        assert "Value 2" in script_content
        assert "Value 3" in script_content

    def test_migrate_custom_fields(self) -> None:
        """Test migrating custom fields using the JSON-based approach."""
        # Set up field data directly for the JSON-based approach
        fields_to_migrate = [{
            "jira_id": "customfield_10001",
            "jira_name": "Test Text Field",
            "openproject_name": "Test Text Field",
            "openproject_type": "text",
            "matched_by": "create",
        }]

        # Mock the file transfer and execute methods directly
        self.mock_op_client.transfer_file_to_container = Mock()

        # Mock the execute_query_to_json_file method to return success
        self.mock_op_client.execute_query_to_json_file.return_value = {
            "status": "success",
            "created_fields": [
                {
                    "name": "Test Text Field",
                    "status": "created",
                    "id": 3,
                    "jira_id": "customfield_10001",
                },
            ],
            "existing_fields": [],
            "error_fields": [],
            "created_count": 1,
            "existing_count": 0,
            "error_count": 0,
        }

        # Call migrate_custom_fields_via_json directly
        result = self.migration.migrate_custom_fields_via_json(fields_to_migrate)

        # Verify the migration succeeded
        assert result

        # Verify that file transfer and execute were called
        self.mock_op_client.transfer_file_to_container.assert_called_once()
        self.mock_op_client.execute_query_to_json_file.assert_called_once()

    def test_migrate_custom_fields_with_error(self) -> None:
        """Test migrating custom fields when an error occurs."""
        # Set up field data directly for the JSON-based approach
        fields_to_migrate = [{
            "jira_id": "customfield_10001",
            "jira_name": "Test Text Field",
            "openproject_name": "Test Text Field",
            "openproject_type": "text",
            "matched_by": "create",
        }]

        # Ensure file transfer succeeds but execution fails
        self.mock_op_client.transfer_file_to_container = Mock()

        # Mock the execute_query_to_json_file method to raise an exception
        self.mock_op_client.execute_query_to_json_file.side_effect = Exception("Test error message")

        # Call migrate_custom_fields_via_json directly
        result = self.migration.migrate_custom_fields_via_json(fields_to_migrate)

        # Verify the migration failed
        assert not result

        # Verify that transfer and execute were called
        self.mock_op_client.transfer_file_to_container.assert_called_once()
        self.mock_op_client.execute_query_to_json_file.assert_called_once()

    def test_container_file_transfer_failure(self) -> None:
        """Test handling of container file transfer failures."""
        # Set up field data directly for the JSON-based approach
        fields_to_migrate = [{
            "jira_id": "customfield_10001",
            "jira_name": "Test Field",
            "openproject_name": "Test Field",
            "openproject_type": "text",
            "matched_by": "create",
        }]

        # Mock the transfer_file_to_container to simulate failure
        self.mock_op_client.transfer_file_to_container.side_effect = Exception("Failed to transfer file")

        # Call the method directly with fields to migrate
        result = self.migration.migrate_custom_fields_via_json(fields_to_migrate)

        # Verify the migration failed
        assert not result

        # Verify transfer was attempted
        self.mock_op_client.transfer_file_to_container.assert_called_once()

        # Verify execute was not called (since transfer failed)
        self.mock_op_client.execute_query_to_json_file.assert_not_called()

    def test_json_file_handling(self) -> None:
        """Test the handling of JSON files for custom field migration."""
        # Set up field data directly for the JSON-based approach
        fields_to_migrate = [{
            "jira_id": "customfield_10001",
            "jira_name": "Test Field",
            "openproject_name": "Test Field",
            "openproject_type": "text",
            "matched_by": "create",
        }]

        # Replace json.dump to capture the data being written
        json_dump_mock = Mock()
        with patch("json.dump", json_dump_mock):
            # Replace tempfile.NamedTemporaryFile to return a controlled temp file
            temp_file_mock = Mock()
            temp_file_mock.name = "/tmp/test_json_file.json"

            with patch("tempfile.NamedTemporaryFile", return_value=temp_file_mock):
                # Mock the file transfer and execute methods
                self.mock_op_client.transfer_file_to_container = Mock()

                # Mock successful return for execute_query_to_json_file method
                self.mock_op_client.execute_query_to_json_file.return_value = {
                    "status": "success",
                    "created_count": 1,
                    "existing_count": 0,
                    "error_count": 0,
                }

                # Call migrate_custom_fields_via_json directly
                self.migration.migrate_custom_fields_via_json(fields_to_migrate)

                # Verify json.dump was called - multiple calls are expected due to update_mapping_file
                assert json_dump_mock.call_count >= 1

                # Check that data contains the expected fields
                calls = json_dump_mock.call_args_list
                for call in calls:
                    args, _ = call
                    # If this is the custom fields data (first arg is a list)
                    if isinstance(args[0], list):
                        # Verify it's the correct structure
                        assert len(args[0]) > 0, "Expected at least one field in the data"

                # Verify that transfer_file_to_container was called at least once
                self.mock_op_client.transfer_file_to_container.assert_called_once()
                # Verify the execute_query_to_json_file method was called
                self.mock_op_client.execute_query_to_json_file.assert_called_once()

    def test_ruby_script_generation(self) -> None:
        """Test the generation of Ruby script with proper structure."""
        # Set up sample data
        test_field = {
            "jira_id": "customfield_10001",
            "jira_name": "Test Field",
            "openproject_name": "Test Field",
            "openproject_type": "text",
            "matched_by": "create",
        }

        # Mock successful return for execute method
        self.mock_op_client.execute = Mock(return_value={"status": "success"})

        # Call the method directly to ensure the mock gets called
        self.migration.create_custom_field_via_rails(test_field)

        # Get the script content from the execute call
        args, _ = self.mock_op_client.execute.call_args
        script_content = args[0]

        # Verify script structure
        assert "begin" in script_content
        assert "rescue" in script_content

        # Check for proper error handling
        assert "Error creating custom field" in script_content


if __name__ == "__main__":
    unittest.main()
