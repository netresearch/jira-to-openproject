#!/usr/bin/env python3
"""Tests for the Large-Scale Migration Optimizer."""

import asyncio
import time

import pytest

from src.utils.large_scale_optimizer import (
    IntelligentCache,
    LargeScaleConfig,
    LargeScaleOptimizer,
    ProgressPersistence,
    ResourceMonitor,
    SystemResources,
    get_optimized_config_for_size,
    optimize_large_scale_migration,
)


class TestSystemResources:
    """Test SystemResources class."""

    def test_system_resources_creation(self) -> None:
        """Test creating SystemResources."""
        resources = SystemResources()
        assert resources.cpu_count > 0
        assert resources.memory_total_gb > 0
        assert resources.memory_available_gb > 0
        assert 0 <= resources.memory_usage_percent <= 100

    def test_memory_pressure_level(self) -> None:
        """Test memory pressure level calculation."""
        resources = SystemResources()
        pressure = resources.get_memory_pressure_level()
        assert pressure in ["low", "medium", "high"]

    def test_optimal_worker_count(self) -> None:
        """Test optimal worker count calculation."""
        resources = SystemResources()
        workers = resources.get_optimal_worker_count(4)
        assert 1 <= workers <= 4


class TestIntelligentCache:
    """Test IntelligentCache class."""

    def test_cache_creation(self) -> None:
        """Test creating IntelligentCache."""
        cache = IntelligentCache(max_size_mb=100, ttl_seconds=60)
        assert cache.max_size_mb == 100
        assert cache.ttl_seconds == 60

    def test_cache_set_get(self) -> None:
        """Test setting and getting cache items."""
        cache = IntelligentCache(max_size_mb=100, ttl_seconds=60)

        # Set item
        success = cache.set("test_key", "test_value", size_mb=0.1)
        assert success is True

        # Get item
        value = cache.get("test_key")
        assert value == "test_value"

    def test_cache_expiration(self) -> None:
        """Test cache item expiration."""
        cache = IntelligentCache(max_size_mb=100, ttl_seconds=0.1)  # Very short TTL

        cache.set("test_key", "test_value", size_mb=0.1)
        time.sleep(0.2)  # Wait for expiration

        value = cache.get("test_key")
        assert value is None

    def test_cache_size_limit(self) -> None:
        """Test cache size limiting."""
        cache = IntelligentCache(max_size_mb=0.1, ttl_seconds=60)  # Very small cache

        # First item should fit
        success1 = cache.set("key1", "value1", size_mb=0.05)
        assert success1 is True

        # Second item should not fit
        success2 = cache.set("key2", "value2", size_mb=0.1)
        assert success2 is False

    def test_cache_clear(self) -> None:
        """Test clearing cache."""
        cache = IntelligentCache(max_size_mb=100, ttl_seconds=60)
        cache.set("test_key", "test_value", size_mb=0.1)

        cache.clear()
        value = cache.get("test_key")
        assert value is None


class TestProgressPersistence:
    """Test ProgressPersistence class."""

    @pytest.fixture
    def temp_file(self, tmp_path):
        """Create temporary file for testing."""
        return tmp_path / "test_progress.json"

    @pytest.mark.asyncio
    async def test_save_load_progress(self, temp_file) -> None:
        """Test saving and loading progress."""
        persistence = ProgressPersistence(temp_file)

        progress_data = {
            "processed_count": 100,
            "failed_count": 5,
            "current_index": 100,
            "timestamp": time.time(),
        }

        # Save progress
        await persistence.save_progress(progress_data)
        assert temp_file.exists()

        # Load progress
        loaded_data = await persistence.load_progress()
        assert loaded_data is not None
        assert loaded_data["processed_count"] == 100
        assert loaded_data["failed_count"] == 5

    @pytest.mark.asyncio
    async def test_load_nonexistent_progress(self, tmp_path) -> None:
        """Test loading progress from nonexistent file."""
        persistence = ProgressPersistence(tmp_path / "nonexistent.json")
        loaded_data = await persistence.load_progress()
        assert loaded_data is None

    @pytest.mark.asyncio
    async def test_checkpoint_operations(self, temp_file) -> None:
        """Test checkpoint save and load operations."""
        persistence = ProgressPersistence(temp_file)

        checkpoint_data = {
            "processed_count": 500,
            "failed_count": 10,
            "current_index": 500,
            "timestamp": time.time(),
        }

        # Save checkpoint
        await persistence.save_checkpoint("test_checkpoint", checkpoint_data)
        checkpoint_path = persistence.get_checkpoint_path("test_checkpoint")
        assert checkpoint_path.exists()

        # Load checkpoint
        loaded_data = await persistence.load_checkpoint("test_checkpoint")
        assert loaded_data is not None
        assert loaded_data["processed_count"] == 500


class TestResourceMonitor:
    """Test ResourceMonitor class."""

    def test_monitor_creation(self) -> None:
        """Test creating ResourceMonitor."""
        monitor = ResourceMonitor(check_interval=1.0, memory_threshold=80.0)
        assert monitor.check_interval == 1.0
        assert monitor.memory_threshold == 80.0
        assert monitor.monitoring is False

    def test_pressure_callback(self) -> None:
        """Test pressure callback functionality."""
        monitor = ResourceMonitor()
        callback_called = False
        callback_pressure = None

        def test_callback(pressure) -> None:
            nonlocal callback_called, callback_pressure
            callback_called = True
            callback_pressure = pressure

        monitor.add_pressure_callback(test_callback)
        monitor._callbacks[0]("high")  # Simulate pressure change

        assert callback_called is True
        assert callback_pressure == "high"

    def test_memory_pressure_check(self) -> None:
        """Test memory pressure checking."""
        monitor = ResourceMonitor()
        pressure = monitor._check_memory_pressure()
        assert pressure in ["low", "medium", "high"]


class TestLargeScaleConfig:
    """Test LargeScaleConfig class."""

    def test_config_creation(self) -> None:
        """Test creating LargeScaleConfig."""
        config = LargeScaleConfig()
        assert config.memory_limit_gb > 0
        assert config.base_batch_size > 0
        assert config.max_batch_size >= config.base_batch_size
        assert config.max_workers > 0

    def test_config_validation(self) -> None:
        """Test config validation and adjustment."""
        # Test with very high memory limit
        config = LargeScaleConfig(memory_limit_gb=1000.0)  # Unrealistic value
        # Should be adjusted to reasonable value
        assert config.memory_limit_gb < 1000.0


class TestLargeScaleOptimizer:
    """Test LargeScaleOptimizer class."""

    @pytest.fixture
    def optimizer(self):
        """Create LargeScaleOptimizer instance."""
        config = LargeScaleConfig(
            enable_resource_monitoring=False,  # Disable for testing
            enable_progress_persistence=False,
            enable_intelligent_caching=False,
        )
        return LargeScaleOptimizer(config)

    def test_optimizer_creation(self, optimizer) -> None:
        """Test creating LargeScaleOptimizer."""
        assert optimizer.config is not None
        assert optimizer.resources is not None
        assert optimizer.cache is not None

    def test_adaptive_batch_size(self, optimizer) -> None:
        """Test adaptive batch size calculation."""
        batch_size = optimizer.get_adaptive_batch_size()
        assert batch_size >= optimizer.config.base_batch_size // 2
        assert batch_size <= optimizer.config.max_batch_size

    @pytest.mark.asyncio
    async def test_process_large_scale_migration(self, optimizer) -> None:
        """Test large-scale migration processing."""
        # Create test data
        items = list(range(100))

        async def test_processor(item) -> str:
            await asyncio.sleep(0.01)  # Simulate processing
            return f"processed_{item}"

        # Process migration
        results = await optimizer.process_large_scale_migration(
            items,
            test_processor,
            "Test migration",
        )

        assert len(results) == 100
        assert all(result.startswith("processed_") for result in results)

    @pytest.mark.asyncio
    async def test_process_batch_sequential(self, optimizer) -> None:
        """Test sequential batch processing."""
        batch = [1, 2, 3, 4, 5]

        async def test_processor(item):
            return item * 2

        results = await optimizer._process_batch_sequential(batch, test_processor)
        assert results == [2, 4, 6, 8, 10]

    def test_performance_metrics(self, optimizer) -> None:
        """Test performance metrics calculation."""
        metrics = optimizer.get_performance_metrics()

        assert "duration_seconds" in metrics
        assert "processed_count" in metrics
        assert "failed_count" in metrics
        assert "success_rate" in metrics
        assert "items_per_second" in metrics
        assert "memory_usage_mb" in metrics
        assert "current_batch_size" in metrics
        assert "memory_pressure" in metrics


class TestConvenienceFunctions:
    """Test convenience functions."""

    def test_get_optimized_config_for_size(self) -> None:
        """Test getting optimized config based on size."""
        # Small migration
        config = get_optimized_config_for_size(1000)
        assert config.base_batch_size == 100
        assert config.max_workers <= 4

        # Medium migration
        config = get_optimized_config_for_size(50000)
        assert config.base_batch_size == 200
        assert config.max_workers <= 8

        # Large migration
        config = get_optimized_config_for_size(500000)
        assert config.base_batch_size == 500
        assert config.max_workers <= 16

    @pytest.mark.asyncio
    async def test_optimize_large_scale_migration(self) -> None:
        """Test convenience function for large-scale migration."""
        items = list(range(50))

        async def test_processor(item):
            await asyncio.sleep(0.01)
            return item * 2

        results = await optimize_large_scale_migration(
            items,
            test_processor,
            description="Test convenience migration",
        )

        assert len(results) == 50
        assert all(
            result == item * 2 for item, result in zip(items, results, strict=False)
        )


class TestIntegration:
    """Integration tests for the large-scale optimizer."""

    @pytest.mark.asyncio
    async def test_full_migration_workflow(self) -> None:
        """Test complete migration workflow with optimizations."""
        # Create large dataset
        items = list(range(1000))

        # Create processor that simulates API calls
        async def api_processor(item) -> str:
            await asyncio.sleep(0.001)  # Simulate API call
            if item % 100 == 0:  # Simulate occasional failures
                msg = f"API error for item {item}"
                raise Exception(msg)
            return f"migrated_{item}"

        # Configure for large-scale processing
        config = LargeScaleConfig(
            base_batch_size=50,
            max_batch_size=200,
            max_workers=4,
            enable_garbage_collection=True,
            enable_intelligent_caching=True,
            enable_progress_persistence=True,
            enable_resource_monitoring=False,  # Disable for testing
            enable_recovery=True,
        )

        optimizer = LargeScaleOptimizer(config)

        try:
            results = await optimizer.process_large_scale_migration(
                items,
                api_processor,
                "Integration test migration",
            )

            # Check results
            assert len(results) < len(items)  # Some items should fail
            assert all(result.startswith("migrated_") for result in results)

            # Check metrics
            metrics = optimizer.get_performance_metrics()
            assert metrics["processed_count"] > 0
            assert metrics["failed_count"] > 0
            assert metrics["success_rate"] > 0

        finally:
            optimizer.cleanup()

    @pytest.mark.asyncio
    async def test_memory_pressure_handling(self) -> None:
        """Test handling of memory pressure scenarios."""

        # Create processor that uses memory
        async def memory_intensive_processor(item) -> str:
            # Simulate memory usage
            large_data = [0] * 1000
            await asyncio.sleep(0.001)
            return f"processed_{item}_{len(large_data)}"

        items = list(range(100))

        config = LargeScaleConfig(
            enable_garbage_collection=True,
            enable_intelligent_caching=True,
            enable_resource_monitoring=False,  # Disable for testing
            gc_threshold=10,  # Frequent garbage collection
        )

        optimizer = LargeScaleOptimizer(config)

        try:
            results = await optimizer.process_large_scale_migration(
                items,
                memory_intensive_processor,
                "Memory pressure test",
            )

            assert len(results) == 100

        finally:
            optimizer.cleanup()


if __name__ == "__main__":
    pytest.main([__file__])
