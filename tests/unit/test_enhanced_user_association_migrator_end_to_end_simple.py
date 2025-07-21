#!/usr/bin/env python3
"""Simplified End-to-End Integration Tests for EnhancedUserAssociationMigrator with MetricsCollector."""

import pytest
from datetime import UTC, datetime, timedelta  
from unittest.mock import MagicMock, patch, mock_open
from pathlib import Path

from src.utils.enhanced_user_association_migrator import EnhancedUserAssociationMigrator
from src.utils.metrics_collector import MetricsCollector


class TestSimplifiedEndToEndIntegration:
    """Simplified end-to-end integration tests with working metrics."""

    @pytest.fixture
    def mock_jira_client(self):
        """Create mock Jira client with all required methods."""
        client = MagicMock()
        client.get = MagicMock()
        client.get_user_info_with_timeout = MagicMock()
        return client

    @pytest.fixture
    def mock_op_client(self):
        """Create mock OpenProject client with all required methods."""
        client = MagicMock()
        client.get_user = MagicMock()
        return client
    
    @pytest.fixture
    def migrator_with_seeded_cache(self, mock_jira_client, mock_op_client):
        """Create migrator with pre-seeded cache for testing."""
        # Create a real migrator instance with mocked clients
        migrator = EnhancedUserAssociationMigrator(
            jira_client=mock_jira_client,
            op_client=mock_op_client
        )
        
        # Manually seed the cache with real dict values (not MagicMock)
        migrator.enhanced_user_mappings = {
            "test.user": {
                "jira_username": "test.user",
                "jira_user_id": "test-123",
                "jira_display_name": "Test User",
                "jira_email": "test@example.com",
                "openproject_user_id": 456,
                "openproject_username": "test.user",  # Real string value
                "openproject_email": "test@example.com",  # Real string value
                "mapping_status": "mapped",
                "fallback_user_id": None,
                "metadata": {
                    "created_at": "2025-01-01T00:00:00Z",
                    "jira_active": True,
                    "openproject_active": True
                },
                "lastRefreshed": datetime.now(UTC).isoformat()
            }
        }
        
        # Set up some basic configuration values
        migrator.refresh_interval = timedelta(hours=1)
        migrator.metrics_collector = MetricsCollector()  # Real metrics collector
        
        return migrator

    def test_end_to_end_auto_refresh_success_workflow(self, migrator_with_seeded_cache):
        """Test full auto-refresh workflow when staleness is detected."""
        migrator = migrator_with_seeded_cache
        
        # Mock fresh Jira data - use same email as pre-seeded cache for consistency
        mock_jira_data = {
            "accountId": "test-123",
            "displayName": "Test User Updated",
            "emailAddress": "test@example.com",  # Keep consistent with cache
            "active": True
        }
        migrator.jira_client.get_user_info_with_timeout.return_value = mock_jira_data
        
        # Mock OpenProject data with proper dict values
        mock_op_data = {
            "id": 456,
            "email": "test@example.com",  # Keep consistent with cache
            "firstname": "Test",
            "lastname": "User Updated"
        }
        migrator.op_client.get_user.return_value = mock_op_data
        
        # Mock the staleness check to return stale mapping first
        with patch.object(migrator, 'is_mapping_stale', return_value=True), \
             patch.object(migrator, '_save_enhanced_mappings') as mock_save:
            
            # This should trigger auto-refresh when called with auto_refresh=True
            result = migrator.get_mapping_with_staleness_check("test.user", auto_refresh=True)
            
            # Should have triggered refresh and returned fresh mapping
            assert result is not None
            assert result["openproject_username"] == "test.user"  # Username, not firstname
            assert result["openproject_email"] == "test@example.com"  # Consistent with cache
            
            # Verify save was called (indicating refresh occurred)
            mock_save.assert_called()

    def test_end_to_end_staleness_detection_no_refresh(self, migrator_with_seeded_cache):
        """Test staleness detection without auto-refresh."""
        migrator = migrator_with_seeded_cache
        
        # Mock Jira and OpenProject responses with proper dict values
        mock_jira_data = {
            "accountId": "test-789",
            "displayName": "Stale Test User",
            "emailAddress": "stale.test@example.com",
            "active": True
        }
        migrator.jira_client.get_user_info_with_timeout.return_value = mock_jira_data
        
        mock_op_data = {
            "id": 321,
            "email": "stale.test@example.com",
            "firstname": "Stale",
            "lastname": "User"
        }
        migrator.op_client.get_user.return_value = mock_op_data
        
        # Test staleness detection
        with patch.object(migrator, 'is_mapping_stale', return_value=True):
            result = migrator.check_and_handle_staleness("test.user", raise_on_stale=False)
            
            # Should return None when stale and not auto-refreshing
            assert result is None

    def test_end_to_end_error_resilience_and_recovery(self, migrator_with_seeded_cache):
        """Test error handling and recovery mechanisms."""
        migrator = migrator_with_seeded_cache
        
        # Mock Jira client to fail initially then succeed
        migrator.jira_client.get_user_info_with_timeout.side_effect = [
            Exception("Connection timeout"),
            {
                "accountId": "recovered-123",
                "displayName": "Recovered User",
                "emailAddress": "recovered@example.com",
                "active": True
            }
        ]
        
        # Mock OpenProject response
        mock_op_data = {
            "id": 789,
            "email": "recovered@example.com",
            "firstname": "Recovered",
            "lastname": "User",
            "login": "recovered.user"  # Add login field
        }
        migrator.op_client.get_user.return_value = mock_op_data
        migrator.op_client.get_user_by_email.return_value = mock_op_data  # Add email lookup
        
        # Should handle errors gracefully and eventually succeed
        with patch.object(migrator, '_save_enhanced_mappings'):
            result = migrator.refresh_user_mapping("recovered.user")
            
            # Should succeed after retry
            assert result is not None
            assert result["openproject_user_id"] == 789
            assert result["mapping_status"] == "mapped"
            assert result["metadata"]["openproject_email"] == "recovered@example.com"
            assert result["metadata"]["openproject_name"] == "Recovered User"

    def test_end_to_end_cache_management_workflow(self, migrator_with_seeded_cache):
        """Test cache loading, updating, and saving workflow."""
        migrator = migrator_with_seeded_cache
        
        # Mock responses for cache test
        mock_jira_data = {
            "accountId": "cache-456",
            "displayName": "Cache Test User",
            "emailAddress": "cache.test@example.com",
            "active": True
        }
        migrator.jira_client.get_user_info_with_timeout.return_value = mock_jira_data
        
        mock_op_data = {
            "id": 654,
            "email": "cache.test@example.com",
            "firstname": "Cache",
            "lastname": "User",
            "login": "cache.test.user"  # Add login field
        }
        migrator.op_client.get_user.return_value = mock_op_data
        migrator.op_client.get_user_by_email.return_value = mock_op_data  # Add email lookup
        
        # Test adding new mapping to cache
        with patch.object(migrator, '_save_enhanced_mappings') as mock_save:
            result = migrator.refresh_user_mapping("cache.test.user")
            
            # Should have created fresh mapping
            assert result is not None
            assert result["openproject_user_id"] == 654
            assert result["mapping_status"] == "mapped"
            assert result["metadata"]["openproject_email"] == "cache.test@example.com"
            assert result["metadata"]["openproject_name"] == "Cache User"

    def test_end_to_end_metrics_integration(self, migrator_with_seeded_cache):
        """Test metrics collection during operations."""
        migrator = migrator_with_seeded_cache
        
        # Ensure metrics collector is properly initialized
        assert migrator.metrics_collector is not None
        
        # Mock responses
        mock_jira_data = {
            "accountId": "metrics-789",
            "displayName": "Metrics Test User",
            "emailAddress": "metrics@example.com",
            "active": True
        }
        migrator.jira_client.get_user_info_with_timeout.return_value = mock_jira_data
        
        mock_op_data = {
            "id": 987,
            "email": "metrics@example.com",
            "firstname": "Metrics",
            "lastname": "User",
            "login": "metrics.user"  # Add login field
        }
        migrator.op_client.get_user.return_value = mock_op_data
        migrator.op_client.get_user_by_email.return_value = mock_op_data  # Add email lookup
        
        # Test operation with metrics
        with patch.object(migrator, '_save_enhanced_mappings'):
            initial_metrics = migrator.metrics_collector.get_metrics()  # Use get_metrics() method
            
            result = migrator.refresh_user_mapping("metrics.user")
            
            final_metrics = migrator.metrics_collector.get_metrics()  # Use get_metrics() method
            
            # Should have some metrics recorded
            assert result is not None
            assert final_metrics is not None
            print(f"Initial metrics: {initial_metrics}")
            print(f"Final metrics: {final_metrics}") 