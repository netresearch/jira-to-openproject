"""Tests for the user migration component."""

import json
import shutil
import tempfile
import unittest
import pytest
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
            {
                "key": "test1",
                "name": "test_user1",
                "emailAddress": "test1@example.com",
                "displayName": "Test User 1",
            },
            {
                "key": "test2",
                "name": "test_user2",
                "emailAddress": "test2@example.com",
                "displayName": "Test User 2",
            },
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
            patch.object(UserMigration, "extract_jira_users", return_value=jira_users),
            patch.object(UserMigration, "extract_openproject_users", return_value=[]),
        ):
            migration = UserMigration(
                jira_client=mock_jira_instance,
                op_client=mock_op_instance,
            )

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
            with patch.object(
                migration,
                "create_user_mapping",
                return_value=user_mapping,
            ):
                migration.user_mapping = user_mapping

                # Configure op_client to succeed when creating users
                mock_op_instance.create_users_in_bulk.return_value = json.dumps(
                    {
                        "created_count": 2,
                        "failed_count": 0,
                        "created_users": [
                            {
                                "id": 101,
                                "login": "test_user1",
                                "email": "test1@example.com",
                            },
                            {
                                "id": 102,
                                "login": "test_user2",
                                "email": "test2@example.com",
                            },
                        ],
                        "failed_users": [],
                    },
                )

                # Test create_missing_users method
                result = migration.create_missing_users()

                # Verify clients were called correctly
                assert mock_op_instance.create_users_in_bulk.call_count == 1

                # Verify result
                assert result["created_count"] == 2

        # Clean up
        shutil.rmtree(data_dir)

    def test_build_fallback_email_basic(self) -> None:
        """Test basic fallback email generation."""
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)

            # Create migration instance
            migration = UserMigration()
            migration.data_dir = data_dir

            # Test basic email generation
            email = migration._build_fallback_email("testuser")
            assert email == "testuser@noreply.migration.local"

    def test_build_fallback_email_sanitization(self) -> None:
        """Test email sanitization for invalid characters."""
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)

            # Create migration instance
            migration = UserMigration()
            migration.data_dir = data_dir

            # Test with invalid characters
            email = migration._build_fallback_email("test@user#$%")
            assert email == "testuser@noreply.migration.local"

            # Test with spaces and special chars
            email = migration._build_fallback_email("test user!@#")
            assert email == "testuser@noreply.migration.local"

    def test_build_fallback_email_collision_handling(self) -> None:
        """Test email collision handling."""
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)

            # Create migration instance
            migration = UserMigration()
            migration.data_dir = data_dir

            existing_emails = {"testuser@noreply.migration.local"}

            # Should generate alternative email
            email = migration._build_fallback_email("testuser", existing_emails)
            assert email == "testuser.1@noreply.migration.local"

            # Test multiple collisions
            existing_emails.add("testuser.1@noreply.migration.local")
            email = migration._build_fallback_email("testuser", existing_emails)
            assert email == "testuser.2@noreply.migration.local"

    def test_build_fallback_email_empty_login(self) -> None:
        """Test fallback email generation for empty login."""
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)

            # Create migration instance
            migration = UserMigration()
            migration.data_dir = data_dir

            # Test with empty string (should use UUID)
            email = migration._build_fallback_email("")
            assert email.endswith("@noreply.migration.local")
            assert len(email.split("@")[0]) == 8  # UUID should be 8 chars

            # Test with only invalid characters
            email = migration._build_fallback_email("!@#$%")
            assert email.endswith("@noreply.migration.local")
            assert len(email.split("@")[0]) == 8  # UUID should be 8 chars

    @patch("src.migrations.user_migration.OpenProjectClient")
    @patch("src.migrations.user_migration.JiraClient")
    @patch("src.migrations.user_migration.config.get_path")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @pytest.mark.xfail(reason="Zero-created gating not yet implemented for UserMigration", strict=False)
    def test_user_migration_fails_when_zero_created_but_users_missing(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_get_path: MagicMock,
        mock_jira_client: MagicMock,
        mock_op_client: MagicMock,
    ) -> None:
        """Placeholder: expect failure when there are unmatched users but zero created."""
        mock_get_path.return_value = Path("/tmp/test_data")
        mock_exists.return_value = True

        migration = UserMigration(
            jira_client=mock_jira_client.return_value,
            op_client=mock_op_client.return_value,
        )
        # Pretend there are unmatched users
        migration.create_user_mapping = lambda: {  # type: ignore[method-assign]
            "missing": {
                "jira_key": "missing",
                "jira_name": "missing",
                "jira_email": "missing@example.com",
                "jira_display_name": "Missing User",
                "openproject_id": None,
                "openproject_login": None,
                "openproject_email": None,
                "matched_by": "none",
            }
        }
        mock_op_client.return_value.create_users_in_bulk.return_value = json.dumps(
            {"created_count": 0, "created": []}
        )
        result = migration.run()
        assert result.success is False


if __name__ == "__main__":
    unittest.main()
