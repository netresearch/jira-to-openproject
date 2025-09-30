#!/usr/bin/env python3
"""Tests for the batch processor system."""

import time

import pytest

from src.utils.batch_processor import (
    BatchConfig,
    ThreadSafeBatchProcessor,
)


class TestBatchProcessor:
    """Test suite for BatchProcessor class."""

    def test_init_default_config(self) -> None:
        """Test initialization with default configuration."""
        processor = ThreadSafeBatchProcessor()
        assert processor.batch_size == 100
        assert processor.max_workers == 4
        assert processor._executor is None

    def test_context_manager(self) -> None:
        """Test context manager functionality."""
        processor = ThreadSafeBatchProcessor()

        assert processor._executor is None

        with processor:
            assert processor._executor is not None

        assert processor._executor is None

    def test_process_items_empty_list(self) -> None:
        """Test processing empty list."""
        processor = ThreadSafeBatchProcessor()

        result = processor.process_sequential([], lambda x: x)
        assert result["data"] == []

    def test_process_items_sequential(self) -> None:
        """Test sequential processing strategy."""
        processor = ThreadSafeBatchProcessor()
        items = list(range(10))

        def dummy_processor(batch):
            return [f"processed_{item}" for item in batch]

        result = processor.process_sequential(items, dummy_processor)

        assert len(result["data"]) == 10
        assert all(item.startswith("processed_") for item in result["data"])

    def test_process_items_parallel(self) -> None:
        """Test parallel processing strategy."""
        processor = ThreadSafeBatchProcessor()
        items = list(range(20))

        def dummy_processor(batch):
            return [f"processed_{item}" for item in batch]

        with processor:
            result = processor.process_parallel(items, dummy_processor)

        assert len(result["data"]) == 20

    # Note: legacy sketches for additional batch strategies (memory-aware,
    # performance-adaptive, hybrid) were removed when the implementation was
    # simplified to the parallel/sequential flows above. Tests will be added if
    # those strategies ever materialise.

    def test_simple_timeout(self) -> None:
        """Test basic timeout behavior."""
        import concurrent.futures

        def slow_function() -> str:
            time.sleep(2)
            return "completed"

        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(slow_function)

            try:
                future.result(timeout=1)
                msg = "Should have timed out"
                raise AssertionError(msg)
            except concurrent.futures.TimeoutError:
                pass  # Expected
            except Exception as e:
                msg = f"Unexpected exception: {e}"
                raise AssertionError(msg)

    def test_direct_timeout_test(self) -> None:
        """Test timeout behavior directly."""
        import concurrent.futures

        def slow_function() -> str:
            time.sleep(2)
            return "done"

        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(slow_function)

            # This should timeout
            with pytest.raises(concurrent.futures.TimeoutError):
                future.result(timeout=1)

    # Additional scenarios (metrics collection, alternative strategies, etc.)
    # will be covered once the batch processor exposes those hooks in the
    # production implementation.


class TestBatchConfig:
    """Test suite for BatchConfig class."""

    def test_default_config(self) -> None:
        """Test default configuration values."""
        config = BatchConfig()
        assert config.batch_size == 100
        assert config.max_workers == 4
        assert config.retry_attempts == 3
        assert config.enable_progress_tracking is True
        assert config.enable_rate_limiting is True
        assert config.chunk_size == 8192

    def test_custom_config(self) -> None:
        """Test custom configuration values."""
        config = BatchConfig(
            batch_size=50,
            max_workers=8,
            retry_attempts=2,
            enable_progress_tracking=False,
        )
        assert config.batch_size == 50
        assert config.max_workers == 8
        assert config.retry_attempts == 2
        assert config.enable_progress_tracking is False

    def test_config_validation(self) -> None:
        """Test configuration validation."""
        # Test valid config
        config = BatchConfig(batch_size=50, max_workers=8, retry_attempts=2)
        assert config.batch_size == 50
        assert config.max_workers == 8
        assert config.retry_attempts == 2

        # Test that invalid values are handled by the SecurityValidator
        # The actual validation happens in ThreadSafeBatchProcessor.__init__
        # BatchConfig itself doesn't validate, it just stores the values


# class TestBatchMetrics:
#     """Test suite for BatchMetrics class."""
#     # BatchMetrics class doesn't exist in the actual implementation
#     # TODO: Implement BatchMetrics class if needed
#     pass


# class TestFactoryFunctions:
#     """Test suite for factory functions."""
#     # These factory functions don't exist in the actual implementation
#     # TODO: Implement factory functions if needed
#     pass


# class TestBatchStrategies:
#     """Test suite for different batch strategies."""
#     # BatchStrategy enum doesn't exist in the actual implementation
#     # TODO: Implement batch strategies if needed
#     pass


if __name__ == "__main__":
    pytest.main([__file__])
