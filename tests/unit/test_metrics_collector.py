#!/usr/bin/env python3
"""Tests for MetricsCollector class."""

import pytest
import threading
import time
from src.utils.metrics_collector import MetricsCollector


class TestMetricsCollector:
    """Test suite for MetricsCollector class."""
    
    def test_initialization(self):
        """Test that MetricsCollector initializes correctly."""
        collector = MetricsCollector()
        
        # Check initial state
        metrics = collector.get_metrics()
        assert metrics["counters"] == {}
        assert metrics["tagged_counters"] == {}
        
        summary = collector.get_summary()
        assert summary["total_metrics"] == 0
        assert summary["total_count"] == 0
        assert summary["tagged_metrics"] == 0
        assert summary["metric_names"] == []
    
    def test_increment_counter_basic(self):
        """Test basic counter increment functionality."""
        collector = MetricsCollector()
        
        # Increment a counter
        collector.increment_counter("test_metric")
        
        # Check the counter value
        assert collector.get_counter("test_metric") == 1
        
        # Increment again
        collector.increment_counter("test_metric")
        assert collector.get_counter("test_metric") == 2
    
    def test_increment_counter_with_tags(self):
        """Test counter increment with tags."""
        collector = MetricsCollector()
        
        # Increment with tags
        tags = {"reason": "missing", "username": "test.user"}
        collector.increment_counter("staleness_detected_total", tags=tags)
        
        # Check tagged counter
        assert collector.get_tagged_counter("staleness_detected_total", tags) == 1
        
        # Check basic counter is also incremented
        assert collector.get_counter("staleness_detected_total") == 1
    
    def test_increment_counter_different_tags(self):
        """Test that different tags create separate counters."""
        collector = MetricsCollector()
        
        # Increment with different tag sets
        tags1 = {"reason": "missing"}
        tags2 = {"reason": "expired"}
        
        collector.increment_counter("staleness_detected_total", tags=tags1)
        collector.increment_counter("staleness_detected_total", tags=tags2)
        collector.increment_counter("staleness_detected_total", tags=tags1)
        
        # Check basic counter (total)
        assert collector.get_counter("staleness_detected_total") == 3
        
        # Check tagged counters (separate)
        assert collector.get_tagged_counter("staleness_detected_total", tags1) == 2
        assert collector.get_tagged_counter("staleness_detected_total", tags2) == 1
    
    def test_get_metrics_comprehensive(self):
        """Test comprehensive metrics retrieval."""
        collector = MetricsCollector()
        
        # Add various metrics
        collector.increment_counter("metric1")
        collector.increment_counter("metric2", tags={"type": "cache"})
        collector.increment_counter("metric2", tags={"type": "refresh"})
        
        metrics = collector.get_metrics()
        
        # Check counters
        assert metrics["counters"]["metric1"] == 1
        assert metrics["counters"]["metric2"] == 2
        
        # Check tagged counters
        assert "metric2" in metrics["tagged_counters"]
        tagged_metric2 = metrics["tagged_counters"]["metric2"]
        assert "type:cache" in tagged_metric2
        assert "type:refresh" in tagged_metric2
        assert tagged_metric2["type:cache"] == 1
        assert tagged_metric2["type:refresh"] == 1
    
    def test_reset_metrics(self):
        """Test metrics reset functionality."""
        collector = MetricsCollector()
        
        # Add some metrics
        collector.increment_counter("test_metric")
        collector.increment_counter("tagged_metric", tags={"key": "value"})
        
        # Verify metrics exist
        assert collector.get_counter("test_metric") == 1
        assert collector.get_counter("tagged_metric") == 1
        
        # Reset and verify empty
        collector.reset_metrics()
        assert collector.get_counter("test_metric") == 0
        assert collector.get_counter("tagged_metric") == 0
        
        metrics = collector.get_metrics()
        assert metrics["counters"] == {}
        assert metrics["tagged_counters"] == {}
    
    def test_get_summary(self):
        """Test metrics summary functionality."""
        collector = MetricsCollector()
        
        # Add various metrics
        collector.increment_counter("metric1")
        collector.increment_counter("metric1")
        collector.increment_counter("metric2", tags={"tag": "value"})
        
        summary = collector.get_summary()
        
        assert summary["total_metrics"] == 2
        assert summary["total_count"] == 3
        assert summary["tagged_metrics"] == 1
        assert set(summary["metric_names"]) == {"metric1", "metric2"}
    
    def test_thread_safety(self):
        """Test thread safety of metrics collection."""
        collector = MetricsCollector()
        
        def increment_worker():
            """Worker function to increment metrics from multiple threads."""
            for _ in range(100):
                collector.increment_counter("thread_test_metric")
                collector.increment_counter("tagged_thread_metric", tags={"thread": "worker"})
        
        # Create multiple threads
        threads = []
        for _ in range(5):
            thread = threading.Thread(target=increment_worker)
            threads.append(thread)
        
        # Start all threads
        for thread in threads:
            thread.start()
        
        # Wait for all threads to complete
        for thread in threads:
            thread.join()
        
        # Check final counts (5 threads * 100 increments each = 500)
        assert collector.get_counter("thread_test_metric") == 500
        assert collector.get_counter("tagged_thread_metric") == 500
        assert collector.get_tagged_counter("tagged_thread_metric", {"thread": "worker"}) == 500
    
    def test_error_handling(self):
        """Test that metrics collection errors don't break functionality."""
        collector = MetricsCollector()
        
        # This should not raise an exception even with problematic input
        collector.increment_counter(None)  # None metric name
        collector.increment_counter("valid_metric", tags=None)  # None tags
        
        # Collector should still work normally
        collector.increment_counter("normal_metric")
        assert collector.get_counter("normal_metric") == 1
    
    def test_staleness_metrics_integration(self):
        """Test metrics collection for staleness detection scenarios."""
        collector = MetricsCollector()
        
        # Simulate staleness detection metrics as expected by the migrator
        collector.increment_counter('staleness_detected_total', 
                                  tags={'reason': 'missing', 'username': 'user1'})
        collector.increment_counter('staleness_detected_total', 
                                  tags={'reason': 'expired', 'username': 'user2'})
        collector.increment_counter('staleness_refreshed_total', 
                                  tags={'success': 'true', 'username': 'user1', 'trigger': 'auto_refresh'})
        collector.increment_counter('mapping_fallback_total', 
                                  tags={'fallback_strategy': 'skip', 'reason': 'validation_failed'})
        
        # Verify expected metrics
        assert collector.get_counter('staleness_detected_total') == 2
        assert collector.get_counter('staleness_refreshed_total') == 1
        assert collector.get_counter('mapping_fallback_total') == 1
        
        # Verify tagged counters work for expected scenarios
        assert collector.get_tagged_counter('staleness_detected_total', 
                                          {'reason': 'missing', 'username': 'user1'}) == 1
        assert collector.get_tagged_counter('mapping_fallback_total', 
                                          {'fallback_strategy': 'skip', 'reason': 'validation_failed'}) == 1 