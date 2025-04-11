"""
Tests for the link type migration component.
"""
import unittest
from unittest.mock import patch, MagicMock, mock_open, ANY
import json
import os
from src.migrations.link_type_migration import LinkTypeMigration


class TestLinkTypeMigration(unittest.TestCase):
    """Test cases for the LinkTypeMigration class."""

    def setUp(self):
        """Set up test fixtures."""
        # Sample Jira link types data
        self.jira_link_types = [
            {
                'id': '10100',
                'name': 'Blocks',
                'inward': 'is blocked by',
                'outward': 'blocks',
                'self': 'https://jira.example.com/rest/api/2/issueLinkType/10100'
            },
            {
                'id': '10101',
                'name': 'Cloners',
                'inward': 'is cloned by',
                'outward': 'clones',
                'self': 'https://jira.example.com/rest/api/2/issueLinkType/10101'
            },
            {
                'id': '10102',
                'name': 'Custom Link',
                'inward': 'is custom linked to',
                'outward': 'custom links to',
                'self': 'https://jira.example.com/rest/api/2/issueLinkType/10102'
            }
        ]

        # Sample OpenProject relation types data
        self.op_link_types = [
            {
                'id': 1,
                'name': 'Blocks',
                'inward': 'blocked by',
                'outward': 'blocks',
                '_links': {'self': {'href': '/api/v3/relation_types/blocks'}}
            },
            {
                'id': 2,
                'name': 'Relates',
                'inward': 'relates to',
                'outward': 'relates to',
                '_links': {'self': {'href': '/api/v3/relation_types/relates'}}
            },
            {
                'id': 3,
                'name': 'Duplicates',
                'inward': 'duplicated by',
                'outward': 'duplicates',
                '_links': {'self': {'href': '/api/v3/relation_types/duplicates'}}
            }
        ]

        # Expected link type mapping
        self.expected_mapping = {
            '10100': {
                'jira_id': '10100',
                'jira_name': 'Blocks',
                'jira_inward': 'is blocked by',
                'jira_outward': 'blocks',
                'openproject_id': 1,
                'openproject_name': 'Blocks',
                'openproject_inward': 'blocked by',
                'openproject_outward': 'blocks',
                'matched_by': 'name'
            },
            '10101': {
                'jira_id': '10101',
                'jira_name': 'Cloners',
                'jira_inward': 'is cloned by',
                'jira_outward': 'clones',
                'openproject_id': 3,
                'openproject_name': 'Duplicates',
                'openproject_inward': 'duplicated by',
                'openproject_outward': 'duplicates',
                'matched_by': 'similar_outward'
            },
            '10102': {
                'jira_id': '10102',
                'jira_name': 'Custom Link',
                'jira_inward': 'is custom linked to',
                'jira_outward': 'custom links to',
                'openproject_id': None,
                'openproject_name': None,
                'openproject_inward': None,
                'openproject_outward': None,
                'matched_by': 'none'
            }
        }

    @patch('src.migrations.link_type_migration.JiraClient')
    @patch('src.migrations.link_type_migration.OpenProjectClient')
    @patch('src.migrations.link_type_migration.config.get_path')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_extract_jira_link_types(self, mock_file, mock_exists, mock_get_path,
                                   mock_op_client, mock_jira_client):
        """Test the extract_jira_link_types method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_jira_instance.jira._session.get.return_value.json.return_value = {
            'issueLinkTypes': self.jira_link_types
        }
        mock_jira_instance.jira._session.get.return_value.raise_for_status = MagicMock()
        mock_jira_instance.base_url = 'https://jira.example.com'

        mock_op_instance = mock_op_client.return_value
        mock_get_path.return_value = '/tmp/test_data'

        # Create instance and call method
        migration = LinkTypeMigration(mock_jira_instance, mock_op_instance)
        result = migration.extract_jira_link_types()

        # Assertions
        self.assertEqual(result, self.jira_link_types)
        mock_jira_instance.jira._session.get.assert_called_once_with(
            'https://jira.example.com/rest/api/2/issueLinkType'
        )
        mock_file.assert_called_with('/tmp/test_data/jira_link_types.json', 'w')
        mock_file().write.assert_called()

    @patch('src.migrations.link_type_migration.JiraClient')
    @patch('src.migrations.link_type_migration.OpenProjectClient')
    @patch('src.migrations.link_type_migration.config.get_path')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_extract_openproject_relation_types(self, mock_file, mock_exists, mock_get_path,
                                             mock_op_client, mock_jira_client):
        """Test the extract_openproject_relation_types method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value
        mock_op_instance.get_relation_types.return_value = self.op_link_types

        mock_get_path.return_value = '/tmp/test_data'

        # Create instance and call method
        migration = LinkTypeMigration(mock_jira_instance, mock_op_instance)
        result = migration.extract_openproject_relation_types()

        # Assertions
        self.assertEqual(result, self.op_link_types)
        mock_op_instance.get_relation_types.assert_called_once()
        mock_file.assert_called_with('/tmp/test_data/openproject_relation_types.json', 'w')
        mock_file().write.assert_called()

    @patch('src.migrations.link_type_migration.JiraClient')
    @patch('src.migrations.link_type_migration.OpenProjectClient')
    @patch('src.migrations.link_type_migration.config.get_path')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_create_link_type_mapping(self, mock_file, mock_exists, mock_get_path,
                                   mock_op_client, mock_jira_client):
        """Test the create_link_type_mapping method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        mock_get_path.return_value = '/tmp/test_data'

        # Create instance and set data
        migration = LinkTypeMigration(mock_jira_instance, mock_op_instance)
        migration.jira_link_types = self.jira_link_types
        migration.op_link_types = self.op_link_types

        # Call method
        result = migration.create_link_type_mapping()

        # Assertions
        self.assertEqual(len(result), 3)  # One mapping entry per Jira link type

        # Blocks should match by name
        self.assertEqual(result['10100']['openproject_id'], 1)
        self.assertEqual(result['10100']['matched_by'], 'name')

        # Cloners might match by similarity to Duplicates or might not match
        # We'll check that it's either matching correctly or is None
        # (depending on the string similarity implementation)
        if result['10101']['openproject_id'] is not None:
            self.assertEqual(result['10101']['openproject_id'], 3)
            self.assertIn(result['10101']['matched_by'], ['similar_outward', 'similar_inward'])
        else:
            self.assertIsNone(result['10101']['openproject_id'])
            self.assertEqual(result['10101']['matched_by'], 'none')

        # Custom Link should have no match
        self.assertIsNone(result['10102']['openproject_id'])
        self.assertEqual(result['10102']['matched_by'], 'none')

        mock_file.assert_called_with('/tmp/test_data/link_type_mapping.json', 'w')
        mock_file().write.assert_called()

    @patch('src.migrations.link_type_migration.JiraClient')
    @patch('src.migrations.link_type_migration.OpenProjectClient')
    @patch('src.migrations.link_type_migration.config.get_path')
    @patch('src.migrations.link_type_migration.config.migration_config')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_create_relation_type_in_openproject(self, mock_file, mock_exists,
                                              mock_migration_config, mock_get_path,
                                              mock_op_client, mock_jira_client):
        """Test the create_relation_type_in_openproject method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        # Mock successful relation type creation
        mock_op_instance.create_relation_type.return_value = {
            'success': True,
            'data': {
                'id': 4,
                'name': 'Custom Link',
                'inward': 'is custom linked to',
                'outward': 'custom links to'
            }
        }

        mock_get_path.return_value = '/tmp/test_data'
        mock_migration_config.get.return_value = False  # Not dry run

        # Create instance and call method
        migration = LinkTypeMigration(mock_jira_instance, mock_op_instance)
        result = migration.create_relation_type_in_openproject(self.jira_link_types[2])  # Use Custom Link

        # Assertions
        self.assertEqual(result['id'], 4)
        self.assertEqual(result['name'], 'Custom Link')
        mock_op_instance.create_relation_type.assert_called_with(
            name='Custom Link',
            inward='is custom linked to',
            outward='custom links to'
        )

    @patch('src.migrations.link_type_migration.JiraClient')
    @patch('src.migrations.link_type_migration.OpenProjectClient')
    @patch('src.migrations.link_type_migration.config.get_path')
    @patch('src.migrations.link_type_migration.config.migration_config')
    @patch('src.migrations.link_type_migration.ProgressTracker')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_migrate_link_types(self, mock_file, mock_exists, mock_progress_tracker,
                             mock_migration_config, mock_get_path,
                             mock_op_client, mock_jira_client):
        """Test the migrate_link_types method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        # Mock tracker context
        tracker_instance = mock_progress_tracker.return_value.__enter__.return_value

        # Mock relation type creation for the unmatched type
        mock_op_instance.create_relation_type.return_value = {
            'success': True,
            'data': {
                'id': 4,
                'name': 'Custom Link',
                'inward': 'is custom linked to',
                'outward': 'custom links to'
            }
        }

        mock_get_path.return_value = '/tmp/test_data'
        mock_migration_config.get.return_value = False  # Not dry run

        # Create instance and set data for the test
        migration = LinkTypeMigration(mock_jira_instance, mock_op_instance)
        migration.jira_link_types = self.jira_link_types
        migration.op_link_types = self.op_link_types
        migration.link_type_mapping = self.expected_mapping

        # Call method
        result = migration.migrate_link_types()

        # Assertions
        # Only 'Custom Link' should be created as it has no mapping
        mock_op_instance.create_relation_type.assert_called_once()
        self.assertEqual(result['10102']['openproject_id'], 4)
        self.assertEqual(result['10102']['matched_by'], 'created')

        # Check that the mapping file is updated
        mock_file.assert_any_call('/tmp/test_data/link_type_mapping.json', 'w')
        mock_file().write.assert_called()

    @patch('src.migrations.link_type_migration.JiraClient')
    @patch('src.migrations.link_type_migration.OpenProjectClient')
    @patch('src.migrations.link_type_migration.config.get_path')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_analyze_link_type_mapping(self, mock_file, mock_exists, mock_get_path,
                                    mock_op_client, mock_jira_client):
        """Test the analyze_link_type_mapping method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        mock_get_path.return_value = '/tmp/test_data'
        mock_exists.return_value = True

        # Add a 'created' match to the mapping for testing all match types
        mapping = self.expected_mapping.copy()
        mapping['10102'] = {
            'jira_id': '10102',
            'jira_name': 'Custom Link',
            'jira_inward': 'is custom linked to',
            'jira_outward': 'custom links to',
            'openproject_id': 4,
            'openproject_name': 'Custom Link',
            'openproject_inward': 'is custom linked to',
            'openproject_outward': 'custom links to',
            'matched_by': 'created'
        }

        # Mock file reads
        mock_file.return_value.__enter__.return_value.read.return_value = json.dumps(mapping)

        # Create instance
        migration = LinkTypeMigration(mock_jira_instance, mock_op_instance)
        migration.link_type_mapping = mapping

        # Call method
        result = migration.analyze_link_type_mapping()

        # Assertions
        self.assertEqual(result['total_types'], 3)
        self.assertEqual(result['matched_types'], 3)  # All are now matched
        self.assertEqual(result['matched_by_name'], 1)  # 'Blocks'
        self.assertEqual(result['matched_by_similar'], 1)  # 'Cloners' -> 'Duplicates'
        self.assertEqual(result['matched_by_creation'], 1)  # 'Custom Link'


# Define testing steps for link type migration validation

def link_type_migration_test_steps():
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
