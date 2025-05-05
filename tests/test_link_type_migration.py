"""
Tests for the link type migration component.
"""

import json
import unittest
from unittest.mock import mock_open, patch, MagicMock

from typing import Any

from src.migrations.link_type_migration import LinkTypeMigration


class TestLinkTypeMigration(unittest.TestCase):
    """Test cases for the LinkTypeMigration class."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        # Sample Jira link types data
        self.jira_link_types = [
            {
                "id": "10100",
                "name": "Blocks",
                "inward": "is blocked by",
                "outward": "blocks",
                "self": "https://jira.example.com/rest/api/2/issueLinkType/10100",
            },
            {
                "id": "10101",
                "name": "Cloners",
                "inward": "is cloned by",
                "outward": "clones",
                "self": "https://jira.example.com/rest/api/2/issueLinkType/10101",
            },
            {
                "id": "10102",
                "name": "Custom Link",
                "inward": "is custom linked to",
                "outward": "custom links to",
                "self": "https://jira.example.com/rest/api/2/issueLinkType/10102",
            },
        ]

        # Sample OpenProject relation types data
        self.op_link_types = [
            {
                "id": 1,
                "name": "Blocks",
                "inward": "blocked by",
                "outward": "blocks",
                "reverseName": "blocked by",
                "_links": {"self": {"href": "/api/v3/relation_types/blocks"}},
            },
            {
                "id": 2,
                "name": "Relates",
                "inward": "relates to",
                "outward": "relates to",
                "reverseName": "relates to",
                "_links": {"self": {"href": "/api/v3/relation_types/relates"}},
            },
            {
                "id": 3,
                "name": "Duplicates",
                "inward": "duplicated by",
                "outward": "duplicates",
                "reverseName": "duplicated by",
                "_links": {"self": {"href": "/api/v3/relation_types/duplicates"}},
            },
        ]

        # Expected link type mapping
        self.expected_mapping = {
            "10100": {
                "jira_id": "10100",
                "jira_name": "Blocks",
                "jira_inward": "is blocked by",
                "jira_outward": "blocks",
                "openproject_id": 1,
                "openproject_name": "Blocks",
                "openproject_inward": "blocked by",
                "openproject_outward": "blocks",
                "matched_by": "name",
            },
            "10101": {
                "jira_id": "10101",
                "jira_name": "Cloners",
                "jira_inward": "is cloned by",
                "jira_outward": "clones",
                "openproject_id": 3,
                "openproject_name": "Duplicates",
                "openproject_inward": "duplicated by",
                "openproject_outward": "duplicates",
                "matched_by": "similar_outward",
            },
            "10102": {
                "jira_id": "10102",
                "jira_name": "Custom Link",
                "jira_inward": "is custom linked to",
                "jira_outward": "custom links to",
                "openproject_id": None,
                "openproject_name": None,
                "openproject_inward": None,
                "openproject_outward": None,
                "matched_by": "none",
            },
        }

    @patch("src.migrations.link_type_migration.JiraClient")
    @patch("src.migrations.link_type_migration.OpenProjectClient")
    @patch("src.migrations.link_type_migration.config.get_path")
    @patch("src.migrations.link_type_migration.config.migration_config")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_extract_jira_link_types(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_migration_config: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the extract_jira_link_types method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_jira_instance.get_issue_link_types.return_value = self.jira_link_types

        mock_op_instance = mock_op_client.return_value
        mock_get_path.return_value = "/tmp/test_data"
        mock_exists.return_value = False

        # Mock the config to return force=True
        mock_migration_config.get.side_effect = lambda key, default=None: True if key == "force" else default

        # Create instance and call method
        migration = LinkTypeMigration(mock_jira_instance, mock_op_instance)
        result = migration.extract_jira_link_types()

        # Assertions
        self.assertEqual(result, self.jira_link_types)
        mock_jira_instance.get_issue_link_types.assert_called_once()
        mock_file.assert_any_call("/tmp/test_data/jira_link_types.json", "w")
        mock_file().write.assert_called()

    @patch("src.migrations.link_type_migration.JiraClient")
    @patch("src.migrations.link_type_migration.OpenProjectClient")
    @patch("src.migrations.link_type_migration.config.get_path")
    @patch("src.migrations.link_type_migration.config.migration_config")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_create_link_type_mapping(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_migration_config: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the create_link_type_mapping method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        mock_get_path.return_value = "/tmp/test_data"
        mock_exists.return_value = False

        # Mock the config to return force=True
        mock_migration_config.get.side_effect = lambda key, default=None: True if key == "force" else default

        # Create instance and set data
        migration = LinkTypeMigration(mock_jira_instance, mock_op_instance)
        migration.jira_link_types = self.jira_link_types
        migration.op_link_types = self.op_link_types

        # Call method
        result = migration.create_link_type_mapping()

        # Assertions
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 3)  # One mapping entry per Jira link type

        # Check that the mapping file is saved
        mock_file.assert_any_call("/tmp/test_data/link_type_mapping.json", "w")
        mock_file().write.assert_called()

    @patch("src.migrations.link_type_migration.JiraClient")
    @patch("src.migrations.link_type_migration.OpenProjectClient")
    @patch("src.migrations.link_type_migration.config.get_path")
    @patch("src.migrations.link_type_migration.config.migration_config")
    @patch("src.display.ProgressTracker")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_run_method(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_progress_tracker: MagicMock,
        mock_migration_config: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the main run method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        # Configure mocks for extraction methods to return actual data
        mock_jira_instance.get_issue_link_types.return_value = self.jira_link_types
        mock_op_instance.get_relation_types.return_value = self.op_link_types

        # Mock create_relation_type for the one type that needs creation
        mock_op_instance.create_relation_type.return_value = {
            "success": True,
            "data": {
                "id": 4,
                "name": "Custom Link",
                "inward": "is custom linked to",
                "outward": "custom links to",
            },
        }

        mock_get_path.return_value = "/tmp/test_data"
        mock_migration_config.get.side_effect = lambda key, default=None: {
            "dry_run": False,
            "force": True,
        }.get(key, default)
        mock_exists.return_value = False  # Ensure extraction and mapping run

        # Mock ProgressTracker to avoid terminal output issues
        mock_progress_tracker_instance = mock_progress_tracker.return_value
        mock_progress_tracker_instance.__enter__.return_value = (
            mock_progress_tracker_instance
        )

        # Create instance
        migration = LinkTypeMigration(mock_jira_instance, mock_op_instance)

        # IMPORTANT: Pre-populate the instance variables as if extraction happened
        # This bypasses the need for _save_to_json to work correctly with mocks
        # in the test environment, which was causing the JSON errors.
        migration.jira_link_types = self.jira_link_types
        migration.op_link_types = self.op_link_types

        # Call run method
        result = migration.run()

        # Basic assertions
        self.assertEqual(result["status"], "partial_success")

    @patch("src.migrations.link_type_migration.JiraClient")
    @patch("src.migrations.link_type_migration.OpenProjectClient")
    @patch("src.migrations.link_type_migration.config.get_path")
    @patch("src.migrations.link_type_migration.config.migration_config")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_analyze_link_type_mapping(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_migration_config: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the analyze_link_type_mapping method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        mock_get_path.return_value = "/tmp/test_data"
        mock_exists.return_value = True

        # Mock the config for any configuration needs
        mock_migration_config.get.return_value = False  # Not dry run

        # Mock file read to return a simplified mapping
        mock_mapping = {
            "10100": {
                "jira_id": "10100",
                "jira_name": "Blocks",
                "matched_by": "name",
                "openproject_id": "1",
            },
            "10101": {
                "jira_id": "10101",
                "jira_name": "Cloners",
                "matched_by": "similar_outward",
                "openproject_id": "3",
            },
            "10102": {
                "jira_id": "10102",
                "jira_name": "Custom Link",
                "matched_by": "created",
                "openproject_id": "4",
            },
        }
        mock_file.return_value.__enter__.return_value.read.return_value = json.dumps(mock_mapping)

        # Create instance
        migration = LinkTypeMigration(mock_jira_instance, mock_op_instance)
        migration.link_type_mapping = mock_mapping

        # Call method
        result = migration.analyze_link_type_mapping()

        # Assertions
        self.assertEqual(result["total_types"], 3)
        self.assertEqual(result["matched_types"], 3)  # All are now matched


# Define testing steps for link type migration validation


def link_type_migration_test_steps() -> Any:
    """
    Testing steps for link type migration validation.

    These steps should be executed in a real environment to validate
    the link type migration functionality:

    1. Verify link type extraction from Jira:
       - Check that all Jira link types are extracted correctly
       - Verify key attributes (name, inward, outward)

    2. Verify relation type extraction from OpenProject:
       - Check that existing OpenProject relation types are identified
       - Verify key attributes

    3. Test link type mapping creation:
       - Check that obvious matches by name are correctly mapped
       - Verify similar matches are identified correctly
       - Verify the mapping file is created with correct information

    4. Test relation type creation in OpenProject:
       - Identify a Jira link type that has no match
       - Run the migration for this type
       - Verify the relation type is created in OpenProject with correct attributes

    5. Test the complete migration process:
       - Run the migrate_link_types method
       - Verify that unmatched types are created in OpenProject
       - Check that the mapping file is updated correctly

    6. Test relation usage in work package migration:
       - Create test issues in Jira with links between them
       - Run the work package migration
       - Verify the links are correctly preserved in OpenProject
       - Check that the correct relation types are used

    7. Test the analysis functionality:
       - Run the analyze_link_type_mapping method
       - Verify it correctly reports on the mapping status
       - Check statistics on match types

    8. Test edge cases:
       - Link type with unusual characters
       - Link type with very similar name but different function
       - Link type that might match multiple OpenProject types

    9. Test relation creation error handling:
       - Simulate API errors during relation type creation
       - Verify the error is handled gracefully
       - Check that the migration continues with other types
    """
    return "Link type migration test steps defined"
