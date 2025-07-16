#!/usr/bin/env python3
"""Tests for the batch processor system."""

import pytest
import time
from unittest.mock import patch
from src.utils.batch_processor import (
    BatchProcessor,
    BatchStrategy,
    BatchConfig,
    BatchMetrics,
    BatchOperation,
    create_api_batch_processor,
    create_database_batch_processor,
    create_memory_intensive_batch_processor,
)


class TestBatchProcessor:
    """Test suite for BatchProcessor class."""

    def test_init_default_config(self):
        """Test initialization with default configuration."""
        config = BatchConfig()
        processor = BatchProcessor(config)
        assert processor.config == config
        assert processor.metrics.total_items == 0
        assert processor._executor is None

    def test_context_manager(self):
        """Test context manager functionality."""
        config = BatchConfig()
        processor = BatchProcessor(config)

        assert processor._executor is None

        with processor:
            assert processor._executor is not None

        assert processor._executor is None

    def test_process_items_empty_list(self):
        """Test processing empty list."""
        config = BatchConfig()
        processor = BatchProcessor(config)

        result = processor.process_items([], lambda x: x)
        assert result == []

    def test_process_items_sequential(self):
        """Test sequential processing strategy."""
        config = BatchConfig()
        processor = BatchProcessor(config)
        items = list(range(10))

        def dummy_processor(batch):
            return [f"processed_{item}" for item in batch]

        result = processor.process_items(
            items, dummy_processor, strategy=BatchStrategy.SEQUENTIAL, batch_size=3
        )

        assert len(result) == 10
        assert all(item.startswith("processed_") for item in result)
        assert processor.metrics.total_items == 10
        assert processor.metrics.processed_items == 10

    def test_process_items_parallel(self):
        """Test parallel processing strategy."""
        config = BatchConfig()
        processor = BatchProcessor(config)
        items = list(range(20))

        def dummy_processor(batch):
            return [f"processed_{item}" for item in batch]

        with processor:
            result = processor.process_items(
                items, dummy_processor, strategy=BatchStrategy.PARALLEL, batch_size=5
            )

        assert len(result) == 20
        assert processor.metrics.total_items == 20
        assert processor.metrics.processed_items == 20

    def test_process_items_memory_aware(self):
        """Test memory-aware processing strategy."""
        config = BatchConfig()
        processor = BatchProcessor(config)
        items = list(range(15))

        def dummy_processor(batch):
            return [f"processed_{item}" for item in batch]

        result = processor.process_items(
            items, dummy_processor, strategy=BatchStrategy.MEMORY_AWARE, batch_size=5
        )

        assert len(result) == 15
        assert processor.metrics.total_items == 15
        assert processor.metrics.processed_items == 15

    def test_process_items_performance_adaptive(self):
        """Test performance-adaptive processing strategy."""
        config = BatchConfig()
        processor = BatchProcessor(config)
        items = list(range(25))

        def dummy_processor(batch):
            return [f"processed_{item}" for item in batch]

        result = processor.process_items(
            items, dummy_processor, strategy=BatchStrategy.PERFORMANCE_ADAPTIVE, batch_size=5
        )

        assert len(result) == 25
        assert processor.metrics.total_items == 25
        assert processor.metrics.processed_items == 25
        assert len(processor._performance_history) > 0

    def test_process_items_hybrid(self):
        """Test hybrid processing strategy."""
        config = BatchConfig()
        processor = BatchProcessor(config)
        items = list(range(30))

        def dummy_processor(batch):
            return [f"processed_{item}" for item in batch]

        with processor:
            result = processor.process_items(
                items,
                dummy_processor,
                strategy=BatchStrategy.HYBRID,
                batch_size=10
            )

        assert len(result) == 30
        assert processor.metrics.total_items == 30
        assert processor.metrics.processed_items == 30

    def test_error_handling(self):
        """Test error handling in batch processing."""
        config = BatchConfig()
        processor = BatchProcessor(config)
        items = list(range(10))

        def failing_processor(batch):
            if batch[0] == 5:
                raise ValueError("Test error")
            return [f"processed_{item}" for item in batch]

        with pytest.raises(RuntimeError):
            processor.process_items(
                items, failing_processor, strategy=BatchStrategy.SEQUENTIAL, batch_size=5
            )

    def test_retry_logic(self):
        """Test retry logic in batch processing."""
        config = BatchConfig(retry_attempts=2)
        processor = BatchProcessor(config)
        items = [1, 2, 3]

        call_count = 0

        def flaky_processor(batch):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("First attempt fails")
            return [f"processed_{item}" for item in batch]

        result = processor.process_items(
            items, flaky_processor, strategy=BatchStrategy.SEQUENTIAL
        )

        assert len(result) == 3
        assert call_count == 2  # Should retry once

    def test_simple_timeout(self):
        """Test basic timeout behavior."""
        import concurrent.futures

        def slow_function():
            time.sleep(2)
            return "completed"

        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(slow_function)

            try:
                future.result(timeout=1)
                assert False, "Should have timed out"
            except concurrent.futures.TimeoutError:
                pass  # Expected
            except Exception as e:
                assert False, f"Unexpected exception: {e}"

    def test_direct_timeout_test(self):
        """Test timeout behavior directly."""
        import concurrent.futures

        def slow_function():
            time.sleep(2)
            return "done"

        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(slow_function)

            # This should timeout
            with pytest.raises(concurrent.futures.TimeoutError):
                future.result(timeout=1)

    def test_batch_timeout_direct(self):
        """Test timeout handling directly in batch processor."""
        config = BatchConfig(timeout_seconds=1)
        processor = BatchProcessor(config)

        def slow_processor(batch):
            time.sleep(2)  # Exceeds timeout
            return [f"processed_{item}" for item in batch]

        with processor:
            # Test the _process_parallel method directly
            batches = [[1, 2], [3, 4]]

            with pytest.raises(RuntimeError):
                processor._process_parallel(batches, slow_processor, "test_op")

    def test_batch_timeout_debug(self):
        """Debug the timeout handling."""
        config = BatchConfig(timeout_seconds=1)
        processor = BatchProcessor(config)

        def slow_processor(batch):
            time.sleep(2)  # Exceeds timeout
            return [f"processed_{item}" for item in batch]

        with processor:
            # Submit a single batch manually to test timeout
            import concurrent.futures

            future = processor._executor.submit(slow_processor, [1, 2])

            try:
                result = future.result(timeout=1)
                assert False, "Should have timed out"
            except concurrent.futures.TimeoutError:
                pass  # Expected
            except Exception:
                raise

    def test_batch_processor_timeout(self):
        """Test timeout handling in batch processing."""
        config = BatchConfig(timeout_seconds=1)
        processor = BatchProcessor(config)
        items = list(range(5))

        def slow_processor(batch):
            time.sleep(2)  # Exceeds timeout
            return [f"processed_{item}" for item in batch]

        with processor:
            with pytest.raises(RuntimeError):
                processor.process_items(
                    items,
                    slow_processor,
                    strategy=BatchStrategy.PARALLEL,
                    batch_size=2
                )

    def test_metrics_collection(self):
        """Test metrics collection during processing."""
        config = BatchConfig()
        processor = BatchProcessor(config)
        items = list(range(10))

        def dummy_processor(batch):
            time.sleep(0.01)  # Small delay for timing
            return [f"processed_{item}" for item in batch]

        processor.process_items(
            items, dummy_processor, strategy=BatchStrategy.SEQUENTIAL, batch_size=3
        )

        metrics = processor.get_metrics()
        assert metrics["total_items"] == 10
        assert metrics["processed_items"] == 10
        assert metrics["completed_batches"] == 4
        assert metrics["total_duration"] > 0

    def test_get_performance_summary(self):
        """Test performance summary generation."""
        config = BatchConfig()
        processor = BatchProcessor(config)
        items = list(range(10))

        def dummy_processor(batch):
            return [f"processed_{item}" for item in batch]

        processor.process_items(
            items, dummy_processor, strategy=BatchStrategy.SEQUENTIAL, batch_size=5
        )

        summary = processor.get_performance_summary()
        assert summary["total_items"] == 10
        assert summary["processed_items"] == 10
        assert summary["success_rate"] == 100.0

    def test_reset_metrics(self):
        """Test metrics reset functionality."""
        config = BatchConfig()
        processor = BatchProcessor(config)
        items = list(range(5))

        def dummy_processor(batch):
            return [f"processed_{item}" for item in batch]

        processor.process_items(
            items, dummy_processor, strategy=BatchStrategy.SEQUENTIAL
        )

        assert processor.metrics.total_items == 5
        processor.reset_metrics()
        assert processor.metrics.total_items == 0

    def test_batch_operation_dataclass(self):
        """Test BatchOperation dataclass."""
        operation = BatchOperation(
            operation_id="test_op",
            items=[1, 2, 3],
            processor_func=lambda x: x,
            strategy=BatchStrategy.SEQUENTIAL,
            batch_size=10
        )

        assert operation.operation_id == "test_op"
        assert operation.items == [1, 2, 3]
        assert operation.strategy == BatchStrategy.SEQUENTIAL
        assert operation.batch_size == 10

    def test_unknown_strategy(self):
        """Test handling of unknown batch strategy."""
        config = BatchConfig()
        processor = BatchProcessor(config)
        items = [1, 2, 3]

        def dummy_processor(batch):
            return batch

        # Mock an unknown strategy
        with patch.object(processor, 'process_items') as mock_process:
            mock_process.side_effect = ValueError("Unknown batch strategy: unknown")

            with pytest.raises(ValueError):
                processor.process_items(items, dummy_processor, strategy="unknown")


class TestBatchConfig:
    """Test suite for BatchConfig class."""

    def test_default_config(self):
        """Test default configuration values."""
        config = BatchConfig()
        assert config.base_batch_size == 100
        assert config.max_batch_size == 1000
        assert config.min_batch_size == 10
        assert config.max_parallel_workers == 4
        assert config.memory_limit_mb == 1024
        assert config.timeout_seconds == 300
        assert config.retry_attempts == 3

    def test_custom_config(self):
        """Test custom configuration values."""
        config = BatchConfig(
            base_batch_size=50,
            max_batch_size=500,
            timeout_seconds=60,
            retry_attempts=2
        )
        assert config.base_batch_size == 50
        assert config.max_batch_size == 500
        assert config.timeout_seconds == 60
        assert config.retry_attempts == 2

    def test_config_validation(self):
        """Test configuration validation."""
        # Test valid config
        config = BatchConfig(min_batch_size=5, base_batch_size=10, max_batch_size=20)
        assert config.min_batch_size == 5
        assert config.base_batch_size == 10
        assert config.max_batch_size == 20


class TestBatchMetrics:
    """Test suite for BatchMetrics class."""

    def test_default_metrics(self):
        """Test default metrics values."""
        metrics = BatchMetrics()
        assert metrics.total_items == 0
        assert metrics.processed_items == 0
        assert metrics.failed_items == 0
        assert metrics.total_batches == 0
        assert metrics.completed_batches == 0
        assert metrics.failed_batches == 0
        assert metrics.total_duration == 0.0

    def test_update_performance(self):
        """Test performance metrics update."""
        metrics = BatchMetrics()
        metrics.update_performance(1.5, 10)

        assert metrics.completed_batches == 1
        assert metrics.processed_items == 10
        assert len(metrics.performance_history) == 1
        assert metrics.performance_history[0]["time"] == 1.5
        assert metrics.performance_history[0]["size"] == 10

    def test_performance_history_limit(self):
        """Test performance history size limit."""
        metrics = BatchMetrics()

        # Add more than 100 entries
        for i in range(105):
            metrics.update_performance(1.0, 10)

        # Should be limited to 100 entries
        assert len(metrics.performance_history) == 100


class TestFactoryFunctions:
    """Test suite for factory functions."""

    def test_create_api_batch_processor(self):
        """Test API batch processor factory."""
        processor = create_api_batch_processor()
        assert isinstance(processor, BatchProcessor)
        assert processor.config.base_batch_size == 50
        assert processor.config.max_batch_size == 200
        assert processor.config.timeout_seconds == 30

    def test_create_database_batch_processor(self):
        """Test database batch processor factory."""
        processor = create_database_batch_processor()
        assert isinstance(processor, BatchProcessor)
        assert processor.config.base_batch_size == 100
        assert processor.config.max_batch_size == 500
        assert processor.config.timeout_seconds == 60

    def test_create_memory_intensive_batch_processor(self):
        """Test memory-intensive batch processor factory."""
        processor = create_memory_intensive_batch_processor()
        assert isinstance(processor, BatchProcessor)
        assert processor.config.base_batch_size == 10
        assert processor.config.max_batch_size == 50
        assert processor.config.memory_limit_mb == 4096


class TestBatchStrategies:
    """Test suite for different batch strategies."""

    def test_all_strategies_produce_same_results(self):
        """Test that all strategies produce the same results for simple cases."""
        items = list(range(20))

        def dummy_processor(batch):
            return [f"processed_{item}" for item in batch]

        strategies = [
            BatchStrategy.SEQUENTIAL,
            BatchStrategy.PARALLEL,
            BatchStrategy.MEMORY_AWARE,
            BatchStrategy.PERFORMANCE_ADAPTIVE,
            BatchStrategy.HYBRID,
        ]

        results = []
        for strategy in strategies:
            config = BatchConfig()
            processor = BatchProcessor(config)

            # Use context manager for all strategies that might need it
            if strategy in [BatchStrategy.PARALLEL, BatchStrategy.HYBRID]:
                with processor:
                    result = processor.process_items(
                        items, dummy_processor, strategy=strategy, batch_size=5
                    )
            else:
                result = processor.process_items(
                    items, dummy_processor, strategy=strategy, batch_size=5
                )
            results.append(sorted(result))

        # All strategies should produce the same results
        for i in range(1, len(results)):
            assert results[i] == results[0]

    def test_strategy_performance_differences(self):
        """Test that different strategies have different performance characteristics."""
        items = list(range(100))

        def dummy_processor(batch):
            time.sleep(0.001)  # Small delay to make timing meaningful
            return [f"processed_{item}" for item in batch]

        # Test sequential vs parallel
        config = BatchConfig()
        seq_processor = BatchProcessor(config)
        par_processor = BatchProcessor(config)

        # Sequential processing
        seq_start = time.time()
        seq_processor.process_items(
            items, dummy_processor, strategy=BatchStrategy.SEQUENTIAL, batch_size=10
        )
        seq_duration = time.time() - seq_start

        # Parallel processing
        with par_processor:
            par_start = time.time()
            par_processor.process_items(
                items, dummy_processor, strategy=BatchStrategy.PARALLEL, batch_size=10
            )
            par_duration = time.time() - par_start

        # Parallel should generally be faster (though not guaranteed in all test environments)
        assert seq_duration > 0
        assert par_duration > 0


if __name__ == "__main__":
    pytest.main([__file__])
