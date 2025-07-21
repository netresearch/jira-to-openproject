#!/usr/bin/env python3
"""Simplified End-to-End Integration Tests for Enhanced User Association Migrator.

This module tests Task 76.6 implementation with focused integration scenarios:
1. Complete staleness detection and refresh workflows
2. Metrics collection integration during end-to-end operations
3. Cache operations with real MetricsCollector
4. Fallback strategy execution with monitoring
"""

import pytest
import logging
from datetime import datetime, UTC, timedelta
from unittest.mock import Mock, patch, MagicMock

from src.utils.enhanced_user_association_migrator import (
    EnhancedUserAssociationMigrator,
    StaleMappingError,
    UserAssociationMapping
)
from src.utils.metrics_collector import MetricsCollector
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient


class TestSimplifiedEndToEndIntegration:
    """Simplified end-to-end integration tests for complete workflows."""
    
    @pytest.fixture
    def metrics_collector(self):
        """Create a real MetricsCollector for testing."""
        return MetricsCollector()
    
    @pytest.fixture
    def migrator_with_seeded_cache(self, metrics_collector):
        """Create migrator with pre-seeded cache for end-to-end testing."""
        mock_jira = Mock(spec=JiraClient)
        mock_op = Mock(spec=OpenProjectClient)
        
        with patch('src.utils.enhanced_user_association_migrator.config.migration_config', {
            'mapping': {'refresh_interval': '1h', 'fallback_strategy': 'skip'}
        }):
            with patch('src.utils.enhanced_user_association_migrator.config.get_path') as mock_path:
                mock_path.return_value.exists.return_value = False
                
                migrator = EnhancedUserAssociationMigrator(
                    jira_client=mock_jira,
                    op_client=mock_op,
                    user_mapping={},
                    metrics_collector=metrics_collector
                )
                
                # Seed cache with test scenarios
                current_time = datetime.now(tz=UTC)
                stale_time = current_time - timedelta(hours=2)
                
                migrator.enhanced_user_mappings = {
                    "fresh.user": {
                        "jira_username": "fresh.user",
                        "openproject_user_id": 123,
                        "mapping_status": "mapped",
                        "lastRefreshed": current_time.isoformat(),
                        "metadata": {"jira_active": True}
                    },
                    "stale.user": {
                        "jira_username": "stale.user",
                        "openproject_user_id": 456, 
                        "mapping_status": "mapped",
                        "lastRefreshed": stale_time.isoformat(),
                        "metadata": {"jira_active": True}
                    }
                }
                
                return migrator, mock_jira, mock_op

    def test_end_to_end_fresh_cache_hit_workflow(self, migrator_with_seeded_cache, metrics_collector, caplog):
        """Test complete workflow with fresh mapping - should hit cache without refresh."""
        migrator, mock_jira, mock_op = migrator_with_seeded_cache
        
        with caplog.at_level(logging.DEBUG):
            # Test fresh mapping lookup
            mapping = migrator.check_and_handle_staleness('fresh.user', raise_on_stale=False)
        
        # Should return fresh mapping without triggering staleness
        assert mapping is not None
        assert mapping["jira_username"] == "fresh.user"
        
        # Should not increment staleness metrics for fresh user
        assert metrics_collector.get_counter('staleness_detected_total') == 0
        
        # Should log cache hit
        debug_logs = [record.message for record in caplog.records if record.levelname == 'DEBUG']
        assert any('Cache hit for fresh.user' in log for log in debug_logs)

    def test_end_to_end_stale_detection_and_metrics(self, migrator_with_seeded_cache, metrics_collector, caplog):
        """Test complete workflow with stale mapping detection and metrics collection."""
        migrator, mock_jira, mock_op = migrator_with_seeded_cache
        
        with caplog.at_level(logging.DEBUG):
            # Test stale mapping detection
            mapping = migrator.check_and_handle_staleness('stale.user', raise_on_stale=False)
        
        # Should return None for stale mapping
        assert mapping is None
        
        # Should increment staleness detection metrics
        assert metrics_collector.get_counter('staleness_detected_total') == 1
        assert metrics_collector.get_tagged_counter(
            'staleness_detected_total',
            {'reason': 'expired', 'username': 'stale.user'}
        ) == 1
        
        # Should log staleness detection
        debug_logs = [record.message for record in caplog.records if record.levelname == 'DEBUG']
        assert any('Staleness detected for stale.user' in log for log in debug_logs)

    def test_end_to_end_cache_miss_detection_and_metrics(self, migrator_with_seeded_cache, metrics_collector, caplog):
        """Test complete workflow with cache miss and metrics collection."""
        migrator, mock_jira, mock_op = migrator_with_seeded_cache
        
        with caplog.at_level(logging.DEBUG):
            # Test missing user (cache miss)
            mapping = migrator.check_and_handle_staleness('missing.user', raise_on_stale=False)
        
        # Should return None for missing user
        assert mapping is None
        
        # Should increment staleness detection metrics for missing user
        assert metrics_collector.get_counter('staleness_detected_total') == 1
        assert metrics_collector.get_tagged_counter(
            'staleness_detected_total',
            {'reason': 'missing', 'username': 'missing.user'}
        ) == 1
        
        # Should log cache miss
        debug_logs = [record.message for record in caplog.records if record.levelname == 'DEBUG']
        assert any('Cache miss for missing.user' in log for log in debug_logs)

    def test_end_to_end_auto_refresh_success_workflow(self, migrator_with_seeded_cache, metrics_collector, caplog):
        """Test complete auto-refresh workflow with successful refresh."""
        migrator, mock_jira, mock_op = migrator_with_seeded_cache
        
        # Mock successful Jira response
        mock_jira.get.return_value.status_code = 200
        mock_jira.get.return_value.json.return_value = [{
            "name": "stale.user",
            "displayName": "Stale User (Refreshed)",
            "emailAddress": "stale@company.com",
            "active": True,
            "accountId": "stale-account-123"
        }]
        
        with patch.object(migrator, '_save_enhanced_mappings'):
            with patch.object(migrator, '_validate_refreshed_user', return_value={"is_valid": True}):
                with caplog.at_level(logging.DEBUG):
                    # Test auto-refresh workflow
                    mapping = migrator.get_mapping_with_staleness_check('stale.user', auto_refresh=True)
        
        # Should return refreshed mapping
        assert mapping is not None
        assert mapping["metadata"]["jira_display_name"] == "Stale User (Refreshed)"
        
        # Should have staleness detection and successful refresh metrics
        assert metrics_collector.get_counter('staleness_detected_total') == 1
        assert metrics_collector.get_counter('staleness_refreshed_total') == 1
        
        # Check specific tagged counters
        assert metrics_collector.get_tagged_counter(
            'staleness_detected_total',
            {'reason': 'expired', 'username': 'stale.user'}
        ) == 1
        
        assert metrics_collector.get_tagged_counter(
            'staleness_refreshed_total',
            {'success': 'true', 'username': 'stale.user', 'trigger': 'auto_refresh'}
        ) == 1
        
        # Should log complete workflow
        debug_logs = [record.message for record in caplog.records if record.levelname == 'DEBUG']
        assert any('Staleness detected for stale.user' in log for log in debug_logs)
        assert any('Attempting automatic refresh' in log for log in debug_logs)
        assert any('Successfully refreshed mapping for stale.user' in log for log in debug_logs)

    def test_end_to_end_auto_refresh_failure_workflow(self, migrator_with_seeded_cache, metrics_collector, caplog):
        """Test complete auto-refresh workflow with failed refresh."""
        migrator, mock_jira, mock_op = migrator_with_seeded_cache
        
        # Mock failed Jira response (404 user not found)
        mock_jira.get.return_value.status_code = 404
        
        with patch.object(migrator, '_save_enhanced_mappings'):
            with caplog.at_level(logging.DEBUG):
                # Test auto-refresh failure workflow
                mapping = migrator.get_mapping_with_staleness_check('stale.user', auto_refresh=True)
        
        # Should return None for failed refresh
        assert mapping is None
        
        # Should have staleness detection and failed refresh metrics
        assert metrics_collector.get_counter('staleness_detected_total') == 1
        assert metrics_collector.get_counter('staleness_refreshed_total') == 1
        
        # Check specific tagged counters
        assert metrics_collector.get_tagged_counter(
            'staleness_detected_total',
            {'reason': 'expired', 'username': 'stale.user'}
        ) == 1
        
        assert metrics_collector.get_tagged_counter(
            'staleness_refreshed_total',
            {'success': 'false', 'username': 'stale.user', 'trigger': 'auto_refresh'}
        ) == 1
        
        # Should log complete workflow including failure
        debug_logs = [record.message for record in caplog.records if record.levelname == 'DEBUG']
        assert any('Staleness detected for stale.user' in log for log in debug_logs)
        assert any('Failed to refresh mapping for stale.user' in log for log in debug_logs)

    def test_end_to_end_batch_operations_with_metrics(self, migrator_with_seeded_cache, metrics_collector):
        """Test batch operations with comprehensive metrics collection."""
        migrator, mock_jira, mock_op = migrator_with_seeded_cache
        
        # Add more users to cache for batch testing
        very_stale_time = datetime.now(tz=UTC) - timedelta(days=1)
        migrator.enhanced_user_mappings["batch.user1"] = {
            "jira_username": "batch.user1",
            "lastRefreshed": very_stale_time.isoformat(),
            "metadata": {}
        }
        migrator.enhanced_user_mappings["batch.user2"] = {
            "jira_username": "batch.user2", 
            "lastRefreshed": very_stale_time.isoformat(),
            "metadata": {}
        }
        
        # Mock mixed Jira responses for batch operations
        def mock_jira_side_effect(url):
            response = Mock()
            if 'batch.user1' in url:
                response.status_code = 200
                response.json.return_value = [{"name": "batch.user1", "active": True}]
            else:
                response.status_code = 404
            return response
        
        mock_jira.get.side_effect = mock_jira_side_effect
        
        with patch.object(migrator, '_save_enhanced_mappings'):
            with patch.object(migrator, '_validate_refreshed_user', return_value={"is_valid": True}):
                # Test batch refresh
                results = migrator.batch_refresh_stale_mappings(['batch.user1', 'batch.user2'])
        
        # Should have batch results
        assert results["refresh_attempted"] == 2
        assert results["refresh_successful"] == 1
        assert results["refresh_failed"] == 1
        
        # Should have comprehensive metrics for batch operations
        refresh_total = metrics_collector.get_counter('staleness_refreshed_total')
        assert refresh_total == 2  # One success, one failure
        
        # Check individual batch refresh metrics
        success_count = metrics_collector.get_tagged_counter(
            'staleness_refreshed_total',
            {'success': 'true', 'username': 'batch.user1', 'trigger': 'batch_refresh', 'attempts': '1'}
        )
        assert success_count == 1
        
        failure_count = metrics_collector.get_tagged_counter(
            'staleness_refreshed_total',
            {'success': 'false', 'username': 'batch.user2', 'trigger': 'batch_refresh', 'attempts': '1'}
        )
        assert failure_count == 1

    def test_end_to_end_fallback_execution_with_metrics(self, migrator_with_seeded_cache, metrics_collector, caplog):
        """Test fallback strategy execution with metrics collection."""
        migrator, mock_jira, mock_op = migrator_with_seeded_cache
        
        current_mapping = {
            "jira_username": "fallback.user",
            "metadata": {}
        }
        
        with patch.object(migrator, '_save_enhanced_mappings'):
            with caplog.at_level(logging.WARNING):
                # Test skip fallback execution
                result = migrator._execute_skip_fallback(
                    'fallback.user', 
                    'user_validation_failed',
                    current_mapping
                )
        
        # Should return None for skip strategy
        assert result is None
        
        # Should increment fallback metrics
        assert metrics_collector.get_counter('mapping_fallback_total') == 1
        assert metrics_collector.get_tagged_counter(
            'mapping_fallback_total',
            {'fallback_strategy': 'skip', 'reason': 'user_validation_failed'}
        ) == 1
        
        # Should log fallback execution
        warning_logs = [record.message for record in caplog.records if record.levelname == 'WARNING']
        assert any('Skipping user mapping for fallback.user' in log for log in warning_logs)

    def test_end_to_end_bulk_staleness_detection_with_monitoring(self, migrator_with_seeded_cache, metrics_collector):
        """Test bulk staleness detection with bulk monitoring tags."""
        migrator, mock_jira, mock_op = migrator_with_seeded_cache
        
        # Test bulk staleness detection
        stale_mappings = migrator.detect_stale_mappings(['fresh.user', 'stale.user', 'missing.user'])
        
        # Should detect stale and missing users
        assert len(stale_mappings) == 2  # stale.user and missing.user
        assert 'stale.user' in stale_mappings
        assert 'missing.user' in stale_mappings
        
        # Should have bulk detection metrics
        assert metrics_collector.get_counter('staleness_detected_total') == 2
        
        # Check bulk detection mode tags
        stale_bulk_count = metrics_collector.get_tagged_counter(
            'staleness_detected_total',
            {'reason': 'expired', 'username': 'stale.user', 'detection_mode': 'bulk'}
        )
        assert stale_bulk_count == 1
        
        missing_bulk_count = metrics_collector.get_tagged_counter(
            'staleness_detected_total',
            {'reason': 'missing', 'username': 'missing.user', 'detection_mode': 'bulk'}
        )
        assert missing_bulk_count == 1

    def test_end_to_end_metrics_aggregation_and_summary(self, migrator_with_seeded_cache, metrics_collector):
        """Test that metrics aggregate correctly across multiple end-to-end operations."""
        migrator, mock_jira, mock_op = migrator_with_seeded_cache
        
        # Execute multiple operations to generate diverse metrics
        migrator.check_and_handle_staleness('fresh.user', raise_on_stale=False)      # Cache hit (no metrics)
        migrator.check_and_handle_staleness('stale.user', raise_on_stale=False)      # Staleness detection  
        migrator.check_and_handle_staleness('missing1', raise_on_stale=False)        # Cache miss
        migrator.check_and_handle_staleness('missing2', raise_on_stale=False)        # Cache miss
        
        # Execute fallback
        with patch.object(migrator, '_save_enhanced_mappings'):
            migrator._execute_skip_fallback('test.user', 'test_reason', {})
        
        # Verify aggregated metrics
        assert metrics_collector.get_counter('staleness_detected_total') == 3  # stale + 2 missing
        assert metrics_collector.get_counter('mapping_fallback_total') == 1
        
        # Test metrics summary
        summary = metrics_collector.get_summary()
        assert summary['total_metrics'] == 2  # staleness_detected, mapping_fallback  
        assert summary['total_count'] == 4    # 3 + 1
        assert 'staleness_detected_total' in summary['metric_names']
        assert 'mapping_fallback_total' in summary['metric_names']
        
        # Verify tagged counter diversity
        tagged_staleness = summary['tagged_counters']['staleness_detected_total']
        assert len(tagged_staleness) == 3  # expired, missing1, missing2

    def test_end_to_end_error_resilience_with_metrics(self, migrator_with_seeded_cache, metrics_collector, caplog):
        """Test that end-to-end workflows are resilient to partial failures."""
        migrator, mock_jira, mock_op = migrator_with_seeded_cache
        
        # Simulate metrics collector failure (should not break core functionality)
        broken_metrics = Mock(spec=MetricsCollector)
        broken_metrics.increment_counter.side_effect = Exception("Metrics system failure")
        
        original_metrics = migrator.metrics_collector
        migrator.metrics_collector = broken_metrics
        
        try:
            with caplog.at_level(logging.DEBUG):
                # Should continue working despite metrics failure
                mapping = migrator.check_and_handle_staleness('fresh.user', raise_on_stale=False)
            
            # Core functionality should work
            assert mapping is not None
            assert mapping["jira_username"] == "fresh.user"
            
        finally:
            # Restore original metrics collector
            migrator.metrics_collector = original_metrics
        
        # After restoration, metrics should work normally
        migrator.check_and_handle_staleness('missing.test', raise_on_stale=False)
        assert metrics_collector.get_counter('staleness_detected_total') == 1 