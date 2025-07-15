#!/usr/bin/env python3
"""Advanced batch processing system for migration operations.

This module provides intelligent batch processing with adaptive sizing,
parallel execution, memory monitoring, and performance optimization.
"""

import time
import logging
from enum import Enum
from typing import Any, List, Callable, Dict, Optional
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
import threading

try:
    import psutil
except ImportError:
    psutil = None

logger = logging.getLogger(__name__)


class BatchStrategy(Enum):
    """Batch processing strategies."""
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    MEMORY_AWARE = "memory_aware"
    PERFORMANCE_ADAPTIVE = "performance_adaptive"
    HYBRID = "hybrid"


@dataclass
class BatchConfig:
    """Configuration for batch processing."""
    base_batch_size: int = 100
    max_batch_size: int = 1000
    min_batch_size: int = 10
    max_parallel_workers: int = 4
    memory_limit_mb: int = 1024
    memory_threshold_percent: float = 80.0
    timeout_seconds: int = 300
    retry_attempts: int = 3
    backoff_factor: float = 1.5
    adaptive_factor: float = 1.5
    performance_window_size: int = 10


@dataclass
class BatchMetrics:
    """Metrics for batch processing."""
    total_items: int = 0
    processed_items: int = 0
    failed_items: int = 0
    total_batches: int = 0
    completed_batches: int = 0
    failed_batches: int = 0
    start_time: float = 0.0
    end_time: float = 0.0
    total_duration: float = 0.0
    average_batch_time: float = 0.0
    items_per_second: float = 0.0
    memory_usage_mb: float = 0.0
    peak_memory_mb: float = 0.0
    batch_size_adjustments: int = 0
    performance_history: List[Dict[str, float]] = field(default_factory=list)

    def update_performance(self, batch_time: float, batch_size: int):
        """Update performance metrics."""
        self.completed_batches += 1
        self.processed_items += batch_size

        # Calculate average batch time
        if self.completed_batches > 0:
            total_time = sum(p.get("time", 0) for p in self.performance_history) + batch_time
            self.average_batch_time = total_time / self.completed_batches

        # Calculate items per second
        if self.total_duration > 0:
            self.items_per_second = self.processed_items / self.total_duration

        # Update memory usage
        if psutil:
            process = psutil.Process()
            memory_mb = process.memory_info().rss / 1024 / 1024
            self.memory_usage_mb = memory_mb
            self.peak_memory_mb = max(self.peak_memory_mb, memory_mb)

        # Add to performance history
        self.performance_history.append({
            "time": batch_time,
            "size": batch_size,
            "throughput": batch_size / batch_time if batch_time > 0 else 0
        })

        # Limit history size
        if len(self.performance_history) > 100:
            self.performance_history.pop(0)


@dataclass
class BatchOperation:
    """Represents a batch operation."""
    operation_id: str
    items: List[Any]
    processor_func: Callable[[List[Any]], List[Any]]
    strategy: BatchStrategy
    batch_size: Optional[int] = None


class BatchProcessor:
    """Optimized batch processor with multiple strategies."""

    def __init__(self, config: BatchConfig):
        self.config = config
        self.metrics = BatchMetrics()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._performance_history: List[tuple] = []
        self._lock = threading.Lock()

    def __enter__(self):
        """Enter context manager."""
        self._executor = ThreadPoolExecutor(max_workers=self.config.max_parallel_workers)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager."""
        if self._executor:
            self._executor.shutdown(wait=True)
            self._executor = None

    def process_items(
        self,
        items: List[Any],
        processor_func: Callable[[List[Any]], List[Any]],
        strategy: BatchStrategy = BatchStrategy.SEQUENTIAL,
        batch_size: Optional[int] = None,
        operation_id: Optional[str] = None
    ) -> List[Any]:
        """
        Process items using the specified batch strategy.

        Args:
            items: Items to process
            processor_func: Function to process each batch
            strategy: Batch processing strategy
            batch_size: Optional batch size override
            operation_id: Optional operation identifier

        Returns:
            List of processed results
        """
        if not items:
            return []

        operation_id = operation_id or f"batch_op_{int(time.time())}"

        # Reset metrics
        self.metrics = BatchMetrics()
        self.metrics.total_items = len(items)
        self.metrics.start_time = time.time()

        logger.info(
            "Starting batch processing: %d items, strategy=%s, operation_id=%s",
            len(items), strategy.value, operation_id
        )

        try:
            # Determine optimal batch size
            optimal_batch_size = batch_size or self._calculate_optimal_batch_size(
                len(items), strategy
            )

            # Create batches
            batches = self._create_batches(items, optimal_batch_size)
            self.metrics.total_batches = len(batches)

            # Process based on strategy
            if strategy == BatchStrategy.SEQUENTIAL:
                results = self._process_sequential(batches, processor_func, operation_id)
            elif strategy == BatchStrategy.PARALLEL:
                results = self._process_parallel(batches, processor_func, operation_id)
            elif strategy == BatchStrategy.MEMORY_AWARE:
                results = self._process_memory_aware(batches, processor_func, operation_id)
            elif strategy == BatchStrategy.PERFORMANCE_ADAPTIVE:
                results = self._process_performance_adaptive(batches, processor_func, operation_id)
            elif strategy == BatchStrategy.HYBRID:
                results = self._process_hybrid(batches, processor_func, operation_id)
            else:
                raise ValueError(f"Unknown batch strategy: {strategy}")

            # Finalize metrics
            self.metrics.end_time = time.time()
            self.metrics.total_duration = self.metrics.end_time - self.metrics.start_time

            if self.metrics.total_duration > 0:
                self.metrics.items_per_second = self.metrics.processed_items / self.metrics.total_duration

            logger.info(
                "Completed batch processing: %d/%d items processed, %d batches, %.2fs",
                self.metrics.processed_items, self.metrics.total_items,
                self.metrics.completed_batches, self.metrics.total_duration
            )

            return results

        except Exception as e:
            logger.error("Batch processing failed: %s", e)
            raise

    def _calculate_optimal_batch_size(self, item_count: int, strategy: BatchStrategy) -> int:
        """Calculate optimal batch size based on item count and strategy."""
        if strategy == BatchStrategy.PARALLEL:
            # For parallel processing, balance between workers
            return min(
                max(item_count // self.config.max_parallel_workers, self.config.min_batch_size),
                self.config.max_batch_size
            )
        elif strategy == BatchStrategy.MEMORY_AWARE:
            # For memory-aware processing, start conservative
            return min(self.config.base_batch_size, item_count)
        elif strategy == BatchStrategy.PERFORMANCE_ADAPTIVE:
            # For adaptive processing, start with base size
            return min(self.config.base_batch_size, item_count)
        else:
            # Default to base batch size
            return min(self.config.base_batch_size, item_count)

    def _create_batches(self, items: List[Any], batch_size: int) -> List[List[Any]]:
        """Create batches from items."""
        batches = []
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]
            batches.append(batch)
        return batches

    def _process_sequential(
        self,
        batches: List[List[Any]],
        processor_func: Callable[[List[Any]], List[Any]],
        operation_id: str
    ) -> List[Any]:
        """Process batches sequentially."""
        results = []

        for i, batch in enumerate(batches):
            batch_start = time.time()

            try:
                batch_result = self._process_single_batch(batch, processor_func, f"{operation_id}_batch_{i}")
                results.extend(batch_result)

                batch_time = time.time() - batch_start
                self.metrics.update_performance(batch_time, len(batch))

                logger.debug("Completed batch %d/%d (size: %d)", i + 1, len(batches), len(batch))

            except Exception as e:
                logger.error("Batch %d failed: %s", i + 1, e)
                self.metrics.failed_batches += 1
                self.metrics.failed_items += len(batch)
                raise

        return results

    def _process_parallel(
        self,
        batches: List[List[Any]],
        processor_func: Callable[[List[Any]], List[Any]],
        operation_id: str
    ) -> List[Any]:
        """Process batches in parallel."""
        if not self._executor:
            raise RuntimeError("Executor not initialized. Use context manager.")

        results = []
        future_to_batch = {}

        # Submit all batches
        for i, batch in enumerate(batches):
            future = self._executor.submit(
                processor_func,
                batch
            )
            future_to_batch[future] = (i, batch)

        # Collect results
        try:
            for future in as_completed(future_to_batch, timeout=self.config.timeout_seconds):
                batch_index, batch = future_to_batch[future]

                try:
                    logger.debug("Waiting for batch %d with timeout %d seconds", batch_index + 1, self.config.timeout_seconds)
                    batch_result = future.result()  # No timeout here since as_completed handles it

                    # Ensure result is a list
                    if batch_result is None:
                        batch_result = []
                    elif not isinstance(batch_result, list):
                        batch_result = [batch_result]

                    results.extend(batch_result)

                    # Update metrics for successful batch
                    self.metrics.completed_batches += 1
                    self.metrics.processed_items += len(batch)

                    logger.debug("Completed batch %d (size: %d)", batch_index + 1, len(batch))

                except Exception as e:
                    logger.error("Batch %d failed: %s", batch_index + 1, e)
                    self.metrics.failed_batches += 1
                    self.metrics.failed_items += len(batch)
                    raise

        except TimeoutError as e:
            logger.error("Batch processing timed out: %s", e)
            # Update metrics for all remaining batches
            for future, (batch_index, batch) in future_to_batch.items():
                if not future.done():
                    self.metrics.failed_batches += 1
                    self.metrics.failed_items += len(batch)
            raise RuntimeError(f"Batch processing timed out after {self.config.timeout_seconds} seconds") from e

        return results

    def _process_memory_aware(
        self,
        batches: List[List[Any]],
        processor_func: Callable[[List[Any]], List[Any]],
        operation_id: str
    ) -> List[Any]:
        """Process batches with memory awareness."""
        results = []
        current_batch_size = len(batches[0]) if batches else self.config.base_batch_size

        for i, batch in enumerate(batches):
            batch_start = time.time()

            # Check memory usage before processing
            if psutil:
                process = psutil.Process()
                memory_mb = process.memory_info().rss / 1024 / 1024
                memory_percent = psutil.virtual_memory().percent

                if memory_percent > self.config.memory_threshold_percent:
                    logger.warning(
                        "Memory usage high: %.1f%% (%.1f MB), reducing batch size",
                        memory_percent, memory_mb
                    )
                    # Reduce batch size for memory pressure
                    if current_batch_size > self.config.min_batch_size:
                        current_batch_size = max(
                            self.config.min_batch_size,
                            int(current_batch_size * 0.8)
                        )
                        self.metrics.batch_size_adjustments += 1

            try:
                batch_result = self._process_single_batch(batch, processor_func, f"{operation_id}_batch_{i}")
                results.extend(batch_result)

                batch_time = time.time() - batch_start
                self.metrics.update_performance(batch_time, len(batch))

                logger.debug("Completed batch %d/%d (size: %d)", i + 1, len(batches), len(batch))

            except Exception as e:
                logger.error("Batch %d failed: %s", i + 1, e)
                self.metrics.failed_batches += 1
                self.metrics.failed_items += len(batch)
                raise

        return results

    def _process_performance_adaptive(
        self,
        batches: List[List[Any]],
        processor_func: Callable[[List[Any]], List[Any]],
        operation_id: str
    ) -> List[Any]:
        """Process batches with performance-based adaptive sizing."""
        results = []
        current_batch_size = len(batches[0]) if batches else self.config.base_batch_size

        for i, batch in enumerate(batches):
            batch_start = time.time()

            try:
                batch_result = self._process_single_batch(batch, processor_func, f"{operation_id}_batch_{i}")
                results.extend(batch_result)

                batch_time = time.time() - batch_start
                self.metrics.update_performance(batch_time, len(batch))

                # Track performance for adaptive sizing
                self._performance_history.append((len(batch), batch_time))
                if len(self._performance_history) > self.config.performance_window_size:
                    self._performance_history.pop(0)

                # Adjust batch size based on performance
                if i < len(batches) - 1 and len(self._performance_history) >= 3:
                    new_batch_size = self._calculate_adaptive_batch_size(current_batch_size, batch_time)

                    if new_batch_size != current_batch_size:
                        logger.debug(
                            "Adapting batch size from %d to %d based on performance",
                            current_batch_size, new_batch_size
                        )
                        current_batch_size = new_batch_size
                        self.metrics.batch_size_adjustments += 1

                        # Recreate remaining batches with new size
                        remaining_items = []
                        for remaining_batch in batches[i + 1:]:
                            remaining_items.extend(remaining_batch)

                        new_batches = self._create_batches(remaining_items, new_batch_size)
                        batches = batches[:i + 1] + new_batches

            except Exception as e:
                logger.error("Batch %d failed: %s", i + 1, e)
                self.metrics.failed_batches += 1
                self.metrics.failed_items += len(batch)
                raise

        return results

    def _calculate_adaptive_batch_size(self, current_size: int, last_batch_time: float) -> int:
        """Calculate new batch size based on recent performance."""
        if not self._performance_history:
            return current_size

        # Calculate average throughput for recent batches
        recent_throughput = []
        for size, time_taken in self._performance_history[-3:]:
            if time_taken > 0:
                recent_throughput.append(size / time_taken)

        if not recent_throughput:
            return current_size

        avg_throughput = sum(recent_throughput) / len(recent_throughput)
        current_throughput = current_size / last_batch_time if last_batch_time > 0 else 0

        # Adjust based on throughput comparison
        if current_throughput > avg_throughput * 1.1:
            # Performance is good, try larger batch
            new_size = int(current_size * self.config.adaptive_factor)
        elif current_throughput < avg_throughput * 0.9:
            # Performance is poor, try smaller batch
            new_size = int(current_size / self.config.adaptive_factor)
        else:
            # Performance is stable, keep current size
            new_size = current_size

        # Ensure within bounds
        return max(self.config.min_batch_size, min(new_size, self.config.max_batch_size))

    def _process_hybrid(
        self,
        batches: List[List[Any]],
        processor_func: Callable[[List[Any]], List[Any]],
        operation_id: str
    ) -> List[Any]:
        """Process batches using hybrid approach (parallel + memory-aware + adaptive)."""
        # Use parallel processing for smaller datasets if executor is available
        if self._executor and len(batches) <= self.config.max_parallel_workers:
            return self._process_parallel(batches, processor_func, operation_id)
        else:
            # Use memory-aware processing for large datasets
            return self._process_memory_aware(batches, processor_func, operation_id)

    def _process_single_batch(
        self,
        batch: List[Any],
        processor_func: Callable[[List[Any]], List[Any]],
        batch_id: str
    ) -> List[Any]:
        """Process a single batch with retry logic."""
        last_error = None

        for attempt in range(self.config.retry_attempts):
            try:
                logger.debug("Processing batch %s (attempt %d/%d)", batch_id, attempt + 1, self.config.retry_attempts)

                result = processor_func(batch)

                if result is None:
                    result = []
                elif not isinstance(result, list):
                    result = [result]

                return result

            except Exception as e:
                last_error = e

                if attempt < self.config.retry_attempts - 1:
                    backoff_time = self.config.backoff_factor ** attempt
                    logger.warning(
                        "Batch %s failed (attempt %d/%d), retrying in %.1fs: %s",
                        batch_id, attempt + 1, self.config.retry_attempts, backoff_time, e
                    )
                    time.sleep(backoff_time)
                else:
                    logger.error("Batch %s failed after %d attempts: %s", batch_id, self.config.retry_attempts, e)

        # All attempts failed
        raise RuntimeError(f"Batch {batch_id} failed after {self.config.retry_attempts} attempts") from last_error

    def get_metrics(self) -> Dict[str, Any]:
        """Get current processing metrics."""
        return {
            "total_items": self.metrics.total_items,
            "processed_items": self.metrics.processed_items,
            "failed_items": self.metrics.failed_items,
            "total_batches": self.metrics.total_batches,
            "completed_batches": self.metrics.completed_batches,
            "failed_batches": self.metrics.failed_batches,
            "total_duration": self.metrics.total_duration,
            "average_batch_time": self.metrics.average_batch_time,
            "items_per_second": self.metrics.items_per_second,
            "memory_usage_mb": self.metrics.memory_usage_mb,
            "peak_memory_mb": self.metrics.peak_memory_mb,
            "batch_size_adjustments": self.metrics.batch_size_adjustments,
            "performance_history": self.metrics.performance_history.copy()
        }

    def reset_metrics(self):
        """Reset all metrics."""
        self.metrics = BatchMetrics()
        self._performance_history = []

    def get_performance_summary(self) -> Dict[str, Any]:
        """Get performance summary."""
        return {
            "total_items": self.metrics.total_items,
            "processed_items": self.metrics.processed_items,
            "success_rate": (self.metrics.processed_items / self.metrics.total_items * 100) if self.metrics.total_items > 0 else 0,
            "total_duration": self.metrics.total_duration,
            "items_per_second": self.metrics.items_per_second,
            "average_batch_time": self.metrics.average_batch_time,
            "batch_size_adjustments": self.metrics.batch_size_adjustments,
            "peak_memory_mb": self.metrics.peak_memory_mb
        }


# Factory functions for common use cases
def create_api_batch_processor() -> BatchProcessor:
    """Create a batch processor optimized for API operations."""
    config = BatchConfig(
        base_batch_size=50,
        max_batch_size=200,
        min_batch_size=10,
        max_parallel_workers=4,
        timeout_seconds=30,
        retry_attempts=3,
        backoff_factor=1.5
    )
    return BatchProcessor(config)


def create_database_batch_processor() -> BatchProcessor:
    """Create a batch processor optimized for database operations."""
    config = BatchConfig(
        base_batch_size=100,
        max_batch_size=500,
        min_batch_size=20,
        max_parallel_workers=2,
        timeout_seconds=60,
        retry_attempts=2,
        backoff_factor=2.0
    )
    return BatchProcessor(config)


def create_memory_intensive_batch_processor() -> BatchProcessor:
    """Create a batch processor optimized for memory-intensive operations."""
    config = BatchConfig(
        base_batch_size=10,
        max_batch_size=50,
        min_batch_size=5,
        max_parallel_workers=2,
        memory_limit_mb=4096,
        memory_threshold_percent=75.0,
        timeout_seconds=300,
        retry_attempts=2
    )
    return BatchProcessor(config)
