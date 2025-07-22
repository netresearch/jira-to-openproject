"""Comprehensive performance management for migration operations.

This module integrates all performance optimization components (batching, rate limiting,
retry logic, and progress tracking) into a unified system for efficient migration processing.
"""

import asyncio
import time
import logging
from typing import List, Dict, Any, Optional, Callable, TypeVar, Generic, Union
from dataclasses import dataclass, field
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import json

from src import config
from src.utils.batch_processor import BatchProcessor, BatchResult
from src.utils.enhanced_rate_limiter import (
    EnhancedRateLimiter, 
    RateLimitConfig, 
    global_rate_limiter_manager
)
from src.utils.retry_manager import RetryManager, RetryConfig
from src.utils.progress_tracker import ProgressTracker

T = TypeVar('T')
R = TypeVar('R')

logger = logging.getLogger(__name__)


@dataclass
class PerformanceConfig:
    """Configuration for migration performance optimization."""
    
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


@dataclass
class MigrationMetrics:
    """Comprehensive metrics for migration performance."""
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    total_items: int = 0
    processed_items: int = 0
    failed_items: int = 0
    skipped_items: int = 0
    
    # Performance metrics
    total_api_calls: int = 0
    failed_api_calls: int = 0
    retry_attempts: int = 0
    rate_limit_hits: int = 0
    
    # Timing metrics
    avg_batch_time: float = 0.0
    max_batch_time: float = 0.0
    total_wait_time: float = 0.0
    
    # Memory metrics
    peak_memory_usage: float = 0.0
    avg_memory_usage: float = 0.0


class MigrationPerformanceManager:
    """Manages all performance aspects of migration operations."""
    
    def __init__(self, config: PerformanceConfig = None):
        self.config = config or PerformanceConfig()
        self.metrics = MigrationMetrics()
        
        # Initialize components
        self._setup_batch_processor()
        self._setup_rate_limiter()
        self._setup_retry_manager()
        self._setup_progress_tracker()
        
        self._executor = ThreadPoolExecutor(
            max_workers=self.config.max_concurrent_batches
        )
        
    def _setup_batch_processor(self):
        """Initialize the batch processor."""
        self.batch_processor = BatchProcessor(
            batch_size=self.config.batch_size,
            max_workers=self.config.max_concurrent_batches,
            rate_limiter=None,  # Will be set separately
            enable_progress_tracking=self.config.enable_progress_tracking,
            retry_attempts=3
        )
    
    def _setup_rate_limiter(self):
        """Initialize the rate limiter."""
        rate_config = RateLimitConfig(
            max_requests=self.config.max_requests_per_minute,
            time_window=60.0,
            burst_size=self.config.burst_size,
            strategy="adaptive" if self.config.adaptive_rate_limiting else "token_bucket"
        )
        self.rate_limiter = global_rate_limiter_manager.get_limiter("migration", rate_config)
    
    def _setup_retry_manager(self):
        """Initialize the retry manager."""
        retry_config = RetryConfig(
            max_attempts=self.config.max_retries,
            base_delay=self.config.base_delay,
            max_delay=self.config.max_delay,
            strategy="exponential_backoff"
        )
        self.retry_manager = RetryManager(retry_config)
    
    def _setup_progress_tracker(self):
        """Initialize the progress tracker."""
        if self.config.enable_progress_tracking:
            self.progress_tracker = ProgressTracker(
                operation_name="Migration",
                enable_console_output=True,
                enable_logging=True,
                enable_file_output=getattr(self.config, 'save_progress_to_file', False),
                update_interval=getattr(self.config, 'progress_update_interval', 0.5)
            )
        else:
            self.progress_tracker = None
    
    async def process_migration_batch(
        self,
        items: List[T],
        processor_func: Callable[[List[T]], R],
        description: str = "Processing migration batch"
    ) -> List[R]:
        """Process a batch of migration items with full performance optimization."""
        
        if self.progress_tracker:
            task_id = self.progress_tracker.add_task(description, total=len(items))
        
        self.metrics.total_items += len(items)
        start_time = time.time()
        
        try:
            # Process in batches with rate limiting and retry logic
            results = await self._process_with_batching(items, processor_func)
            
            # Update metrics
            batch_time = time.time() - start_time
            self.metrics.avg_batch_time = (
                (self.metrics.avg_batch_time * self.metrics.processed_items + batch_time) /
                (self.metrics.processed_items + len(items))
            )
            self.metrics.max_batch_time = max(self.metrics.max_batch_time, batch_time)
            self.metrics.processed_items += len(items)
            
            if self.progress_tracker:
                self.progress_tracker.update(task_id, advance=len(items))
            
            return results
            
        except Exception as e:
            self.metrics.failed_items += len(items)
            logger.error(f"Batch processing failed: {e}")
            raise
        finally:
            if self.progress_tracker:
                self.progress_tracker.stop_task(task_id)
    
    async def _process_with_batching(
        self,
        items: List[T],
        processor_func: Callable[[List[T]], R]
    ) -> List[R]:
        """Process items using the batch processor with rate limiting and retries."""
        
        def rate_limited_processor(batch: List[T]) -> R:
            """Wrapper that applies rate limiting and retries to batch processing."""
            
            def process_with_retry():
                # Wait for rate limiting
                delay = self.rate_limiter.wait_if_needed()
                self.metrics.total_wait_time += delay
                
                # Process the batch
                try:
                    result = processor_func(batch)
                    self.metrics.total_api_calls += 1
                    self.rate_limiter.handle_api_response(True)
                    return result
                except Exception as e:
                    self.metrics.failed_api_calls += 1
                    self.rate_limiter.handle_api_response(False)
                    raise
            
            # Apply retry logic
            return self.retry_manager.execute_with_retry(process_with_retry)
        
        # Use batch processor
        batch_results = await self.batch_processor.process_batches(
            items,
            rate_limited_processor
        )
        
        # Flatten results
        results = []
        for batch_result in batch_results:
            if batch_result.success:
                results.extend(batch_result.data)
            else:
                self.metrics.failed_items += batch_result.failed_count
        
        return results
    
    def process_json_files_streaming(
        self,
        file_paths: List[Path],
        processor_func: Callable[[Dict[str, Any]], Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Process JSON files using streaming to optimize memory usage."""
        
        if self.progress_tracker:
            task_id = self.progress_tracker.add_task(
                "Processing JSON files", 
                total=len(file_paths)
            )
        
        results = []
        
        for file_path in file_paths:
            try:
                if self.config.enable_streaming:
                    # Stream large JSON files
                    file_results = self._stream_process_json_file(file_path, processor_func)
                else:
                    # Load entire file for smaller files
                    file_results = self._load_process_json_file(file_path, processor_func)
                
                results.extend(file_results)
                
                if self.progress_tracker:
                    self.progress_tracker.update(task_id, advance=1)
                    
            except Exception as e:
                logger.error(f"Failed to process file {file_path}: {e}")
                self.metrics.failed_items += 1
        
        if self.progress_tracker:
            self.progress_tracker.stop_task(task_id)
        
        return results
    
    def _stream_process_json_file(
        self,
        file_path: Path,
        processor_func: Callable[[Dict[str, Any]], Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Stream process a JSON file to minimize memory usage."""
        import ijson  # For streaming JSON parsing
        
        results = []
        
        try:
            with open(file_path, 'rb') as file:
                # Parse JSON objects from the stream
                parser = ijson.parse(file)
                current_object = {}
                object_path = []
                
                for prefix, event, value in parser:
                    if event == 'start_map':
                        if not object_path:
                            current_object = {}
                        object_path.append(prefix)
                    elif event == 'end_map':
                        if object_path:
                            object_path.pop()
                        if not object_path and current_object:
                            # Process the complete object
                            try:
                                processed = processor_func(current_object)
                                if processed:
                                    results.append(processed)
                            except Exception as e:
                                logger.warning(f"Failed to process object in {file_path}: {e}")
                            current_object = {}
                    elif event in ('string', 'number', 'boolean', 'null'):
                        # Build the current object
                        keys = prefix.split('.')
                        obj = current_object
                        for key in keys[:-1]:
                            if key not in obj:
                                obj[key] = {}
                            obj = obj[key]
                        obj[keys[-1]] = value
                        
        except Exception as e:
            logger.error(f"Streaming JSON processing failed for {file_path}: {e}")
            raise
        
        return results
    
    def _load_process_json_file(
        self,
        file_path: Path,
        processor_func: Callable[[Dict[str, Any]], Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Load and process entire JSON file."""
        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                data = json.load(file)
            
            # Process based on data structure
            if isinstance(data, list):
                results = []
                for item in data:
                    try:
                        processed = processor_func(item)
                        if processed:
                            results.append(processed)
                    except Exception as e:
                        logger.warning(f"Failed to process item in {file_path}: {e}")
                return results
            elif isinstance(data, dict):
                processed = processor_func(data)
                return [processed] if processed else []
            else:
                logger.warning(f"Unexpected JSON structure in {file_path}")
                return []
                
        except Exception as e:
            logger.error(f"JSON file processing failed for {file_path}: {e}")
            raise
    
    def get_performance_summary(self) -> Dict[str, Any]:
        """Get comprehensive performance summary."""
        self.metrics.end_time = time.time()
        
        total_time = self.metrics.end_time - self.metrics.start_time
        
        return {
            "timing": {
                "total_time_seconds": total_time,
                "avg_batch_time": self.metrics.avg_batch_time,
                "max_batch_time": self.metrics.max_batch_time,
                "total_wait_time": self.metrics.total_wait_time,
                "processing_efficiency": (
                    (total_time - self.metrics.total_wait_time) / total_time
                    if total_time > 0 else 0
                )
            },
            "throughput": {
                "items_per_second": (
                    self.metrics.processed_items / total_time
                    if total_time > 0 else 0
                ),
                "total_items": self.metrics.total_items,
                "processed_items": self.metrics.processed_items,
                "failed_items": self.metrics.failed_items,
                "success_rate": (
                    self.metrics.processed_items / self.metrics.total_items
                    if self.metrics.total_items > 0 else 0
                )
            },
            "api_metrics": {
                "total_api_calls": self.metrics.total_api_calls,
                "failed_api_calls": self.metrics.failed_api_calls,
                "retry_attempts": self.metrics.retry_attempts,
                "rate_limit_hits": self.metrics.rate_limit_hits,
                "api_success_rate": (
                    (self.metrics.total_api_calls - self.metrics.failed_api_calls) /
                    self.metrics.total_api_calls
                    if self.metrics.total_api_calls > 0 else 0
                )
            },
            "rate_limiter": self.rate_limiter.get_metrics().__dict__,
            "batch_processor": self.batch_processor.get_metrics(),
            "retry_manager": self.retry_manager.get_metrics()
        }
    
    def save_performance_report(self, output_path: Path):
        """Save detailed performance report to file."""
        report = {
            "migration_performance_report": {
                "timestamp": time.time(),
                "config": self.config.__dict__,
                "summary": self.get_performance_summary(),
                "recommendations": self._generate_performance_recommendations()
            }
        }
        
        with open(output_path, 'w') as f:
            json.dump(report, f, indent=2)
        
        logger.info(f"Performance report saved to {output_path}")
    
    def _generate_performance_recommendations(self) -> List[str]:
        """Generate performance optimization recommendations based on metrics."""
        recommendations = []
        
        summary = self.get_performance_summary()
        
        # Analyze success rates
        if summary["throughput"]["success_rate"] < 0.9:
            recommendations.append(
                "Consider increasing retry attempts or improving error handling - "
                f"success rate is {summary['throughput']['success_rate']:.1%}"
            )
        
        # Analyze API performance
        if summary["api_metrics"]["api_success_rate"] < 0.95:
            recommendations.append(
                "API calls are failing frequently - consider implementing circuit breaker pattern"
            )
        
        # Analyze rate limiting
        if summary["api_metrics"]["rate_limit_hits"] > 0:
            recommendations.append(
                f"Hit rate limits {summary['api_metrics']['rate_limit_hits']} times - "
                "consider reducing request rate or implementing smarter backoff"
            )
        
        # Analyze wait time
        efficiency = summary["timing"]["processing_efficiency"]
        if efficiency < 0.7:
            recommendations.append(
                f"Low processing efficiency ({efficiency:.1%}) - "
                "consider optimizing rate limiting or batch sizes"
            )
        
        # Analyze throughput
        items_per_sec = summary["throughput"]["items_per_second"]
        if items_per_sec < 10:
            recommendations.append(
                f"Low throughput ({items_per_sec:.1f} items/sec) - "
                "consider increasing batch sizes or concurrent processing"
            )
        
        return recommendations
    
    def cleanup(self):
        """Clean up resources."""
        if self.progress_tracker:
            self.progress_tracker.stop()
        
        if hasattr(self, '_executor'):
            self._executor.shutdown(wait=True)


# Convenience functions for common migration patterns

async def process_migration_with_optimization(
    items: List[T],
    processor_func: Callable[[List[T]], R],
    config: PerformanceConfig = None,
    description: str = "Migration processing"
) -> List[R]:
    """Process migration items with full performance optimization."""
    
    manager = MigrationPerformanceManager(config)
    
    try:
        results = await manager.process_migration_batch(
            items, processor_func, description
        )
        
        # Log performance summary
        summary = manager.get_performance_summary()
        logger.info(f"Migration completed: {summary['throughput']['items_per_second']:.1f} items/sec")
        
        return results
    finally:
        manager.cleanup()


def process_json_files_optimized(
    file_paths: List[Path],
    processor_func: Callable[[Dict[str, Any]], Dict[str, Any]],
    config: PerformanceConfig = None
) -> List[Dict[str, Any]]:
    """Process JSON files with memory and performance optimization."""
    
    manager = MigrationPerformanceManager(config)
    
    try:
        results = manager.process_json_files_streaming(file_paths, processor_func)
        
        # Log performance summary
        summary = manager.get_performance_summary()
        logger.info(f"JSON processing completed: processed {len(results)} items")
        
        return results
    finally:
        manager.cleanup() 