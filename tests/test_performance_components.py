"""Comprehensive tests for performance optimization components.

This test suite covers all performance optimization components including:
- BatchProcessor for efficient API call batching
- RetryManager for robust retry mechanisms  
- EnhancedRateLimiter for API throttling
- ProgressTracker for real-time progress reporting
- MigrationPerformanceManager for integrated performance management
"""

import asyncio
import pytest
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch, AsyncMock, call
from collections import deque
from dataclasses import dataclass
from typing import List, Any, Optional

# Import the components we're testing
from src.utils.batch_processor import BatchProcessor, BatchResult
from src.utils.retry_manager import RetryManager, RetryConfig, RetryStrategy
from src.utils.enhanced_rate_limiter import (
    EnhancedRateLimiter, RateLimitConfig, RateLimitStrategy, 
    GlobalRateLimiterManager, rate_limited
)
from src.utils.progress_tracker import ProgressTracker, ProgressStage
from src.performance.migration_performance_manager import (
    MigrationPerformanceManager, PerformanceConfig, MigrationMetrics
)


class TestBatchProcessor:
    """Test cases for BatchProcessor component."""

    def test_batch_processor_initialization(self):
        """Test proper initialization of BatchProcessor."""
        processor = BatchProcessor(batch_size=50, max_workers=2)
        
        assert processor.batch_size == 50
        assert processor.max_workers == 2
        assert processor.total_items == 0
        assert processor.processed_items == 0
        assert processor.failed_items == 0
        assert len(processor.batch_results) == 0

    def test_process_empty_items(self):
        """Test processing empty list of items."""
        processor = BatchProcessor()
        
        def mock_process_func(batch):
            return [f"processed_{item}" for item in batch]
        
        results = processor.process_items([], mock_process_func)
        
        assert len(results) == 0
        assert processor.total_items == 0
        assert processor.processed_items == 0

    def test_process_single_batch(self):
        """Test processing items that fit in a single batch."""
        processor = BatchProcessor(batch_size=10)
        items = list(range(5))
        
        def mock_process_func(batch):
            return [item * 2 for item in batch]
        
        results = processor.process_items(items, mock_process_func)
        
        assert len(results) == 5
        assert results == [0, 2, 4, 6, 8]
        assert processor.total_items == 5
        assert processor.processed_items == 5
        assert processor.failed_items == 0

    def test_process_multiple_batches(self):
        """Test processing items across multiple batches."""
        processor = BatchProcessor(batch_size=3)
        items = list(range(10))
        
        def mock_process_func(batch):
            return [item + 100 for item in batch]
        
        results = processor.process_items(items, mock_process_func)
        
        assert len(results) == 10
        assert results == [100, 101, 102, 103, 104, 105, 106, 107, 108, 109]
        assert processor.total_items == 10
        assert processor.processed_items == 10

    def test_batch_processing_with_failures(self):
        """Test handling of failures during batch processing."""
        processor = BatchProcessor(batch_size=2, retry_attempts=1)
        items = [1, 2, 3, 4, 5]
        
        def failing_process_func(batch):
            # Fail on batch containing item 3
            if 3 in batch:
                raise ValueError("Simulated batch failure")
            return [item * 10 for item in batch]
        
        results = processor.process_items(items, failing_process_func)
        
        # Should have some successful results and some failures
        assert len(results) < 5  # Not all items processed successfully
        assert processor.failed_items > 0

    def test_parallel_processing(self):
        """Test parallel processing with multiple workers."""
        processor = BatchProcessor(batch_size=2, max_workers=3)
        items = list(range(12))
        
        def slow_process_func(batch):
            time.sleep(0.01)  # Small delay to simulate work
            return [item + 1000 for item in batch]
        
        start_time = time.time()
        results = processor.process_items(items, slow_process_func)
        end_time = time.time()
        
        assert len(results) == 12
        # Parallel processing should be faster than sequential
        assert end_time - start_time < 0.1  # Should complete quickly with parallelization

    def test_progress_callback_integration(self):
        """Test integration with progress tracking callbacks."""
        processor = BatchProcessor(batch_size=2)
        items = list(range(6))
        progress_updates = []
        
        def progress_callback(total, processed, failed):
            progress_updates.append((total, processed, failed))
        
        processor.add_progress_callback(progress_callback)
        
        def mock_process_func(batch):
            return [item + 1 for item in batch]
        
        processor.process_items(items, mock_process_func)
        
        # Should have received progress updates
        assert len(progress_updates) > 0
        final_update = progress_updates[-1]
        assert final_update[0] == 6  # total
        assert final_update[1] == 6  # processed

    def test_batch_results_storage(self):
        """Test that batch results are properly stored."""
        processor = BatchProcessor(batch_size=3)
        items = list(range(7))
        
        def mock_process_func(batch):
            return [item * 2 for item in batch]
        
        processor.process_items(items, mock_process_func)
        
        # Should have 3 batches: [0,1,2], [3,4,5], [6]
        assert len(processor.batch_results) == 3
        
        for result in processor.batch_results:
            assert isinstance(result, BatchResult)
            assert result.success is True
            assert result.processed_count > 0
            assert result.failed_count == 0


class TestRetryManager:
    """Test cases for RetryManager component."""

    def test_retry_manager_initialization(self):
        """Test proper initialization of RetryManager."""
        config = RetryConfig(max_attempts=5, base_delay=2.0)
        manager = RetryManager(config)
        
        assert manager.config.max_attempts == 5
        assert manager.config.base_delay == 2.0

    def test_successful_operation_no_retry(self):
        """Test that successful operations don't trigger retries."""
        manager = RetryManager()
        call_count = 0
        
        def successful_operation():
            nonlocal call_count
            call_count += 1
            return "success"
        
        result = manager.execute_with_retry(successful_operation)
        
        assert result == "success"
        assert call_count == 1

    @patch('time.sleep')
    def test_retry_on_exception(self, mock_sleep):
        """Test retry behavior when exceptions occur."""
        config = RetryConfig(max_attempts=3, base_delay=1.0)
        manager = RetryManager(config)
        call_count = 0
        
        def failing_operation():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("Temporary failure")
            return "success"
        
        result = manager.execute_with_retry(failing_operation)
        
        assert result == "success"
        assert call_count == 3
        assert mock_sleep.call_count == 2  # Slept twice between 3 attempts

    @patch('time.sleep')
    def test_exponential_backoff_delays(self, mock_sleep):
        """Test that exponential backoff produces increasing delays."""
        config = RetryConfig(
            max_attempts=4, 
            base_delay=1.0, 
            strategy=RetryStrategy.EXPONENTIAL_BACKOFF,
            jitter=False  # Disable jitter for predictable testing
        )
        manager = RetryManager(config)
        
        def always_failing_operation():
            raise ValueError("Always fails")
        
        with pytest.raises(ValueError):
            manager.execute_with_retry(always_failing_operation)
        
        # Should have called sleep 3 times with exponential delays
        expected_delays = [1.0, 2.0, 4.0]
        actual_delays = [call[0][0] for call in mock_sleep.call_args_list]
        assert actual_delays == expected_delays

    @patch('time.sleep')
    def test_linear_backoff_delays(self, mock_sleep):
        """Test linear backoff strategy."""
        config = RetryConfig(
            max_attempts=4,
            base_delay=2.0,
            strategy=RetryStrategy.LINEAR_BACKOFF,
            jitter=False
        )
        manager = RetryManager(config)
        
        def always_failing_operation():
            raise ValueError("Always fails")
        
        with pytest.raises(ValueError):
            manager.execute_with_retry(always_failing_operation)
        
        # Linear backoff: base_delay * attempt_number
        expected_delays = [2.0, 4.0, 6.0]
        actual_delays = [call[0][0] for call in mock_sleep.call_args_list]
        assert actual_delays == expected_delays

    @patch('time.sleep')
    def test_fibonacci_backoff_delays(self, mock_sleep):
        """Test Fibonacci backoff strategy."""
        config = RetryConfig(
            max_attempts=5,
            base_delay=1.0,
            strategy=RetryStrategy.FIBONACCI_BACKOFF,
            jitter=False
        )
        manager = RetryManager(config)
        
        def always_failing_operation():
            raise ValueError("Always fails")
        
        with pytest.raises(ValueError):
            manager.execute_with_retry(always_failing_operation)
        
        # Fibonacci sequence: 1, 1, 2, 3, 5...
        expected_delays = [1.0, 1.0, 2.0, 3.0]
        actual_delays = [call[0][0] for call in mock_sleep.call_args_list]
        assert actual_delays == expected_delays

    def test_non_retryable_exceptions(self):
        """Test that non-retryable exceptions are not retried."""
        config = RetryConfig(
            max_attempts=3,
            non_retryable_exceptions=(KeyboardInterrupt, SystemExit)
        )
        manager = RetryManager(config)
        call_count = 0
        
        def operation_with_non_retryable_exception():
            nonlocal call_count
            call_count += 1
            raise KeyboardInterrupt("Should not retry")
        
        with pytest.raises(KeyboardInterrupt):
            manager.execute_with_retry(operation_with_non_retryable_exception)
        
        assert call_count == 1  # Should not have retried

    def test_max_delay_limit(self):
        """Test that delays are capped at max_delay."""
        config = RetryConfig(
            max_attempts=10,
            base_delay=1.0,
            max_delay=5.0,
            strategy=RetryStrategy.EXPONENTIAL_BACKOFF,
            jitter=False
        )
        manager = RetryManager(config)
        
        # Test internal delay calculation
        delay = manager._calculate_delay(8)  # Large attempt number
        assert delay <= 5.0

    @patch('random.uniform')
    @patch('time.sleep')
    def test_jitter_application(self, mock_sleep, mock_random):
        """Test that jitter is properly applied to delays."""
        mock_random.return_value = 0.05  # 5% jitter
        
        config = RetryConfig(
            max_attempts=3,
            base_delay=2.0,
            jitter=True,
            jitter_range=0.1
        )
        manager = RetryManager(config)
        
        def failing_operation():
            raise ValueError("Failure")
        
        with pytest.raises(ValueError):
            manager.execute_with_retry(failing_operation)
        
        # Check that jitter was applied
        mock_random.assert_called()
        assert mock_sleep.call_count == 2


class TestEnhancedRateLimiter:
    """Test cases for EnhancedRateLimiter component."""

    def test_rate_limiter_initialization(self):
        """Test proper initialization of EnhancedRateLimiter."""
        config = RateLimitConfig(
            max_requests=100,
            time_window=60.0,
            strategy=RateLimitStrategy.TOKEN_BUCKET
        )
        limiter = EnhancedRateLimiter(config)
        
        assert limiter.config.max_requests == 100
        assert limiter.config.time_window == 60.0
        assert limiter._tokens == 100

    @patch('time.time')
    def test_token_bucket_allow_requests(self, mock_time):
        """Test token bucket allows requests when tokens available."""
        mock_time.return_value = 1000.0
        
        config = RateLimitConfig(max_requests=5, time_window=10.0)
        limiter = EnhancedRateLimiter(config)
        
        # Should allow requests when tokens available
        for i in range(5):
            assert limiter.acquire() is True
        
        # Should deny when no tokens left
        assert limiter.acquire() is False

    @patch('time.time')
    def test_token_bucket_refill(self, mock_time):
        """Test token bucket refills tokens over time."""
        config = RateLimitConfig(max_requests=10, time_window=10.0)
        limiter = EnhancedRateLimiter(config)
        
        # Start at time 1000, consume all tokens
        mock_time.return_value = 1000.0
        for _ in range(10):
            limiter.acquire()
        
        # Move forward 5 seconds (half window), should refill 5 tokens
        mock_time.return_value = 1005.0
        limiter._refill_tokens()
        
        # Should now allow 5 more requests
        allowed_count = 0
        for _ in range(10):
            if limiter.acquire():
                allowed_count += 1
        
        assert allowed_count == 5

    @patch('time.time')
    def test_sliding_window_rate_limiting(self, mock_time):
        """Test sliding window strategy."""
        config = RateLimitConfig(
            max_requests=3,
            time_window=10.0,
            strategy=RateLimitStrategy.SLIDING_WINDOW
        )
        limiter = EnhancedRateLimiter(config)
        
        # Allow 3 requests at time 1000
        mock_time.return_value = 1000.0
        for _ in range(3):
            assert limiter.acquire() is True
        
        # Should deny 4th request
        assert limiter.acquire() is False
        
        # Move forward 11 seconds, window should reset
        mock_time.return_value = 1011.0
        assert limiter.acquire() is True

    @patch('time.time')
    def test_adaptive_rate_limiting(self, mock_time):
        """Test adaptive rate limiting adjusts based on success/failure."""
        config = RateLimitConfig(
            max_requests=10,
            time_window=10.0,
            strategy=RateLimitStrategy.ADAPTIVE
        )
        limiter = EnhancedRateLimiter(config)
        mock_time.return_value = 1000.0
        
        # Record some failures
        for _ in range(5):
            limiter.record_failure()
        
        # Rate should be reduced
        assert limiter._current_max_requests < 10
        
        # Record successes to increase rate
        for _ in range(10):
            limiter.record_success()
        
        # Rate should increase but not exceed original max
        assert limiter._current_max_requests <= 10

    def test_burst_handling(self):
        """Test burst handling functionality."""
        config = RateLimitConfig(
            max_requests=5,
            time_window=10.0,
            burst_size=3
        )
        limiter = EnhancedRateLimiter(config)
        
        # Consume all regular tokens
        for _ in range(5):
            limiter.acquire()
        
        # Should still allow burst requests
        for _ in range(3):
            assert limiter.acquire() is True
        
        # Should deny after burst is exhausted
        assert limiter.acquire() is False

    def test_thread_safety(self):
        """Test that rate limiter is thread-safe."""
        config = RateLimitConfig(max_requests=100, time_window=1.0)
        limiter = EnhancedRateLimiter(config)
        
        results = []
        
        def worker():
            for _ in range(10):
                result = limiter.acquire()
                results.append(result)
        
        threads = []
        for _ in range(5):
            thread = threading.Thread(target=worker)
            threads.append(thread)
            thread.start()
        
        for thread in threads:
            thread.join()
        
        # Should have some successful acquisitions
        successful = sum(1 for r in results if r)
        assert successful > 0
        assert successful <= 100  # Should not exceed limit

    def test_global_rate_limiter_manager(self):
        """Test GlobalRateLimiterManager functionality."""
        manager = GlobalRateLimiterManager()
        
        config = RateLimitConfig(max_requests=10, time_window=1.0)
        limiter = manager.get_limiter("test_api", config)
        
        # Should return same instance for same name
        limiter2 = manager.get_limiter("test_api", config)
        assert limiter is limiter2
        
        # Should return different instance for different name
        limiter3 = manager.get_limiter("other_api", config)
        assert limiter is not limiter3

    def test_rate_limited_decorator(self):
        """Test rate_limited decorator functionality."""
        config = RateLimitConfig(max_requests=2, time_window=1.0)
        
        @rate_limited("test_decorator", config)
        def test_function():
            return "success"
        
        # First two calls should succeed
        assert test_function() == "success"
        assert test_function() == "success"
        
        # Third call should be rate limited
        with pytest.raises(Exception):  # Should raise rate limit exception
            test_function()


class TestProgressTracker:
    """Test cases for ProgressTracker component."""

    def test_progress_tracker_initialization(self):
        """Test proper initialization of ProgressTracker."""
        tracker = ProgressTracker(
            operation_name="Test Operation",
            enable_console_output=False
        )
        
        assert tracker.operation_name == "Test Operation"
        assert tracker.total_items == 0
        assert tracker.processed_items == 0
        assert tracker.failed_items == 0

    def test_start_stop_tracking(self):
        """Test starting and stopping progress tracking."""
        tracker = ProgressTracker(enable_console_output=False)
        
        tracker.start(total_items=100)
        assert tracker.total_items == 100
        assert tracker.is_active is True
        
        tracker.stop()
        assert tracker.is_active is False

    def test_update_progress(self):
        """Test updating progress increments."""
        tracker = ProgressTracker(enable_console_output=False)
        tracker.start(total_items=10)
        
        tracker.update_progress(processed=3)
        assert tracker.processed_items == 3
        
        tracker.update_progress(processed=2, failed=1)
        assert tracker.processed_items == 5
        assert tracker.failed_items == 1

    def test_eta_calculation(self):
        """Test ETA calculation accuracy."""
        tracker = ProgressTracker(enable_console_output=False)
        tracker.start(total_items=100)
        
        # Simulate some progress
        tracker.update_progress(processed=25)
        time.sleep(0.1)  # Small delay to establish rate
        
        eta = tracker.get_eta()
        assert eta is None or eta > 0  # ETA should be positive or None if insufficient data

    def test_progress_percentage(self):
        """Test progress percentage calculation."""
        tracker = ProgressTracker(enable_console_output=False)
        tracker.start(total_items=50)
        
        tracker.update_progress(processed=25)
        assert tracker.get_progress_percentage() == 50.0
        
        tracker.update_progress(processed=25)  # Total 50
        assert tracker.get_progress_percentage() == 100.0

    def test_stage_management(self):
        """Test progress stage management."""
        tracker = ProgressTracker(enable_console_output=False)
        tracker.start(total_items=100)
        
        tracker.set_stage(ProgressStage.PREPARING)
        assert tracker.current_stage == ProgressStage.PREPARING
        
        tracker.set_stage(ProgressStage.PROCESSING)
        assert tracker.current_stage == ProgressStage.PROCESSING

    def test_zero_total_items_handling(self):
        """Test handling of zero total items."""
        tracker = ProgressTracker(enable_console_output=False)
        tracker.start(total_items=0)
        
        # Should handle gracefully
        percentage = tracker.get_progress_percentage()
        assert percentage == 100.0  # 0/0 should be treated as complete

    def test_callback_notifications(self):
        """Test progress callback notifications."""
        tracker = ProgressTracker(enable_console_output=False)
        callback_data = []
        
        def progress_callback(processed, total, failed, percentage):
            callback_data.append((processed, total, failed, percentage))
        
        tracker.add_callback(progress_callback)
        tracker.start(total_items=10)
        tracker.update_progress(processed=5)
        
        assert len(callback_data) > 0
        assert callback_data[-1] == (5, 10, 0, 50.0)


class TestMigrationPerformanceManager:
    """Test cases for MigrationPerformanceManager integration."""

    def test_performance_manager_initialization(self):
        """Test proper initialization of MigrationPerformanceManager."""
        config = PerformanceConfig(
            batch_size=50,
            max_concurrent_batches=3,
            enable_rate_limiting=True
        )
        manager = MigrationPerformanceManager(config)
        
        assert manager.config.batch_size == 50
        assert manager.config.max_concurrent_batches == 3
        assert manager.batch_processor is not None
        assert manager.rate_limiter is not None
        assert manager.retry_manager is not None
        assert manager.progress_tracker is not None

    @pytest.mark.asyncio
    async def test_process_migration_batch_success(self):
        """Test successful batch processing."""
        manager = MigrationPerformanceManager()
        
        async def mock_migration_func(items):
            return [f"migrated_{item}" for item in items]
        
        items = [1, 2, 3, 4, 5]
        results = await manager.process_migration_batch(items, mock_migration_func)
        
        assert len(results) == 5
        assert all("migrated_" in str(result) for result in results)

    @pytest.mark.asyncio
    async def test_process_migration_batch_with_failures(self):
        """Test batch processing with some failures."""
        manager = MigrationPerformanceManager()
        
        async def partially_failing_func(items):
            results = []
            for item in items:
                if item == 3:
                    raise ValueError(f"Failed to process {item}")
                results.append(f"migrated_{item}")
            return results
        
        items = [1, 2, 3, 4, 5]
        results = await manager.process_migration_batch(items, partially_failing_func)
        
        # Should handle failures gracefully
        assert len(results) < 5  # Some items failed
        assert manager.metrics.failed_operations > 0

    def test_json_streaming_functionality(self):
        """Test JSON file streaming for memory efficiency."""
        import json
        import tempfile
        import os
        
        # Create temporary JSON file
        test_data = [{"id": i, "name": f"item_{i}"} for i in range(100)]
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(test_data, f)
            temp_file = f.name
        
        try:
            manager = MigrationPerformanceManager()
            
            def mock_processor(items):
                return len(items)  # Just count items
            
            results = list(manager.process_json_file_streaming(temp_file, mock_processor))
            
            # Should have processed all items in batches
            total_processed = sum(results)
            assert total_processed > 0
            
        finally:
            os.unlink(temp_file)

    def test_performance_metrics_collection(self):
        """Test that performance metrics are properly collected."""
        manager = MigrationPerformanceManager()
        
        # Simulate some operations
        manager.metrics.total_operations = 100
        manager.metrics.successful_operations = 95
        manager.metrics.failed_operations = 5
        manager.metrics.total_processing_time = 10.5
        
        metrics_dict = manager.get_performance_metrics()
        
        assert metrics_dict['total_operations'] == 100
        assert metrics_dict['successful_operations'] == 95
        assert metrics_dict['failed_operations'] == 5
        assert metrics_dict['success_rate'] == 95.0
        assert 'average_processing_time' in metrics_dict

    def test_resource_cleanup(self):
        """Test proper resource cleanup."""
        manager = MigrationPerformanceManager()
        
        # Ensure cleanup doesn't raise exceptions
        manager.cleanup()
        
        # Should be able to call cleanup multiple times
        manager.cleanup()

    @pytest.mark.asyncio
    async def test_concurrent_batch_processing(self):
        """Test concurrent processing of multiple batches."""
        config = PerformanceConfig(
            batch_size=10,
            max_concurrent_batches=3
        )
        manager = MigrationPerformanceManager(config)
        
        async def slow_migration_func(items):
            await asyncio.sleep(0.01)  # Simulate slow operation
            return [f"processed_{item}" for item in items]
        
        items = list(range(50))  # 5 batches of 10 items each
        
        start_time = time.time()
        results = await manager._process_with_batching(items, slow_migration_func)
        end_time = time.time()
        
        assert len(results) == 50
        # Concurrent processing should be faster than sequential
        assert end_time - start_time < 0.5  # Should complete quickly

    def test_adaptive_performance_tuning(self):
        """Test adaptive performance tuning based on system load."""
        manager = MigrationPerformanceManager()
        
        # Simulate high failure rate
        manager.metrics.failed_operations = 50
        manager.metrics.total_operations = 100
        
        # Should adapt to reduce load
        manager._adapt_performance_settings()
        
        # Check that some settings were adapted (implementation specific)
        assert manager.config.batch_size > 0  # Should still be valid


# Integration tests that test multiple components working together
class TestPerformanceIntegration:
    """Integration tests for performance components working together."""

    @pytest.mark.asyncio
    async def test_full_migration_pipeline(self):
        """Test complete migration pipeline with all performance features."""
        config = PerformanceConfig(
            batch_size=5,
            max_concurrent_batches=2,
            enable_rate_limiting=True,
            enable_progress_tracking=True
        )
        manager = MigrationPerformanceManager(config)
        
        # Mock migration function that simulates API calls
        call_count = 0
        async def mock_api_migration(items):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)  # Simulate API delay
            return [f"api_result_{item}" for item in items]
        
        items = list(range(20))  # Should create 4 batches of 5 items each
        
        results = await manager.process_migration_batch(items, mock_api_migration)
        
        assert len(results) == 20
        assert call_count > 0  # Should have made API calls
        assert manager.metrics.successful_operations > 0

    def test_rate_limiter_with_retry_integration(self):
        """Test rate limiter working with retry manager."""
        rate_config = RateLimitConfig(max_requests=2, time_window=1.0)
        rate_limiter = EnhancedRateLimiter(rate_config)
        
        retry_config = RetryConfig(max_attempts=5, base_delay=0.1)
        retry_manager = RetryManager(retry_config)
        
        call_count = 0
        
        def rate_limited_operation():
            nonlocal call_count
            call_count += 1
            
            if not rate_limiter.acquire():
                raise Exception("Rate limited")
            
            if call_count <= 2:
                raise ValueError("Temporary failure")
            
            return "success"
        
        # Should eventually succeed after retries and rate limiting
        result = retry_manager.execute_with_retry(rate_limited_operation)
        assert result == "success"
        assert call_count >= 3

    def test_progress_tracking_with_batch_processing(self):
        """Test progress tracking integrated with batch processing."""
        processor = BatchProcessor(batch_size=3, enable_progress_tracking=True)
        tracker = ProgressTracker(enable_console_output=False)
        
        # Connect progress tracking
        progress_updates = []
        def track_progress(total, processed, failed):
            progress_updates.append((total, processed, failed))
        
        processor.add_progress_callback(track_progress)
        
        items = list(range(10))
        
        def mock_process_func(batch):
            return [item + 100 for item in batch]
        
        results = processor.process_items(items, mock_process_func)
        
        assert len(results) == 10
        assert len(progress_updates) > 0
        # Final update should show all items processed
        assert progress_updates[-1][0] == 10  # total
        assert progress_updates[-1][1] == 10  # processed


# Performance and load tests
class TestPerformanceLoad:
    """Performance and load testing for the optimization components."""

    def test_large_dataset_processing(self):
        """Test processing of large datasets."""
        processor = BatchProcessor(batch_size=100, max_workers=4)
        large_dataset = list(range(10000))
        
        def simple_process_func(batch):
            return [item + 1 for item in batch]
        
        start_time = time.time()
        results = processor.process_items(large_dataset, simple_process_func)
        end_time = time.time()
        
        assert len(results) == 10000
        assert end_time - start_time < 5.0  # Should complete within 5 seconds
        assert processor.processed_items == 10000

    def test_high_concurrency_rate_limiting(self):
        """Test rate limiter under high concurrency."""
        config = RateLimitConfig(max_requests=100, time_window=1.0)
        limiter = EnhancedRateLimiter(config)
        
        successful_acquisitions = 0
        
        def worker():
            nonlocal successful_acquisitions
            for _ in range(20):
                if limiter.acquire():
                    successful_acquisitions += 1
        
        threads = []
        for _ in range(10):  # 10 threads, 20 requests each = 200 total
            thread = threading.Thread(target=worker)
            threads.append(thread)
            thread.start()
        
        for thread in threads:
            thread.join()
        
        # Should have limited to approximately the configured rate
        assert successful_acquisitions <= 120  # Some tolerance for timing
        assert successful_acquisitions >= 80   # Should allow reasonable throughput

    @pytest.mark.asyncio
    async def test_memory_efficiency_streaming(self):
        """Test memory efficiency of streaming operations."""
        import json
        import tempfile
        import os
        import psutil
        
        # Create large JSON file
        large_dataset = [{"id": i, "data": f"item_{i}" * 100} for i in range(1000)]
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(large_dataset, f)
            temp_file = f.name
        
        try:
            manager = MigrationPerformanceManager()
            
            process = psutil.Process()
            initial_memory = process.memory_info().rss
            
            def memory_efficient_processor(items):
                # Simple processing that doesn't accumulate data
                return len(items)
            
            results = list(manager.process_json_file_streaming(temp_file, memory_efficient_processor))
            
            final_memory = process.memory_info().rss
            memory_increase = final_memory - initial_memory
            
            # Memory increase should be reasonable (less than 100MB)
            assert memory_increase < 100 * 1024 * 1024
            assert sum(results) > 0  # Should have processed items
            
        finally:
            os.unlink(temp_file)


if __name__ == "__main__":
    pytest.main([__file__, "-v"]) 