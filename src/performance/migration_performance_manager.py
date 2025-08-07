"""Comprehensive performance management for migration operations.

This module integrates all performance optimization components (batching, rate limiting,
retry logic, and progress tracking) into a unified system for efficient migration processing.
"""

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from src.utils.batch_processor import BatchProcessor
from src.utils.config_validation import ConfigurationValidationError, SecurityValidator
from src.utils.enhanced_rate_limiter import (
    RateLimitConfig,
    RateLimitStrategy,
    global_rate_limiter_manager,
)
from src.utils.progress_tracker import ProgressTracker
from src.utils.retry_manager import RetryConfig, RetryManager, RetryStrategy

T = TypeVar("T")
R = TypeVar("R")

logger = logging.getLogger(__name__)


@dataclass
class MigrationMetrics:
    """Metrics tracking for migration performance."""

    start_time: float = field(default_factory=time.time)
    end_time: float | None = None
    total_items: int = 0
    processed_items: int = 0
    failed_items: int = 0
    api_calls: int = 0
    successful_api_calls: int = 0
    retry_attempts: int = 0
    rate_limit_hits: int = 0
    total_batch_time: float = 0.0
    total_wait_time: float = 0.0
    memory_usage_mb: float = 0.0

    def __post_init__(self):
        """Initialize metrics with current time."""
        if self.end_time is None:
            self.end_time = time.time()

    def get_duration(self) -> float:
        """Get total duration in seconds."""
        return (self.end_time or time.time()) - self.start_time

    def get_success_rate(self) -> float:
        """Get success rate as percentage."""
        if self.total_items == 0:
            return 0.0
        return (self.processed_items / self.total_items) * 100

    def get_api_success_rate(self) -> float:
        """Get API call success rate as percentage."""
        if self.api_calls == 0:
            return 0.0
        return (self.successful_api_calls / self.api_calls) * 100


@dataclass
class PerformanceConfig:
    """Configuration for migration performance optimization with comprehensive security validation."""

    # Batching configuration
    batch_size: int = 100
    max_concurrent_batches: int = 5
    batch_timeout: float = 30.0

    # Rate limiting configuration
    max_requests_per_minute: int = 100
    burst_size: int = 10
    adaptive_rate_limiting: bool = True

    # Retry configuration
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0

    # Progress tracking
    enable_progress_tracking: bool = True
    progress_update_interval: float = 1.0
    save_progress_to_file: bool = True

    # Performance tuning
    enable_parallel_processing: bool = True
    memory_limit_mb: int = 512
    enable_streaming: bool = True

    def __post_init__(self):
        """Validate configuration parameters using SecurityValidator for comprehensive security checks."""
        try:
            # Validate core batching parameters
            self.batch_size = SecurityValidator.validate_numeric_parameter(
                "batch_size",
                self.batch_size,
            )
            self.max_concurrent_batches = SecurityValidator.validate_numeric_parameter(
                "max_concurrent_batches",
                self.max_concurrent_batches,
            )
            self.batch_timeout = SecurityValidator.validate_numeric_parameter(
                "batch_timeout",
                self.batch_timeout,
            )

            # Validate rate limiting parameters
            self.max_requests_per_minute = SecurityValidator.validate_numeric_parameter(
                "max_requests_per_minute",
                self.max_requests_per_minute,
            )
            self.burst_size = SecurityValidator.validate_numeric_parameter(
                "burst_size",
                self.burst_size,
            )

            # Validate retry parameters
            self.max_retries = SecurityValidator.validate_numeric_parameter(
                "max_retries",
                self.max_retries,
            )
            self.base_delay = SecurityValidator.validate_numeric_parameter(
                "base_delay",
                self.base_delay,
            )
            self.max_delay = SecurityValidator.validate_numeric_parameter(
                "max_delay",
                self.max_delay,
            )

            # Validate progress tracking parameters
            self.progress_update_interval = (
                SecurityValidator.validate_numeric_parameter(
                    "progress_update_interval",
                    self.progress_update_interval,
                )
            )

            # Validate memory and performance parameters
            self.memory_limit_mb = SecurityValidator.validate_numeric_parameter(
                "memory_limit_mb",
                self.memory_limit_mb,
            )

            # Validate timing relationships
            SecurityValidator.validate_timing_relationships(
                self.base_delay,
                self.max_delay,
            )

            # Validate resource allocation combinations to prevent system overload
            # Use a reasonable estimate for max_workers since it's not in PerformanceConfig itself
            estimated_max_workers = min(
                self.max_concurrent_batches,
                8,
            )  # Conservative estimate
            SecurityValidator.validate_resource_allocation(
                self.batch_size,
                estimated_max_workers,
                self.memory_limit_mb,
            )

        except ConfigurationValidationError as e:
            logger.exception(f"PerformanceConfig validation failed: {e}")
            raise


class ThreadSafeMigrationMetrics:
    """Thread-safe metrics container for migration performance."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._start_time = time.time()
        self._end_time = None
        self._total_items = 0
        self._processed_items = 0
        self._failed_items = 0
        self._skipped_items = 0
        self._total_api_calls = 0
        self._failed_api_calls = 0
        self._retry_attempts = 0
        self._rate_limit_hits = 0
        self._batch_times = []
        self._total_wait_time = 0.0
        self._peak_memory_usage = 0.0
        self._memory_measurements = []

    def add_processed_items(self, count: int) -> None:
        """Atomically add processed items."""
        with self._lock:
            self._processed_items += count

    def add_failed_items(self, count: int) -> None:
        """Atomically add failed items."""
        with self._lock:
            self._failed_items += count

    def add_total_items(self, count: int) -> None:
        """Atomically add total items."""
        with self._lock:
            self._total_items += count

    def record_api_call(self, success: bool) -> None:
        """Atomically record API call result."""
        with self._lock:
            self._total_api_calls += 1
            if not success:
                self._failed_api_calls += 1

    def record_retry_attempt(self) -> None:
        """Atomically record retry attempt."""
        with self._lock:
            self._retry_attempts += 1

    def record_rate_limit_hit(self) -> None:
        """Atomically record rate limit hit."""
        with self._lock:
            self._rate_limit_hits += 1

    def record_batch_time(self, batch_time: float) -> None:
        """Atomically record batch execution time."""
        with self._lock:
            self._batch_times.append(batch_time)

    def add_wait_time(self, wait_time: float) -> None:
        """Atomically add wait time."""
        with self._lock:
            self._total_wait_time += wait_time

    def record_memory_usage(self, memory_mb: float) -> None:
        """Atomically record memory usage."""
        with self._lock:
            self._memory_measurements.append(memory_mb)
            self._peak_memory_usage = max(self._peak_memory_usage, memory_mb)

    def set_end_time(self) -> None:
        """Atomically set end time."""
        with self._lock:
            self._end_time = time.time()

    def get_snapshot(self) -> dict[str, Any]:
        """Get thread-safe snapshot of current metrics."""
        with self._lock:
            end_time = self._end_time or time.time()
            total_time = end_time - self._start_time

            avg_batch_time = (
                sum(self._batch_times) / len(self._batch_times)
                if self._batch_times
                else 0.0
            )
            max_batch_time = max(self._batch_times) if self._batch_times else 0.0
            avg_memory = (
                sum(self._memory_measurements) / len(self._memory_measurements)
                if self._memory_measurements
                else 0.0
            )

            return {
                "start_time": self._start_time,
                "end_time": end_time,
                "total_time": total_time,
                "total_items": self._total_items,
                "processed_items": self._processed_items,
                "failed_items": self._failed_items,
                "skipped_items": self._skipped_items,
                "total_api_calls": self._total_api_calls,
                "failed_api_calls": self._failed_api_calls,
                "retry_attempts": self._retry_attempts,
                "rate_limit_hits": self._rate_limit_hits,
                "avg_batch_time": avg_batch_time,
                "max_batch_time": max_batch_time,
                "total_wait_time": self._total_wait_time,
                "peak_memory_usage": self._peak_memory_usage,
                "avg_memory_usage": avg_memory,
            }


class MigrationPerformanceManager:
    """Thread-safe performance manager for migration operations."""

    def __init__(self, config: PerformanceConfig = None) -> None:
        self.config = config or PerformanceConfig()
        self.metrics = ThreadSafeMigrationMetrics()
        self._shutdown_event = threading.Event()
        self._setup_lock = threading.Lock()

        # Initialize components
        self._setup_batch_processor()
        self._setup_rate_limiter()
        self._setup_retry_manager()
        self._setup_progress_tracker()

        self._executor = None

    def _setup_batch_processor(self) -> None:
        """Initialize the batch processor."""
        with self._setup_lock:
            self.batch_processor = BatchProcessor(
                batch_size=self.config.batch_size,
                max_workers=self.config.max_concurrent_batches,
                rate_limiter=None,  # Will be set separately
                enable_progress_tracking=self.config.enable_progress_tracking,
                retry_attempts=self.config.max_retries,
            )

    def _setup_rate_limiter(self) -> None:
        """Initialize the rate limiter."""
        with self._setup_lock:
            rate_config = RateLimitConfig(
                max_requests=self.config.max_requests_per_minute,
                time_window=60.0,
                burst_size=self.config.burst_size,
                strategy=(
                    RateLimitStrategy.ADAPTIVE
                    if self.config.adaptive_rate_limiting
                    else RateLimitStrategy.TOKEN_BUCKET
                ),
            )
            self.rate_limiter = global_rate_limiter_manager.get_limiter(
                "migration",
                rate_config,
            )

    def _setup_retry_manager(self) -> None:
        """Initialize the retry manager."""
        with self._setup_lock:
            retry_config = RetryConfig(
                max_attempts=self.config.max_retries,
                base_delay=self.config.base_delay,
                max_delay=self.config.max_delay,
                strategy=RetryStrategy.EXPONENTIAL_BACKOFF,
            )
            self.retry_manager = RetryManager(retry_config)

    def _setup_progress_tracker(self) -> None:
        """Initialize the progress tracker."""
        with self._setup_lock:
            if self.config.enable_progress_tracking:
                self.progress_tracker = ProgressTracker(
                    operation_name="Migration",
                    enable_console_output=True,
                    enable_logging=True,
                    enable_file_output=getattr(
                        self.config,
                        "save_progress_to_file",
                        False,
                    ),
                    update_interval=getattr(
                        self.config,
                        "progress_update_interval",
                        0.5,
                    ),
                )
            else:
                self.progress_tracker = None

    @contextmanager
    def _get_executor(self):
        """Context manager for thread pool executor."""
        try:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.config.max_concurrent_batches,
                )
            yield self._executor
        finally:
            # Executor cleanup handled in __exit__ or cleanup()
            pass

    async def process_migration_batch(
        self,
        items: list[T],
        processor_func: Callable[[list[T]], R],
        description: str = "Processing migration batch",
    ) -> list[R]:
        """Process a batch of migration items with full performance optimization."""
        if self._shutdown_event.is_set():
            msg = "Manager is shutting down"
            raise RuntimeError(msg)

        if self.progress_tracker:
            self.progress_tracker.start(total_items=len(items))

        self.metrics.add_total_items(len(items))
        start_time = time.time()

        try:
            # Process in batches with rate limiting and retry logic
            results = await self._process_with_batching(items, processor_func)

            # Update metrics atomically
            batch_time = time.time() - start_time
            self.metrics.record_batch_time(batch_time)
            self.metrics.add_processed_items(len(items))

            if self.progress_tracker:
                self.progress_tracker.update(processed=len(items))

            return results

        except Exception as e:
            self.metrics.add_failed_items(len(items))
            logger.exception(f"Batch processing failed: {e}")
            if self.progress_tracker:
                self.progress_tracker.finish(
                    success=False,
                    final_message=f"Batch failed: {e}",
                )
            raise
        else:
            if self.progress_tracker:
                self.progress_tracker.finish(
                    success=True,
                    final_message=f"Processed {len(items)} items",
                )

    async def _process_with_batching(
        self,
        items: list[T],
        processor_func: Callable[[list[T]], R],
    ) -> list[R]:
        """Process items using the batch processor with proper async/sync integration."""

        def rate_limited_processor(batch: list[T]) -> R:
            """Thread-safe wrapper that applies rate limiting and retries."""
            if self._shutdown_event.is_set():
                msg = "Processing cancelled - manager shutting down"
                raise RuntimeError(msg)

            def process_with_retry():
                # Wait for rate limiting
                delay = self.rate_limiter.wait_if_needed()
                self.metrics.add_wait_time(delay)

                # Process the batch
                try:
                    result = processor_func(batch)
                    self.metrics.record_api_call(True)
                    self.rate_limiter.handle_api_response(True)
                    return result
                except Exception:
                    self.metrics.record_api_call(False)
                    self.rate_limiter.handle_api_response(False)
                    raise

            # Apply retry logic with thread-safe metrics
            try:
                return self.retry_manager.execute_with_retry(process_with_retry)
            except Exception:
                self.metrics.record_retry_attempt()
                raise

        # Use asyncio.to_thread for proper async/sync integration
        batch_stats = await asyncio.to_thread(
            self._run_batch_processor,
            items,
            rate_limited_processor,
        )

        # Update metrics atomically
        self.metrics.add_failed_items(batch_stats["failed_items"])

        return batch_stats["data"]

    def _run_batch_processor(
        self,
        items: list[T],
        processor_func: Callable[[list[T]], R],
    ) -> dict[str, Any]:
        """Run batch processor in a thread-safe manner."""
        with self.batch_processor as bp:
            return bp.process_parallel(items, processor_func)

    async def process_json_files_streaming_async(
        self,
        file_paths: list[Path],
        processor_func: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Process JSON files using streaming with async/await patterns."""
        if self._shutdown_event.is_set():
            msg = "Manager is shutting down"
            raise RuntimeError(msg)

        if self.progress_tracker:
            self.progress_tracker.start(total_items=len(file_paths))

        results = []
        processed_count = 0

        # Use asyncio to process files concurrently
        async def process_file_async(file_path: Path) -> list[dict[str, Any]]:
            """Process a single file asynchronously."""
            return await asyncio.to_thread(
                self._process_single_file,
                file_path,
                processor_func,
            )

        # Create tasks for concurrent processing
        tasks = [process_file_async(file_path) for file_path in file_paths]

        # Process tasks as they complete
        for coro in asyncio.as_completed(tasks):
            try:
                file_results = await coro
                results.extend(file_results)
                processed_count += 1

                if self.progress_tracker:
                    self.progress_tracker.update(processed=1)

            except Exception as e:
                self.logger.exception(f"Failed to process file: {e}")
                self.metrics.add_failed_items(1)
                if self.progress_tracker:
                    self.progress_tracker.update(failed=1)

        if self.progress_tracker:
            self.progress_tracker.finish(
                success=True,
                final_message=f"Processed {processed_count} files",
            )

        return results

    def process_json_files_streaming(
        self,
        file_paths: list[Path],
        processor_func: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Process JSON files using streaming (sync version for backward compatibility)."""
        if self._shutdown_event.is_set():
            msg = "Manager is shutting down"
            raise RuntimeError(msg)

        if self.progress_tracker:
            self.progress_tracker.start(total_items=len(file_paths))

        results = []
        processed_count = 0

        with self._get_executor() as executor:
            # Process files in parallel with thread safety
            future_to_file = {
                executor.submit(
                    self._process_single_file,
                    file_path,
                    processor_func,
                ): file_path
                for file_path in file_paths
            }

            for future in as_completed(future_to_file):
                file_path = future_to_file[future]
                try:
                    file_results = future.result()
                    results.extend(file_results)
                    processed_count += 1

                    if self.progress_tracker:
                        self.progress_tracker.update(processed=1)

                except Exception as e:
                    self.logger.exception(f"Failed to process file {file_path}: {e}")
                    self.metrics.add_failed_items(1)
                    if self.progress_tracker:
                        self.progress_tracker.update(failed=1)

        if self.progress_tracker:
            self.progress_tracker.finish(
                success=True,
                final_message=f"Processed {processed_count} files",
            )

        return results

    def _process_single_file(
        self,
        file_path: Path,
        processor_func: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Thread-safe processing of a single file."""
        if self.config.enable_streaming:
            return self._stream_process_json_file(file_path, processor_func)
        return self._load_process_json_file(file_path, processor_func)

    def _stream_process_json_file(
        self,
        file_path: Path,
        processor_func: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Stream process a JSON file to minimize memory usage."""
        import ijson  # For streaming JSON parsing

        results = []

        try:
            with open(file_path, "rb") as file:
                # Parse JSON objects from the stream
                parser = ijson.parse(file)
                current_object = {}
                object_path = []

                for prefix, event, value in parser:
                    if self._shutdown_event.is_set():
                        msg = "Processing cancelled"
                        raise RuntimeError(msg)

                    if event == "start_map":
                        if not object_path:
                            current_object = {}
                        object_path.append(prefix)
                    elif event == "end_map":
                        if object_path:
                            object_path.pop()
                        if not object_path and current_object:
                            # Process the complete object
                            try:
                                processed = processor_func(current_object)
                                if processed:
                                    results.append(processed)
                            except Exception as e:
                                logger.warning(
                                    f"Failed to process object in {file_path}: {e}",
                                )
                            current_object = {}
                    elif event in ("string", "number", "boolean", "null"):
                        # Build the current object
                        keys = prefix.split(".")
                        obj = current_object
                        for key in keys[:-1]:
                            if key not in obj:
                                obj[key] = {}
                            obj = obj[key]
                        obj[keys[-1]] = value

        except Exception as e:
            logger.exception(f"Streaming JSON processing failed for {file_path}: {e}")
            raise

        return results

    def _load_process_json_file(
        self,
        file_path: Path,
        processor_func: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Load and process entire JSON file."""
        try:
            with open(file_path, encoding="utf-8") as file:
                data = json.load(file)

            # Process based on data structure
            if isinstance(data, list):
                results = []
                for item in data:
                    if self._shutdown_event.is_set():
                        msg = "Processing cancelled"
                        raise RuntimeError(msg)
                    try:
                        processed = processor_func(item)
                        if processed:
                            results.append(processed)
                    except Exception as e:
                        logger.warning(f"Failed to process item in {file_path}: {e}")
                return results
            if isinstance(data, dict):
                processed = processor_func(data)
                return [processed] if processed else []
            logger.warning(f"Unexpected JSON structure in {file_path}")
            return []

        except Exception as e:
            logger.exception(f"JSON file processing failed for {file_path}: {e}")
            raise

    def get_performance_summary(self) -> dict[str, Any]:
        """Get comprehensive performance summary."""
        self.metrics.set_end_time()
        snapshot = self.metrics.get_snapshot()

        total_time = snapshot["total_time"]

        return {
            "timing": {
                "total_time_seconds": total_time,
                "avg_batch_time": snapshot["avg_batch_time"],
                "max_batch_time": snapshot["max_batch_time"],
                "total_wait_time": snapshot["total_wait_time"],
                "processing_efficiency": (
                    (total_time - snapshot["total_wait_time"]) / total_time
                    if total_time > 0
                    else 0
                ),
            },
            "throughput": {
                "items_per_second": (
                    snapshot["processed_items"] / total_time if total_time > 0 else 0
                ),
                "total_items": snapshot["total_items"],
                "processed_items": snapshot["processed_items"],
                "failed_items": snapshot["failed_items"],
                "success_rate": (
                    snapshot["processed_items"] / snapshot["total_items"]
                    if snapshot["total_items"] > 0
                    else 0
                ),
            },
            "api_metrics": {
                "total_api_calls": snapshot["total_api_calls"],
                "failed_api_calls": snapshot["failed_api_calls"],
                "retry_attempts": snapshot["retry_attempts"],
                "rate_limit_hits": snapshot["rate_limit_hits"],
                "api_success_rate": (
                    (snapshot["total_api_calls"] - snapshot["failed_api_calls"])
                    / snapshot["total_api_calls"]
                    if snapshot["total_api_calls"] > 0
                    else 0
                ),
            },
            "rate_limiter": self.rate_limiter.get_metrics().__dict__,
            "batch_processor": {
                "batch_size": self.batch_processor.batch_size,
                "max_workers": self.batch_processor.max_workers,
                "total_items": getattr(self.batch_processor, "total_items", 0),
                "processed_items": getattr(self.batch_processor, "processed_items", 0),
                "failed_items": getattr(self.batch_processor, "failed_items", 0),
            },
            "retry_manager": self.retry_manager.get_metrics(),
        }

    def save_performance_report(self, output_path: Path) -> None:
        """Save detailed performance report to file."""
        report = {
            "migration_performance_report": {
                "timestamp": time.time(),
                "config": self.config.__dict__,
                "summary": self.get_performance_summary(),
                "recommendations": self._generate_performance_recommendations(),
            },
        }

        with open(output_path, "w") as f:
            json.dump(report, f, indent=2)

        logger.info(f"Performance report saved to {output_path}")

    def _generate_performance_recommendations(self) -> list[str]:
        """Generate performance optimization recommendations based on metrics."""
        recommendations = []

        summary = self.get_performance_summary()

        # Analyze success rates
        if summary["throughput"]["success_rate"] < 0.9:
            recommendations.append(
                "Consider increasing retry attempts or improving error handling - "
                f"success rate is {summary['throughput']['success_rate']:.1%}",
            )

        # Analyze API performance
        if summary["api_metrics"]["api_success_rate"] < 0.95:
            recommendations.append(
                "API calls are failing frequently - consider implementing circuit breaker pattern",
            )

        # Analyze rate limiting
        if summary["api_metrics"]["rate_limit_hits"] > 0:
            recommendations.append(
                f"Hit rate limits {summary['api_metrics']['rate_limit_hits']} times - "
                "consider reducing request rate or implementing smarter backoff",
            )

        # Analyze wait time
        efficiency = summary["timing"]["processing_efficiency"]
        if efficiency < 0.7:
            recommendations.append(
                f"Low processing efficiency ({efficiency:.1%}) - "
                "consider optimizing rate limiting or batch sizes",
            )

        # Analyze throughput
        items_per_sec = summary["throughput"]["items_per_second"]
        if items_per_sec < 10:
            recommendations.append(
                f"Low throughput ({items_per_sec:.1f} items/sec) - "
                "consider increasing batch sizes or concurrent processing",
            )

        return recommendations

    def get_performance_metrics(self) -> dict[str, Any]:
        """Get performance metrics in the format expected by tests and external APIs."""
        summary = self.get_performance_summary()

        return {
            "migration": summary["throughput"],
            "rate_limiter": summary["rate_limiter"],
            "batch_processor": summary["batch_processor"],
            "retry_manager": summary["retry_manager"],
        }

    def shutdown(self) -> None:
        """Initiate graceful shutdown."""
        self._shutdown_event.set()
        self.cleanup()

    def cleanup(self) -> None:
        """Clean up resources."""
        if self.progress_tracker:
            self.progress_tracker.finish(
                success=True,
                final_message="Migration completed",
            )

        if self._executor:
            self._executor.shutdown(wait=True)
            self._executor = None


# Convenience functions for common migration patterns


async def process_migration_with_optimization(
    items: list[T],
    processor_func: Callable[[list[T]], R],
    config: PerformanceConfig = None,
    description: str = "Migration processing",
) -> list[R]:
    """Process migration items with full performance optimization."""
    manager = MigrationPerformanceManager(config)

    try:
        results = await manager.process_migration_batch(
            items,
            processor_func,
            description,
        )

        # Log performance summary
        summary = manager.get_performance_summary()
        logger.info(
            f"Migration completed: {summary['throughput']['items_per_second']:.1f} items/sec",
        )

        return results
    finally:
        manager.cleanup()


def process_json_files_optimized(
    file_paths: list[Path],
    processor_func: Callable[[dict[str, Any]], dict[str, Any]],
    config: PerformanceConfig = None,
) -> list[dict[str, Any]]:
    """Process JSON files with memory and performance optimization."""
    manager = MigrationPerformanceManager(config)

    try:
        results = manager.process_json_files_streaming(file_paths, processor_func)

        # Log performance summary
        manager.get_performance_summary()
        logger.info(f"JSON processing completed: processed {len(results)} items")

        return results
    finally:
        manager.cleanup()
