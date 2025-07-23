"""Comprehensive thread safety tests for performance system components.

Tests verify thread-safe behavior under concurrent load, proper resource cleanup,
async/sync integration, and race condition prevention.
"""

import asyncio
import threading
import time
import random
import weakref
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, MagicMock

import pytest

from src.performance.migration_performance_manager import (
    ThreadSafeMigrationMetrics,
    MigrationPerformanceManager,
    PerformanceConfig,
)
from src.utils.batch_processor import ThreadSafeBatchProcessor


class TestThreadSafeMigrationMetrics:
    """Test thread safety of metrics collection."""

    def test_concurrent_counter_updates(self):
        """Multiple threads increment counters concurrently - verify final totals."""
        metrics = ThreadSafeMigrationMetrics()
        
        threads = []
        increments_per_thread = 1_000
        thread_count = 50
        
        def worker():
            for i in range(increments_per_thread):
                metrics.add_processed_items(1)
                metrics.add_total_items(1)
                
                # Mix in some failures and API calls
                if i % 10 == 0:
                    metrics.add_failed_items(1)
                if i % 5 == 0:
                    metrics.record_api_call(success=i % 20 != 0)
                
                # Record some timing data
                if i % 100 == 0:
                    metrics.record_batch_time(random.uniform(0.1, 2.0))
                    metrics.record_memory_usage(random.uniform(100, 500))
        
        # Start all threads
        for _ in range(thread_count):
            t = threading.Thread(target=worker)
            t.start()
            threads.append(t)
        
        # Wait for completion
        for t in threads:
            t.join()
        
        # Verify atomic updates
        snapshot = metrics.get_snapshot()
        expected_processed = thread_count * increments_per_thread
        expected_total = thread_count * increments_per_thread
        expected_failed = thread_count * (increments_per_thread // 10)
        expected_api_calls = thread_count * (increments_per_thread // 5)
        
        assert snapshot["processed_items"] == expected_processed
        assert snapshot["total_items"] == expected_total
        assert snapshot["failed_items"] == expected_failed
        assert snapshot["total_api_calls"] == expected_api_calls
        
        # Verify timing data was recorded
        assert snapshot["avg_batch_time"] > 0
        assert snapshot["max_batch_time"] > 0
        assert snapshot["peak_memory_usage"] > 0
        assert snapshot["avg_memory_usage"] > 0

    def test_concurrent_snapshot_consistency(self):
        """Concurrent snapshots during updates must be consistent."""
        metrics = ThreadSafeMigrationMetrics()
        snapshots = []
        
        def updater():
            for i in range(1000):
                metrics.add_processed_items(1)
                metrics.add_total_items(1)
                time.sleep(0.001)  # Small delay to encourage race conditions
        
        def snapshot_reader():
            for _ in range(100):
                snapshot = metrics.get_snapshot()
                snapshots.append(snapshot)
                time.sleep(0.01)
        
        # Start concurrent operations
        update_thread = threading.Thread(target=updater)
        read_thread = threading.Thread(target=snapshot_reader)
        
        update_thread.start()
        read_thread.start()
        
        update_thread.join()
        read_thread.join()
        
        # Verify all snapshots are internally consistent
        for snapshot in snapshots:
            assert snapshot["processed_items"] <= snapshot["total_items"]
            assert snapshot["failed_items"] <= snapshot["total_items"]
            assert snapshot["total_time"] >= 0
            assert snapshot["avg_batch_time"] >= 0

    def test_memory_usage_tracking_thread_safety(self):
        """Memory measurements from multiple threads should be recorded safely."""
        metrics = ThreadSafeMigrationMetrics()
        
        def memory_recorder():
            for _ in range(500):
                metrics.record_memory_usage(random.uniform(50, 1000))
        
        threads = [threading.Thread(target=memory_recorder) for _ in range(10)]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        snapshot = metrics.get_snapshot()
        assert snapshot["peak_memory_usage"] > 0
        assert snapshot["avg_memory_usage"] > 0
        assert snapshot["peak_memory_usage"] >= snapshot["avg_memory_usage"]


class TestThreadSafeBatchProcessor:
    """Test thread safety of batch processing operations."""

    def test_parallel_processing_thread_safety(self):
        """Validate parallel processing with internal counter atomicity."""
        items = list(range(1_000))
        
        def echo_processor(batch):
            # Simulate some processing time to encourage race conditions
            time.sleep(random.uniform(0.001, 0.01))
            return batch
        
        with ThreadSafeBatchProcessor(
            batch_size=25, 
            max_workers=8, 
            enable_progress_tracking=False
        ) as processor:
            result = processor.process_parallel(items, echo_processor)
        
        # Verify processing integrity
        assert result["success"] is True
        assert result["processed_items"] == len(items)
        assert result["failed_items"] == 0
        assert sorted(result["data"]) == items
        
        # Verify executor cleanup
        assert processor._executor is None
        assert processor._shutdown_event.is_set()

    def test_context_manager_cleanup_on_exception(self):
        """Ensure __exit__ cleans up even when exceptions occur."""
        shutdown_called = threading.Event()
        
        # Mock the ThreadPoolExecutor to track shutdown calls
        with patch('src.utils.batch_processor.ThreadPoolExecutor') as mock_executor_class:
            mock_executor = MagicMock()
            mock_executor_class.return_value = mock_executor
            
            def track_shutdown(*args, **kwargs):
                shutdown_called.set()
            
            mock_executor.shutdown = track_shutdown
            
            # Use context manager and raise exception
            with pytest.raises(ValueError, match="test exception"):
                with ThreadSafeBatchProcessor(
                    batch_size=10, 
                    max_workers=2
                ) as processor:
                    raise ValueError("test exception")
            
            # Verify shutdown was called
            assert shutdown_called.is_set()
            mock_executor.shutdown.assert_called_once_with(wait=True)

    def test_progress_callback_thread_safety(self):
        """Progress callbacks from multiple threads should be thread-safe."""
        callback_calls = []
        callback_lock = threading.Lock()
        
        def progress_callback(processed, total, failed):
            with callback_lock:
                callback_calls.append((processed, total, failed))
        
        items = list(range(200))
        
        with ThreadSafeBatchProcessor(
            batch_size=10, 
            max_workers=4, 
            enable_progress_tracking=True
        ) as processor:
            processor.add_progress_callback(progress_callback)
            
            def simple_processor(batch):
                time.sleep(0.01)  # Small delay
                return batch
            
            result = processor.process_parallel(items, simple_processor)
        
        assert result["success"] is True
        assert len(callback_calls) > 0
        
        # Verify callback consistency
        for processed, total, failed in callback_calls:
            assert processed <= total
            assert failed <= total
            assert processed >= 0
            assert total == len(items)

    def test_shutdown_during_processing(self):
        """Test graceful shutdown during active processing."""
        items = list(range(100))
        processing_started = threading.Event()
        
        def slow_processor(batch):
            processing_started.set()
            time.sleep(0.1)  # Simulate slow processing
            return batch
        
        with ThreadSafeBatchProcessor(
            batch_size=10, 
            max_workers=2
        ) as processor:
            # Start processing in background thread
            def run_processing():
                return processor.process_parallel(items, slow_processor)
            
            process_thread = threading.Thread(target=run_processing)
            process_thread.start()
            
            # Wait for processing to start, then trigger shutdown
            processing_started.wait(timeout=1.0)
            processor.shutdown()
            
            process_thread.join(timeout=2.0)
            
            # Verify shutdown state
            assert processor._shutdown_event.is_set()

    def test_concurrent_batch_operations(self):
        """Multiple concurrent batch operations should be thread-safe."""
        results = []
        results_lock = threading.Lock()
        
        def run_batch_operation(items_offset):
            items = list(range(items_offset, items_offset + 50))
            
            with ThreadSafeBatchProcessor(
                batch_size=5, 
                max_workers=2
            ) as processor:
                result = processor.process_parallel(items, lambda batch: batch)
                
                with results_lock:
                    results.append(result)
        
        # Start multiple concurrent operations
        threads = []
        for i in range(5):
            thread = threading.Thread(target=run_batch_operation, args=(i * 50,))
            thread.start()
            threads.append(thread)
        
        for thread in threads:
            thread.join()
        
        # Verify all operations completed successfully
        assert len(results) == 5
        for result in results:
            assert result["success"] is True
            assert result["processed_items"] == 50
            assert result["failed_items"] == 0


class TestMigrationPerformanceManager:
    """Test thread safety and async/sync integration of performance manager."""

    @pytest.mark.asyncio
    async def test_async_batch_processing_integration(self):
        """Test async batch processing with proper resource management."""
        config = PerformanceConfig(
            enable_progress_tracking=False,
            max_concurrent_batches=4,
            batch_size=20
        )
        manager = MigrationPerformanceManager(config)
        
        items = list(range(100))
        
        async def process_items():
            return await manager.process_migration_batch(
                items, 
                lambda batch: [x * 2 for x in batch]
            )
        
        # Run async processing
        results = await process_items()
        
        # Verify results
        expected = [x * 2 for x in items]
        assert results == expected
        
        # Check metrics
        snapshot = manager.metrics.get_snapshot()
        assert snapshot["processed_items"] == len(items)
        assert snapshot["failed_items"] == 0
        assert snapshot["total_api_calls"] > 0
        
        # Cleanup and verify resource management
        manager.cleanup()
        assert manager._executor is None

    @pytest.mark.asyncio
    async def test_concurrent_async_operations(self):
        """Multiple concurrent async operations should coordinate properly."""
        config = PerformanceConfig(
            enable_progress_tracking=False,
            max_concurrent_batches=2,
            batch_size=10
        )
        manager = MigrationPerformanceManager(config)
        
        async def process_batch(batch_id):
            items = list(range(batch_id * 20, (batch_id + 1) * 20))
            return await manager.process_migration_batch(
                items,
                lambda batch: [x + 1000 for x in batch]
            )
        
        # Run multiple concurrent operations
        tasks = [process_batch(i) for i in range(5)]
        results = await asyncio.gather(*tasks)
        
        # Verify all operations completed
        assert len(results) == 5
        for i, result in enumerate(results):
            expected = [x + 1000 for x in range(i * 20, (i + 1) * 20)]
            assert result == expected
        
        # Check final metrics
        snapshot = manager.metrics.get_snapshot()
        assert snapshot["processed_items"] == 100  # 5 batches * 20 items
        assert snapshot["failed_items"] == 0
        
        manager.cleanup()

    @pytest.mark.asyncio
    async def test_async_sync_boundary_exception_handling(self):
        """Test exception propagation across async/sync boundaries."""
        config = PerformanceConfig(enable_progress_tracking=False)
        manager = MigrationPerformanceManager(config)
        
        def failing_processor(batch):
            if len(batch) > 5:
                raise ValueError("Batch too large")
            return batch
        
        items = list(range(20))  # This will create batches larger than 5
        
        with pytest.raises(ValueError, match="Batch too large"):
            await manager.process_migration_batch(items, failing_processor)
        
        # Verify metrics recorded the failure
        snapshot = manager.metrics.get_snapshot()
        assert snapshot["failed_items"] > 0
        
        manager.cleanup()

    def test_shutdown_coordination(self):
        """Test shutdown coordination across all manager components."""
        config = PerformanceConfig(enable_progress_tracking=False)
        manager = MigrationPerformanceManager(config)
        
        # Start some background processing
        def background_task():
            time.sleep(0.1)
            return manager.metrics.get_snapshot()
        
        thread = threading.Thread(target=background_task)
        thread.start()
        
        # Trigger shutdown
        manager.shutdown()
        
        # Verify shutdown state
        assert manager._shutdown_event.is_set()
        
        thread.join()
        
        # Verify cleanup completed
        assert manager._executor is None

    @pytest.mark.asyncio
    async def test_json_streaming_async_integration(self):
        """Test async JSON file processing with proper coordination."""
        from pathlib import Path
        import tempfile
        import json
        
        config = PerformanceConfig(enable_progress_tracking=False)
        manager = MigrationPerformanceManager(config)
        
        # Create test JSON files
        test_data = [{"id": i, "value": f"item_{i}"} for i in range(20)]
        
        with tempfile.TemporaryDirectory() as temp_dir:
            files = []
            for i in range(3):
                file_path = Path(temp_dir) / f"test_{i}.json"
                with file_path.open('w') as f:
                    json.dump(test_data[i*7:(i+1)*7], f)
                files.append(file_path)
            
            # Process files asynchronously
            results = await manager.process_json_files_streaming_async(
                files,
                lambda item: {"processed_id": item["id"], "processed_value": item["value"].upper()}
            )
        
        # Verify results
        assert len(results) > 0
        for result in results:
            assert "processed_id" in result
            assert "processed_value" in result
            assert result["processed_value"].isupper()
        
        manager.cleanup()


class TestIntegrationScenarios:
    """Integration tests for complex multi-component scenarios."""

    @pytest.mark.asyncio
    async def test_high_concurrency_stress_test(self):
        """Stress test with many concurrent operations."""
        config = PerformanceConfig(
            enable_progress_tracking=False,
            max_concurrent_batches=8,
            batch_size=50
        )
        manager = MigrationPerformanceManager(config)
        
        async def stress_operation(operation_id):
            items = list(range(operation_id * 100, (operation_id + 1) * 100))
            return await manager.process_migration_batch(
                items,
                lambda batch: [x * operation_id for x in batch]
            )
        
        # Run 20 concurrent operations
        tasks = [stress_operation(i) for i in range(1, 21)]
        results = await asyncio.gather(*tasks)
        
        # Verify all operations completed successfully
        assert len(results) == 20
        
        # Check final metrics
        snapshot = manager.metrics.get_snapshot()
        assert snapshot["processed_items"] == 2000  # 20 operations * 100 items
        assert snapshot["failed_items"] == 0
        assert snapshot["total_api_calls"] > 0
        
        manager.cleanup()

    def test_resource_leak_prevention(self):
        """Verify no resource leaks under various failure scenarios."""
        import gc
        
        # Track executor objects with weak references
        executor_refs = []
        
        def create_and_destroy_manager():
            config = PerformanceConfig(enable_progress_tracking=False)
            manager = MigrationPerformanceManager(config)
            
            with manager.batch_processor as bp:
                if bp._executor:
                    executor_refs.append(weakref.ref(bp._executor))
            
            manager.cleanup()
        
        # Create and destroy multiple managers
        for _ in range(10):
            create_and_destroy_manager()
        
        # Force garbage collection
        gc.collect()
        
        # Verify all executors were cleaned up
        live_executors = [ref for ref in executor_refs if ref() is not None]
        assert len(live_executors) == 0, f"Found {len(live_executors)} live executor references"

    def test_mixed_threading_async_coordination(self):
        """Test coordination between threading and asyncio operations."""
        config = PerformanceConfig(enable_progress_tracking=False)
        manager = MigrationPerformanceManager(config)
        
        results = {}
        results_lock = threading.Lock()
        
        def threaded_operation(thread_id):
            # Simulate sync operation that updates metrics
            for i in range(50):
                manager.metrics.add_processed_items(1)
                manager.metrics.record_api_call(True)
                time.sleep(0.001)
            
            with results_lock:
                results[f"thread_{thread_id}"] = "completed"
        
        async def async_operation(async_id):
            items = list(range(async_id * 25, (async_id + 1) * 25))
            result = await manager.process_migration_batch(
                items,
                lambda batch: batch
            )
            results[f"async_{async_id}"] = result
        
        # Start mixed operations
        threads = []
        for i in range(3):
            thread = threading.Thread(target=threaded_operation, args=(i,))
            thread.start()
            threads.append(thread)
        
        async def run_async_ops():
            tasks = [async_operation(i) for i in range(3)]
            await asyncio.gather(*tasks)
        
        # Run async operations
        asyncio.run(run_async_ops())
        
        # Wait for threads
        for thread in threads:
            thread.join()
        
        # Verify all operations completed
        assert len(results) == 6  # 3 threads + 3 async operations
        
        # Check metrics consistency
        snapshot = manager.metrics.get_snapshot()
        assert snapshot["processed_items"] >= 150  # 3 threads * 50 + async operations
        assert snapshot["total_api_calls"] > 0
        
        manager.cleanup()


if __name__ == "__main__":
    pytest.main([__file__, "-v"]) 