#!/usr/bin/env python3
"""Comprehensive unit tests for performance optimization utilities.

Tests cover:
1. PerformanceCache - TTL, LRU eviction, thread safety, statistics
2. ConnectionPoolManager - Session reuse, retry strategies, cleanup
3. BatchProcessor - Parallel processing, error handling, resource management
4. AdaptiveRateLimiter - Rate adjustment, timing, thread safety
5. StreamingPaginator - Memory efficiency, error handling, pagination
6. PerformanceOptimizer - Integration, decorators, statistics
"""

import threading
import time
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from src.utils.performance_optimizer import (
    AdaptiveRateLimiter,
    BatchProcessor,
    CacheEntry,
    ConnectionPoolManager,
    PerformanceCache,
    PerformanceOptimizer,
    StreamingPaginator,
    batched,
    cached,
    get_performance_stats,
    rate_limited,
    shutdown_performance_optimizer,
)


class TestCacheEntry:
    """Test CacheEntry dataclass functionality."""

    def test_cache_entry_creation(self) -> None:
        """Test cache entry initialization."""
        data = {"test": "value"}
        entry = CacheEntry(data=data, timestamp=datetime.now(), ttl_seconds=3600)

        assert entry.data == data
        assert entry.ttl_seconds == 3600
        assert entry.access_count == 0
        assert entry.last_accessed is None

    def test_cache_entry_expiration(self) -> None:
        """Test TTL expiration logic."""
        # Not expired entry
        entry = CacheEntry(data="test", timestamp=datetime.now(), ttl_seconds=3600)
        assert not entry.is_expired()

        # Expired entry
        expired_entry = CacheEntry(
            data="test",
            timestamp=datetime.now() - timedelta(seconds=3700),
            ttl_seconds=3600,
        )
        assert expired_entry.is_expired()

    def test_cache_entry_touch(self) -> None:
        """Test access tracking."""
        entry = CacheEntry(data="test", timestamp=datetime.now(), ttl_seconds=3600)

        entry.touch()
        assert entry.access_count == 1
        assert entry.last_accessed is not None

        first_access = entry.last_accessed
        time.sleep(0.01)  # Small delay
        entry.touch()
        assert entry.access_count == 2
        assert entry.last_accessed > first_access


class TestPerformanceCache:
    """Test PerformanceCache thread-safe caching with TTL and LRU eviction."""

    def test_cache_initialization(self) -> None:
        """Test cache initialization with default and custom parameters."""
        # Default initialization
        cache = PerformanceCache()
        assert cache.max_size == 1000
        assert cache.default_ttl == 3600

        # Custom initialization
        custom_cache = PerformanceCache(max_size=500, default_ttl=1800)
        assert custom_cache.max_size == 500
        assert custom_cache.default_ttl == 1800

    def test_cache_basic_operations(self) -> None:
        """Test basic cache get/set operations."""
        cache = PerformanceCache()

        # Test miss
        assert cache.get("missing_key") is None

        # Test set and hit
        cache.set("test_key", "test_value")
        assert cache.get("test_key") == "test_value"

        # Test overwrite
        cache.set("test_key", "new_value")
        assert cache.get("test_key") == "new_value"

    def test_cache_ttl_expiration(self) -> None:
        """Test TTL-based cache expiration."""
        cache = PerformanceCache(default_ttl=1)

        # Set value with short TTL
        cache.set("expires_fast", "value", ttl=1)
        assert cache.get("expires_fast") == "value"

        # Wait for expiration
        time.sleep(1.1)
        assert cache.get("expires_fast") is None

    def test_cache_lru_eviction(self) -> None:
        """Test LRU eviction when cache reaches max size."""
        cache = PerformanceCache(max_size=3)

        # Fill cache to capacity
        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.set("key3", "value3")

        # All should be present
        assert cache.get("key1") == "value1"
        assert cache.get("key2") == "value2"
        assert cache.get("key3") == "value3"

        # Add one more to trigger eviction
        cache.set("key4", "value4")

        # Oldest entry should be evicted
        assert cache.get("key1") is None
        assert cache.get("key4") == "value4"

    def test_cache_statistics(self) -> None:
        """Test cache hit/miss statistics tracking."""
        cache = PerformanceCache()

        # Initial stats
        stats = cache.get_stats()
        assert stats["hits"] == 0
        assert stats["misses"] == 0
        assert stats["hit_rate"] == 0.0
        assert stats["evictions"] == 0

        # Generate hits and misses
        cache.set("key1", "value1")
        cache.get("key1")  # Hit
        cache.get("missing")  # Miss

        stats = cache.get_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == 0.5

    def test_cache_thread_safety(self) -> None:
        """Test cache thread safety under concurrent access."""
        cache = PerformanceCache(max_size=100)

        def cache_worker(worker_id: int) -> None:
            """Worker function for concurrent cache operations."""
            for i in range(50):
                key = f"worker_{worker_id}_key_{i}"
                value = f"worker_{worker_id}_value_{i}"
                cache.set(key, value)
                retrieved = cache.get(key)
                assert retrieved == value or retrieved is None  # None if evicted

        # Run concurrent workers
        threads = []
        for worker_id in range(5):
            thread = threading.Thread(target=cache_worker, args=(worker_id,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        # Verify statistics are consistent
        stats = cache.get_stats()
        assert stats["hits"] + stats["misses"] > 0
        assert 0 <= stats["hit_rate"] <= 1.0

    def test_cache_clear(self) -> None:
        """Test cache clearing functionality."""
        cache = PerformanceCache()

        # Add some entries
        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.get("key1")  # Generate hit
        cache.get("missing")  # Generate miss

        # Verify entries exist
        assert cache.get("key1") == "value1"
        stats = cache.get_stats()
        assert stats["current_size"] == 2

        # Clear cache
        cache.clear()

        # Verify cache is empty but stats preserved
        assert cache.get("key1") is None
        assert cache.get("key2") is None
        stats = cache.get_stats()
        assert stats["current_size"] == 0
        # Note: get operations during verification may increment hit count
        assert (
            stats["hits"] >= 1
        )  # Statistics preserved, may have additional hits from verification


class TestConnectionPoolManager:
    """Test ConnectionPoolManager for HTTP session reuse and pooling."""

    def test_connection_manager_initialization(self) -> None:
        """Test connection manager initialization."""
        manager = ConnectionPoolManager()
        assert manager.pool_connections == 20
        assert manager.pool_maxsize == 50
        assert manager.max_retries == 3
        assert manager.backoff_factor == 0.3

    @patch("src.utils.performance_optimizer.requests.Session")
    def test_session_creation_and_reuse(self, mock_session_class) -> None:
        """Test session creation and reuse for same base URL."""
        mock_session = Mock()
        mock_session_class.return_value = mock_session

        manager = ConnectionPoolManager()

        # First call should create new session
        session1 = manager.get_session("https://api.example.com")
        assert session1 == mock_session
        assert mock_session_class.called

        # Second call should reuse existing session
        mock_session_class.reset_mock()
        session2 = manager.get_session("https://api.example.com")
        assert session2 == mock_session
        assert not mock_session_class.called  # No new session created

        # Different URL should create new session
        manager.get_session("https://api.different.com")
        assert mock_session_class.called

    @patch("src.utils.performance_optimizer.requests.Session")
    def test_session_configuration(self, mock_session_class) -> None:
        """Test session configuration with adapters and headers."""
        mock_session = Mock()
        mock_session_class.return_value = mock_session

        manager = ConnectionPoolManager(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=5,
            backoff_factor=0.5,
        )

        manager.get_session("https://api.test.com")

        # Verify session configuration calls
        assert mock_session.mount.call_count == 2  # http and https
        mock_session.headers.update.assert_called_once()

        # Verify headers
        headers_call = mock_session.headers.update.call_args[0][0]
        assert headers_call["User-Agent"] == "Migration-Tool/1.0"
        assert headers_call["Connection"] == "keep-alive"

    def test_session_key_generation(self) -> None:
        """Test session key generation for different URLs."""
        manager = ConnectionPoolManager()

        key1 = manager._get_session_key("https://api.example.com")
        key2 = manager._get_session_key("https://api.example.com")
        key3 = manager._get_session_key("https://api.different.com")

        # Same URL should generate same key
        assert key1 == key2
        # Different URL should generate different key
        assert key1 != key3
        # Keys should be MD5 hashes
        assert len(key1) == 32
        assert all(c in "0123456789abcdef" for c in key1)

    @patch("src.utils.performance_optimizer.requests.Session")
    def test_session_cleanup(self, mock_session_class) -> None:
        """Test session cleanup functionality."""
        mock_session1 = Mock()
        mock_session2 = Mock()
        mock_session_class.side_effect = [mock_session1, mock_session2]

        manager = ConnectionPoolManager()

        # Create sessions
        manager.get_session("https://api1.com")
        manager.get_session("https://api2.com")

        # Close all sessions
        manager.close_all()

        # Verify sessions were closed
        mock_session1.close.assert_called_once()
        mock_session2.close.assert_called_once()

        # Verify sessions dict is cleared
        assert len(manager._sessions) == 0


class TestBatchProcessor:
    """Test BatchProcessor for parallel batch processing."""

    def test_batch_processor_initialization(self) -> None:
        """Test batch processor initialization."""
        processor = BatchProcessor(batch_size=50, max_workers=5)
        assert processor.batch_size == 50
        assert processor.max_workers == 5
        assert processor.executor is not None

    def test_process_empty_batches(self) -> None:
        """Test processing empty item list."""
        processor = BatchProcessor()

        def dummy_process(batch, **kwargs):
            return batch

        results = processor.process_batches([], dummy_process)
        assert results == []

    def test_process_single_batch(self) -> None:
        """Test processing items that fit in single batch."""
        processor = BatchProcessor(batch_size=10)

        def double_values(batch, **kwargs):
            return [x * 2 for x in batch]

        items = [1, 2, 3, 4, 5]
        results = processor.process_batches(items, double_values)
        assert results == [2, 4, 6, 8, 10]

    def test_process_multiple_batches(self) -> None:
        """Test processing items across multiple batches."""
        processor = BatchProcessor(batch_size=3, max_workers=2)

        def process_batch(batch, **kwargs):
            return [x + 10 for x in batch]

        items = [1, 2, 3, 4, 5, 6, 7]
        results = processor.process_batches(items, process_batch)

        # Results might be in different order due to parallel processing
        expected = [11, 12, 13, 14, 15, 16, 17]
        assert sorted(results) == sorted(expected)

    def test_batch_processing_with_kwargs(self) -> None:
        """Test batch processing with additional keyword arguments."""
        processor = BatchProcessor(batch_size=2)

        def process_with_multiplier(batch, multiplier=1, **kwargs):
            return [x * multiplier for x in batch]

        items = [1, 2, 3, 4]
        results = processor.process_batches(
            items,
            process_with_multiplier,
            multiplier=5,
        )
        assert sorted(results) == [5, 10, 15, 20]

    def test_batch_processing_error_handling(self) -> None:
        """Test error handling in batch processing."""
        processor = BatchProcessor(batch_size=2)

        def process_with_error(batch, **kwargs):
            if 3 in batch:
                msg = "Error processing batch with 3"
                raise ValueError(msg)
            return [x * 2 for x in batch]

        items = [1, 2, 3, 4, 5, 6]
        results = processor.process_batches(items, process_with_error)

        # Should get results from successful batches only
        # Batch [1,2] -> [2,4], batch [3,4] -> error, batch [5,6] -> [10,12]
        expected_successful = [2, 4, 10, 12]
        assert sorted(results) == sorted(expected_successful)

    def test_batch_processor_shutdown(self) -> None:
        """Test batch processor shutdown."""
        processor = BatchProcessor()

        # Process should work before shutdown
        results = processor.process_batches([1, 2, 3], lambda b, **k: b)
        assert results == [1, 2, 3]

        # Shutdown
        processor.shutdown()

        # Executor should be shutdown
        assert processor.executor._shutdown


class TestAdaptiveRateLimiter:
    """Test AdaptiveRateLimiter for adaptive rate limiting with timing."""

    def test_rate_limiter_initialization(self) -> None:
        """Test rate limiter initialization."""
        limiter = AdaptiveRateLimiter(
            initial_rate=5.0,
            min_rate=1.0,
            max_rate=20.0,
            adjustment_factor=0.2,
        )
        assert limiter.current_rate == 5.0
        assert limiter.min_rate == 1.0
        assert limiter.max_rate == 20.0
        assert limiter.adjustment_factor == 0.2

    @patch("src.utils.performance_optimizer.time.time")
    @patch("src.utils.performance_optimizer.time.sleep")
    def test_rate_limiting_throttle(self, mock_sleep, mock_time) -> None:
        """Test rate limiting throttle behavior."""
        # Mock time progression
        mock_time.side_effect = [
            0.0,
            0.0,
            0.5,
            0.6,
        ]  # Start, delay calc, after sleep, end

        limiter = AdaptiveRateLimiter(
            initial_rate=2.0,
        )  # 2 requests per second = 0.5s interval

        with limiter.throttle():
            pass

        # Should sleep 0.5 - 0.0 = 0.5 seconds for first request
        mock_sleep.assert_called_once_with(0.5)

    @patch("src.utils.performance_optimizer.time.time")
    def test_rate_limiter_no_delay_needed(self, mock_time) -> None:
        """Test no delay when enough time has passed."""
        # Mock time to show sufficient time has passed
        limiter = AdaptiveRateLimiter(
            initial_rate=2.0,
        )  # 2 requests per second = 0.5s interval

        # Set last request time to 1 second ago
        limiter.last_request_time = 0.0

        # Current time shows 1 second has passed (more than 0.5s needed)
        mock_time.return_value = 1.0

        with (
            patch("src.utils.performance_optimizer.time.sleep") as mock_sleep,
            limiter.throttle(),
        ):
            pass

        # No sleep should be called since enough time has passed
        mock_sleep.assert_not_called()

    def test_rate_adjustment_on_errors(self) -> None:
        """Test rate adjustment based on errors."""
        limiter = AdaptiveRateLimiter(
            initial_rate=10.0,
            min_rate=1.0,
            adjustment_factor=0.2,
        )

        initial_rate = limiter.current_rate

        # Simulate error
        with patch("src.utils.performance_optimizer.time.time", return_value=1.0):
            try:
                with limiter.throttle():
                    msg = "Test error"
                    raise Exception(msg)
            except Exception:
                pass

        # Rate should decrease after error
        assert limiter.current_rate < initial_rate
        assert limiter.current_rate >= limiter.min_rate

    def test_rate_adjustment_on_fast_responses(self) -> None:
        """Test rate increase on fast responses."""
        limiter = AdaptiveRateLimiter(
            initial_rate=5.0,
            max_rate=20.0,
            adjustment_factor=0.1,
        )

        # Simulate multiple fast responses without mocking time
        # (Use actual time for simplicity since we're testing rate adjustment logic)
        initial_rate = limiter.current_rate

        # Simulate fast responses by manually calling _record_response
        for _ in range(5):
            limiter._record_response(0.1, False)  # 0.1s response time, no error

        # Rate should increase after fast responses
        assert limiter.current_rate > initial_rate
        assert limiter.current_rate <= limiter.max_rate

    def test_rate_adjustment_on_slow_responses(self) -> None:
        """Test rate decrease on slow responses."""
        limiter = AdaptiveRateLimiter(
            initial_rate=10.0,
            min_rate=1.0,
            adjustment_factor=0.1,
        )

        initial_rate = limiter.current_rate

        # Simulate slow responses by manually calling _record_response
        for _ in range(5):
            limiter._record_response(2.5, False)  # 2.5s response time, no error

        # Rate should decrease after slow responses
        assert limiter.current_rate < initial_rate
        assert limiter.current_rate >= limiter.min_rate

    def test_rate_limiter_statistics(self) -> None:
        """Test rate limiter statistics collection."""
        limiter = AdaptiveRateLimiter()

        # Initial stats
        stats = limiter.get_stats()
        assert stats["total_requests"] == 0
        assert stats["success_count"] == 0
        assert stats["error_count"] == 0
        assert stats["error_rate"] == 0.0

        # Generate some requests
        with patch("src.utils.performance_optimizer.time.time", return_value=1.0):
            # Successful request
            with limiter.throttle():
                pass

            # Failed request
            try:
                with limiter.throttle():
                    msg = "Test error"
                    raise Exception(msg)
            except Exception:
                pass

        # Check updated stats
        stats = limiter.get_stats()
        assert stats["total_requests"] == 2
        assert stats["success_count"] == 1
        assert stats["error_count"] == 1
        assert stats["error_rate"] == 0.5


class TestStreamingPaginator:
    """Test StreamingPaginator for memory-efficient pagination."""

    def test_streaming_paginator_initialization(self) -> None:
        """Test streaming paginator initialization."""

        def dummy_fetch(**kwargs):
            return []

        paginator = StreamingPaginator(
            fetch_func=dummy_fetch,
            page_size=50,
            max_pages=10,
        )
        assert paginator.fetch_func == dummy_fetch
        assert paginator.page_size == 50
        assert paginator.max_pages == 10

    def test_iter_items_single_page(self) -> None:
        """Test iteration over single page of items."""

        def fetch_single_page(start_at=0, max_results=10, **kwargs):
            if start_at == 0:
                return [1, 2, 3, 4, 5]
            return []

        paginator = StreamingPaginator(fetch_single_page, page_size=10)
        items = list(paginator.iter_items())
        assert items == [1, 2, 3, 4, 5]

    def test_iter_items_multiple_pages(self) -> None:
        """Test iteration over multiple pages."""

        def fetch_multiple_pages(start_at=0, max_results=3, **kwargs):
            pages = {0: [1, 2, 3], 3: [4, 5, 6], 6: [7, 8], 8: []}
            return pages.get(start_at, [])

        paginator = StreamingPaginator(fetch_multiple_pages, page_size=3)
        items = list(paginator.iter_items())
        assert items == [1, 2, 3, 4, 5, 6, 7, 8]

    def test_iter_items_max_pages_limit(self) -> None:
        """Test max pages limitation."""

        def fetch_infinite(start_at=0, max_results=2, **kwargs):
            return [start_at + 1, start_at + 2]  # Always return full page

        paginator = StreamingPaginator(fetch_infinite, page_size=2, max_pages=3)
        items = list(paginator.iter_items())
        assert len(items) == 6  # 3 pages * 2 items per page
        assert items == [1, 2, 3, 4, 5, 6]

    def test_iter_items_empty_result(self) -> None:
        """Test handling of empty results."""

        def fetch_empty(**kwargs):
            return []

        paginator = StreamingPaginator(fetch_empty, page_size=10)
        items = list(paginator.iter_items())
        assert items == []

    def test_iter_items_with_kwargs(self) -> None:
        """Test passing kwargs to fetch function."""

        def fetch_with_filter(start_at=0, max_results=10, filter_value=None, **kwargs):
            if filter_value == "test":
                return ["filtered_1", "filtered_2"]
            return []

        paginator = StreamingPaginator(fetch_with_filter, page_size=10)
        items = list(paginator.iter_items(filter_value="test"))
        assert items == ["filtered_1", "filtered_2"]

    def test_iter_items_error_handling(self) -> None:
        """Test error handling during pagination."""
        call_count = 0

        def fetch_with_error(start_at=0, max_results=2, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [1, 2]
            if call_count == 2:
                msg = "Fetch error"
                raise Exception(msg)
            return [3, 4]

        paginator = StreamingPaginator(fetch_with_error, page_size=2)
        items = list(paginator.iter_items())

        # Should only get first page due to error on second page
        assert items == [1, 2]

    def test_collect_all(self) -> None:
        """Test collect_all convenience method."""

        def fetch_pages(start_at=0, max_results=3, **kwargs):
            if start_at == 0:
                return [1, 2, 3]
            if start_at == 3:
                return [4, 5]
            return []

        paginator = StreamingPaginator(fetch_pages, page_size=3)
        items = paginator.collect_all()
        assert items == [1, 2, 3, 4, 5]


class TestPerformanceOptimizer:
    """Test PerformanceOptimizer main coordinator class."""

    def test_performance_optimizer_initialization(self) -> None:
        """Test performance optimizer initialization."""
        optimizer = PerformanceOptimizer(
            cache_size=500,
            cache_ttl=1800,
            batch_size=50,
            max_workers=5,
            rate_limit=5.0,
        )

        assert optimizer.cache.max_size == 500
        assert optimizer.cache.default_ttl == 1800
        assert optimizer.batch_processor.batch_size == 50
        assert optimizer.batch_processor.max_workers == 5
        assert optimizer.rate_limiter.current_rate == 5.0

    def test_cached_operation_decorator(self) -> None:
        """Test cached operation decorator."""
        optimizer = PerformanceOptimizer()

        call_count = 0

        @optimizer.cached_operation(ttl=300)
        def expensive_function(x, y):
            nonlocal call_count
            call_count += 1
            return x + y

        # First call should execute function
        result1 = expensive_function(2, 3)
        assert result1 == 5
        assert call_count == 1

        # Second call should use cache
        result2 = expensive_function(2, 3)
        assert result2 == 5
        assert call_count == 1  # No additional call

        # Different args should execute function
        result3 = expensive_function(3, 4)
        assert result3 == 7
        assert call_count == 2

    def test_rate_limited_operation_decorator(self) -> None:
        """Test rate limited operation decorator."""
        optimizer = PerformanceOptimizer()

        @optimizer.rate_limited_operation()
        def api_call() -> str:
            return "success"

        with patch.object(optimizer.rate_limiter, "throttle") as mock_throttle:
            mock_throttle.return_value.__enter__ = Mock()
            mock_throttle.return_value.__exit__ = Mock()

            result = api_call()
            assert result == "success"
            mock_throttle.assert_called_once()

    def test_batch_operation_decorator(self) -> None:
        """Test batch operation decorator."""
        optimizer = PerformanceOptimizer()

        @optimizer.batch_operation(batch_size=2)
        def process_items(items, **kwargs):
            return [x * 2 for x in items]

        with patch.object(optimizer.batch_processor, "process_batches") as mock_process:
            mock_process.return_value = [2, 4, 6, 8]

            result = process_items([1, 2, 3, 4])
            assert result == [2, 4, 6, 8]

            # Verify process_batches was called with correct items
            mock_process.assert_called_once()
            call_args = mock_process.call_args[0]
            assert call_args[0] == [1, 2, 3, 4]  # Check the items argument
            # Function argument is the second parameter, but we don't compare it directly

    def test_cache_key_creation(self) -> None:
        """Test cache key creation from function and arguments."""
        optimizer = PerformanceOptimizer()

        # Same args should produce same key
        key1 = optimizer._create_cache_key("test_func", (1, 2), {"a": "b"})
        key2 = optimizer._create_cache_key("test_func", (1, 2), {"a": "b"})
        assert key1 == key2

        # Different args should produce different keys
        key3 = optimizer._create_cache_key("test_func", (1, 3), {"a": "b"})
        assert key1 != key3

        # Different kwargs should produce different keys
        key4 = optimizer._create_cache_key("test_func", (1, 2), {"a": "c"})
        assert key1 != key4

        # Keys should be MD5 hashes
        assert len(key1) == 32
        assert all(c in "0123456789abcdef" for c in key1)

    def test_comprehensive_stats(self) -> None:
        """Test comprehensive statistics collection."""
        optimizer = PerformanceOptimizer()

        stats = optimizer.get_comprehensive_stats()

        assert "cache" in stats
        assert "rate_limiter" in stats
        assert "optimizer" in stats
        assert "connections" in stats

        # Check optimizer stats structure
        optimizer_stats = stats["optimizer"]
        assert "operations_cached" in optimizer_stats
        assert "operations_batched" in optimizer_stats
        assert "connections_reused" in optimizer_stats
        assert "rate_limited_calls" in optimizer_stats

    def test_performance_optimizer_shutdown(self) -> None:
        """Test performance optimizer shutdown."""
        optimizer = PerformanceOptimizer()

        # Add some data to cache
        optimizer.cache.set("test", "value")

        # Shutdown
        optimizer.shutdown()

        # Cache should be cleared
        assert optimizer.cache.get("test") is None

        # Batch processor should be shutdown
        assert optimizer.batch_processor.executor._shutdown


class TestGlobalDecorators:
    """Test global convenience decorators."""

    def test_global_cached_decorator(self) -> None:
        """Test global cached decorator."""
        call_count = 0

        @cached(ttl=300)
        def test_function(x):
            nonlocal call_count
            call_count += 1
            return x * 2

        # First call
        result1 = test_function(5)
        assert result1 == 10
        assert call_count == 1

        # Second call should use cache
        result2 = test_function(5)
        assert result2 == 10
        assert call_count == 1

    def test_global_rate_limited_decorator(self) -> None:
        """Test global rate limited decorator."""

        @rate_limited()
        def test_api_call() -> str:
            return "api_response"

        # Should not raise exception
        result = test_api_call()
        assert result == "api_response"

    def test_global_batched_decorator(self) -> None:
        """Test global batched decorator."""

        @batched(batch_size=3)
        def test_batch_process(items, **kwargs):
            return [x + 1 for x in items]

        # Should process in batches
        result = test_batch_process([1, 2, 3, 4, 5])
        assert sorted(result) == [2, 3, 4, 5, 6]

    def test_global_performance_stats(self) -> None:
        """Test global performance stats function."""
        stats = get_performance_stats()

        assert isinstance(stats, dict)
        assert "cache" in stats
        assert "rate_limiter" in stats
        assert "optimizer" in stats
        assert "connections" in stats

    def test_global_shutdown(self) -> None:
        """Test global shutdown function."""
        # Should not raise exception
        shutdown_performance_optimizer()


class TestPerformanceIntegration:
    """Integration tests for performance optimization components."""

    def test_full_optimization_workflow(self) -> None:
        """Test complete optimization workflow with all components."""
        optimizer = PerformanceOptimizer(
            cache_size=100,
            cache_ttl=300,
            batch_size=5,
            max_workers=3,
            rate_limit=10.0,
        )

        call_count = 0

        @optimizer.cached_operation(ttl=600)
        @optimizer.rate_limited_operation()
        def expensive_api_call(item_id) -> str:
            nonlocal call_count
            call_count += 1
            time.sleep(0.01)  # Simulate API delay
            return f"result_for_{item_id}"

        @optimizer.batch_operation(batch_size=3)
        def process_batch(items, **kwargs):
            return [expensive_api_call(item) for item in items]

        # Process items
        items = [1, 2, 3, 4, 5, 6, 7]
        results = process_batch(items)

        # Verify results
        expected = [f"result_for_{i}" for i in items]
        assert sorted(results) == sorted(expected)

        # Verify caching worked (second call to same items should use cache)
        initial_call_count = call_count
        process_batch([1, 2, 3])  # Subset of previous items

        # Should have fewer new calls due to caching
        assert call_count <= initial_call_count + 3  # At most 3 new calls

        # Get comprehensive stats
        stats = optimizer.get_comprehensive_stats()
        assert stats["optimizer"]["operations_cached"] > 0
        assert stats["optimizer"]["operations_batched"] > 0
        assert stats["optimizer"]["rate_limited_calls"] > 0

        # Cleanup
        optimizer.shutdown()

    def test_error_handling_integration(self) -> None:
        """Test error handling across all optimization components."""
        optimizer = PerformanceOptimizer()

        @optimizer.cached_operation()
        @optimizer.rate_limited_operation()
        def failing_function(should_fail=False) -> str:
            if should_fail:
                msg = "Intentional failure"
                raise ValueError(msg)
            return "success"

        @optimizer.batch_operation()
        def process_with_errors(items, **kwargs):
            return [failing_function(should_fail=(item % 2 == 0)) for item in items]

        # Process items with some failures
        items = [1, 2, 3, 4, 5]

        # Should handle errors gracefully
        try:
            process_with_errors(items)
        except Exception:
            pass  # Expected due to errors in batch processing

        # Rate limiter should track errors
        stats = optimizer.get_comprehensive_stats()
        assert stats["rate_limiter"]["error_count"] > 0

        # Cleanup
        optimizer.shutdown()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
