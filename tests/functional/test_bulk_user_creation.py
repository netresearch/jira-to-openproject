from unittest.mock import MagicMock, Mock

import pytest

from src.clients.openproject_client import OpenProjectClient
from src.migrations.user_migration import UserMigration


@pytest.fixture
def mock_ssh_client() -> MagicMock:
    return Mock()


@pytest.fixture
def mock_docker_client() -> MagicMock:
    return Mock()


@pytest.fixture
def mock_rails_client() -> MagicMock:
    return Mock()


@pytest.fixture
def op_client(
    mock_ssh_client: MagicMock,
    mock_docker_client: MagicMock,
    mock_rails_client: MagicMock,
) -> OpenProjectClient:
    return OpenProjectClient(
        container_name="test-container",
        ssh_host="test-host",
        ssh_user="test-user",
        ssh_client=mock_ssh_client,
        docker_client=mock_docker_client,
        rails_client=mock_rails_client,
    )


@pytest.fixture
def jira_client() -> MagicMock:
    return Mock()


@pytest.fixture
def user_migration(
    jira_client: MagicMock, op_client: OpenProjectClient
) -> UserMigration:
    # Create a user migration instance with mocked clients
    return UserMigration(
        jira_client=jira_client,
        op_client=op_client,
    )


def test_create_users_in_bulk(
    op_client: OpenProjectClient, mock_rails_client: MagicMock
) -> None:
    """Test creating multiple users in bulk."""
    # Mock the rails_client._send_command_to_tmux method (used by execute_query)
    mock_script_output = """
    {
      "created_count": 3,
      "failed_count": 0,
      "created_users": [
        {"id": 1, "login": "user1", "firstname": "User", "lastname": "One", "email": "user1@example.com"},
        {"id": 2, "login": "user2", "firstname": "User", "lastname": "Two", "email": "user2@example.com"},
        {"id": 3, "login": "user3", "firstname": "User", "lastname": "Three", "email": "user3@example.com"}
      ],
      "failed_users": []
    }
    """

    # Set up the mock to return our JSON string
    mock_rails_client._send_command_to_tmux.return_value = mock_script_output

    # Test users
    users_data = [
        {
            "login": "user1",
            "firstname": "User",
            "lastname": "One",
            "email": "user1@example.com",
        },
        {
            "login": "user2",
            "firstname": "User",
            "lastname": "Two",
            "email": "user2@example.com",
        },
        {
            "login": "user3",
            "firstname": "User",
            "lastname": "Three",
            "email": "user3@example.com",
        },
    ]

    # Call the method
    result = op_client.create_users_in_bulk(users_data)

    # Verify rails_client._send_command_to_tmux was called
    mock_rails_client._send_command_to_tmux.assert_called_once()

    # Check result (should be string output now)
    assert "created_count" in result
    assert "3" in result  # For the count
    assert "user1@example.com" in result
    assert "user2@example.com" in result
    assert "user3@example.com" in result


def test_user_migration_create_missing_users(
    monkeypatch: pytest.MonkeyPatch,
    user_migration: UserMigration,
    op_client: OpenProjectClient,
    jira_client: MagicMock,
) -> None:
    """Test creating missing users."""
    # Mock get_users to return empty list (no existing users)
    mock_get_users = MagicMock(return_value=[])
    monkeypatch.setattr(op_client, "get_users", mock_get_users)

    # Mock jira_client to return test users
    jira_users = [
        {
            "key": "user1",
            "name": "user1",
            "emailAddress": "user1@example.com",
            "displayName": "User One",
        },
        {
            "key": "user2",
            "name": "user2",
            "emailAddress": "user2@example.com",
            "displayName": "User Two",
        },
    ]
    monkeypatch.setattr(jira_client, "get_users", Mock(return_value=jira_users))

    # Mock create_user_mapping to return unmatched users and avoid any file operations
    user_mapping = {
        "user1": {
            "jira_key": "user1",
            "jira_name": "user1",
            "jira_email": "user1@example.com",
            "jira_display_name": "User One",
            "openproject_id": None,
            "openproject_login": None,
            "openproject_email": None,
            "matched_by": "none",
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
    mock_create_user_mapping = MagicMock(return_value=user_mapping)
    monkeypatch.setattr(user_migration, "create_user_mapping", mock_create_user_mapping)
    user_migration.user_mapping = user_mapping

    # Mock extract methods to avoid file operations
    monkeypatch.setattr(
        user_migration, "extract_jira_users", Mock(return_value=jira_users)
    )
    monkeypatch.setattr(
        user_migration, "extract_openproject_users", Mock(return_value=[])
    )
    monkeypatch.setattr(user_migration, "_save_to_json", Mock())

    # Mock create_users_in_bulk to return a string response instead of dict
    mock_create_users_in_bulk = Mock(
        return_value="""
        {
            "created_count": 2,
            "failed_count": 0,
            "created_users": [
                {"id": 1, "login": "user1", "email": "user1@example.com"},
                {"id": 2, "login": "user2", "email": "user2@example.com"}
            ],
            "failed_users": []
        }
        """,
    )
    monkeypatch.setattr(
        op_client,
        "create_users_in_bulk",
        mock_create_users_in_bulk,
    )

    # Call the method
    result = user_migration.create_missing_users(batch_size=2)

    # Verify that only one method was called (and not called again during the operation)
    assert mock_create_user_mapping.call_count == 1
    assert mock_create_users_in_bulk.call_count == 1

    # Check that we get the expected result stats
    assert "created_count" in result
    assert result["created_count"] == 2


def test_user_migration_create_missing_users_no_unmatched(
    monkeypatch: pytest.MonkeyPatch,
    user_migration: UserMigration,
    op_client: OpenProjectClient,
    jira_client: MagicMock,
) -> None:
    """Test handling empty user list (no unmatched users)."""
    # Create test data with all users matched
    user_mapping = {
        "user1": {"matched_by": "name"},
        "user2": {"matched_by": "email"},
    }

    # Mock jira_client.get_users to return a valid list for completion
    jira_users = [
        {
            "key": "user1",
            "name": "user1",
            "emailAddress": "user1@example.com",
            "displayName": "User One",
        },
        {
            "key": "user2",
            "name": "user2",
            "emailAddress": "user2@example.com",
            "displayName": "User Two",
        },
    ]
    monkeypatch.setattr(jira_client, "get_users", MagicMock(return_value=jira_users))

    # Mock create_user_mapping to return matched users
    monkeypatch.setattr(
        user_migration, "create_user_mapping", MagicMock(return_value=user_mapping)
    )

    # Set user_mapping directly
    user_migration.user_mapping = user_mapping

    # Replace op_client.create_users_in_bulk with a MagicMock
    mock_create_users = MagicMock()
    monkeypatch.setattr(op_client, "create_users_in_bulk", mock_create_users)

    # UserMigration doesn't have sync_ldap_users, and we don't need to mock it
    # Just mock _save_to_json to avoid IO problems
    monkeypatch.setattr(user_migration, "_save_to_json", MagicMock(return_value=True))

    # Call the method
    result = user_migration.create_missing_users()

    # Verify behavior
    # Should have an empty result due to no unmatched users
    assert isinstance(result, dict)

    # Verify create_users_in_bulk was not called due to no unmatched users
    mock_create_users.assert_not_called()


def test_user_migration_create_missing_users_with_existing_email(
    monkeypatch: pytest.MonkeyPatch,
    user_migration: UserMigration,
    op_client: OpenProjectClient,
    jira_client: MagicMock,
) -> None:
    """Test handling mixed case where some users exist but with different emails."""
    # Mock jira_client to return test users
    jira_users = [
        {
            "key": "user1",
            "name": "user1",
            "emailAddress": "user1@example.com",
            "displayName": "User One",
        },
        {
            "key": "user2",
            "name": "user2",
            "emailAddress": "user2@example.com",
            "displayName": "User Two",
        },
        {
            "key": "user3",
            "name": "user3",
            "emailAddress": "user3@example.com",
            "displayName": "User Three",
        },
    ]
    jira_client.get_users = Mock(return_value=jira_users)

    # Mock op_client to return some existing users
    op_users = [
        {
            "id": 101,
            "login": "op_user1",
            "email": "user1@example.com",
            "firstName": "OP User",
            "lastName": "One",
        },
    ]
    mock_get_users = MagicMock(return_value=op_users)
    monkeypatch.setattr(op_client, "get_users", mock_get_users)

    # Mock create_user_mapping to return one matched user and two unmatched
    monkeypatch.setattr(
        user_migration,
        "create_user_mapping",
        Mock(
            return_value={
                "user1": {
                    "jira_key": "user1",
                    "jira_name": "user1",
                    "jira_email": "user1@example.com",
                    "jira_display_name": "User One",
                    "openproject_id": 101,
                    "openproject_login": "op_user1",
                    "openproject_email": "user1@example.com",
                    "matched_by": "email",
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
                "user3": {
                    "jira_key": "user3",
                    "jira_name": "user3",
                    "jira_email": "user3@example.com",
                    "jira_display_name": "User Three",
                    "openproject_id": None,
                    "openproject_login": None,
                    "openproject_email": None,
                    "matched_by": "none",
                },
            },
        ),
    )
    user_migration.user_mapping = user_migration.create_user_mapping()

    # Mock create_users_in_bulk to return a string response instead of dict
    mock_create_users_in_bulk = Mock(
        return_value="""
        {
            "created_count": 2,
            "created_users": [
                {"id": 102, "login": "user2", "email": "user2@example.com"},
                {"id": 103, "login": "user3", "email": "user3@example.com"}
            ],
            "failed_users": []
        }
        """,
    )
    monkeypatch.setattr(
        op_client,
        "create_users_in_bulk",
        mock_create_users_in_bulk,
    )

    # Call the method
    result = user_migration.create_missing_users()

    # Verify that the user mapping is updated correctly
    assert "created_count" in result
    assert result["created_count"] == 2

    # Verify only the non-matched users were created
    assert mock_create_users_in_bulk.call_count == 1
    users_to_create = mock_create_users_in_bulk.call_args[0][0]
    assert len(users_to_create) == 2
    assert (
        users_to_create[0]["login"] == "user2" or users_to_create[0]["login"] == "user3"
    )
    assert (
        users_to_create[1]["login"] == "user3" or users_to_create[1]["login"] == "user2"
    )


def test_bulk_creation_error_handling(
    monkeypatch: pytest.MonkeyPatch, op_client: OpenProjectClient
) -> None:
    """Test handling of errors when creating users in bulk."""
    # Mock rails_client._send_command_to_tmux to fail
    mock_send_command_to_tmux = MagicMock(
        side_effect=Exception("Failed to create users in bulk.")
    )
    monkeypatch.setattr(
        op_client.rails_client,
        "_send_command_to_tmux",
        mock_send_command_to_tmux,
    )

    # Test users
    users = [
        {
            "login": "user1",
            "firstname": "User",
            "lastname": "One",
            "email": "user1@example.com",
        }
    ]

    # Call method with error expected
    with pytest.raises(Exception) as e:
        op_client.create_users_in_bulk(users)

    # Verify the error message
    assert "Failed to create users in bulk." in str(e.value)
