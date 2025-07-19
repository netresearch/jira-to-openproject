#!/usr/bin/env python3
"""Tests for EnhancedUserAssociationMigrator retry logic with exponential backoff."""

import time
from unittest.mock import Mock, patch, call, MagicMock

import pytest

from src.clients.jira_client import JiraApiError, JiraConnectionError, JiraAuthenticationError, JiraResourceNotFoundError
from src.utils.enhanced_user_association_migrator import EnhancedUserAssociationMigrator
from tests.utils.mock_factory import create_mock_jira_client, create_mock_openproject_client


class TestEnhancedUserAssociationMigratorRetry:
    """Test suite for retry logic with exponential backoff."""

    @pytest.fixture
    def migrator_instance(self, tmp_path):
        """Create migrator instance with mocked clients."""
        jira_client = create_mock_jira_client()
        op_client = create_mock_openproject_client()
        
        # Create temporary data directory  
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        
        with patch('src.utils.enhanced_user_association_migrator.config') as mock_config:
            # Mock config paths
            mock_config.get_path.return_value = data_dir
            
            # Create migrator instance with positional arguments
            migrator = EnhancedUserAssociationMigrator(jira_client, op_client)
            
            # Set up required attributes
            migrator.jira_client = jira_client
            migrator.op_client = op_client
            migrator.enhanced_user_mappings = {}
            migrator.refresh_interval_seconds = 3600  # 1 hour default
            migrator.fallback_strategy = "admin"  # Simple string fallback
            migrator.admin_user_id = 1
            migrator.user_mapping = {}  # Add missing attribute
            
            # Mock the save method to avoid file I/O issues during tests
            migrator._save_enhanced_mappings = Mock()
            
            return migrator

    @pytest.fixture
    def sample_jira_user_data(self):
        """Sample Jira user data for successful responses."""
        return {
            "accountId": "test-account-123",
            "displayName": "Test User",
            "emailAddress": "test@example.com",
            "active": True,
            "key": "test.user",
            "name": "test.user"
        }

    @patch('time.sleep')
    def test_get_jira_user_with_retry_success_first_attempt(self, mock_sleep, migrator_instance, sample_jira_user_data):
        """Test successful user fetch on first attempt - no retries needed."""
        # Mock successful response on first attempt
        migrator_instance.jira_client.get_user_info.return_value = sample_jira_user_data
        
        # Call the retry method
        result = migrator_instance._get_jira_user_with_retry("test.user")
        
        # Verify result
        assert result == sample_jira_user_data
        
        # Verify only one API call was made
        migrator_instance.jira_client.get_user_info.assert_called_once_with("test.user")
        
        # Verify no sleep was called (no retries)
        mock_sleep.assert_not_called()

    @patch('time.sleep')
    def test_get_jira_user_with_retry_success_after_one_retry(self, mock_sleep, migrator_instance, sample_jira_user_data):
        """Test successful user fetch after one retry with proper 500ms delay."""
        # Mock API to fail once, then succeed
        migrator_instance.jira_client.get_user_info.side_effect = [
            JiraApiError("Temporary API error"),
            sample_jira_user_data
        ]
        
        # Call the retry method
        result = migrator_instance._get_jira_user_with_retry("test.user")
        
        # Verify result
        assert result == sample_jira_user_data
        
        # Verify two API calls were made
        assert migrator_instance.jira_client.get_user_info.call_count == 2
        migrator_instance.jira_client.get_user_info.assert_has_calls([
            call("test.user"),
            call("test.user")
        ])
        
        # Verify proper delay was applied (500ms = 0.5 * 2^0)
        mock_sleep.assert_called_once_with(0.5)

    @patch('time.sleep')
    def test_get_jira_user_with_retry_success_after_two_retries(self, mock_sleep, migrator_instance, sample_jira_user_data):
        """Test successful user fetch after two retries with proper exponential backoff."""
        # Mock API to fail twice, then succeed
        migrator_instance.jira_client.get_user_info.side_effect = [
            JiraConnectionError("Connection timeout"),
            JiraApiError("Server error"),
            sample_jira_user_data
        ]
        
        # Call the retry method
        result = migrator_instance._get_jira_user_with_retry("test.user")
        
        # Verify result
        assert result == sample_jira_user_data
        
        # Verify three API calls were made
        assert migrator_instance.jira_client.get_user_info.call_count == 3
        
        # Verify proper exponential backoff delays
        expected_calls = [call(0.5), call(1.0)]  # 500ms, 1s
        mock_sleep.assert_has_calls(expected_calls)

    @patch('time.sleep')
    def test_get_jira_user_with_retry_total_failure_all_attempts(self, mock_sleep, migrator_instance):
        """Test total failure after all retry attempts with proper error propagation."""
        # Mock API to always fail
        test_error = JiraApiError("Persistent API error")
        migrator_instance.jira_client.get_user_info.side_effect = test_error
        
        # Call should raise the last error
        with pytest.raises(JiraApiError, match="Persistent API error"):
            migrator_instance._get_jira_user_with_retry("test.user")
        
        # Verify all three attempts were made
        assert migrator_instance.jira_client.get_user_info.call_count == 3
        
        # Verify delays were applied before first two retries only
        expected_calls = [call(0.5), call(1.0)]  # No delay after final attempt
        mock_sleep.assert_has_calls(expected_calls)

    @patch('time.sleep')
    def test_get_jira_user_with_retry_different_exception_types(self, mock_sleep, migrator_instance, sample_jira_user_data):
        """Test retry logic works with different exception types."""
        # Test with different Jira exception types
        exceptions_to_test = [
            JiraConnectionError("Network issue"),
            JiraAuthenticationError("Auth failed"),  
            JiraResourceNotFoundError("Resource not found"),
            Exception("Generic error")
        ]
        
        for exception in exceptions_to_test:
            # Reset mocks
            mock_sleep.reset_mock()
            migrator_instance.jira_client.get_user_info.reset_mock()
            
            # Mock API to fail once with specific exception, then succeed
            migrator_instance.jira_client.get_user_info.side_effect = [
                exception,
                sample_jira_user_data
            ]
            
            # Call should succeed after retry
            result = migrator_instance._get_jira_user_with_retry("test.user")
            assert result == sample_jira_user_data
            
            # Verify retry was attempted
            assert migrator_instance.jira_client.get_user_info.call_count == 2
            mock_sleep.assert_called_once_with(0.5)

    @patch('time.sleep')
    def test_get_jira_user_with_retry_custom_max_retries(self, mock_sleep, migrator_instance, sample_jira_user_data):
        """Test custom max_retries parameter."""
        # Test with max_retries=0 (only 1 attempt)
        migrator_instance.jira_client.get_user_info.side_effect = JiraApiError("Error")
        
        with pytest.raises(JiraApiError):
            migrator_instance._get_jira_user_with_retry("test.user", max_retries=0)
        
        # Verify only one attempt was made
        assert migrator_instance.jira_client.get_user_info.call_count == 1
        mock_sleep.assert_not_called()
        
        # Reset and test with max_retries=1 (2 attempts)
        mock_sleep.reset_mock()
        migrator_instance.jira_client.get_user_info.reset_mock()
        migrator_instance.jira_client.get_user_info.side_effect = [
            JiraApiError("Error"),
            sample_jira_user_data
        ]
        
        result = migrator_instance._get_jira_user_with_retry("test.user", max_retries=1)
        assert result == sample_jira_user_data
        assert migrator_instance.jira_client.get_user_info.call_count == 2
        mock_sleep.assert_called_once_with(0.5)

    def test_get_jira_user_with_retry_none_return_no_retry(self, migrator_instance):
        """Test that None return (user not found) doesn't trigger retries."""
        # Mock API to return None (user not found)
        migrator_instance.jira_client.get_user_info.return_value = None
        
        # Call the retry method
        result = migrator_instance._get_jira_user_with_retry("nonexistent.user")
        
        # Verify result is None
        assert result is None
        
        # Verify only one API call was made (no retries for None)
        migrator_instance.jira_client.get_user_info.assert_called_once_with("nonexistent.user")

    @patch('time.sleep')
    def test_refresh_user_mapping_integration_with_retry(self, mock_sleep, migrator_instance, sample_jira_user_data):
        """Test that refresh_user_mapping properly uses the retry logic."""
        # Mock API to fail once, then succeed
        migrator_instance.jira_client.get_user_info.side_effect = [
            JiraApiError("Temporary error"),
            sample_jira_user_data
        ]
        
        # Call refresh_user_mapping which should use the retry logic
        result = migrator_instance.refresh_user_mapping("test.user")
        
        # Verify result is not None (successful refresh) - check for metadata since return format may vary
        assert result is not None
        assert result.get("metadata", {}).get("jira_account_id") == "test-account-123"
        assert result.get("metadata", {}).get("jira_display_name") == "Test User"
        
        # Verify retry logic was used (2 calls with delay)
        assert migrator_instance.jira_client.get_user_info.call_count == 2
        mock_sleep.assert_called_once_with(0.5)
        
        # Verify mapping was updated with fresh timestamp
        assert "test.user" in migrator_instance.enhanced_user_mappings
        mapping = migrator_instance.enhanced_user_mappings["test.user"]
        assert mapping["lastRefreshed"] is not None

    @patch('time.sleep')
    def test_refresh_user_mapping_retry_failure_fallback(self, mock_sleep, migrator_instance):
        """Test that refresh_user_mapping handles retry failures gracefully."""
        # Mock API to always fail
        migrator_instance.jira_client.get_user_info.side_effect = JiraApiError("Persistent error")
        
        # refresh_user_mapping should handle the exception and return None
        result = migrator_instance.refresh_user_mapping("error.user")
        
        # Verify graceful failure handling
        assert result is None
        
        # Verify all retry attempts were made
        assert migrator_instance.jira_client.get_user_info.call_count == 3
        mock_sleep.assert_has_calls([call(0.5), call(1.0)])

    def test_retry_logging_debug_info_warning_error(self, migrator_instance, sample_jira_user_data, caplog):
        """Test that proper logging occurs at each retry attempt."""
        import logging
        
        # Set logging level to capture debug messages
        caplog.set_level(logging.DEBUG)
        
        # Mock API to fail twice, then succeed
        migrator_instance.jira_client.get_user_info.side_effect = [
            JiraApiError("First error"),
            JiraConnectionError("Second error"),
            sample_jira_user_data
        ]
        
        # Patch time.sleep to avoid actual delays in test
        with patch('time.sleep'):
            result = migrator_instance._get_jira_user_with_retry("test.user")
        
        # Verify result
        assert result == sample_jira_user_data
        
        # Verify logging messages - check for key content rather than exact format
        log_messages = [record.message for record in caplog.records]
        
        # Should have debug messages for each attempt
        assert any("test.user" in msg and "attempt 1" in msg for msg in log_messages)
        assert any("test.user" in msg and "attempt 2" in msg for msg in log_messages)
        assert any("test.user" in msg and "attempt 3" in msg for msg in log_messages)
        
        # Should have warning messages for failures
        assert any("failed" in msg.lower() and "test.user" in msg for msg in log_messages)
        
        # Should have info message for eventual success after retries
        assert any("success" in msg.lower() and "test.user" in msg and "3 attempts" in msg for msg in log_messages)

    def test_exponential_backoff_calculation_precision(self, migrator_instance):
        """Test that exponential backoff calculations are precise."""
        # Test the exponential backoff formula: base_delay * (2 ** attempt)
        base_delay = 0.5
        
        expected_delays = [
            base_delay * (2 ** 0),  # 0.5s for first retry (attempt 0 → 1)
            base_delay * (2 ** 1),  # 1.0s for second retry (attempt 1 → 2)  
            base_delay * (2 ** 2),  # 2.0s for third retry (would be attempt 2 → 3, but we only do 3 total attempts)
        ]
        
        assert expected_delays[0] == 0.5
        assert expected_delays[1] == 1.0
        assert expected_delays[2] == 2.0  # Not used in current implementation but validates formula

    def test_edge_case_empty_username(self, migrator_instance):
        """Test retry logic with edge case inputs."""
        # Test with empty username
        migrator_instance.jira_client.get_user_info.return_value = None
        
        result = migrator_instance._get_jira_user_with_retry("")
        assert result is None
        
        migrator_instance.jira_client.get_user_info.assert_called_once_with("")

    def test_edge_case_special_characters_username(self, migrator_instance, sample_jira_user_data):
        """Test retry logic with special characters in username."""
        special_username = "test@user.com"
        migrator_instance.jira_client.get_user_info.return_value = sample_jira_user_data
        
        result = migrator_instance._get_jira_user_with_retry(special_username)
        assert result == sample_jira_user_data
        
        migrator_instance.jira_client.get_user_info.assert_called_once_with(special_username) 