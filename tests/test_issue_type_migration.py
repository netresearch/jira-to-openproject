#!/usr/bin/env python3
"""
Test module for IssueTypeMigration.

This module contains test cases for validating the issue type migration from Jira to OpenProject.
"""

import json
import unittest
from typing import Any, Dict, cast
from unittest.mock import MagicMock, call, mock_open, patch

from src.migrations.issue_type_migration import IssueTypeMigration


class TestIssueTypeMigration(unittest.TestCase):
    """Test cases for the IssueTypeMigration class."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.test_data_dir = "/tmp/test_data"

        # Sample Jira issue types data
        self.jira_issue_types = [
            {
                "id": "10000",
                "name": "Bug",
                "description": "A bug in the software",
                "iconUrl": "https://jira.example.com/images/icons/bug.svg",
                "subtask": False,
            },
            {
                "id": "10001",
                "name": "Task",
                "description": "A task that needs to be done",
                "iconUrl": "https://jira.example.com/images/icons/task.svg",
                "subtask": False,
            },
            {
                "id": "10002",
                "name": "Epic",
                "description": "A big user story",
                "iconUrl": "https://jira.example.com/images/icons/epic.svg",
                "subtask": False,
            },
            {
                "id": "10003",
                "name": "Custom Type",
                "description": "A custom issue type",
                "iconUrl": "https://jira.example.com/images/icons/custom.svg",
                "subtask": False,
            },
        ]

        # Sample OpenProject work package types data
        self.op_work_package_types = [
            {
                "id": 1,
                "name": "Bug",
                "color": "#E44D42",
                "position": 1,
                "is_default": False,
                "is_milestone": False,
                "_links": {"self": {"href": "/api/v3/types/1"}},
            },
            {
                "id": 2,
                "name": "Task",
                "color": "#1A67A3",
                "position": 2,
                "is_default": True,
                "is_milestone": False,
                "_links": {"self": {"href": "/api/v3/types/2"}},
            },
            {
                "id": 3,
                "name": "Milestone",
                "color": "#E73E97",
                "position": 3,
                "is_default": False,
                "is_milestone": True,
                "_links": {"self": {"href": "/api/v3/types/3"}},
            },
        ]

        # Expected issue type mapping
        self.expected_mapping = {
            "Bug": {
                "jira_id": "10000",
                "jira_name": "Bug",
                "jira_description": "A bug in the software",
                "openproject_id": 1,
                "openproject_name": "Bug",
                "color": "#E44D42",
                "is_milestone": False,
                "matched_by": "exact_match",
            },
            "Task": {
                "jira_id": "10001",
                "jira_name": "Task",
                "jira_description": "A task that needs to be done",
                "openproject_id": 2,
                "openproject_name": "Task",
                "color": "#1A67A3",
                "is_milestone": False,
                "matched_by": "exact_match",
            },
            "Epic": {
                "jira_id": "10002",
                "jira_name": "Epic",
                "jira_description": "A big user story",
                "openproject_id": None,
                "openproject_name": "Epic",
                "color": "#9B59B6",
                "is_milestone": False,
                "matched_by": "default_mapping_to_create",
            },
            "Custom Type": {
                "jira_id": "10003",
                "jira_name": "Custom Type",
                "jira_description": "A custom issue type",
                "openproject_id": None,
                "openproject_name": "Custom Type",
                "color": "#1A67A3",
                "is_milestone": False,
                "matched_by": "same_name",
            },
        }

        # Expected ID mapping
        self.expected_id_mapping = {
            "10000": 1,  # Bug
            "10001": 2,  # Task
        }

    @patch("src.migrations.issue_type_migration.JiraClient")
    @patch("src.migrations.issue_type_migration.OpenProjectClient")
    @patch("src.migrations.issue_type_migration.config.get_path")
    @patch("src.migrations.issue_type_migration.config.migration_config")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_extract_jira_issue_types(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_migration_config: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test extracting Jira issue types."""
        # Setup
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value
        # Setup rails_client on the op_instance for the new architecture
        mock_rails_instance = MagicMock()
        mock_op_instance.rails_client = mock_rails_instance

        mock_jira_instance.get_issue_types.return_value = self.jira_issue_types
        mock_get_path.return_value = "/tmp/test_data"
        mock_exists.return_value = False

        # Mock the config to return force=True
        mock_migration_config.get.side_effect = lambda key, default=None: True if key == "force" else default

        # Execute - no longer passing rails_console
        issue_migration = IssueTypeMigration(mock_jira_instance, mock_op_instance)
        result = issue_migration.extract_jira_issue_types()

        # Assert
        mock_jira_instance.get_issue_types.assert_called_once()
        mock_file.assert_called_with("/tmp/test_data/jira_issue_types.json", "w")
        self.assertEqual(result, self.jira_issue_types)

    @patch("src.migrations.issue_type_migration.JiraClient")
    @patch("src.migrations.issue_type_migration.OpenProjectClient")
    @patch("src.migrations.issue_type_migration.config.get_path")
    @patch("src.migrations.issue_type_migration.config.migration_config")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_extract_openproject_work_package_types(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_migration_config: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test extracting OpenProject work package types."""
        # Setup
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value
        # Setup rails_client on the op_instance for the new architecture
        mock_rails_instance = MagicMock()
        mock_op_instance.rails_client = mock_rails_instance

        mock_op_instance.get_work_package_types.return_value = self.op_work_package_types
        mock_get_path.return_value = "/tmp/test_data"
        mock_exists.return_value = False

        # Mock the config to return force=True
        mock_migration_config.get.side_effect = lambda key, default=None: True if key == "force" else default

        # Execute - no longer passing rails_console
        issue_migration = IssueTypeMigration(mock_jira_instance, mock_op_instance)
        result = issue_migration.extract_openproject_work_package_types()

        # Assert
        mock_op_instance.get_work_package_types.assert_called_once()
        mock_file.assert_called_with("/tmp/test_data/openproject_work_package_types.json", "w")
        self.assertEqual(result, self.op_work_package_types)

    @patch("src.migrations.issue_type_migration.JiraClient")
    @patch("src.migrations.issue_type_migration.OpenProjectClient")
    @patch("src.migrations.issue_type_migration.IssueTypeMigration._save_to_json")
    def test_migrate_issue_types_via_rails(
        self, mock_save_to_json: MagicMock, mock_op_client: MagicMock, mock_jira_client: MagicMock
    ) -> None:
        """Test the migrate_issue_types_via_rails method with fully mocked file operations."""
        # Set up mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        # Setup rails_client directly on mock_op_instance for new architecture
        mock_op_instance.rails_client = mock_op_instance  # Using same mock for simplicity

        # Use a more direct approach of mocking the rails_console operations
        with (
            patch("os.path.exists", return_value=True),
            patch("os.path.join", return_value="/mock/path"),
            patch(
                "builtins.open",
                mock_open(
                    # Provide non-empty file content for the results file
                    read_data=json.dumps(
                        {
                            "created": [{"id": 4, "name": "Epic", "status": "created", "jira_type_name": "Epic"}],
                            "errors": [],
                        }
                    )
                ),
            ),
            patch("json.dump"),
            patch(
                "json.load",
                return_value={
                    "created": [{"id": 4, "name": "Epic", "status": "created", "jira_type_name": "Epic"}],
                    "errors": [],
                },
            ),
            patch.object(IssueTypeMigration, "check_existing_work_package_types", return_value=[]),
        ):

            # Configure the mock OpenProjectClient
            mock_op_instance.execute_query.return_value = {
                "status": "success",
                "output": "BULK_CREATE_COMPLETED: Created 1 types, Errors: 0\nCREATED_TYPE: 4 - Epic (created)",
            }
            mock_op_instance.transfer_file_to_container.return_value = True
            mock_op_instance.transfer_file_from_container.return_value = True

            # Create test instance - no longer passing rails_console parameter
            migration = IssueTypeMigration(jira_client=mock_jira_instance, op_client=mock_op_instance)

            # Set test data
            migration.issue_type_mapping = {
                "Epic": {
                    "jira_id": "10002",
                    "jira_name": "Epic",
                    "openproject_id": None,
                    "matched_by": "default_mapping_to_create",
                    "color": "#9B59B6",
                }
            }

            # Call the method
            result = migration.migrate_issue_types_via_rails()

            # Assertions
            self.assertTrue(result)

            # Verify required operations were called
            mock_op_instance.execute_query.assert_called()
            mock_op_instance.transfer_file_to_container.assert_called_once()
            mock_op_instance.transfer_file_from_container.assert_called_once()

            # Verify mapping files were saved
            self.assertTrue(mock_save_to_json.called)

    @patch("src.migrations.issue_type_migration.JiraClient")
    @patch("src.migrations.issue_type_migration.OpenProjectClient")
    def test_migrate_issue_types(self, mock_op_client: MagicMock, mock_jira_client: MagicMock) -> None:
        """Test the migrate_issue_types method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        # Setup rails_client for new architecture
        mock_op_instance.rails_client = MagicMock()

        # Create instance and patch migrate_issue_types_via_rails
        with (
            patch.object(IssueTypeMigration, "migrate_issue_types_via_rails", return_value=True),
            patch.object(
                IssueTypeMigration,
                "analyze_issue_type_mapping",
                return_value={
                    "total_jira_types": 4,
                    "matched_op_types": 2,
                    "types_to_create": 2,
                },
            ),
            patch.object(IssueTypeMigration, "_save_to_json"),
            patch.object(IssueTypeMigration, "extract_jira_issue_types"),
            patch.object(IssueTypeMigration, "extract_openproject_work_package_types"),
            patch.object(IssueTypeMigration, "create_issue_type_mapping"),
            patch.object(IssueTypeMigration, "normalize_issue_types"),
        ):

            # Create instance without rails_console parameter
            migration = IssueTypeMigration(jira_client=mock_jira_instance, op_client=mock_op_instance)

            # Set test data
            migration.jira_issue_types = self.jira_issue_types
            migration.op_work_package_types = self.op_work_package_types
            migration.issue_type_mapping = cast(dict[str, dict[str, Any]], self.expected_mapping)

            # Call method
            result = migration.migrate_issue_types()

            # Assertions
            self.assertEqual(result["total_jira_types"], 4)
            self.assertEqual(result["matched_op_types"], 2)
            self.assertEqual(result["types_to_create"], 2)

    @patch("src.migrations.issue_type_migration.JiraClient")
    @patch("src.migrations.issue_type_migration.OpenProjectClient")
    @patch("src.migrations.issue_type_migration.config.get_path")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_analyze_issue_type_mapping(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the analyze_issue_type_mapping method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        # Setup rails_client for new architecture
        mock_op_instance.rails_client = MagicMock()

        mock_get_path.return_value = "/tmp/test_data"
        mock_exists.return_value = True

        # Create instance without rails_console parameter
        migration = IssueTypeMigration(jira_client=mock_jira_instance, op_client=mock_op_instance)
        migration.issue_type_mapping = cast(dict[str, dict[str, Any]], self.expected_mapping)

        # Call method
        result = migration.analyze_issue_type_mapping()

        # Assertions
        self.assertEqual(result["total_jira_types"], 4)
        self.assertEqual(result["matched_op_types"], 2)
        self.assertEqual(result["types_to_create"], 2)
        self.assertEqual(result["match_percentage"], 50.0)

        mock_file.assert_called_with("/tmp/test_data/issue_type_analysis.json", "w")
        mock_file().write.assert_called()

    @patch("src.migrations.issue_type_migration.JiraClient")
    @patch("src.migrations.issue_type_migration.OpenProjectClient")
    @patch("src.migrations.issue_type_migration.config.get_path")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_update_mapping_file(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the update_mapping_file method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        # Setup rails_client for new architecture
        mock_op_instance.rails_client = MagicMock()

        mock_get_path.return_value = "/tmp/test_data"
        mock_exists.return_value = True

        # Create instance and set data for the test - without rails_console parameter
        migration = IssueTypeMigration(jira_client=mock_jira_instance, op_client=mock_op_instance)

        # Rest of test is unchanged
        mock_exists.return_value = True
        mock_file.return_value.__enter__.return_value.read.return_value = json.dumps(
            {
                "Bug": {
                    "jira_id": "10000",
                    "jira_name": "Bug",
                    "openproject_id": None,
                    "openproject_name": "Bug",
                    "matched_by": "none",
                },
                "Task": {
                    "jira_id": "10001",
                    "jira_name": "Task",
                    "openproject_id": None,
                    "openproject_name": "Task",
                    "matched_by": "none",
                },
            }
        )

        # Mock OpenProject client to return work package types that match EXACTLY the names
        # in the mapping ('Bug' and 'Task')
        mock_op_instance.get_work_package_types.return_value = [
            {"id": 1, "name": "Bug", "color": "#FF0000"},
            {"id": 2, "name": "Task", "color": "#00FF00"},
        ]

        # Set the mapping attribute directly to match what was mocked in the file
        migration.issue_type_mapping = {
            "Bug": {
                "jira_id": "10000",
                "jira_name": "Bug",
                "openproject_id": None,
                "openproject_name": "Bug",
                "matched_by": "none",
            },
            "Task": {
                "jira_id": "10001",
                "jira_name": "Task",
                "openproject_id": None,
                "openproject_name": "Task",
                "matched_by": "none",
            },
        }

        result = migration.update_mapping_file()

        # Assertions
        self.assertTrue(result)  # Should return True for success

        # Verify the mapping file is updated
        self.assertIn(
            call("/tmp/test_data/issue_type_mapping.json", "w"),
            mock_file.call_args_list,
        )
        mock_file().write.assert_called()

    @patch("src.migrations.issue_type_migration.JiraClient")
    @patch("src.migrations.issue_type_migration.OpenProjectClient")
    @patch("src.migrations.issue_type_migration.config.get_path")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_create_issue_type_mapping(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the create_issue_type_mapping method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        # Setup rails_client for new architecture
        mock_op_instance.rails_client = MagicMock()

        mock_get_path.return_value = "/tmp/test_data"

        # Mock file exists check - file does not exist so we'll create a new mapping
        mock_exists.return_value = False

        # Create instance - without rails_console parameter
        migration = IssueTypeMigration(jira_client=mock_jira_instance, op_client=mock_op_instance)
        migration.jira_issue_types = self.jira_issue_types
        migration.op_work_package_types = self.op_work_package_types

        # Call method
        result = migration.create_issue_type_mapping()

        # Assertions for result structure and key mappings
        self.assertEqual(len(result), 4)  # One entry per Jira issue type
        self.assertEqual(result["Bug"]["jira_id"], "10000")
        self.assertEqual(result["Bug"]["openproject_id"], 1)
        self.assertEqual(result["Bug"]["matched_by"], "exact_match")

        self.assertIsNone(result["Epic"]["openproject_id"])
        self.assertIn(result["Epic"]["matched_by"], ["default_mapping_to_create", "none"])

        # Verify the template file is created
        mock_file.assert_called_with("/tmp/test_data/issue_type_mapping_template.json", "w")
        mock_file().write.assert_called()


# Define testing steps for issue type migration validation


def issue_type_migration_test_steps() -> Any:
    """
    Testing steps for issue type migration validation.

    These steps should be executed in a real environment to validate
    the issue type migration functionality:

    1. Verify issue type extraction from Jira:
       - Check that all Jira issue types are extracted correctly
       - Verify key attributes (name, description, subtask flag)

    2. Verify work package type extraction from OpenProject:
       - Check that existing OpenProject work package types are identified
       - Verify key attributes (name, color, milestone flag)

    3. Test issue type mapping creation:
       - Check that exact matches by name are correctly mapped
       - Verify default mappings are applied correctly
       - Verify the mapping template file is created with correct information

    4. Test work package type creation via Rails:
       - Identify Jira issue types that have no match in OpenProject
       - Run the migration for these types using the Rails console
       - Verify work package types are created in OpenProject with correct attributes
       - Check both direct execution and script generation options

    5. Test the complete migration process:
       - Run the migrate_issue_types method
       - Verify the mapping analysis is generated correctly
       - Check that the ID mapping file is created with correct mappings

    6. Test work package type usage in work package migration:
       - Create test issues in Jira of different types
       - Run the work package migration
       - Verify the issues are created with correct work package types in OpenProject

    7. Test the analysis functionality:
       - Run the analyze_issue_type_mapping method
       - Verify it correctly reports on matched vs. unmatched types
       - Check that it identifies types that need to be created

    8. Test updating the mapping file:
       - After manually creating work package types in OpenProject
       - Run update_mapping_file method
       - Verify the mapping file is updated with correct IDs

    9. Test edge cases:
       - Issue type with unusual name
       - Issue type that has no default mapping
       - Sub-task issue types
       - Milestone issue types
    """
    return "Issue type migration test steps defined"
