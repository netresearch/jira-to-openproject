import pytest
from unittest.mock import Mock

from src.clients.openproject_client import OpenProjectClient
from src.migrations.user_migration import UserMigration


@pytest.fixture
def mock_ssh_client():
    return Mock()


@pytest.fixture
def mock_docker_client():
    return Mock()


@pytest.fixture
def mock_rails_client():
    return Mock()


@pytest.fixture
def op_client(mock_ssh_client, mock_docker_client, mock_rails_client):
    client = OpenProjectClient(
        container_name="test-container",
        ssh_host="test-host",
        ssh_user="test-user",
        ssh_client=mock_ssh_client,
        docker_client=mock_docker_client,
        rails_client=mock_rails_client,
    )
    return client


@pytest.fixture
def jira_client():
    return Mock()


@pytest.fixture
def user_migration(jira_client, op_client):
    # Create a user migration instance with mocked clients
    migration = UserMigration(
        jira_client=jira_client,
        op_client=op_client,
    )
    return migration


def test_create_users_in_bulk(op_client, mock_rails_client):
    """Test creating multiple users in bulk."""
    # Mock the _transfer_and_execute_script method
    op_client._transfer_and_execute_script = Mock()

    # Setup mock response for the script execution
    mock_response = {
        "created_count": 3,
        "failed_count": 0,
        "created_users": [
            {"id": 1, "login": "user1", "firstname": "User", "lastname": "One", "email": "user1@example.com"},
            {"id": 2, "login": "user2", "firstname": "User", "lastname": "Two", "email": "user2@example.com"},
            {"id": 3, "login": "user3", "firstname": "User", "lastname": "Three", "email": "user3@example.com"}
        ],
        "failed_users": []
    }
    op_client._transfer_and_execute_script.return_value = mock_response

    # Test users
    users_data = [
        {"login": "user1", "firstname": "User", "lastname": "One", "email": "user1@example.com"},
        {"login": "user2", "firstname": "User", "lastname": "Two", "email": "user2@example.com"},
        {"login": "user3", "firstname": "User", "lastname": "Three", "email": "user3@example.com"}
    ]

    # Call the method
    result = op_client.create_users_in_bulk(users_data)

    # Verify the method was called with a script containing the user data
    op_client._transfer_and_execute_script.assert_called_once()

    # Check result
    assert result["created_count"] == 3
    assert len(result["created_users"]) == 3
    assert result["failed_count"] == 0


def test_user_migration_create_missing_users(user_migration, op_client, jira_client):
    """Test creating missing users."""
    # Mock get_users to return empty list (no existing users)
    op_client.get_users = Mock(return_value=[])

    # Mock jira_client to return test users
    jira_users = [
        {
            "key": "user1",
            "name": "user1",
            "emailAddress": "user1@example.com",
            "displayName": "User One"
        },
        {
            "key": "user2",
            "name": "user2",
            "emailAddress": "user2@example.com",
            "displayName": "User Two"
        }
    ]
    jira_client.get_users = Mock(return_value=jira_users)

    # Mock existing user_mapping
    user_migration.user_mapping = {}

    # Mock create_users_in_bulk to succeed
    op_client.create_users_in_bulk = Mock(return_value={
        "created_count": 2,
        "failed_count": 0,
        "created_users": [
            {"id": 1, "login": "user1", "email": "user1@example.com"},
            {"id": 2, "login": "user2", "email": "user2@example.com"}
        ],
        "failed_users": []
    })

    # Call the method
    result = user_migration.create_missing_users(batch_size=2)

    # Verify that the client methods were called
    jira_client.get_users.assert_called_once()
    op_client.get_users.assert_called_once()
    op_client.create_users_in_bulk.assert_called_once()

    # Check that we get the expected result stats
    assert "created_count" in result
    assert result["created_count"] == 2


def test_user_migration_create_missing_users_no_unmatched(user_migration, op_client, jira_client):
    """Test no users to create when all are matched."""
    # Mock the jira client
    jira_users = [
        {
            "key": "user1",
            "name": "user1",
            "emailAddress": "user1@example.com",
            "displayName": "User One"
        },
        {
            "key": "user2",
            "name": "user2",
            "emailAddress": "user2@example.com",
            "displayName": "User Two"
        }
    ]
    jira_client.get_users = Mock(return_value=jira_users)

    # Mock the op client to return users with matching emails
    op_users = [
        {
            "id": 101,
            "login": "op_user1",
            "email": "user1@example.com",
            "firstName": "OP User",
            "lastName": "One"
        },
        {
            "id": 102,
            "login": "op_user2",
            "email": "user2@example.com",
            "firstName": "OP User",
            "lastName": "Two"
        }
    ]
    op_client.get_users = Mock(return_value=op_users)

    # Create a separate Mock for create_users_in_bulk that can be verified
    create_mock = Mock()
    op_client.create_users_in_bulk = create_mock

    # Call the method
    result = user_migration.create_missing_users()

    # Verify the create_users_in_bulk was not called because no users need to be created
    create_mock.assert_not_called()

    # Verify the result
    assert result.get("created_count", 0) == 0


def test_user_migration_create_missing_users_with_existing_email(user_migration, op_client, jira_client):
    """Test handling mixed case where some users exist but with different emails."""
    # Mock jira_client to return test users
    jira_users = [
        {
            "key": "user1",
            "name": "user1",
            "emailAddress": "user1@example.com",
            "displayName": "User One"
        },
        {
            "key": "user2",
            "name": "user2",
            "emailAddress": "user2@example.com",
            "displayName": "User Two"
        },
        {
            "key": "user3",
            "name": "user3",
            "emailAddress": "user3@example.com",
            "displayName": "User Three"
        }
    ]
    jira_client.get_users = Mock(return_value=jira_users)

    # Mock op_client to return some existing users
    op_users = [
        {
            "id": 101,
            "login": "op_user1",
            "email": "user1@example.com",
            "firstName": "OP User",
            "lastName": "One"
        }
    ]
    op_client.get_users = Mock(return_value=op_users)

    # Mock existing user_mapping
    user_migration.user_mapping = {}

    # Mock create_users_in_bulk to succeed with 2 users
    op_client.create_users_in_bulk = Mock(return_value={
        "created_count": 2,
        "total_users": 3,
        "created_users": [
            {"id": 102, "login": "user2", "email": "user2@example.com"},
            {"id": 103, "login": "user3", "email": "user3@example.com"}
        ]
    })

    # Call the method
    result = user_migration.create_missing_users()

    # Verify that the user mapping is updated correctly
    assert "created_count" in result
    assert result["created_count"] == 2

    # Verify only the non-matched users were created
    op_client.create_users_in_bulk.assert_called_once()
    users_to_create = op_client.create_users_in_bulk.call_args[0][0]
    assert len(users_to_create) == 2
    assert users_to_create[0]["login"] == "user2" or users_to_create[0]["login"] == "user3"
    assert users_to_create[1]["login"] == "user3" or users_to_create[1]["login"] == "user2"


def test_bulk_creation_error_handling(op_client):
    """Test handling of errors when creating users in bulk."""
    # Mock the script execution to fail
    op_client._transfer_and_execute_script = Mock(side_effect=Exception("Failed to execute script"))

    # Test users
    users = [
        {"login": "user1", "firstname": "User", "lastname": "One", "email": "user1@example.com"}
    ]

    # Call method with error expected
    with pytest.raises(Exception) as e:
        op_client.create_users_in_bulk(users)

    # Verify the error message
    assert "Failed to execute script" in str(e.value)
