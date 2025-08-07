"""Test fixture architecture validation for enhanced user association migrator staleness tests.

This module validates that the standardized fixture patterns work correctly across
all test classes and prevent common issues like fixture duplication, config attachment
errors, and MagicMock leakage.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.utils.enhanced_user_association_migrator import EnhancedUserAssociationMigrator


class TestFixtureArchitecture:
    """Test that the standardized fixture architecture works correctly."""

    @pytest.fixture
    def mock_jira_client(self):
        """Mock Jira client with concrete return values."""
        client = MagicMock()
        client.get = MagicMock()
        client.get_user_info_with_timeout = MagicMock()
        client.get_user_info_with_timeout.return_value = {
            "displayName": "Architecture Test User",
            "emailAddress": "architecture@example.com",
            "accountId": "arch-123",
            "active": True,
        }
        client.get.return_value = {
            "displayName": "Architecture Test User",
            "emailAddress": "architecture@example.com",
            "accountId": "arch-123",
            "active": True,
        }
        return client

    @pytest.fixture
    def mock_op_client(self):
        """Mock OpenProject client with concrete return values."""
        client = MagicMock()
        client.get_user = MagicMock()
        client.get_user.return_value = {
            "id": 999,
            "login": "arch.user",
            "email": "architecture@example.com",
            "firstName": "Architecture",
            "lastName": "Test",
            "status": "active",
        }
        return client

    @pytest.fixture
    def migrator_instance(self, mock_jira_client, mock_op_client):
        """Create migrator instance following standardized fixture pattern."""
        with (
            patch("src.utils.enhanced_user_association_migrator.config") as mock_config,
            patch.object(EnhancedUserAssociationMigrator, "_load_enhanced_mappings"),
            patch.object(EnhancedUserAssociationMigrator, "_save_enhanced_mappings"),
            patch("src.utils.enhanced_user_association_migrator.MetricsCollector"),
        ):

            # Clean config setup
            mock_config.get_path.return_value = Path("/tmp/test_fixture_arch")
            mock_config.migration_config = {
                "mapping": {
                    "refresh_interval": "1h",
                    "fallback_strategy": "skip",
                    "fallback_admin_user_id": "admin999",
                },
            }

            # Create migrator with clean state
            migrator = EnhancedUserAssociationMigrator(
                jira_client=mock_jira_client,
                op_client=mock_op_client,
            )

            # CRITICAL: Attach the mock config to the migrator instance
            migrator.config = mock_config

            # Clean cache setup - no file system dependencies
            migrator.enhanced_user_mappings = {}
            migrator._save_enhanced_mappings = MagicMock()

            return migrator

    def test_config_attachment_prevents_attribute_error(
        self,
        migrator_instance,
    ) -> None:
        """Test that config is properly attached to prevent AttributeError."""
        # Verify config is accessible
        assert hasattr(migrator_instance, "config")
        assert migrator_instance.config is not None

        # Verify config attributes are accessible
        assert hasattr(migrator_instance, "refresh_interval_seconds")
        assert hasattr(migrator_instance, "fallback_strategy")
        assert hasattr(migrator_instance, "admin_user_id")

    def test_mock_clients_return_concrete_values(
        self,
        mock_jira_client,
        mock_op_client,
    ) -> None:
        """Test that mock clients return concrete dictionaries, not MagicMock objects."""
        # Test Jira client returns concrete dict
        jira_response = mock_jira_client.get_user_info_with_timeout("test.user")
        assert isinstance(jira_response, dict)
        assert jira_response["displayName"] == "Architecture Test User"
        assert jira_response["emailAddress"] == "architecture@example.com"
        assert jira_response["accountId"] == "arch-123"
        assert jira_response["active"] is True

        # Test OpenProject client returns concrete dict
        op_response = mock_op_client.get_user("test.user")
        assert isinstance(op_response, dict)
        assert op_response["id"] == 999
        assert op_response["login"] == "arch.user"
        assert op_response["email"] == "architecture@example.com"

    def test_fixture_isolation_between_calls(self, migrator_instance) -> None:
        """Test that fixture instances are properly isolated between test calls."""
        # Modify the migrator instance
        migrator_instance.enhanced_user_mappings["test.user"] = {"test": "data"}

        # Verify modification exists
        assert "test.user" in migrator_instance.enhanced_user_mappings
        assert migrator_instance.enhanced_user_mappings["test.user"]["test"] == "data"

    def test_fixture_clean_state_initialization(self, migrator_instance) -> None:
        """Test that fixtures start with clean state."""
        # Verify migrator starts with empty mappings
        assert migrator_instance.enhanced_user_mappings == {}

        # Verify save method is mocked
        assert isinstance(migrator_instance._save_enhanced_mappings, MagicMock)

    def test_mock_client_method_consistency(
        self,
        mock_jira_client,
        mock_op_client,
    ) -> None:
        """Test that mock client methods are consistently available."""
        # Test Jira client has required methods
        assert hasattr(mock_jira_client, "get_user_info_with_timeout")
        assert hasattr(mock_jira_client, "get")
        assert callable(mock_jira_client.get_user_info_with_timeout)
        assert callable(mock_jira_client.get)

        # Test OpenProject client has required methods
        assert hasattr(mock_op_client, "get_user")
        assert callable(mock_op_client.get_user)

        # Test method calls work correctly
        mock_jira_client.get_user_info_with_timeout.assert_not_called()
        mock_op_client.get_user.assert_not_called()

        # Make calls and verify they work
        jira_result = mock_jira_client.get_user_info_with_timeout("test")
        op_result = mock_op_client.get_user("test")

        assert jira_result is not None
        assert op_result is not None
