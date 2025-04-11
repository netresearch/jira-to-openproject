"""
Tests for the project migration component.
"""
import unittest
from unittest.mock import patch, MagicMock, mock_open, ANY
import json
import os
from src.migrations.project_migration import ProjectMigration


class TestProjectMigration(unittest.TestCase):
    """Test cases for the ProjectMigration class."""

    def setUp(self):
        """Set up test fixtures."""
        # Sample Jira projects data
        self.jira_projects = [
            {'id': '10001', 'key': 'PROJ1', 'name': 'Project One', 'description': 'First test project'},
            {'id': '10002', 'key': 'PROJ2', 'name': 'Project Two', 'description': 'Second test project'},
            {'id': '10003', 'key': 'PROJ3', 'name': 'Project Three', 'description': 'Third test project with no account'},
        ]

        # Sample OpenProject projects data
        self.op_projects = [
            {'id': 1, 'name': 'Project One', 'identifier': 'proj1', 'description': {'raw': 'First test project'}},
            {'id': 3, 'name': 'Existing Project', 'identifier': 'existing-project', 'description': {'raw': 'This already exists'}},
        ]

        # Sample project account mapping
        self.project_account_mapping = {
            'PROJ1': 101,
            'PROJ2': 102,
        }

        # Sample account mapping
        self.account_mapping = {
            '101': {'tempo_id': '101', 'tempo_name': 'Account One'},
            '102': {'tempo_id': '102', 'tempo_name': 'Account Two'},
        }

        # Expected project mapping
        self.expected_mapping = {
            'PROJ1': {
                'jira_key': 'PROJ1',
                'jira_name': 'Project One',
                'openproject_id': 1,
                'openproject_identifier': 'proj1',
                'openproject_name': 'Project One',
                'account_id': 101,
                'account_name': 'Account One',
                'created_new': False,
            },
            'PROJ2': {
                'jira_key': 'PROJ2',
                'jira_name': 'Project Two',
                'openproject_id': 2,
                'openproject_identifier': 'proj2',
                'openproject_name': 'Project Two',
                'account_id': 102,
                'account_name': 'Account Two',
                'created_new': True,
            },
            'PROJ3': {
                'jira_key': 'PROJ3',
                'jira_name': 'Project Three',
                'openproject_id': 4,
                'openproject_identifier': 'proj3',
                'openproject_name': 'Project Three',
                'account_id': None,
                'account_name': None,
                'created_new': True,
            }
        }

    @patch('src.migrations.project_migration.JiraClient')
    @patch('src.migrations.project_migration.OpenProjectClient')
    @patch('src.migrations.project_migration.config.get_path')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_extract_jira_projects(self, mock_file, mock_exists, mock_get_path,
                                   mock_op_client, mock_jira_client):
        """Test the extract_jira_projects method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_jira_instance.get_projects.return_value = self.jira_projects

        mock_op_instance = mock_op_client.return_value
        mock_get_path.return_value = '/tmp/test_data'
        mock_exists.return_value = False  # No cached data

        # Create instance and call method
        migration = ProjectMigration(mock_jira_instance, mock_op_instance)
        result = migration.extract_jira_projects(force=True)

        # Assertions
        self.assertEqual(result, self.jira_projects)
        mock_jira_instance.get_projects.assert_called_once()
        mock_file.assert_called_with('/tmp/test_data/jira_projects.json', 'w')
        mock_file().write.assert_called()

    @patch('src.migrations.project_migration.JiraClient')
    @patch('src.migrations.project_migration.OpenProjectClient')
    @patch('src.migrations.project_migration.config.get_path')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_extract_openproject_projects(self, mock_file, mock_exists, mock_get_path,
                                         mock_op_client, mock_jira_client):
        """Test the extract_openproject_projects method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value
        mock_op_instance.get_projects.return_value = self.op_projects

        mock_get_path.return_value = '/tmp/test_data'
        mock_exists.return_value = False  # No cached data

        # Create instance and call method
        migration = ProjectMigration(mock_jira_instance, mock_op_instance)
        result = migration.extract_openproject_projects(force=True)

        # Assertions
        self.assertEqual(result, self.op_projects)
        mock_op_instance.get_projects.assert_called_once()
        mock_file.assert_called_with('/tmp/test_data/openproject_projects.json', 'w')
        mock_file().write.assert_called()

    @patch('src.migrations.project_migration.JiraClient')
    @patch('src.migrations.project_migration.OpenProjectClient')
    @patch('src.migrations.project_migration.config.get_path')
    @patch('src.migrations.project_migration.config.migration_config')
    @patch('os.path.exists')
    @patch('builtins.open')
    def test_create_project_in_openproject(self, mock_open_func, mock_exists,
                                          mock_migration_config, mock_get_path,
                                          mock_op_client, mock_jira_client):
        """Test the create_project_in_openproject method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        # Mock successful project creation
        mock_op_instance.create_project.return_value = (
            {'id': 2, 'name': 'Project Two', 'identifier': 'proj2'},
            True  # was_created flag
        )

        # Mock setting custom field
        mock_op_instance.set_project_custom_field.return_value = True

        mock_get_path.return_value = '/tmp/test_data'
        mock_migration_config.get.return_value = False  # Not dry run

        # Create instance
        migration = ProjectMigration(mock_jira_instance, mock_op_instance)
        migration.account_custom_field_id = 100  # Set a fake custom field ID
        migration.account_mapping = self.account_mapping

        # Call method with account
        jira_project = self.jira_projects[1]  # PROJ2
        result = migration.create_project_in_openproject(jira_project, account_id=102)

        # Assertions
        self.assertEqual(result['id'], 2)
        self.assertEqual(result['name'], 'Project Two')
        self.assertEqual(result['account_name'], 'Account Two')
        mock_op_instance.create_project.assert_called_with(
            name='Project Two',
            identifier='proj2',
            description='Second test project'
        )
        mock_op_instance.set_project_custom_field.assert_called_with(
            project_id=2,
            custom_field_id=100,
            value='Account Two'
        )

    @patch('src.migrations.project_migration.JiraClient')
    @patch('src.migrations.project_migration.OpenProjectClient')
    @patch('src.migrations.project_migration.config.get_path')
    @patch('src.migrations.project_migration.config.migration_config')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_migrate_projects(self, mock_file, mock_exists, mock_migration_config,
                             mock_get_path, mock_op_client, mock_jira_client):
        """Test the migrate_projects method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        # Set up return values
        mock_jira_instance.get_projects.return_value = self.jira_projects
        mock_op_instance.get_projects.return_value = self.op_projects

        # Mock project creation for new projects
        def create_project_side_effect(name, identifier, description):
            if name == 'Project One':
                return {'id': 1, 'name': name, 'identifier': identifier}, False
            elif name == 'Project Two':
                return {'id': 2, 'name': name, 'identifier': identifier}, True
            elif name == 'Project Three':
                return {'id': 4, 'name': name, 'identifier': identifier}, True
            else:
                return None, False

        mock_op_instance.create_project.side_effect = create_project_side_effect
        mock_op_instance.set_project_custom_field.return_value = True

        mock_get_path.return_value = '/tmp/test_data'
        mock_migration_config.get.return_value = False  # Not dry run and not force
        mock_exists.return_value = True

        # Mock file reads - we need to provide all file handles that will be used
        mock_file.return_value.__enter__.return_value.read.side_effect = [
            json.dumps(self.project_account_mapping),  # For project_account_mapping.json
            json.dumps(self.account_mapping)           # For account_mapping.json
        ]

        # Create instance
        migration = ProjectMigration(mock_jira_instance, mock_op_instance)
        migration.jira_projects = self.jira_projects
        migration.op_projects = self.op_projects
        migration.account_custom_field_id = 100  # Set a fake custom field ID
        migration.project_account_mapping = self.project_account_mapping
        migration.account_mapping = self.account_mapping

        # Call method
        result = migration.migrate_projects()

        # Assertions
        self.assertEqual(len(result), 3)  # One mapping entry per project
        self.assertEqual(result['PROJ1']['openproject_id'], 1)
        self.assertEqual(result['PROJ2']['openproject_id'], 2)
        self.assertEqual(result['PROJ3']['openproject_id'], 4)

        # Check that we created new projects as expected
        self.assertFalse(result['PROJ1']['created_new'])
        self.assertTrue(result['PROJ2']['created_new'])
        self.assertTrue(result['PROJ3']['created_new'])

        # Check account associations
        self.assertEqual(result['PROJ1']['account_id'], 101)
        self.assertEqual(result['PROJ2']['account_id'], 102)
        self.assertIsNone(result['PROJ3']['account_id'])

        # Verify the file was written
        mock_file.assert_any_call('/tmp/test_data/project_mapping.json', 'w')

    @patch('src.migrations.project_migration.JiraClient')
    @patch('src.migrations.project_migration.OpenProjectClient')
    @patch('src.migrations.project_migration.config.get_path')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_analyze_project_mapping(self, mock_file, mock_exists, mock_get_path,
                                    mock_op_client, mock_jira_client):
        """Test the analyze_project_mapping method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        mock_get_path.return_value = '/tmp/test_data'
        mock_exists.return_value = True

        # Mock file reads
        mock_file.return_value.__enter__.return_value.read.return_value = json.dumps(self.expected_mapping)

        # Create instance
        migration = ProjectMigration(mock_jira_instance, mock_op_instance)
        migration.project_mapping = self.expected_mapping

        # Call method
        result = migration.analyze_project_mapping()

        # Assertions
        self.assertEqual(result['total_projects'], 3)
        self.assertEqual(result['migrated_projects'], 3)
        self.assertEqual(result['new_projects'], 2)  # PROJ2 and PROJ3 are new
        self.assertEqual(result['existing_projects'], 1)  # PROJ1 already existed
        self.assertEqual(result['projects_with_accounts'], 2)  # PROJ1 and PROJ2 have accounts


# Define testing steps for project migration validation

def project_migration_test_steps():
    """
    Testing steps for project migration validation.

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
