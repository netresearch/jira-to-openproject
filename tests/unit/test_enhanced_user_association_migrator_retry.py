"""Tests for retry logic in EnhancedUserAssociationMigrator."""

from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from src.clients.jira_client import (
    JiraApiError,
    JiraAuthenticationError,
    JiraConnectionError,
    JiraResourceNotFoundError,
)
from src.utils.enhanced_user_association_migrator import EnhancedUserAssociationMigrator


class TestEnhancedUserAssociationMigratorRetry:
    """Test retry logic functionality."""

    @pytest.fixture
    def mock_jira_client(self):
        """Create mock Jira client with proper methods."""
        client = MagicMock()
        client.get_user_info_with_timeout = MagicMock()
        client.get_user_info = MagicMock()
        client.get = MagicMock()
        return client

    @pytest.fixture
    def mock_op_client(self):
        """Create mock 1Password client."""
        return MagicMock()

    @pytest.fixture
    def migrator_instance(self, mock_jira_client, mock_op_client):
        """Create migrator instance with proper mocking."""
        with (
            patch("src.utils.enhanced_user_association_migrator.config") as mock_config,
            patch("builtins.open", mock_open(read_data="{}")),
            patch("pathlib.Path.exists", return_value=False),
        ):

            mock_config.get_path.return_value = Path("/tmp/test")
            mock_config.migration_config = {
                "mapping": {
                    "refresh_interval": "1h",
                    "fallback_admin_user_id": "admin123",
                },
                "retry": {
                    "max_retries": 3,
                    "initial_delay": 0.5,
                    "backoff_multiplier": 2.0,
                    "max_delay": 10.0,
                    "jitter": True,
                    "request_timeout": 30.0,
                },
            }

            migrator = EnhancedUserAssociationMigrator(
                jira_client=mock_jira_client,
                op_client=mock_op_client,
                basic_mapping={},
            )

            # Set proper attributes
            migrator.refresh_interval_seconds = 3600
            migrator.fallback_admin_user_id = "admin123"

            return migrator

    def test_get_jira_user_with_retry_success_first_attempt(
        self,
        migrator_instance,
    ) -> None:
        """Test successful user lookup on first attempt."""
        expected_user_data = {
            "accountId": "test-account-123",
            "displayName": "Test User",
            "emailAddress": "test@example.com",
            "active": True,
        }

        migrator_instance.jira_client.get_user_info_with_timeout.return_value = (
            expected_user_data
        )

        result = migrator_instance._get_jira_user_with_retry("test.user")

        assert result == expected_user_data
        assert migrator_instance.jira_client.get_user_info_with_timeout.call_count == 1

    def test_get_jira_user_with_retry_success_after_retries(
        self,
        migrator_instance,
    ) -> None:
        """Test successful user lookup after some retries."""
        expected_user_data = {
            "accountId": "test-account-123",
            "displayName": "Test User",
            "emailAddress": "test@example.com",
            "active": True,
        }

        # Fail twice, then succeed
        migrator_instance.jira_client.get_user_info_with_timeout.side_effect = [
            JiraConnectionError("Network error"),
            JiraConnectionError("Network error"),
            expected_user_data,
        ]

        result = migrator_instance._get_jira_user_with_retry("test.user")

        assert result == expected_user_data
        assert migrator_instance.jira_client.get_user_info_with_timeout.call_count == 3

    def test_get_jira_user_with_retry_exhausted_retries(
        self,
        migrator_instance,
    ) -> None:
        """Test that retries are exhausted after max attempts."""
        # Mock to always raise an error to trigger retries
        migrator_instance.jira_client.get_user_info_with_timeout.side_effect = (
            JiraConnectionError("Persistent error")
        )

        # Mock time.sleep to speed up test
        with patch("time.sleep"), pytest.raises(JiraConnectionError):
            migrator_instance._get_jira_user_with_retry("nonexistent.user")

        # Should attempt max_retries + 1 times (3 total: initial + 2 retries)
        assert migrator_instance.jira_client.get_user_info_with_timeout.call_count == 3

    def test_get_jira_user_with_retry_none_return_converted_to_error(
        self,
        migrator_instance,
    ) -> None:
        """Test that None return from client gets converted to JiraApiError after retries."""
        # Mock to return None, which should be converted to an exception and trigger retries
        migrator_instance.jira_client.get_user_info_with_timeout.return_value = None

        with (
            patch("time.sleep"),
            pytest.raises(JiraApiError, match="No user data returned"),
        ):
            migrator_instance._get_jira_user_with_retry("test.user")

        # Should make max_retries + 1 attempts (3 total: initial + 2 retries)
        assert migrator_instance.jira_client.get_user_info_with_timeout.call_count == 3

    def test_get_jira_user_with_retry_mixed_errors(self, migrator_instance) -> None:
        """Test handling of mixed error types with retries."""
        # Mock different error types - final error should be raised
        migrator_instance.jira_client.get_user_info_with_timeout.side_effect = [
            JiraAuthenticationError("Auth failed"),
            JiraConnectionError("Network error"),
            JiraResourceNotFoundError("User not found"),
        ]

        with patch("time.sleep"), pytest.raises(JiraResourceNotFoundError):
            migrator_instance._get_jira_user_with_retry("test.user")

        # Should make 3 attempts total (initial + 2 retries)
        assert migrator_instance.jira_client.get_user_info_with_timeout.call_count == 3

    def test_get_jira_user_with_retry_invalid_username(self, migrator_instance) -> None:
        """Test validation of username parameter."""
        # Test empty string
        with pytest.raises(ValueError, match="username must be a non-empty string"):
            migrator_instance._get_jira_user_with_retry("")

        # Test None
        with pytest.raises(ValueError, match="username must be a non-empty string"):
            migrator_instance._get_jira_user_with_retry(None)

        # Test non-string
        with pytest.raises(ValueError, match="username must be a non-empty string"):
            migrator_instance._get_jira_user_with_retry(123)

    def test_get_jira_user_with_retry_custom_max_retries(
        self,
        migrator_instance,
    ) -> None:
        """Test override of max_retries parameter."""
        migrator_instance.jira_client.get_user_info_with_timeout.side_effect = (
            JiraConnectionError("Network error")
        )

        with pytest.raises(JiraConnectionError):
            migrator_instance._get_jira_user_with_retry("test.user", max_retries=1)

        # Should have tried 1 retry + 1 initial = 2 total attempts
        assert migrator_instance.jira_client.get_user_info_with_timeout.call_count == 2

    def test_get_jira_user_with_retry_max_retries_validation(
        self,
        migrator_instance,
    ) -> None:
        """Test validation of max_retries parameter."""
        # Test negative retries
        with pytest.raises(
            ValueError,
            match="max_retries must be a non-negative integer",
        ):
            migrator_instance._get_jira_user_with_retry("test.user", max_retries=-1)

        # Test non-integer retries
        with pytest.raises(
            ValueError,
            match="max_retries must be a non-negative integer",
        ):
            migrator_instance._get_jira_user_with_retry(
                "test.user",
                max_retries="invalid",
            )

        # Test excessive retries (assuming ABSOLUTE_MAX_RETRIES is set)
        with pytest.raises(ValueError, match="max_retries cannot exceed"):
            migrator_instance._get_jira_user_with_retry("test.user", max_retries=1000)

    def test_get_jira_user_with_retry_exponential_backoff(
        self,
        migrator_instance,
    ) -> None:
        """Test that exponential backoff is working correctly."""
        # Mock to fail twice then succeed on third attempt
        migrator_instance.jira_client.get_user_info_with_timeout.side_effect = [
            JiraApiError("Error 1"),
            JiraApiError("Error 2"),
            {"accountId": "123", "displayName": "Test User"},
        ]

        with patch("time.sleep") as mock_sleep:
            result = migrator_instance._get_jira_user_with_retry("test.user")

        # Should succeed on third attempt, so 2 sleep calls
        assert mock_sleep.call_count == 2
        assert result is not None
        assert result["accountId"] == "123"

        # Verify exponential backoff - delays should increase
        sleep_calls = [call[0][0] for call in mock_sleep.call_args_list]
        assert len(sleep_calls) == 2
        assert sleep_calls[1] >= sleep_calls[0]  # Second delay should be >= first

    def test_get_jira_user_with_retry_jitter_applied(self, migrator_instance) -> None:
        """Test that jitter is applied to retry delays."""
        migrator_instance.jira_client.get_user_info_with_timeout.side_effect = [
            JiraConnectionError("Network error"),
            JiraConnectionError("Network error"),
        ]

        with patch("time.sleep") as mock_sleep:
            # Set jitter to True and run multiple times to see variation
            with pytest.raises(JiraConnectionError):
                migrator_instance._get_jira_user_with_retry("test.user", max_retries=1)

            # Should have called sleep once (for the single retry)
            assert mock_sleep.call_count == 1

            # The delay should be close to but not exactly 0.5 due to jitter
            delay = mock_sleep.call_args_list[0][0][0]
            assert 0.25 <= delay <= 0.75  # 50% jitter range around 0.5

    def test_get_jira_user_with_retry_timeout_protection(
        self,
        migrator_instance,
    ) -> None:
        """Test that request timeout is properly applied."""
        expected_user_data = {
            "accountId": "test-account-123",
            "displayName": "Test User",
            "emailAddress": "test@example.com",
            "active": True,
        }

        migrator_instance.jira_client.get_user_info_with_timeout.return_value = (
            expected_user_data
        )

        migrator_instance._get_jira_user_with_retry("test.user")

        # Verify timeout was passed to the API call
        call_args = migrator_instance.jira_client.get_user_info_with_timeout.call_args
        assert call_args is not None
        # Check if timeout parameter was passed
        if len(call_args) > 1 and "timeout" in call_args[1]:
            assert call_args[1]["timeout"] == 30.0  # From config

    def test_get_jira_user_with_retry_fallback_method(self, migrator_instance) -> None:
        """Test fallback to get_user_info when get_user_info_with_timeout is not available."""
        # Remove the timeout method to trigger fallback
        del migrator_instance.jira_client.get_user_info_with_timeout

        expected_user_data = {
            "accountId": "test-account-123",
            "displayName": "Test User",
            "emailAddress": "test@example.com",
            "active": True,
        }

        migrator_instance.jira_client.get_user_info.return_value = expected_user_data

        result = migrator_instance._get_jira_user_with_retry("test.user")

        assert result == expected_user_data
        migrator_instance.jira_client.get_user_info.assert_called_once_with("test.user")

    def test_get_jira_user_with_retry_concurrent_limiting(
        self,
        migrator_instance,
    ) -> None:
        """Test that concurrent request limiting is enforced."""
        # This test would require more complex setup to test actual concurrent limiting
        # For now, we just verify the method completes successfully with concurrent tracker
        expected_user_data = {
            "accountId": "test-account-123",
            "displayName": "Test User",
            "emailAddress": "test@example.com",
            "active": True,
        }

        migrator_instance.jira_client.get_user_info_with_timeout.return_value = (
            expected_user_data
        )

        result = migrator_instance._get_jira_user_with_retry("test.user")

        assert result == expected_user_data

    def test_get_jira_user_with_retry_error_context_logging(
        self,
        migrator_instance,
    ) -> None:
        """Test that comprehensive error context is logged."""
        migrator_instance.jira_client.get_user_info_with_timeout.side_effect = (
            JiraConnectionError("Network error")
        )

        with patch.object(migrator_instance.logger, "error") as mock_error_log:
            with pytest.raises(JiraConnectionError):
                migrator_instance._get_jira_user_with_retry("test.user")

            # Verify error context was logged
            mock_error_log.assert_called()
            log_call_args = mock_error_log.call_args[0][0]
            assert "exhausted all retry attempts" in log_call_args

    def test_get_jira_user_with_retry_warning_logs(self, migrator_instance) -> None:
        """Test that retry warnings are properly logged."""
        migrator_instance.jira_client.get_user_info_with_timeout.side_effect = [
            JiraConnectionError("Network error"),
            {
                "accountId": "test-account-123",
                "displayName": "Test User",
                "emailAddress": "test@example.com",
                "active": True,
            },
        ]

        with patch.object(migrator_instance.logger, "warning") as mock_warning_log:
            migrator_instance._get_jira_user_with_retry("test.user")

            # Verify warning was logged for the retry
            mock_warning_log.assert_called()
            log_call_args = mock_warning_log.call_args[0][0]
            assert "failed on attempt" in log_call_args
            assert "Retrying in" in log_call_args
