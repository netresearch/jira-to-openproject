#!/usr/bin/env python3
"""Test suite for StalenessManager component.

Tests TTL-based caching, staleness detection, automatic refresh,
fallback strategies, and metrics collection.
"""

import time
from datetime import datetime, timedelta, UTC
from unittest.mock import Mock, patch

import pytest

from src.clients.jira_client import JiraClient, JiraApiError
from src.clients.openproject_client import OpenProjectClient
from src.utils.staleness_manager import (
    StalenessManager,
    CacheEntry,
    RefreshResult,
    FallbackStrategy,
)


@pytest.fixture
def mock_jira_client():
    """Create a mock JiraClient for testing."""
    mock_client = Mock(spec=JiraClient)
    return mock_client


@pytest.fixture
def mock_op_client():
    """Create a mock OpenProject client for testing."""
    mock_client = Mock(spec=OpenProjectClient)
    return mock_client


@pytest.fixture
def staleness_manager(mock_jira_client, mock_op_client):
    """Create a StalenessManager instance for testing."""
    return StalenessManager(
        jira_client=mock_jira_client,
        op_client=mock_op_client,
        refresh_interval="1h",
        fallback_strategy="skip",
        fallback_admin_user_id=None
    )


class TestCacheEntry:
    """Test CacheEntry data class."""
    
    def test_cache_entry_age_calculation(self):
        """Test that cache entry age is calculated correctly."""
        past_time = datetime.now(tz=UTC) - timedelta(hours=2)
        entry = CacheEntry(
            mapped_user={"accountId": "123"},
            last_refreshed=past_time
        )
        
        age = entry.age()
        assert age >= timedelta(hours=1, minutes=59)  # Allow some test execution time
        assert age <= timedelta(hours=2, minutes=1)
    
    def test_cache_entry_staleness_detection(self):
        """Test staleness detection logic."""
        # Fresh entry
        fresh_entry = CacheEntry(
            mapped_user={"accountId": "123"},
            last_refreshed=datetime.now(tz=UTC)
        )
        assert not fresh_entry.is_stale(timedelta(hours=1))
        
        # Stale entry
        stale_entry = CacheEntry(
            mapped_user={"accountId": "123"},
            last_refreshed=datetime.now(tz=UTC) - timedelta(hours=2)
        )
        assert stale_entry.is_stale(timedelta(hours=1))
        
        # Entry with no mapped user is always stale
        none_entry = CacheEntry(
            mapped_user=None,
            last_refreshed=datetime.now(tz=UTC)
        )
        assert none_entry.is_stale(timedelta(hours=1))


class TestStalenessManager:
    """Test StalenessManager functionality."""
    
    def test_initialization(self, mock_jira_client, mock_op_client):
        """Test StalenessManager initialization."""
        manager = StalenessManager(
            jira_client=mock_jira_client,
            op_client=mock_op_client,
            refresh_interval="24h",
            fallback_strategy="assign_admin",
            fallback_admin_user_id="admin_123"
        )
        
        assert manager.refresh_interval == timedelta(hours=24)
        assert manager.fallback_strategy == FallbackStrategy.ASSIGN_ADMIN
        assert manager.fallback_admin_user_id == "admin_123"
        assert len(manager.cache) == 0
        assert all(value == 0 for value in manager.metrics.values())
    
    def test_duration_parsing(self, staleness_manager):
        """Test duration string parsing."""
        # Test valid formats
        assert staleness_manager._parse_duration("24h") == timedelta(hours=24)
        assert staleness_manager._parse_duration("2d") == timedelta(days=2)
        assert staleness_manager._parse_duration("30m") == timedelta(minutes=30)
        assert staleness_manager._parse_duration("60s") == timedelta(seconds=60)
        
        # Test case insensitivity and whitespace
        assert staleness_manager._parse_duration(" 12H ") == timedelta(hours=12)
        
        # Test invalid format
        with pytest.raises(ValueError, match="Invalid duration format"):
            staleness_manager._parse_duration("invalid")
    
    def test_cache_miss(self, staleness_manager):
        """Test cache miss scenario."""
        result = staleness_manager.get_cached_mapping("nonexistent_user")
        
        assert result is None
        assert staleness_manager.metrics["cache_misses"] == 1
        assert staleness_manager.metrics["cache_hits"] == 0
    
    def test_cache_hit_fresh_data(self, staleness_manager):
        """Test cache hit with fresh data."""
        user_data = {"accountId": "123", "displayName": "Test User"}
        staleness_manager.add_entry("test_user", user_data)
        
        result = staleness_manager.get_cached_mapping("test_user")
        
        assert result == user_data
        assert staleness_manager.metrics["cache_hits"] == 1
        assert staleness_manager.metrics["staleness_detected"] == 0
    
    def test_cache_hit_stale_data_successful_refresh(self, staleness_manager):
        """Test cache hit with stale data that refreshes successfully."""
        # Add stale entry
        old_data = {"accountId": "123", "displayName": "Old User"}
        staleness_manager.add_entry("test_user", old_data)
        
        # Make the entry stale by backdating it
        staleness_manager.cache["test_user"].last_refreshed = datetime.now(tz=UTC) - timedelta(hours=2)
        
        # Mock successful refresh
        fresh_data = {"accountId": "123", "displayName": "Fresh User", "active": True}
        staleness_manager.jira_client.get_user_info.return_value = fresh_data
        
        result = staleness_manager.get_cached_mapping("test_user")
        
        assert result == fresh_data
        assert staleness_manager.metrics["staleness_detected"] == 1
        assert staleness_manager.metrics["refreshes_attempted"] == 1
        assert staleness_manager.metrics["refreshes_successful"] == 1
    
    def test_cache_hit_stale_data_refresh_fails_skip_strategy(self, staleness_manager):
        """Test cache hit with stale data where refresh fails and skip strategy applies."""
        # Add stale entry
        old_data = {"accountId": "123", "displayName": "Old User"}
        staleness_manager.add_entry("test_user", old_data)
        staleness_manager.cache["test_user"].last_refreshed = datetime.now(tz=UTC) - timedelta(hours=2)
        
        # Mock failed refresh
        staleness_manager.jira_client.get_user_info.side_effect = JiraApiError("User not found")
        
        result = staleness_manager.get_cached_mapping("test_user")
        
        assert result is None
        assert staleness_manager.metrics["staleness_detected"] == 1
        assert staleness_manager.metrics["refreshes_attempted"] == 1
        assert staleness_manager.metrics["fallbacks_applied"] == 1
        assert "test_user" not in staleness_manager.cache  # Should be removed by skip strategy
    
    def test_user_validation_inactive_user(self, staleness_manager):
        """Test user validation failing for inactive user."""
        inactive_user = {"accountId": "123", "displayName": "Inactive User", "active": False}
        assert not staleness_manager._validate_user_data(inactive_user)
    
    def test_user_validation_missing_fields(self, staleness_manager):
        """Test user validation failing for missing required fields."""
        incomplete_user = {"accountId": "123"}  # Missing displayName
        assert not staleness_manager._validate_user_data(incomplete_user)
    
    def test_user_validation_success(self, staleness_manager):
        """Test successful user validation."""
        valid_user = {"accountId": "123", "displayName": "Valid User", "active": True}
        assert staleness_manager._validate_user_data(valid_user)
    
    def test_assign_admin_fallback_strategy(self, mock_jira_client, mock_op_client):
        """Test assign_admin fallback strategy."""
        manager = StalenessManager(
            jira_client=mock_jira_client,
            op_client=mock_op_client,
            refresh_interval="1h",
            fallback_strategy="assign_admin",
            fallback_admin_user_id="admin_123"
        )
        
        # Add stale entry and fail refresh
        manager.add_entry("test_user", {"accountId": "123"})
        manager.cache["test_user"].last_refreshed = datetime.now(tz=UTC) - timedelta(hours=2)
        manager.jira_client.get_user_info.return_value = None
        
        result = manager.get_cached_mapping("test_user")
        
        assert result is not None
        assert result["accountId"] == "admin_123"
        assert result["_fallback"] is True
        assert result["_original_user"] == "test_user"
        assert manager.metrics["fallbacks_applied"] == 1
    
    def test_assign_admin_fallback_no_admin_configured(self, mock_jira_client, mock_op_client):
        """Test assign_admin fallback strategy with no admin user configured."""
        manager = StalenessManager(
            jira_client=mock_jira_client,
            op_client=mock_op_client,
            refresh_interval="1h",
            fallback_strategy="assign_admin",
            fallback_admin_user_id=None  # No admin configured
        )
        
        # Add stale entry and fail refresh
        manager.add_entry("test_user", {"accountId": "123"})
        manager.cache["test_user"].last_refreshed = datetime.now(tz=UTC) - timedelta(hours=2)
        manager.jira_client.get_user_info.return_value = None
        
        result = manager.get_cached_mapping("test_user")
        
        # Should fall back to skip strategy
        assert result is None
        assert "test_user" not in manager.cache
        assert manager.metrics["fallbacks_applied"] == 1
    
    def test_create_placeholder_fallback_strategy(self, mock_jira_client, mock_op_client):
        """Test create_placeholder fallback strategy."""
        manager = StalenessManager(
            jira_client=mock_jira_client,
            op_client=mock_op_client,
            refresh_interval="1h",
            fallback_strategy="create_placeholder",
            fallback_admin_user_id=None
        )
        
        # Add stale entry and fail refresh
        manager.add_entry("test_user", {"accountId": "123"})
        manager.cache["test_user"].last_refreshed = datetime.now(tz=UTC) - timedelta(hours=2)
        manager.jira_client.get_user_info.return_value = None
        
        result = manager.get_cached_mapping("test_user")
        
        assert result is not None
        assert result["accountId"] == "PLACEHOLDER_test_user"
        assert result["_placeholder"] is True
        assert result["_manual_review_required"] is True
        assert manager.metrics["fallbacks_applied"] == 1
    
    @patch('time.sleep')  # Mock sleep to speed up tests
    def test_exponential_backoff_retry(self, mock_sleep, staleness_manager):
        """Test exponential backoff retry logic."""
        # Add stale entry
        staleness_manager.add_entry("test_user", {"accountId": "123"})
        staleness_manager.cache["test_user"].last_refreshed = datetime.now(tz=UTC) - timedelta(hours=2)
        
        # Mock two failures then success
        staleness_manager.jira_client.get_user_info.side_effect = [
            JiraApiError("Server error"),
            JiraApiError("Server error"),
            {"accountId": "123", "displayName": "User", "active": True}
        ]
        
        result = staleness_manager.get_cached_mapping("test_user")
        
        assert result is not None
        assert staleness_manager.metrics["refreshes_attempted"] == 1
        assert staleness_manager.metrics["refreshes_successful"] == 1
        
        # Verify exponential backoff sleep calls
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(0.5)  # First retry: 0.5s
        mock_sleep.assert_any_call(1.0)  # Second retry: 1.0s
    
    def test_cache_stats(self, staleness_manager):
        """Test cache statistics retrieval."""
        # Add some data and metrics
        staleness_manager.add_entry("user1", {"accountId": "123"})
        staleness_manager.metrics["cache_hits"] = 5
        staleness_manager.metrics["staleness_detected"] = 2
        
        stats = staleness_manager.get_cache_stats()
        
        assert stats["cache_size"] == 1
        assert stats["metrics"]["cache_hits"] == 5
        assert stats["metrics"]["staleness_detected"] == 2
        assert stats["config"]["refresh_interval"] == "1:00:00"
        assert stats["config"]["fallback_strategy"] == "skip"
    
    def test_clear_cache(self, staleness_manager):
        """Test cache clearing."""
        # Add data and metrics
        staleness_manager.add_entry("user1", {"accountId": "123"})
        staleness_manager.metrics["cache_hits"] = 5
        
        staleness_manager.clear_cache()
        
        assert len(staleness_manager.cache) == 0
        assert all(value == 0 for value in staleness_manager.metrics.values())
    
    def test_get_jira_user_data_exception_handling(self, staleness_manager):
        """Test that get_jira_user_data handles exceptions gracefully."""
        staleness_manager.jira_client.get_user_info.side_effect = Exception("Network error")
        
        result = staleness_manager._get_jira_user_data("test_user")
        assert result is None


class TestRefreshResult:
    """Test RefreshResult data class."""
    
    def test_refresh_result_creation(self):
        """Test RefreshResult creation with all fields."""
        result = RefreshResult(
            success=True,
            user_data={"accountId": "123"},
            error_reason=None,
            attempts=2,
            fallback_applied=False,
            fallback_strategy=None
        )
        
        assert result.success is True
        assert result.user_data == {"accountId": "123"}
        assert result.error_reason is None
        assert result.attempts == 2
        assert result.fallback_applied is False
        assert result.fallback_strategy is None


class TestFallbackStrategy:
    """Test FallbackStrategy enum."""
    
    def test_fallback_strategy_values(self):
        """Test that all expected fallback strategies exist."""
        assert FallbackStrategy.SKIP.value == "skip"
        assert FallbackStrategy.ASSIGN_ADMIN.value == "assign_admin"
        assert FallbackStrategy.CREATE_PLACEHOLDER.value == "create_placeholder" 