"""
Tests for the issue type migration component.
"""
import unittest
from unittest.mock import patch, MagicMock, mock_open, ANY, call
import json
import os
from src.migrations.issue_type_migration import IssueTypeMigration


class TestIssueTypeMigration(unittest.TestCase):
    """Test cases for the IssueTypeMigration class."""

    def setUp(self):
        """Set up test fixtures."""
        # Sample Jira issue types data
        self.jira_issue_types = [
            {
                'id': '10000',
                'name': 'Bug',
                'description': 'A bug in the software',
                'iconUrl': 'https://jira.example.com/images/icons/bug.svg',
                'subtask': False
            },
            {
                'id': '10001',
                'name': 'Task',
                'description': 'A task that needs to be done',
                'iconUrl': 'https://jira.example.com/images/icons/task.svg',
                'subtask': False
            },
            {
                'id': '10002',
                'name': 'Epic',
                'description': 'A big user story',
                'iconUrl': 'https://jira.example.com/images/icons/epic.svg',
                'subtask': False
            },
            {
                'id': '10003',
                'name': 'Custom Type',
                'description': 'A custom issue type',
                'iconUrl': 'https://jira.example.com/images/icons/custom.svg',
                'subtask': False
            }
        ]

        # Sample OpenProject work package types data
        self.op_work_package_types = [
            {
                'id': 1,
                'name': 'Bug',
                'color': '#E44D42',
                'position': 1,
                'is_default': False,
                'is_milestone': False,
                '_links': {'self': {'href': '/api/v3/types/1'}}
            },
            {
                'id': 2,
                'name': 'Task',
                'color': '#1A67A3',
                'position': 2,
                'is_default': True,
                'is_milestone': False,
                '_links': {'self': {'href': '/api/v3/types/2'}}
            },
            {
                'id': 3,
                'name': 'Milestone',
                'color': '#E73E97',
                'position': 3,
                'is_default': False,
                'is_milestone': True,
                '_links': {'self': {'href': '/api/v3/types/3'}}
            }
        ]

        # Expected issue type mapping
        self.expected_mapping = {
            'Bug': {
                'jira_id': '10000',
                'jira_name': 'Bug',
                'jira_description': 'A bug in the software',
                'openproject_id': 1,
                'openproject_name': 'Bug',
                'color': '#E44D42',
                'is_milestone': False,
                'matched_by': 'exact_match'
            },
            'Task': {
                'jira_id': '10001',
                'jira_name': 'Task',
                'jira_description': 'A task that needs to be done',
                'openproject_id': 2,
                'openproject_name': 'Task',
                'color': '#1A67A3',
                'is_milestone': False,
                'matched_by': 'exact_match'
            },
            'Epic': {
                'jira_id': '10002',
                'jira_name': 'Epic',
                'jira_description': 'A big user story',
                'openproject_id': None,
                'openproject_name': 'Epic',
                'color': '#9B59B6',
                'is_milestone': False,
                'matched_by': 'default_mapping_to_create'
            },
            'Custom Type': {
                'jira_id': '10003',
                'jira_name': 'Custom Type',
                'jira_description': 'A custom issue type',
                'openproject_id': None,
                'openproject_name': 'Custom Type',
                'color': '#1A67A3',
                'is_milestone': False,
                'matched_by': 'same_name'
            }
        }

        # Expected ID mapping
        self.expected_id_mapping = {
            '10000': 1,  # Bug
            '10001': 2,  # Task
        }

    @patch('src.migrations.issue_type_migration.JiraClient')
    @patch('src.migrations.issue_type_migration.OpenProjectClient')
    @patch('src.migrations.issue_type_migration.config.get_path')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_extract_jira_issue_types(self, mock_file, mock_exists, mock_get_path,
                                    mock_op_client, mock_jira_client):
        """Test the extract_jira_issue_types method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_jira_instance.get_issue_types.return_value = self.jira_issue_types

        mock_op_instance = mock_op_client.return_value
        mock_get_path.return_value = '/tmp/test_data'
        mock_exists.return_value = False  # No cached data

        # Create instance and call method
        migration = IssueTypeMigration(
            jira_client=mock_jira_instance,
            op_client=mock_op_instance
        )
        result = migration.extract_jira_issue_types(force=True)

        # Assertions
        self.assertEqual(result, self.jira_issue_types)
        mock_jira_instance.get_issue_types.assert_called_once()
        mock_file.assert_called_with('/tmp/test_data/jira_issue_types.json', 'w')
        mock_file().write.assert_called()

    @patch('src.migrations.issue_type_migration.JiraClient')
    @patch('src.migrations.issue_type_migration.OpenProjectClient')
    @patch('src.migrations.issue_type_migration.config.get_path')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_extract_openproject_work_package_types(self, mock_file, mock_exists, mock_get_path,
                                                mock_op_client, mock_jira_client):
        """Test the extract_openproject_work_package_types method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value
        mock_op_instance.get_work_package_types.return_value = self.op_work_package_types

        mock_get_path.return_value = '/tmp/test_data'
        mock_exists.return_value = False  # No cached data

        # Create instance and call method
        migration = IssueTypeMigration(
            jira_client=mock_jira_instance,
            op_client=mock_op_instance
        )
        result = migration.extract_openproject_work_package_types(force=True)

        # Assertions
        self.assertEqual(result, self.op_work_package_types)
        mock_op_instance.get_work_package_types.assert_called_once()
        mock_file.assert_called_with('/tmp/test_data/openproject_work_package_types.json', 'w')
        mock_file().write.assert_called()

    @patch('src.migrations.issue_type_migration.JiraClient')
    @patch('src.migrations.issue_type_migration.OpenProjectClient')
    @patch('src.migrations.issue_type_migration.config.get_path')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_create_issue_type_mapping(self, mock_file, mock_exists, mock_get_path,
                                    mock_op_client, mock_jira_client):
        """Test the create_issue_type_mapping method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        mock_get_path.return_value = '/tmp/test_data'

        # Mock file exists check - file does not exist so we'll create a new mapping
        mock_exists.return_value = False

        # Create instance and set data
        migration = IssueTypeMigration(
            jira_client=mock_jira_instance,
            op_client=mock_op_instance
        )
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
        mock_file.assert_called_with('/tmp/test_data/issue_type_mapping_template.json', 'w')
        mock_file().write.assert_called()

    @patch('src.migrations.issue_type_migration.JiraClient')
    @patch('src.migrations.issue_type_migration.OpenProjectClient')
    @patch('src.migrations.issue_type_migration.OpenProjectRailsClient')
    @patch('src.migrations.issue_type_migration.config.get_path')
    @patch('src.migrations.issue_type_migration.ProgressTracker')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_migrate_issue_types_via_rails(self, mock_file, mock_exists, mock_progress_tracker,
                                        mock_get_path, mock_rails_client, mock_op_client,
                                        mock_jira_client):
        """Test the migrate_issue_types_via_rails method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value
        mock_rails_instance = mock_rails_client.return_value

        # Mock tracker context
        tracker_instance = mock_progress_tracker.return_value.__enter__.return_value

        # Mock rails client execute script
        mock_rails_instance.execute_ruby_script.return_value = {
            'created': [
                {'id': 4, 'name': 'Epic', 'color': '#9B59B6', 'is_milestone': False}
            ],
            'existing': []
        }

        # Mock the connection to Rails console
        mock_rails_instance.connect_to_rails_console = MagicMock(return_value=True)
        mock_rails_instance.check_existing_work_package_types = MagicMock(return_value=[])

        mock_get_path.return_value = '/tmp/test_data'

        # Create instance and set data for the test
        migration = IssueTypeMigration(
            jira_client=mock_jira_instance,
            op_client=mock_op_instance,
            rails_console=mock_rails_instance
        )

        # Set up issue type mapping
        # Create a dictionary with simple values instead of MagicMock objects
        migration.issue_type_mapping = {
            "Epic": {
                "jira_id": "10002",
                "jira_name": "Epic",
                "openproject_id": None,
                "matched_by": "default_mapping_to_create",
                "color": "#9B59B6"
            }
        }

        # Add methods needed for the test that we'll mock
        migration.connect_to_rails_console = MagicMock(return_value=True)
        migration.check_existing_work_package_types = MagicMock(return_value=[])
        migration.create_work_package_type_via_rails = MagicMock(return_value={
            'status': 'success',
            'id': 4
        })

        # Call method
        result = migration.migrate_issue_types_via_rails()

        # Assertions
        self.assertTrue(result)  # Should return True for success
        mock_file.assert_called_with('/tmp/test_data/issue_type_migration_results.json', 'w')
        mock_file().write.assert_called()

    @patch('src.migrations.issue_type_migration.JiraClient')
    @patch('src.migrations.issue_type_migration.OpenProjectClient')
    @patch('src.migrations.issue_type_migration.config.get_path')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_migrate_issue_types(self, mock_file, mock_exists, mock_get_path,
                              mock_op_client, mock_jira_client):
        """Test the migrate_issue_types method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        mock_get_path.return_value = '/tmp/test_data'
        mock_exists.return_value = True

        # Create instance and set data for the test
        migration = IssueTypeMigration(
            jira_client=mock_jira_instance,
            op_client=mock_op_instance
        )
        migration.jira_issue_types = self.jira_issue_types
        migration.op_work_package_types = self.op_work_package_types
        migration.issue_type_mapping = self.expected_mapping

        # Mock analyze_issue_type_mapping to return a simple analysis
        migration.analyze_issue_type_mapping = MagicMock(return_value={
            'total_jira_types': 4,
            'matched_op_types': 2,
            'types_to_create': 2
        })

        # Call method
        result = migration.migrate_issue_types()

        # Assertions
        self.assertEqual(result['total_jira_types'], 4)
        self.assertEqual(result['matched_op_types'], 2)
        self.assertEqual(result['types_to_create'], 2)

        # Verify the ID mapping file is updated
        mock_file.assert_called_with('/tmp/test_data/issue_type_id_mapping.json', 'w')
        mock_file().write.assert_called()

        # Verify analyze_issue_type_mapping was called
        migration.analyze_issue_type_mapping.assert_called_once()

    @patch('src.migrations.issue_type_migration.JiraClient')
    @patch('src.migrations.issue_type_migration.OpenProjectClient')
    @patch('src.migrations.issue_type_migration.config.get_path')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_analyze_issue_type_mapping(self, mock_file, mock_exists, mock_get_path,
                                     mock_op_client, mock_jira_client):
        """Test the analyze_issue_type_mapping method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        mock_get_path.return_value = '/tmp/test_data'
        mock_exists.return_value = True

        # Create instance
        migration = IssueTypeMigration(
            jira_client=mock_jira_instance,
            op_client=mock_op_instance
        )
        migration.issue_type_mapping = self.expected_mapping

        # Call method
        result = migration.analyze_issue_type_mapping()

        # Assertions
        self.assertEqual(result['total_jira_types'], 4)
        self.assertEqual(result['matched_op_types'], 2)
        self.assertEqual(result['types_to_create'], 2)
        self.assertEqual(result['match_percentage'], 50.0)

        mock_file.assert_called_with('/tmp/test_data/issue_type_analysis.json', 'w')
        mock_file().write.assert_called()

    @patch('src.migrations.issue_type_migration.JiraClient')
    @patch('src.migrations.issue_type_migration.OpenProjectClient')
    @patch('src.migrations.issue_type_migration.config.get_path')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_update_mapping_file(self, mock_file, mock_exists, mock_get_path,
                              mock_op_client, mock_jira_client):
        """Test the update_mapping_file method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        # Mock existing mapping file
        mock_exists.return_value = True
        mock_file.return_value.__enter__.return_value.read.return_value = json.dumps(
            {
                "Bug": {
                    "jira_id": "10000",
                    "jira_name": "Bug",
                    "openproject_id": None,
                    "openproject_name": "Bug",
                    "matched_by": "none"
                },
                "Task": {
                    "jira_id": "10001",
                    "jira_name": "Task",
                    "openproject_id": None,
                    "openproject_name": "Task",
                    "matched_by": "none"
                }
            }
        )

        # Mock OpenProject client to return work package types that match EXACTLY the names
        # in the mapping ('Bug' and 'Task')
        mock_op_instance.get_work_package_types.return_value = [
            {
                "id": 1,
                "name": "Bug",
                "color": "#FF0000"
            },
            {
                "id": 2,
                "name": "Task",
                "color": "#00FF00"
            }
        ]

        mock_get_path.return_value = '/tmp/test_data'

        # Create instance and call method
        migration = IssueTypeMigration(
            jira_client=mock_jira_instance,
            op_client=mock_op_instance
        )

        # Set the mapping attribute directly to match what was mocked in the file
        migration.issue_type_mapping = {
            "Bug": {
                "jira_id": "10000",
                "jira_name": "Bug",
                "openproject_id": None,
                "openproject_name": "Bug",
                "matched_by": "none"
            },
            "Task": {
                "jira_id": "10001",
                "jira_name": "Task",
                "openproject_id": None,
                "openproject_name": "Task",
                "matched_by": "none"
            }
        }

        result = migration.update_mapping_file()

        # Assertions
        self.assertTrue(result)  # Should return True for success

        # Verify the mapping file is updated
        self.assertIn(call('/tmp/test_data/issue_type_mapping.json', 'w'), mock_file.call_args_list)
        mock_file().write.assert_called()


# Define testing steps for issue type migration validation

def issue_type_migration_test_steps():
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
