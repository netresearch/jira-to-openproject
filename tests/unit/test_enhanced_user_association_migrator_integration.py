#!/usr/bin/env python3
"""Integration and security tests for Enhanced User Association Migrator staleness functionality."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests

from src.utils.enhanced_user_association_migrator import EnhancedUserAssociationMigrator


@pytest.fixture
def migrator_instance(mock_clients):
    """Fixture to create a fully initialized migrator instance with mocks."""
    jira_client, op_client = mock_clients
    with patch("src.utils.enhanced_user_association_migrator.config") as mock_config:
        # Configure the mock to return a valid config
        mock_config.migration_config = {
            "mapping": {
                "refresh_interval": "24h",
                "fallback_strategy": "skip",
            },
        }
        # Patch path lookups
        mock_config.get_path.return_value.exists.return_value = False

        # Use __new__ and manually call __init__ components to control setup
        migrator = EnhancedUserAssociationMigrator.__new__(
            EnhancedUserAssociationMigrator,
        )
        migrator.jira_client = jira_client
        migrator.op_client = op_client
        migrator.logger = MagicMock()
        migrator.user_mapping = {}
        migrator.enhanced_user_mappings = {}
        migrator.fallback_users = {}
        migrator._rails_operations_cache = []

        # Call the specific method under test
        migrator._load_staleness_config()

        return migrator


class TestSecurityURLEncoding:
    """Security tests for URL encoding in user info fetching."""

    @pytest.mark.parametrize(
        ("username", "expected_encoded"),
        [
            ("user name", "user%20name"),
            ("user?name", "user%3Fname"),
            ("user&name", "user%26name"),
            ("user/name", "user%2Fname"),
            ("user+name", "user%2Bname"),
            ("user%name", "user%25name"),
            ("test@example.com", "test%40example.com"),
            ("jira_user", "jira_user"),
            ("!#$()*,;=", "%21%23%24%28%29%2A%2C%3B%3D"),
            ("你好世界", "%E4%BD%A0%E5%A5%BD%E4%B8%96%E7%95%8C"),  # Unicode
            ("👾", "%F0%9F%91%BE"),  # Emoji
            ("../../../etc/passwd", "..%2F..%2F..%2Fetc%2Fpasswd"),  # Path traversal
            (
                "'; DROP TABLE users; --",
                "%27%3B%20DROP%20TABLE%20users%3B%20--",
            ),  # SQL injection
            (
                "<script>alert('xss')</script>",
                "%3Cscript%3Ealert%28%27xss%27%29%3C%2Fscript%3E",
            ),  # XSS
        ],
    )
    def test_get_jira_user_info_url_encodes_special_characters(
        self,
        migrator_instance,
        username,
        expected_encoded,
    ) -> None:
        """Verify that _get_jira_user_info correctly URL-encodes usernames to prevent injection."""
        # Arrange
        migrator_instance.jira_client.get.return_value = MagicMock(status_code=404)

        # Act
        migrator_instance._get_jira_user_info(username)

        # Assert
        expected_url = f"user/search?username={expected_encoded}"
        migrator_instance.jira_client.get.assert_called_once_with(expected_url)

    @pytest.mark.parametrize(
        "malicious_username",
        [
            "",  # Empty string
            " ",  # Whitespace only
            "\x00\x01\x02",  # Control characters
            "a" * 1000,  # Very long username
            "\n\r\t",  # Newlines and tabs
            "user\0admin",  # Null byte injection
        ],
    )
    def test_get_jira_user_info_handles_edge_case_usernames(
        self,
        migrator_instance,
        malicious_username,
    ) -> None:
        """Verify that edge case usernames are handled without errors."""
        # Arrange
        migrator_instance.jira_client.get.return_value = MagicMock(status_code=404)

        # Act & Assert - Should not raise an exception
        result = migrator_instance._get_jira_user_info(malicious_username)
        assert result is None
        migrator_instance.jira_client.get.assert_called_once()


class TestAutoRefreshWorkflow:
    """Tests for auto-refresh workflow integration."""

    def test_get_jira_user_info_calls_refresh_for_stale_mapping(
        self,
        migrator_instance,
    ) -> None:
        """Verify that a stale mapping triggers the refresh workflow upon successful API fetch."""
        # Arrange
        username = "stale_user"
        mock_user_response = MagicMock()
        mock_user_response.status_code = 200
        mock_user_response.json.return_value = [
            {"accountId": "123", "displayName": "Stale User"},
        ]
        migrator_instance.jira_client.get.return_value = mock_user_response

        with (
            patch.object(
                migrator_instance,
                "is_mapping_stale",
                return_value=True,
            ) as mock_is_stale,
            patch.object(migrator_instance, "refresh_user_mapping") as mock_refresh,
        ):
            # Act
            user_info = migrator_instance._get_jira_user_info(username)

            # Assert
            assert user_info is not None
            mock_is_stale.assert_called_once_with(username)
            # The refresh is triggered *after* the successful fetch
            mock_refresh.assert_called_once_with(username)

    def test_get_jira_user_info_does_not_call_refresh_for_fresh_mapping(
        self,
        migrator_instance,
    ) -> None:
        """Verify that a fresh mapping does NOT trigger the refresh workflow."""
        # Arrange
        username = "fresh_user"
        mock_user_response = MagicMock()
        mock_user_response.status_code = 200
        mock_user_response.json.return_value = [
            {"accountId": "456", "displayName": "Fresh User"},
        ]
        migrator_instance.jira_client.get.return_value = mock_user_response

        with (
            patch.object(
                migrator_instance,
                "is_mapping_stale",
                return_value=False,
            ) as mock_is_stale,
            patch.object(migrator_instance, "refresh_user_mapping") as mock_refresh,
        ):
            # Act
            migrator_instance._get_jira_user_info(username)

            # Assert
            mock_is_stale.assert_called_once_with(username)
            mock_refresh.assert_not_called()

    def test_get_jira_user_info_caches_staleness_check_result(
        self,
        migrator_instance,
    ) -> None:
        """Verify that is_mapping_stale is only called once per invocation for performance."""
        # Arrange
        username = "test_user"
        migrator_instance.jira_client.get.return_value = MagicMock(status_code=404)

        # The is_mapping_stale method is a real method, so we wrap it to count calls
        migrator_instance.is_mapping_stale = MagicMock(return_value=True)

        # Act
        migrator_instance._get_jira_user_info(username)

        # Assert
        migrator_instance.is_mapping_stale.assert_called_once()


class TestErrorHandlingAndResilience:
    """Tests for error handling and resilience."""

    @pytest.mark.parametrize(
        "exception",
        [
            requests.exceptions.RequestException("Network Error"),
            requests.exceptions.Timeout("Request Timed Out"),
            requests.exceptions.ConnectionError("Connection Failed"),
            requests.exceptions.HTTPError("HTTP Error"),
        ],
    )
    def test_get_jira_user_info_handles_request_exception(
        self,
        migrator_instance,
        exception,
    ) -> None:
        """Verify that network-related exceptions are caught and handled gracefully."""
        # Arrange
        username = "error_user"
        migrator_instance.jira_client.get.side_effect = exception

        # Act
        result = migrator_instance._get_jira_user_info(username)

        # Assert
        assert result is None
        migrator_instance.logger.error.assert_called_once()
        assert "Failed to fetch Jira user info" in migrator_instance.logger.error.call_args[0][0]

    def test_get_jira_user_info_handles_malformed_json_response(
        self,
        migrator_instance,
    ) -> None:
        """Verify that a non-JSON response from the API is handled gracefully."""
        # Arrange
        username = "json_error_user"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("Decoding JSON has failed")
        migrator_instance.jira_client.get.return_value = mock_response

        # Act
        result = migrator_instance._get_jira_user_info(username)

        # Assert
        assert result is None
        migrator_instance.logger.error.assert_called_once()
        assert "Failed to fetch Jira user info" in migrator_instance.logger.error.call_args[0][0]

    def test_get_jira_user_info_handles_empty_user_list_response(
        self,
        migrator_instance,
    ) -> None:
        """Verify that an empty list in the JSON response is handled correctly."""
        # Arrange
        username = "not_found_user"
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []  # Empty list, user not found
        migrator_instance.jira_client.get.return_value = mock_response

        # Act
        result = migrator_instance._get_jira_user_info(username)

        # Assert
        assert result is None
        migrator_instance.logger.error.assert_not_called()  # This is not an error condition

    @pytest.mark.parametrize("status_code", [400, 401, 403, 404, 500, 502, 503])
    def test_get_jira_user_info_handles_http_error_codes(
        self,
        migrator_instance,
        status_code,
    ) -> None:
        """Verify that various HTTP error codes are handled correctly."""
        # Arrange
        username = "error_user"
        mock_response = MagicMock()
        mock_response.status_code = status_code
        migrator_instance.jira_client.get.return_value = mock_response

        # Act
        result = migrator_instance._get_jira_user_info(username)

        # Assert
        assert result is None


class TestRefreshUserMapping:
    """Tests for refresh_user_mapping functionality."""

    def test_refresh_user_mapping_updates_existing_entry(
        self,
        migrator_instance,
    ) -> None:
        """Verify that refresh_user_mapping correctly updates an existing user entry."""
        # Arrange
        username = "outdated_user"
        old_timestamp = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        new_timestamp = datetime.now(UTC).isoformat()

        migrator_instance.enhanced_user_mappings[username] = {
            "jira_username": username,
            "jira_display_name": "Old Name",
            "lastRefreshed": old_timestamp,
            "metadata": {"jira_active": False},
        }

        fresh_jira_data = {
            "accountId": "xyz-789",
            "displayName": "New Name",
            "active": True,
        }

        with (
            patch.object(
                migrator_instance,
                "_get_jira_user_info",
                return_value=fresh_jira_data,
            ),
            patch.object(
                migrator_instance,
                "_get_current_timestamp",
                return_value=new_timestamp,
            ),
        ):
            # Act
            success = migrator_instance.refresh_user_mapping(username)

            # Assert
            assert success is True
            updated_mapping = migrator_instance.enhanced_user_mappings[username]
            assert updated_mapping["jira_display_name"] == "New Name"
            assert updated_mapping["metadata"]["jira_active"] is True
            assert updated_mapping["lastRefreshed"] == new_timestamp

    def test_refresh_user_mapping_creates_new_entry_when_none_exists(
        self,
        migrator_instance,
    ) -> None:
        """Verify that refresh_user_mapping creates a new entry when none exists."""
        # Arrange
        username = "new_user"
        timestamp = datetime.now(UTC).isoformat()
        fresh_jira_data = {
            "accountId": "new-123",
            "displayName": "New User",
            "active": True,
        }

        with (
            patch.object(
                migrator_instance,
                "_get_jira_user_info",
                return_value=fresh_jira_data,
            ),
            patch.object(
                migrator_instance,
                "_get_current_timestamp",
                return_value=timestamp,
            ),
            patch.object(
                migrator_instance,
                "_get_openproject_user_info",
                return_value=None,
            ),
        ):
            # Act
            success = migrator_instance.refresh_user_mapping(username)

            # Assert
            assert success is True
            assert username in migrator_instance.enhanced_user_mappings
            new_mapping = migrator_instance.enhanced_user_mappings[username]
            assert new_mapping["jira_display_name"] == "New User"
            assert new_mapping["lastRefreshed"] == timestamp

    def test_refresh_user_mapping_fails_gracefully_on_api_error(
        self,
        migrator_instance,
    ) -> None:
        """Verify that refresh_user_mapping returns False if the Jira API fails."""
        # Arrange
        username = "api_fail_user"
        with patch.object(migrator_instance, "_get_jira_user_info", return_value=None):
            # Act
            success = migrator_instance.refresh_user_mapping(username)

            # Assert
            assert success is False
            migrator_instance.logger.warning.assert_called_once_with(
                "Could not fetch fresh user info for %s",
                username,
            )

    @pytest.mark.parametrize(
        "exception",
        [
            requests.exceptions.RequestException("Network Error"),
            ValueError("Invalid data format"),
            KeyError("Missing key"),
        ],
    )
    def test_refresh_user_mapping_handles_specific_exceptions(
        self,
        migrator_instance,
        exception,
    ) -> None:
        """Verify that refresh_user_mapping handles specific exception types correctly."""
        # Arrange
        username = "exception_user"
        with patch.object(
            migrator_instance,
            "_get_jira_user_info",
            side_effect=exception,
        ):
            # Act
            success = migrator_instance.refresh_user_mapping(username)

            # Assert
            assert success is False
            migrator_instance.logger.error.assert_called_once()

    def test_refresh_user_mapping_handles_unexpected_exceptions(
        self,
        migrator_instance,
    ) -> None:
        """Verify that refresh_user_mapping handles unexpected exceptions gracefully."""
        # Arrange
        username = "unexpected_error_user"
        unexpected_error = RuntimeError("Unexpected error")

        with patch.object(
            migrator_instance,
            "_get_jira_user_info",
            side_effect=unexpected_error,
        ):
            # Act
            success = migrator_instance.refresh_user_mapping(username)

            # Assert
            assert success is False
            # Should log the unexpected error
            assert len(migrator_instance.logger.error.call_args_list) == 1
            error_message = migrator_instance.logger.error.call_args_list[0][0][0]
            assert "Unexpected error" in error_message


class TestConfigurationValidation:
    """Tests for configuration loading and validation."""

    def test_load_staleness_config_with_valid_configuration(
        self,
        migrator_instance,
    ) -> None:
        """Verify that valid staleness configuration is loaded correctly."""
        # Act & Assert - Already configured in fixture
        assert migrator_instance.refresh_interval_seconds == 86400  # 24h in seconds
        assert migrator_instance.fallback_strategy == "skip"

    def test_load_staleness_config_with_invalid_duration_falls_back_to_defaults(
        self,
        mock_clients,
    ) -> None:
        """Verify that invalid duration format falls back to defaults."""
        # Arrange
        jira_client, op_client = mock_clients
        with patch(
            "src.utils.enhanced_user_association_migrator.config",
        ) as mock_config:
            mock_config.migration_config = {
                "mapping": {
                    "refresh_interval": "invalid_duration",
                    "fallback_strategy": "skip",
                },
            }
            mock_config.get_path.return_value.exists.return_value = False

            migrator = EnhancedUserAssociationMigrator.__new__(
                EnhancedUserAssociationMigrator,
            )
            migrator.jira_client = jira_client
            migrator.logger = MagicMock()

            # Act
            migrator._load_staleness_config()

            # Assert
            assert migrator.refresh_interval_seconds == 86400  # Default 24h
            assert migrator.fallback_strategy == "skip"
            migrator.logger.warning.assert_called_once()

    def test_load_staleness_config_with_missing_admin_user_id_warns(
        self,
        mock_clients,
    ) -> None:
        """Verify that missing admin user ID for assign_admin strategy logs a warning."""
        # Arrange
        jira_client, op_client = mock_clients
        with patch(
            "src.utils.enhanced_user_association_migrator.config",
        ) as mock_config:
            mock_config.migration_config = {
                "mapping": {
                    "refresh_interval": "12h",
                    "fallback_strategy": "assign_admin",
                    # Missing fallback_admin_user_id
                },
            }
            mock_config.get_path.return_value.exists.return_value = False

            migrator = EnhancedUserAssociationMigrator.__new__(
                EnhancedUserAssociationMigrator,
            )
            migrator.jira_client = jira_client
            migrator.logger = MagicMock()

            # Act
            migrator._load_staleness_config()

            # Assert
            assert migrator.fallback_strategy == "assign_admin"
            assert migrator.admin_user_id is None
            # Should log warning about missing admin user ID
            warning_calls = [call for call in migrator.logger.warning.call_args_list if "assign_admin" in str(call)]
            assert len(warning_calls) > 0


class TestEdgeCasesAndBoundaries:
    """Tests for edge cases and boundary conditions."""

    def test_staleness_threshold_boundary_conditions(self, migrator_instance) -> None:
        """Test staleness detection at exact threshold boundaries."""
        # Arrange
        username = "boundary_user"
        now = datetime.now(UTC)

        # Test case 1: Exactly at threshold (should be stale with >= comparison)
        exactly_stale_time = (now - timedelta(seconds=86400)).isoformat()
        migrator_instance.enhanced_user_mappings[username] = {
            "lastRefreshed": exactly_stale_time,
        }

        # Act & Assert
        assert migrator_instance.is_mapping_stale(username) is True

        # Test case 2: Just under threshold (should be fresh)
        just_fresh_time = (now - timedelta(seconds=86399)).isoformat()
        migrator_instance.enhanced_user_mappings[username]["lastRefreshed"] = just_fresh_time

        # Act & Assert
        assert migrator_instance.is_mapping_stale(username) is False

    def test_malformed_timestamp_handling(self, migrator_instance) -> None:
        """Test handling of malformed lastRefreshed timestamps."""
        # Arrange
        username = "malformed_user"
        migrator_instance.enhanced_user_mappings[username] = {
            "lastRefreshed": "not-a-timestamp",
        }

        # Act & Assert
        assert migrator_instance.is_mapping_stale(username) is True
        migrator_instance.logger.warning.assert_called_once()

    def test_missing_lastRefreshed_field(self, migrator_instance) -> None:
        """Test handling of missing lastRefreshed field."""
        # Arrange
        username = "missing_field_user"
        migrator_instance.enhanced_user_mappings[username] = {
            "jira_username": username,
            # Missing lastRefreshed field
        }

        # Act & Assert
        assert migrator_instance.is_mapping_stale(username) is True
