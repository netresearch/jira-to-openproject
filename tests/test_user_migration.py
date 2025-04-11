"""
Tests for the user migration component.
"""
import unittest
from unittest.mock import patch, MagicMock, mock_open, ANY
import json
import os
from src.migrations.user_migration import UserMigration


class TestUserMigration(unittest.TestCase):
    """Test cases for the UserMigration class."""

    def setUp(self):
        """Set up test fixtures."""
        # Sample Jira users data
        self.jira_users = [
            {'key': 'user1', 'name': 'user1', 'emailAddress': 'user1@example.com', 'displayName': 'User One', 'active': True},
            {'key': 'user2', 'name': 'user2', 'emailAddress': 'user2@example.com', 'displayName': 'User Two', 'active': True},
        ]

        # Sample OpenProject users data
        self.op_users = [
            {'id': 1, 'login': 'admin', 'email': 'admin@example.com', 'firstName': 'Admin', 'lastName': 'User', 'status': 'active'},
            {'id': 2, 'login': 'user1', 'email': 'user1@example.com', 'firstName': 'User', 'lastName': 'One', 'status': 'active'},
        ]

        # Expected user mapping
        self.expected_mapping = {
            'user1': {'jira_key': 'user1', 'jira_name': 'user1', 'jira_email': 'user1@example.com', 'jira_display_name': 'User One',
                     'openproject_id': 2, 'openproject_login': 'user1', 'openproject_email': 'user1@example.com', 'matched_by': 'username'},
            'user2': {'jira_key': 'user2', 'jira_name': 'user2', 'jira_email': 'user2@example.com', 'jira_display_name': 'User Two',
                     'openproject_id': None, 'openproject_login': None, 'openproject_email': None, 'matched_by': 'none'}
        }

    @patch('src.migrations.user_migration.JiraClient')
    @patch('src.migrations.user_migration.OpenProjectClient')
    @patch('src.migrations.user_migration.config.get_path')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_extract_jira_users(self, mock_file, mock_exists, mock_get_path, mock_op_client, mock_jira_client):
        """Test the extract_jira_users method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_jira_instance.get_users.return_value = self.jira_users

        mock_op_instance = mock_op_client.return_value

        mock_get_path.return_value = '/tmp/test_data'
        mock_exists.return_value = True

        # Initialize migration
        migration = UserMigration(mock_jira_instance, mock_op_instance)

        # Call extract_jira_users
        result = migration.extract_jira_users()

        # Verify calls
        mock_jira_instance.get_users.assert_called_once()

        # Verify data was extracted
        self.assertEqual(len(result), 2)
        self.assertEqual(migration.jira_users, self.jira_users)

    @patch('src.migrations.user_migration.JiraClient')
    @patch('src.migrations.user_migration.OpenProjectClient')
    @patch('src.migrations.user_migration.config.get_path')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_extract_openproject_users(self, mock_file, mock_exists, mock_get_path, mock_op_client, mock_jira_client):
        """Test the extract_openproject_users method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value

        mock_op_instance = mock_op_client.return_value
        mock_op_instance.get_users.return_value = self.op_users

        mock_get_path.return_value = '/tmp/test_data'
        mock_exists.return_value = True

        # Initialize migration
        migration = UserMigration(mock_jira_instance, mock_op_instance)

        # Call extract_openproject_users
        result = migration.extract_openproject_users()

        # Verify calls
        mock_op_instance.get_users.assert_called_once()

        # Verify data was extracted
        self.assertEqual(len(result), 2)
        self.assertEqual(migration.op_users, self.op_users)

    @patch('src.migrations.user_migration.JiraClient')
    @patch('src.migrations.user_migration.OpenProjectClient')
    @patch('src.migrations.user_migration.config.get_path')
    @patch('src.migrations.user_migration.ProgressTracker')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_create_user_mapping(self, mock_file, mock_exists, mock_tracker, mock_get_path, mock_op_client, mock_jira_client):
        """Test the create_user_mapping method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_jira_instance.get_users.return_value = self.jira_users

        mock_op_instance = mock_op_client.return_value
        mock_op_instance.get_users.return_value = self.op_users

        mock_get_path.return_value = '/tmp/test_data'
        mock_exists.return_value = True

        # Mock the progress tracker context manager
        mock_tracker_instance = MagicMock()
        mock_tracker.return_value.__enter__.return_value = mock_tracker_instance

        # Initialize migration
        migration = UserMigration(mock_jira_instance, mock_op_instance)

        # Call create_user_mapping
        result = migration.create_user_mapping()

        # Verify mappings
        self.assertIn('user1', result)
        self.assertIn('user2', result)
        self.assertEqual(result['user1']['openproject_id'], 2)
        self.assertEqual(result['user1']['openproject_login'], 'user1')
        self.assertEqual(result['user1']['openproject_email'], 'user1@example.com')
        self.assertEqual(result['user1']['matched_by'], 'username')
        self.assertIsNone(result['user2']['openproject_id'])
        self.assertIsNone(result['user2']['openproject_login'])
        self.assertIsNone(result['user2']['openproject_email'])
        self.assertEqual(result['user2']['matched_by'], 'none')

    @patch('src.migrations.user_migration.JiraClient')
    @patch('src.migrations.user_migration.OpenProjectClient')
    @patch('src.migrations.user_migration.config.get_path')
    @patch('src.migrations.user_migration.ProgressTracker')
    @patch('src.migrations.user_migration.logger')
    @patch('os.path.exists')
    @patch('builtins.open', new_callable=mock_open)
    def test_create_missing_users(self, mock_file, mock_exists, mock_logger, mock_tracker,
                                 mock_get_path, mock_op_client, mock_jira_client):
        """Test the create_missing_users method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value

        mock_op_instance = mock_op_client.return_value
        mock_op_instance.create_user.return_value = {'id': 3, 'login': 'user2', 'email': 'user2@example.com'}
        mock_op_instance._request = MagicMock()
        mock_op_instance._request.return_value = {'id': 3, 'login': 'user2', 'email': 'user2@example.com'}

        mock_get_path.return_value = '/tmp/test_data'
        mock_exists.return_value = True

        # Mock the progress tracker context manager
        mock_tracker_instance = MagicMock()
        mock_tracker.return_value.__enter__.return_value = mock_tracker_instance

        # Initialize migration
        migration = UserMigration(mock_jira_instance, mock_op_instance)

        # Set up the user mapping
        migration.user_mapping = self.expected_mapping

        # Mock the _save_to_json method to avoid serialization issues
        with patch.object(migration, '_save_to_json'):
            # Call create_missing_users
            result = migration.create_missing_users()

            # Verify _request was called with the expected POST parameters
            mock_op_instance._request.assert_called_with("POST", "/users", data=ANY)

            # Check that the updated mapping has the expected values
            self.assertIn('user2', result)
            self.assertEqual(result['user2']['matched_by'], 'created')
            self.assertEqual(result['user2']['openproject_id'], 3)
            self.assertEqual(result['user2']['openproject_login'], 'user2')
            self.assertEqual(result['user2']['openproject_email'], 'user2@example.com')


if __name__ == '__main__':
    unittest.main()
