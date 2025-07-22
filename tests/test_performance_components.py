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
from unittest.mock import Mock, patch, AsyncMock, call, MagicMock
from collections import deque
from dataclasses import dataclass
from typing import List, Any, Optional
import random
from pathlib import Path

# Import the components we're testing
from src.utils.batch_processor import BatchProcessor, BatchResult
from src.utils.retry_manager import RetryManager, RetryConfig, RetryStrategy
from src.utils.enhanced_rate_limiter import (
    EnhancedRateLimiter, RateLimitConfig, RateLimitStrategy,
    GlobalRateLimiterManager, global_rate_limiter_manager
)
from src.utils.progress_tracker import ProgressTracker, ProgressStage
from src.performance.migration_performance_manager import (
    MigrationPerformanceManager, PerformanceConfig, MigrationMetrics
)


class FakeTime:
    """Controllable fake time for deterministic testing."""
    def __init__(self, initial_time=0.0):
        self._time = initial_time
        
    def time(self):
        return self._time
        
    def sleep(self, seconds):
        self._time += seconds
        
    def advance(self, seconds):
        self._time += seconds


@pytest.fixture
def fake_time():
    """Fixture providing controllable time for tests."""
    return FakeTime()


@pytest.fixture
def mock_time(monkeypatch, fake_time):
    """Fixture that patches time.time and time.sleep with fake_time."""
    monkeypatch.setattr(time, "time", fake_time.time)
    monkeypatch.setattr(time, "sleep", fake_time.sleep)
    return fake_time


class TestBatchProcessor:
    """Test cases for BatchProcessor component."""
    
    def test_initialization(self):
        """Test BatchProcessor initialization with various configurations."""
        # Test default initialization
        processor = BatchProcessor()
        assert processor.batch_size == 100
        assert processor.max_workers == 4
        assert processor.retry_attempts == 3
        
        # Test custom initialization
        processor = BatchProcessor(
            batch_size=50,
            max_workers=2,
            retry_attempts=5
        )
        assert processor.batch_size == 50
        assert processor.max_workers == 2
        assert processor.retry_attempts == 5
    
    def test_create_batches(self):
        """Test batch creation logic."""
        processor = BatchProcessor(batch_size=3)
        items = list(range(10))  # [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
        
        batches = processor._create_batches(items)
        
        assert len(batches) == 4  # 3, 3, 3, 1
        assert batches[0] == [0, 1, 2]
        assert batches[1] == [3, 4, 5]
        assert batches[2] == [6, 7, 8]
        assert batches[3] == [9]
    
    def test_create_batches_empty_input(self):
        """Test batch creation with empty input."""
        processor = BatchProcessor(batch_size=3)
        batches = processor._create_batches([])
        assert batches == []
    
    def test_process_parallel_success(self):
        """Test successful parallel processing."""
        processor = BatchProcessor(batch_size=2, max_workers=2)
        
        def simple_processor(batch):
            return [x * 2 for x in batch]
        
        items = [1, 2, 3, 4, 5]
        result = processor.process_parallel(items, simple_processor)
        
        assert result["success"] is True
        assert result["total_items"] == 5
        assert result["processed_items"] == 5
        assert result["failed_items"] == 0
        assert sorted(result["data"]) == [2, 4, 6, 8, 10]
    
    def test_process_parallel_with_failures(self):
        """Test parallel processing with some batch failures."""
        processor = BatchProcessor(batch_size=2, max_workers=2, retry_attempts=1)
        
        call_count = 0
        def failing_processor(batch):
            nonlocal call_count
            call_count += 1
            if batch[0] == 3:  # Fail for batch starting with 3
                raise ValueError("Simulated failure")
            return [x * 2 for x in batch]
        
        items = [1, 2, 3, 4, 5]
        result = processor.process_parallel(items, failing_processor)
        
        assert result["success"] is False
        assert result["failed_items"] > 0
        assert len(result["errors"]) > 0
    
    def test_process_sequential_success(self):
        """Test successful sequential processing."""
        processor = BatchProcessor(batch_size=2)
        
        def simple_processor(batch):
            return [x * 3 for x in batch]
        
        items = [1, 2, 3, 4]
        result = processor.process_sequential(items, simple_processor)
        
        assert result["success"] is True
        assert result["total_items"] == 4
        assert result["processed_items"] == 4
        assert result["failed_items"] == 0
        assert result["data"] == [3, 6, 9, 12]
    
    def test_process_batch_with_retry_success_after_retry(self, mock_time):
        """Test batch processing succeeds after initial failure."""
        processor = BatchProcessor(retry_attempts=2)
        
        call_count = 0
        def flaky_processor(batch):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("First attempt fails")
            return [x * 2 for x in batch]
        
        batch = [1, 2, 3]
        result = processor._process_batch_with_retry(batch, 0, flaky_processor)
        
        assert result.success is True
        assert result.data == [2, 4, 6]
        assert call_count == 2
    
    def test_process_batch_with_retry_all_attempts_fail(self, mock_time):
        """Test batch processing fails after all retry attempts."""
        processor = BatchProcessor(retry_attempts=2)
        
        def always_failing_processor(batch):
            raise RuntimeError("Always fails")
        
        batch = [1, 2, 3]
        result = processor._process_batch_with_retry(batch, 0, always_failing_processor)
        
        assert result.success is False
        assert result.failed_count == 3
        assert "Always fails" in result.errors[0]
    
    def test_progress_callbacks(self):
        """Test progress callback functionality."""
        processor = BatchProcessor(batch_size=2)
        
        progress_updates = []
        def progress_callback(processed, failed, total):
            progress_updates.append((processed, failed, total))
        
        processor.add_progress_callback(progress_callback)
        
        def simple_processor(batch):
            return [x * 2 for x in batch]
        
        items = [1, 2, 3, 4]
        processor.process_parallel(items, simple_processor)
        
        # Should have received progress updates
        assert len(progress_updates) > 0
        final_update = progress_updates[-1]
        assert final_update[2] == 4  # total items


class TestRetryManager:
    """Test cases for RetryManager component."""
    
    def test_initialization_with_defaults(self):
        """Test RetryManager initialization with default config."""
        manager = RetryManager()
        assert manager.config.max_attempts == 3
        assert manager.config.base_delay == 1.0
        assert manager.config.strategy == RetryStrategy.EXPONENTIAL_BACKOFF
    
    def test_initialization_with_custom_config(self):
        """Test RetryManager initialization with custom config."""
        config = RetryConfig(
            max_attempts=5,
            base_delay=0.5,
            strategy=RetryStrategy.LINEAR_BACKOFF,
            jitter=False
        )
        manager = RetryManager(config)
        assert manager.config.max_attempts == 5
        assert manager.config.base_delay == 0.5
        assert manager.config.strategy == RetryStrategy.LINEAR_BACKOFF
        assert manager.config.jitter is False
    
    def test_calculate_delay_exponential_backoff(self):
        """Test exponential backoff delay calculation."""
        config = RetryConfig(base_delay=1.0, strategy=RetryStrategy.EXPONENTIAL_BACKOFF, jitter=False)
        manager = RetryManager(config)
        
        assert manager._calculate_delay(0) == 1.0  # 1.0 * 2^0
        assert manager._calculate_delay(1) == 2.0  # 1.0 * 2^1
        assert manager._calculate_delay(2) == 4.0  # 1.0 * 2^2
    
    def test_calculate_delay_linear_backoff(self):
        """Test linear backoff delay calculation."""
        config = RetryConfig(base_delay=1.0, strategy=RetryStrategy.LINEAR_BACKOFF, jitter=False)
        manager = RetryManager(config)
        
        assert manager._calculate_delay(0) == 1.0  # 1.0 * (0 + 1)
        assert manager._calculate_delay(1) == 2.0  # 1.0 * (1 + 1)
        assert manager._calculate_delay(2) == 3.0  # 1.0 * (2 + 1)
    
    def test_calculate_delay_fixed_delay(self):
        """Test fixed delay calculation."""
        config = RetryConfig(base_delay=2.5, strategy=RetryStrategy.FIXED_DELAY, jitter=False)
        manager = RetryManager(config)
        
        assert manager._calculate_delay(0) == 2.5
        assert manager._calculate_delay(1) == 2.5
        assert manager._calculate_delay(2) == 2.5
    
    def test_calculate_delay_fibonacci_backoff(self):
        """Test fibonacci backoff delay calculation."""
        config = RetryConfig(base_delay=1.0, strategy=RetryStrategy.FIBONACCI_BACKOFF, jitter=False)
        manager = RetryManager(config)
        
        assert manager._calculate_delay(0) == 1.0  # base_delay * 1
        assert manager._calculate_delay(1) == 1.0  # base_delay * 1
        assert manager._calculate_delay(2) == 2.0  # base_delay * 2
        assert manager._calculate_delay(3) == 3.0  # base_delay * 3
        assert manager._calculate_delay(4) == 5.0  # base_delay * 5
    
    def test_calculate_delay_with_jitter(self):
        """Test delay calculation with jitter."""
        config = RetryConfig(base_delay=10.0, jitter=True, jitter_range=0.2)
        manager = RetryManager(config)
        
        # With jitter, delay should vary around the base value
        delays = [manager._calculate_delay(0) for _ in range(10)]
        assert all(8.0 <= delay <= 12.0 for delay in delays)  # ±20% of 10.0
        assert len(set(delays)) > 1  # Should have some variation
    
    def test_calculate_delay_max_delay_cap(self):
        """Test that delays are capped at max_delay."""
        config = RetryConfig(base_delay=1.0, max_delay=5.0, jitter=False)
        manager = RetryManager(config)
        
        # High attempt should be capped
        assert manager._calculate_delay(10) == 5.0
    
    def test_should_retry_within_attempts_limit(self):
        """Test retry decision within attempt limits."""
        manager = RetryManager(RetryConfig(max_attempts=3))
        
        # Should retry within limits
        assert manager._should_retry(ValueError("test"), None, 0) is True
        assert manager._should_retry(ValueError("test"), None, 1) is True
        assert manager._should_retry(ValueError("test"), None, 2) is True
        
        # Should not retry beyond limits
        assert manager._should_retry(ValueError("test"), None, 3) is False
    
    def test_should_retry_non_retryable_exceptions(self):
        """Test retry decision with non-retryable exceptions."""
        config = RetryConfig(non_retryable_exceptions=(KeyboardInterrupt, SystemExit))
        manager = RetryManager(config)
        
        # Should not retry non-retryable exceptions
        assert manager._should_retry(KeyboardInterrupt(), None, 0) is False
        assert manager._should_retry(SystemExit(), None, 0) is False
        
        # Should retry other exceptions
        assert manager._should_retry(ValueError("test"), None, 0) is True
    
    def test_should_retry_retryable_exceptions(self):
        """Test retry decision with specific retryable exceptions."""
        config = RetryConfig(retryable_exceptions=(ValueError, TypeError))
        manager = RetryManager(config)
        
        # Should retry specified exceptions
        assert manager._should_retry(ValueError("test"), None, 0) is True
        assert manager._should_retry(TypeError("test"), None, 0) is True
        
        # Should not retry unspecified exceptions
        assert manager._should_retry(RuntimeError("test"), None, 0) is False
    
    def test_execute_with_retry_success_first_attempt(self, mock_time):
        """Test successful execution on first attempt."""
        manager = RetryManager()
        
        def successful_func():
            return "success"
        
        result = manager.execute_with_retry(successful_func)
        
        assert result.success is True
        assert result.result == "success"
        assert result.attempts_made == 1
        assert result.total_delay == 0.0
    
    def test_execute_with_retry_success_after_retries(self, mock_time):
        """Test successful execution after retries."""
        manager = RetryManager(RetryConfig(max_attempts=3, base_delay=1.0, jitter=False))
        
        call_count = 0
        def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("Not ready yet")
            return "success"
        
        result = manager.execute_with_retry(flaky_func)
        
        assert result.success is True
        assert result.result == "success"
        assert result.attempts_made == 3
        assert result.total_delay == 3.0  # 1.0 + 2.0 delays
    
    def test_execute_with_retry_all_attempts_fail(self, mock_time):
        """Test execution fails after all attempts."""
        manager = RetryManager(RetryConfig(max_attempts=2, base_delay=1.0, jitter=False))
        
        def always_failing_func():
            raise ValueError("Always fails")
        
        result = manager.execute_with_retry(always_failing_func)
        
        assert result.success is False
        assert result.exception is not None
        assert "Always fails" in str(result.exception)
        assert result.attempts_made == 2
        assert result.total_delay == 1.0  # Only one delay between attempts
    
    def test_execute_with_retry_with_result_condition(self, mock_time):
        """Test retry based on result condition."""
        config = RetryConfig(
            max_attempts=3,
            retry_on_result=lambda x: x == "retry",
            base_delay=1.0,
            jitter=False
        )
        manager = RetryManager(config)
        
        call_count = 0
        def result_based_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return "retry"
            return "success"
        
        result = manager.execute_with_retry(result_based_func)
        
        assert result.success is True
        assert result.result == "success"
        assert result.attempts_made == 3


class TestEnhancedRateLimiter:
    """Test cases for EnhancedRateLimiter component."""
    
    def test_initialization(self):
        """Test EnhancedRateLimiter initialization."""
        config = RateLimitConfig(
            max_requests=10,
            time_window=60.0,
            strategy=RateLimitStrategy.TOKEN_BUCKET
        )
        limiter = EnhancedRateLimiter(config)
        
        assert limiter.config.max_requests == 10
        assert limiter.config.time_window == 60.0
        assert limiter.config.strategy == RateLimitStrategy.TOKEN_BUCKET
        assert limiter._tokens == 10  # Should start with full tokens
    
    def test_acquire_token_bucket_success(self):
        """Test successful token acquisition in token bucket mode."""
        config = RateLimitConfig(max_requests=5, strategy=RateLimitStrategy.TOKEN_BUCKET)
        limiter = EnhancedRateLimiter(config)
        
        # Should succeed with available tokens
        assert limiter.acquire(1) is True
        assert limiter._tokens == 4
        
        assert limiter.acquire(2) is True
        assert limiter._tokens == 2
    
    def test_acquire_token_bucket_insufficient_tokens(self):
        """Test token acquisition failure when insufficient tokens."""
        config = RateLimitConfig(max_requests=3, strategy=RateLimitStrategy.TOKEN_BUCKET)
        limiter = EnhancedRateLimiter(config)
        
        # Consume all tokens
        assert limiter.acquire(3) is True
        assert limiter._tokens == 0
        
        # Should fail when no tokens left
        assert limiter.acquire(1) is False
    
    def test_token_bucket_refill(self, mock_time):
        """Test token bucket refill over time."""
        config = RateLimitConfig(
            max_requests=10,
            time_window=10.0,  # 1 token per second
            strategy=RateLimitStrategy.TOKEN_BUCKET
        )
        limiter = EnhancedRateLimiter(config)
        
        # Consume all tokens
        assert limiter.acquire(10) is True
        assert limiter._tokens == 0
        
        # Advance time by 3 seconds
        mock_time.advance(3.0)
        
        # Try to acquire - should trigger refill
        assert limiter.acquire(1) is True
        assert limiter._tokens == 2  # 3 tokens refilled, 1 consumed
    
    def test_sliding_window_strategy(self, mock_time):
        """Test sliding window rate limiting strategy."""
        config = RateLimitConfig(
            max_requests=3,
            time_window=10.0,
            strategy=RateLimitStrategy.SLIDING_WINDOW
        )
        limiter = EnhancedRateLimiter(config)
        
        # Make requests
        assert limiter.acquire(1) is True
        mock_time.advance(1.0)
        assert limiter.acquire(1) is True
        mock_time.advance(1.0)
        assert limiter.acquire(1) is True
        
        # Should fail - 3 requests in window
        assert limiter.acquire(1) is False
        
        # Advance past first request
        mock_time.advance(8.0)  # Total 10 seconds from first request
        
        # Should succeed now
        assert limiter.acquire(1) is True
    
    def test_wait_if_needed_calculation(self, mock_time):
        """Test wait delay calculation."""
        config = RateLimitConfig(
            max_requests=2,
            time_window=10.0,
            strategy=RateLimitStrategy.TOKEN_BUCKET,
            min_delay=0.1
        )
        limiter = EnhancedRateLimiter(config)
        
        # Consume all tokens
        limiter.acquire(2)
        
        # Calculate wait time
        delay = limiter.wait_if_needed()
        assert delay >= 0.1  # Should be at least min_delay
    
    def test_adaptive_rate_limiting(self, mock_time):
        """Test adaptive rate limiting behavior."""
        config = RateLimitConfig(
            max_requests=10,
            time_window=60.0,
            strategy=RateLimitStrategy.ADAPTIVE,
            backoff_factor=0.5,
            recovery_factor=1.1
        )
        limiter = EnhancedRateLimiter(config)
        
        initial_rate = limiter._current_max_requests
        
        # Simulate failures - should reduce rate
        for _ in range(5):
            limiter.handle_api_response(False)
        
        assert limiter._current_max_requests < initial_rate
        
        # Simulate successes - should gradually recover
        for _ in range(15):  # Need many successes for recovery
            limiter.handle_api_response(True)
        
        assert limiter._current_max_requests > limiter._current_max_requests
    
    def test_burst_handling(self, mock_time):
        """Test burst token handling."""
        config = RateLimitConfig(
            max_requests=5,
            burst_size=3,
            strategy=RateLimitStrategy.TOKEN_BUCKET
        )
        limiter = EnhancedRateLimiter(config)
        
        # Should allow burst requests
        assert limiter.acquire(8) is True  # max_requests + burst_size
        assert limiter._tokens < 0  # In burst mode
    
    def test_thread_safety(self):
        """Test thread safety under concurrent access."""
        config = RateLimitConfig(max_requests=100, strategy=RateLimitStrategy.TOKEN_BUCKET)
        limiter = EnhancedRateLimiter(config)
        
        results = []
        errors = []
        
        def worker():
            try:
                result = limiter.acquire(1)
                results.append(result)
            except Exception as e:
                errors.append(e)
        
        # Run many concurrent workers
        threads = [threading.Thread(target=worker) for _ in range(150)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Should have no errors
        assert len(errors) == 0
        
        # Should have exactly 100 successes (max_requests)
        assert sum(results) == 100
        assert len(results) == 150
    
    def test_get_metrics(self):
        """Test metrics collection."""
        config = RateLimitConfig(max_requests=5, strategy=RateLimitStrategy.TOKEN_BUCKET)
        limiter = EnhancedRateLimiter(config)
        
        # Make some requests
        limiter.acquire(2)
        limiter.acquire(1)
        
        metrics = limiter.get_metrics()
        assert metrics.total_requests == 3
        assert metrics.throttled_requests >= 0


class TestProgressTracker:
    """Test cases for ProgressTracker component."""
    
    def test_initialization(self):
        """Test ProgressTracker initialization."""
        tracker = ProgressTracker(
            operation_name="Test Operation",
            enable_console_output=False,  # Disable for testing
            enable_logging=False,
            enable_file_output=False
        )
        
        assert tracker.operation_name == "Test Operation"
        assert tracker.enable_console_output is False
        assert tracker.enable_logging is False
        assert tracker.enable_file_output is False
    
    def test_start_and_finish(self):
        """Test basic start and finish operations."""
        tracker = ProgressTracker(
            enable_console_output=False,
            enable_logging=False,
            enable_file_output=False
        )
        
        tracker.start(total_items=100)
        assert tracker.metrics.total_items == 100
        assert tracker.metrics.stage == ProgressStage.PROCESSING
        assert tracker.metrics.start_time > 0
        
        tracker.finish(success=True, final_message="Completed")
        assert tracker.metrics.stage == ProgressStage.COMPLETED
    
    def test_update_progress(self):
        """Test progress updates."""
        tracker = ProgressTracker(
            enable_console_output=False,
            enable_logging=False,
            enable_file_output=False
        )
        
        tracker.start(total_items=100)
        
        # Test updates
        tracker.update(processed=10, failed=1)
        assert tracker.metrics.processed_items == 10
        assert tracker.metrics.failed_items == 1
        
        tracker.update(processed=20, failed=2)
        assert tracker.metrics.processed_items == 30  # Cumulative
        assert tracker.metrics.failed_items == 3     # Cumulative
    
    def test_stage_transitions(self):
        """Test progress stage transitions."""
        tracker = ProgressTracker(
            enable_console_output=False,
            enable_logging=False,
            enable_file_output=False
        )
        
        tracker.start(total_items=100, stage=ProgressStage.INITIALIZING)
        assert tracker.metrics.stage == ProgressStage.INITIALIZING
        
        tracker.update(stage=ProgressStage.PROCESSING)
        assert tracker.metrics.stage == ProgressStage.PROCESSING
        
        tracker.update(stage=ProgressStage.FINALIZING)
        assert tracker.metrics.stage == ProgressStage.FINALIZING
    
    def test_callbacks(self):
        """Test progress callback functionality."""
        tracker = ProgressTracker(
            enable_console_output=False,
            enable_logging=False,
            enable_file_output=False
        )
        
        callback_calls = []
        def progress_callback(metrics):
            callback_calls.append((metrics.processed_items, metrics.failed_items))
        
        tracker.add_callback(progress_callback)
        tracker.start(total_items=100)
        
        tracker.update(processed=10)
        tracker.update(processed=20)
        
        # Should have called callback for updates
        assert len(callback_calls) >= 2
    
    def test_thread_safety(self):
        """Test thread safety under concurrent updates."""
        tracker = ProgressTracker(
            enable_console_output=False,
            enable_logging=False,
            enable_file_output=False,
            update_interval=0.01  # Fast updates for testing
        )
        
        tracker.start(total_items=1000)
        
        errors = []
        
        def worker(worker_id):
            try:
                for i in range(10):
                    tracker.update(processed=1)
                    time.sleep(0.001)  # Small delay
            except Exception as e:
                errors.append(e)
        
        # Run concurrent workers
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        tracker.finish(success=True)
        
        # Should have no errors
        assert len(errors) == 0
        
        # Should have processed all items
        assert tracker.metrics.processed_items == 100  # 10 workers * 10 items each
    
    def test_eta_calculation(self):
        """Test ETA calculation accuracy."""
        tracker = ProgressTracker(
            enable_console_output=False,
            enable_logging=False,
            enable_file_output=False
        )
        
        with patch('time.time') as mock_time:
            mock_time.return_value = 0.0
            tracker.start(total_items=100)
            
            mock_time.return_value = 10.0  # 10 seconds elapsed
            tracker.update(processed=25)  # 25% complete
            
            eta = tracker.get_eta()
            # Should estimate 30 more seconds (75% remaining at current rate)
            assert 25 <= eta <= 35  # Allow some variance


class TestMigrationPerformanceManager:
    """Test cases for MigrationPerformanceManager integration."""
    
    def test_initialization_with_defaults(self):
        """Test manager initialization with default config."""
        manager = MigrationPerformanceManager()
        
        assert manager.config.batch_size == 100
        assert manager.config.max_concurrent_batches == 5
        assert manager.config.enable_progress_tracking is True
        assert manager.batch_processor is not None
        assert manager.rate_limiter is not None
        assert manager.retry_manager is not None
    
    def test_initialization_with_custom_config(self):
        """Test manager initialization with custom config."""
        config = PerformanceConfig(
            batch_size=50,
            max_concurrent_batches=3,
            enable_progress_tracking=False
        )
        manager = MigrationPerformanceManager(config)
        
        assert manager.config.batch_size == 50
        assert manager.config.max_concurrent_batches == 3
        assert manager.progress_tracker is None
    
    def test_config_validation(self):
        """Test configuration validation."""
        # Test invalid batch_size
        with pytest.raises(ValueError, match="batch_size must be positive"):
            PerformanceConfig(batch_size=0)
        
        with pytest.raises(ValueError, match="batch_size must be positive"):
            PerformanceConfig(batch_size=-1)
        
        # Test invalid max_concurrent_batches
        with pytest.raises(ValueError, match="max_concurrent_batches must be positive"):
            PerformanceConfig(max_concurrent_batches=0)
        
        # Test invalid rate limits
        with pytest.raises(ValueError, match="max_requests_per_minute must be positive"):
            PerformanceConfig(max_requests_per_minute=0)
        
        # Test invalid retry attempts
        with pytest.raises(ValueError, match="max_retries must be non-negative"):
            PerformanceConfig(max_retries=-1)
    
    @pytest.mark.asyncio
    async def test_process_migration_batch_success(self):
        """Test successful batch processing."""
        config = PerformanceConfig(
            batch_size=2,
            enable_progress_tracking=False  # Disable to simplify test
        )
        manager = MigrationPerformanceManager(config)
        
        items = [1, 2, 3, 4, 5]
        
        def simple_processor(batch):
            return [x * 2 for x in batch]
        
        results = await manager.process_migration_batch(
            items, simple_processor, "Test processing"
        )
        
        assert len(results) == 3  # 3 batches (2, 2, 1)
        # Flatten results to check values
        all_results = []
        for batch_result in results:
            if hasattr(batch_result, 'result'):
                all_results.extend(batch_result.result)
            else:
                all_results.extend(batch_result)
        
        expected = [2, 4, 6, 8, 10]
        assert sorted(all_results) == expected
    
    @pytest.mark.asyncio
    async def test_process_migration_batch_with_failures(self):
        """Test batch processing with some failures."""
        config = PerformanceConfig(
            batch_size=2,
            enable_progress_tracking=False,
            max_retries=1
        )
        manager = MigrationPerformanceManager(config)
        
        items = [1, 2, 3, 4, 5]
        
        def failing_processor(batch):
            if 3 in batch:
                raise ValueError("Simulated failure")
            return [x * 2 for x in batch]
        
        with pytest.raises(Exception):  # Should propagate failure
            await manager.process_migration_batch(
                items, failing_processor, "Test with failures"
            )
    
    def test_process_json_files_streaming(self):
        """Test JSON file streaming functionality."""
        config = PerformanceConfig(enable_streaming=True)
        manager = MigrationPerformanceManager(config)
        
        # Mock file paths and processor
        file_paths = [Path("/fake/file1.json"), Path("/fake/file2.json")]
        
        def mock_processor(data):
            return {"processed": True, "data": data}
        
        # Mock the streaming method to avoid file I/O
        with patch.object(manager, '_stream_process_json_file') as mock_stream:
            mock_stream.return_value = [{"result": "mocked"}]
            
            results = manager.process_json_files_streaming(file_paths, mock_processor)
            
            assert len(results) == 2  # One result per file
            assert mock_stream.call_count == 2
    
    def test_get_performance_metrics(self):
        """Test performance metrics collection."""
        manager = MigrationPerformanceManager()
        
        # Update some metrics
        manager.metrics.total_items = 100
        manager.metrics.processed_items = 80
        manager.metrics.failed_items = 20
        
        metrics = manager.get_performance_metrics()
        
        assert "migration" in metrics
        assert "rate_limiter" in metrics
        assert "batch_processor" in metrics
        assert "retry_manager" in metrics
        
        assert metrics["migration"]["total_items"] == 100
        assert metrics["migration"]["processed_items"] == 80
        assert metrics["migration"]["failed_items"] == 20
    
    def test_cleanup(self):
        """Test resource cleanup."""
        manager = MigrationPerformanceManager()
        
        # Should not raise exceptions
        manager.cleanup()
        
        # Check that executor is shut down
        assert manager._executor._shutdown is True


class TestPerformanceIntegration:
    """Integration tests for performance components working together."""
    
    def test_batch_processor_with_rate_limiter(self, mock_time):
        """Test BatchProcessor integration with rate limiter."""
        # Create rate limiter
        rate_config = RateLimitConfig(
            max_requests=2,
            time_window=10.0,
            strategy=RateLimitStrategy.TOKEN_BUCKET
        )
        rate_limiter = EnhancedRateLimiter(rate_config)
        
        # Create batch processor with rate limiter
        processor = BatchProcessor(
            batch_size=1,
            max_workers=1,
            rate_limiter=rate_limiter
        )
        
        call_times = []
        def timed_processor(batch):
            call_times.append(mock_time.time())
            return batch
        
        items = [1, 2, 3]
        result = processor.process_sequential(items, timed_processor)
        
        assert result["success"] is True
        assert len(call_times) == 3
        
        # Should have delays between calls due to rate limiting
        assert mock_time.time() > 0  # Some time should have passed
    
    def test_retry_manager_with_rate_limiter(self, mock_time):
        """Test RetryManager integration with rate limiting."""
        retry_manager = RetryManager(RetryConfig(max_attempts=3, base_delay=1.0, jitter=False))
        
        rate_config = RateLimitConfig(max_requests=1, time_window=5.0)
        rate_limiter = EnhancedRateLimiter(rate_config)
        
        call_count = 0
        def rate_limited_func():
            nonlocal call_count
            call_count += 1
            
            # Apply rate limiting
            rate_limiter.wait_if_needed()
            
            if call_count < 3:
                raise ValueError("Not ready")
            return "success"
        
        result = retry_manager.execute_with_retry(rate_limited_func)
        
        assert result.success is True
        assert result.result == "success"
        assert call_count == 3
    
    @pytest.mark.asyncio
    async def test_full_integration_workflow(self):
        """Test complete integration workflow."""
        config = PerformanceConfig(
            batch_size=2,
            max_concurrent_batches=2,
            enable_progress_tracking=False,  # Simplify test
            max_requests_per_minute=60
        )
        
        manager = MigrationPerformanceManager(config)
        
        # Simulate migration data
        items = list(range(10))
        
        def migration_processor(batch):
            # Simulate some processing
            return [f"processed_{item}" for item in batch]
        
        try:
            results = await manager.process_migration_batch(
                items, migration_processor, "Integration test"
            )
            
            # Verify results
            assert len(results) > 0
            
            # Check metrics were updated
            metrics = manager.get_performance_metrics()
            assert metrics["migration"]["total_items"] > 0
            
        finally:
            manager.cleanup()


class TestPerformanceLoad:
    """Performance and load testing for the optimization components."""
    
    def test_batch_processor_high_volume(self):
        """Test BatchProcessor with high volume data."""
        processor = BatchProcessor(batch_size=100, max_workers=4)
        
        # Large dataset
        items = list(range(10000))
        
        def simple_processor(batch):
            return [x * 2 for x in batch]
        
        start_time = time.time()
        result = processor.process_parallel(items, simple_processor)
        end_time = time.time()
        
        assert result["success"] is True
        assert result["processed_items"] == 10000
        assert result["failed_items"] == 0
        
        # Should complete in reasonable time
        execution_time = end_time - start_time
        assert execution_time < 30.0  # Should complete within 30 seconds
        
        # Check throughput
        throughput = result["throughput_items_per_second"]
        assert throughput > 100  # Should process at least 100 items/second
    
    def test_rate_limiter_concurrent_load(self):
        """Test rate limiter under concurrent load."""
        config = RateLimitConfig(
            max_requests=100,
            time_window=60.0,
            strategy=RateLimitStrategy.TOKEN_BUCKET
        )
        limiter = EnhancedRateLimiter(config)
        
        successes = []
        failures = []
        
        def worker():
            result = limiter.acquire(1)
            if result:
                successes.append(1)
            else:
                failures.append(1)
        
        # High concurrent load
        threads = [threading.Thread(target=worker) for _ in range(500)]
        
        start_time = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        end_time = time.time()
        
        # Should handle load without errors
        assert len(successes) + len(failures) == 500
        
        # Should respect rate limits
        assert len(successes) <= 100  # Can't exceed max_requests
        
        # Should complete quickly
        assert end_time - start_time < 5.0
    
    def test_progress_tracker_rapid_updates(self):
        """Test ProgressTracker with rapid updates."""
        tracker = ProgressTracker(
            enable_console_output=False,
            enable_logging=False,
            enable_file_output=False,
            update_interval=0.001  # Very fast updates
        )
        
        tracker.start(total_items=10000)
        
        # Rapid updates from multiple threads
        def rapid_updater():
            for _ in range(100):
                tracker.update(processed=1)
        
        threads = [threading.Thread(target=rapid_updater) for _ in range(10)]
        
        start_time = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        end_time = time.time()
        
        tracker.finish(success=True)
        
        # Should handle rapid updates
        assert tracker.metrics.processed_items == 100  # 10 threads * 100 updates
        
        # Should complete quickly
        assert end_time - start_time < 5.0
    
    @pytest.mark.asyncio
    async def test_migration_manager_stress_test(self):
        """Stress test the MigrationPerformanceManager."""
        config = PerformanceConfig(
            batch_size=50,
            max_concurrent_batches=8,
            enable_progress_tracking=False
        )
        
        manager = MigrationPerformanceManager(config)
        
        # Large dataset
        items = list(range(5000))
        
        def stress_processor(batch):
            # Simulate some work
            import random
            time.sleep(random.uniform(0.001, 0.01))  # 1-10ms work
            return [f"processed_{item}" for item in batch]
        
        try:
            start_time = time.time()
            results = await manager.process_migration_batch(
                items, stress_processor, "Stress test"
            )
            end_time = time.time()
            
            # Verify completion
            total_processed = sum(len(r.result if hasattr(r, 'result') else r) for r in results)
            assert total_processed == 5000
            
            # Check performance metrics
            metrics = manager.get_performance_metrics()
            assert metrics["migration"]["total_items"] == 5000
            
            # Should complete in reasonable time
            execution_time = end_time - start_time
            assert execution_time < 60.0  # Should complete within 1 minute
            
        finally:
            manager.cleanup()


if __name__ == "__main__":
    pytest.main([__file__, "-v"]) 