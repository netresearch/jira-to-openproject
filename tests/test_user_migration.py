"""Tests for the user migration component."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

from src.migrations.user_migration import UserMigration


class TestUserMigration(unittest.TestCase):
    """Test cases for the UserMigration class."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        # Sample Jira users data
        self.jira_users = [
            {
                "key": "user1",
                "name": "user1",
                "emailAddress": "user1@example.com",
                "displayName": "User One",
                "active": True,
            },
            {
                "key": "user2",
                "name": "user2",
                "emailAddress": "user2@example.com",
                "displayName": "User Two",
                "active": True,
            },
        ]

        # Sample OpenProject users data
        self.op_users = [
            {
                "id": 1,
                "login": "admin",
                "email": "admin@example.com",
                "firstName": "Admin",
                "lastName": "User",
                "status": "active",
            },
            {
                "id": 2,
                "login": "user1",
                "email": "user1@example.com",
                "firstName": "User",
                "lastName": "One",
                "status": "active",
            },
        ]

        # Expected user mapping
        self.expected_mapping = {
            "user1": {
                "jira_key": "user1",
                "jira_name": "user1",
                "jira_email": "user1@example.com",
                "jira_display_name": "User One",
                "openproject_id": 2,
                "openproject_login": "user1",
                "openproject_email": "user1@example.com",
                "matched_by": "username",
            },
            "user2": {
                "jira_key": "user2",
                "jira_name": "user2",
                "jira_email": "user2@example.com",
                "jira_display_name": "User Two",
                "openproject_id": None,
                "openproject_login": None,
                "openproject_email": None,
                "matched_by": "none",
            },
        }

    @patch("src.clients.jira_client.JiraClient")
    @patch("src.clients.openproject_client.OpenProjectClient")
    @patch("src.migrations.user_migration.config.get_path")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_extract_jira_users(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the extract_jira_users method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_jira_instance.get_users.return_value = self.jira_users

        mock_op_instance = mock_op_client.return_value

        mock_get_path.return_value = Path("/tmp/test_data")
        mock_exists.return_value = True

        # Initialize migration
        migration = UserMigration(mock_jira_instance, mock_op_instance)

        # Call extract_jira_users
        result = migration.extract_jira_users()

        # Verify calls
        mock_jira_instance.get_users.assert_called_once()

        # Verify data was extracted
        assert len(result) == 2
        assert migration.jira_users == self.jira_users

    @patch("src.clients.jira_client.JiraClient")
    @patch("src.clients.openproject_client.OpenProjectClient")
    @patch("src.migrations.user_migration.config.get_path")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_extract_openproject_users(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the extract_openproject_users method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value

        mock_op_instance = mock_op_client.return_value
        mock_op_instance.get_users.return_value = self.op_users

        mock_get_path.return_value = Path("/tmp/test_data")
        mock_exists.return_value = True

        # Initialize migration
        migration = UserMigration(mock_jira_instance, mock_op_instance)

        # Call extract_openproject_users
        result = migration.extract_openproject_users()

        # Verify calls
        mock_op_instance.get_users.assert_called_once()

        # Verify data was extracted
        assert len(result) == 2
        assert migration.op_users == self.op_users

    @patch("src.clients.jira_client.JiraClient")
    @patch("src.clients.openproject_client.OpenProjectClient")
    @patch("src.migrations.user_migration.config.get_path")
    @patch("src.migrations.user_migration.ProgressTracker")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_create_user_mapping(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_tracker: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the create_user_mapping method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_jira_instance.get_users.return_value = self.jira_users

        mock_op_instance = mock_op_client.return_value
        mock_op_instance.get_users.return_value = self.op_users

        mock_get_path.return_value = Path("/tmp/test_data")
        mock_exists.return_value = True

        # Mock the progress tracker context manager
        mock_tracker_instance = MagicMock()
        mock_tracker.return_value.__enter__.return_value = mock_tracker_instance

        # Initialize migration
        migration = UserMigration(mock_jira_instance, mock_op_instance)

        # Call create_user_mapping
        result = migration.create_user_mapping()

        # Verify mappings
        assert "user1" in result
        assert "user2" in result
        assert result["user1"]["openproject_id"] == 2
        assert result["user1"]["openproject_login"] == "user1"
        assert result["user1"]["openproject_email"] == "user1@example.com"
        assert result["user1"]["matched_by"] == "username"
        assert result["user2"]["openproject_id"] is None
        assert result["user2"]["openproject_login"] is None
        assert result["user2"]["openproject_email"] is None
        assert result["user2"]["matched_by"] == "none"

    @patch("src.clients.jira_client.JiraClient")
    @patch("src.clients.openproject_client.OpenProjectClient")
    @patch("src.migrations.user_migration.config.get_path")
    @patch("src.migrations.user_migration.ProgressTracker")
    @patch("src.migrations.user_migration.logger")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_create_missing_users(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_logger: MagicMock,
        mock_tracker: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the create_missing_users method."""
        # Set up mock OpenProject and Jira clients
        mock_op_instance = mock_op_client.return_value
        mock_jira_instance = mock_jira_client.return_value

        # Configure jira_client to return test users
        jira_users = [
            {"key": "test1", "name": "test_user1", "emailAddress": "test1@example.com", "displayName": "Test User 1"},
            {"key": "test2", "name": "test_user2", "emailAddress": "test2@example.com", "displayName": "Test User 2"},
        ]

        # Mock the progress tracker context manager
        mock_tracker_instance = MagicMock()
        mock_tracker.return_value.__enter__.return_value = mock_tracker_instance

        # Create UserMigration instance with mocked clients
        data_dir = Path(tempfile.mkdtemp())

        # Use patch.object to avoid calling the real methods during initialization
        with (
            patch.object(UserMigration, "_save_to_json"),
            patch.object(UserMigration, "_load_from_json", return_value=None),
        ):

            migration = UserMigration(
                jira_client=mock_jira_instance,
                op_client=mock_op_instance,
            )

            # Add mocks to avoid file operations that cause serialization errors
            migration._save_to_json = MagicMock()
            migration.extract_jira_users = MagicMock(return_value=jira_users)
            migration.extract_openproject_users = MagicMock(return_value=[])

            # Mock the create_user_mapping method to return unmatched users
            user_mapping = {
                "test1": {
                    "jira_key": "test1",
                    "jira_name": "test_user1",
                    "jira_email": "test1@example.com",
                    "jira_display_name": "Test User 1",
                    "openproject_id": None,
                    "openproject_login": None,
                    "openproject_email": None,
                    "matched_by": "none",
                },
                "test2": {
                    "jira_key": "test2",
                    "jira_name": "test_user2",
                    "jira_email": "test2@example.com",
                    "jira_display_name": "Test User 2",
                    "openproject_id": None,
                    "openproject_login": None,
                    "openproject_email": None,
                    "matched_by": "none",
                },
            }
            migration.create_user_mapping = MagicMock(return_value=user_mapping)
            migration.user_mapping = user_mapping

            # Configure op_client to succeed when creating users
            mock_op_instance.create_users_in_bulk.return_value = json.dumps(
                {
                    "created_count": 2,
                    "failed_count": 0,
                    "created_users": [
                        {"id": 101, "login": "test_user1", "email": "test1@example.com"},
                        {"id": 102, "login": "test_user2", "email": "test2@example.com"},
                    ],
                    "failed_users": [],
                },
            )

            # Test create_missing_users method
            result = migration.create_missing_users()

            # Verify clients were called correctly
            migration.create_user_mapping.assert_called_once()
            mock_op_instance.create_users_in_bulk.assert_called_once()

            # Verify result
            assert result["created_count"] == 2

        # Clean up
        shutil.rmtree(data_dir)


if __name__ == "__main__":
    unittest.main()
