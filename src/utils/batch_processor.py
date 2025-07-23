"""Efficient batching system for API calls in migration components.

This module provides a configurable batching mechanism that can process large datasets
in chunks, with support for parallel processing, error handling, and progress tracking.
"""

import asyncio
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, List, Dict, Optional, TypeVar, Generic, Union
from dataclasses import dataclass
from pathlib import Path
import json
from contextlib import contextmanager

from src import config
from src.utils.rate_limiter import RateLimiter
from src.utils.config_validation import SecurityValidator, ConfigurationValidationError

T = TypeVar('T')
R = TypeVar('R')

@dataclass
class BatchResult:
    """Result of a batch operation."""
    success: bool
    batch_id: int
    processed_count: int
    failed_count: int
    data: List[Any]
    errors: List[str]
    execution_time: float


class ThreadSafeBatchProcessor(Generic[T, R]):
    """Thread-safe batch processor for API operations with parallelization support."""
    
    def __init__(
        self,
        batch_size: int = 100,
        max_workers: int = 4,
        rate_limiter: Optional[RateLimiter] = None,
        enable_progress_tracking: bool = True,
        retry_attempts: int = 3,
    ):
        # Comprehensive input validation using SecurityValidator
        try:
            self.batch_size = SecurityValidator.validate_numeric_parameter('batch_size', batch_size)
            self.max_workers = SecurityValidator.validate_numeric_parameter('max_workers', max_workers)
            self.retry_attempts = SecurityValidator.validate_numeric_parameter('retry_attempts', retry_attempts)
            
            # Validate resource allocation to prevent system overload
            SecurityValidator.validate_resource_allocation(self.batch_size, self.max_workers, 1024)  # Default 1GB memory limit
            
        except ConfigurationValidationError as e:
            config.logger.error(f"BatchProcessor validation failed: {e}")
            raise
            
        self.rate_limiter = rate_limiter
        self.enable_progress_tracking = enable_progress_tracking
        self.logger = config.logger
        
        # Thread-safe state management
        self._lock = threading.RLock()
        self._total_items = 0
        self._processed_items = 0
        self._failed_items = 0
        self._progress_callbacks: List[Callable[[int, int, int], None]] = []
        self._batch_results: List[BatchResult] = []
        self._executor = None
        self._shutdown_event = threading.Event()
        
    def __enter__(self):
        """Context manager entry - initialize executor."""
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=self.max_workers)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensure executor cleanup."""
        self._shutdown_event.set()
        with self._lock:
            if self._executor:
                self._executor.shutdown(wait=True)
                self._executor = None
        
    def add_progress_callback(self, callback: Callable[[int, int, int], None]) -> None:
        """Thread-safely add a progress callback function.
        
        Args:
            callback: Function that receives (processed, total, failed) counts
        """
        with self._lock:
            self._progress_callbacks.append(callback)
            
    def _emit_progress(self) -> None:
        """Thread-safely emit progress to all registered callbacks."""
        if not self.enable_progress_tracking:
            return
            
        with self._lock:
            processed = self._processed_items
            total = self._total_items
            failed = self._failed_items
            callbacks = self._progress_callbacks.copy()
        
        # Call callbacks outside the lock to prevent deadlock
        for callback in callbacks:
            try:
                callback(processed, total, failed)
            except Exception as e:
                self.logger.warning(f"Progress callback error: {e}")
                
    def _create_batches(self, items: List[T]) -> List[List[T]]:
        """Split items into batches of configured size.
        
        Args:
            items: List of items to batch
            
        Returns:
            List of batches
        """
        batches = []
        for i in range(0, len(items), self.batch_size):
            batch = items[i:i + self.batch_size]
            batches.append(batch)
        return batches
        
    def _process_batch_with_retry(
        self,
        batch: List[T],
        batch_id: int,
        processor_func: Callable[[List[T]], List[R]],
    ) -> BatchResult:
        """Process a single batch with retry logic and thread safety.
        
        Args:
            batch: Batch of items to process
            batch_id: Unique identifier for this batch
            processor_func: Function to process the batch
            
        Returns:
            BatchResult with processing outcome
        """
        if self._shutdown_event.is_set():
            raise RuntimeError("Processor is shutting down")
            
        start_time = time.time()
        last_exception = None
        
        for attempt in range(self.retry_attempts + 1):
            try:
                # Apply rate limiting if configured
                if self.rate_limiter:
                    self.rate_limiter.acquire()
                
                # Check for shutdown before processing
                if self._shutdown_event.is_set():
                    raise RuntimeError("Processing cancelled - shutting down")
                
                # Process the batch
                results = processor_func(batch)
                
                # Create successful result
                execution_time = time.time() - start_time
                return BatchResult(
                    success=True,
                    batch_id=batch_id,
                    processed_count=len(batch),
                    failed_count=0,
                    data=results,
                    errors=[],
                    execution_time=execution_time
                )
                
            except Exception as e:
                last_exception = e
                self.logger.warning(
                    f"Batch {batch_id} attempt {attempt + 1} failed: {e}"
                )
                
                # Exponential backoff for retries
                if attempt < self.retry_attempts and not self._shutdown_event.is_set():
                    delay = 2 ** attempt
                    time.sleep(delay)
                    
        # All attempts failed
        execution_time = time.time() - start_time
        return BatchResult(
            success=False,
            batch_id=batch_id,
            processed_count=0,
            failed_count=len(batch),
            data=[],
            errors=[str(last_exception)],
            execution_time=execution_time
        )
        
    def _update_progress_atomically(self, batch_result: BatchResult) -> None:
        """Atomically update progress counters."""
        with self._lock:
            self._batch_results.append(batch_result)
            
            if batch_result.success:
                self._processed_items += batch_result.processed_count
            else:
                self._failed_items += batch_result.failed_count
        
        # Emit progress outside lock
        self._emit_progress()
        
    def process_parallel(
        self,
        items: List[T],
        processor_func: Callable[[List[T]], List[R]],
    ) -> Dict[str, Any]:
        """Process items in parallel batches with thread safety.
        
        Args:
            items: List of items to process
            processor_func: Function that processes a batch and returns results
            
        Returns:
            Dictionary with processing results and statistics
        """
        # Initialize state atomically
        with self._lock:
            self._total_items = len(items)
            self._processed_items = 0
            self._failed_items = 0
            self._batch_results = []
        
        self.logger.info(
            f"Starting parallel batch processing: {self._total_items} items, "
            f"batch_size={self.batch_size}, workers={self.max_workers}"
        )
        
        start_time = time.time()
        batches = self._create_batches(items)
        all_results = []
        all_errors = []
        
        # Ensure we have an executor
        if self._executor is None:
            raise RuntimeError("BatchProcessor must be used as context manager")
        
        # Process batches in parallel
        try:
            # Submit all batch jobs
            future_to_batch = {
                self._executor.submit(
                    self._process_batch_with_retry, 
                    batch, 
                    batch_id, 
                    processor_func
                ): batch_id
                for batch_id, batch in enumerate(batches)
            }
            
            # Collect results as they complete
            for future in as_completed(future_to_batch):
                if self._shutdown_event.is_set():
                    break
                    
                batch_result = future.result()
                self._update_progress_atomically(batch_result)
                
                if batch_result.success:
                    all_results.extend(batch_result.data)
                else:
                    all_errors.extend(batch_result.errors)
                    
                self.logger.debug(
                    f"Batch {batch_result.batch_id} completed: "
                    f"success={batch_result.success}, "
                    f"processed={batch_result.processed_count}, "
                    f"time={batch_result.execution_time:.2f}s"
                )
        
        except Exception as e:
            self.logger.error(f"Parallel processing error: {e}")
            raise
        
        total_time = time.time() - start_time
        
        # Get final counts atomically
        with self._lock:
            processed_count = self._processed_items
            failed_count = self._failed_items
            successful_batches = sum(1 for r in self._batch_results if r.success)
            failed_batches = sum(1 for r in self._batch_results if not r.success)
        
        # Compile final results
        results = {
            "success": failed_count == 0,
            "total_items": self._total_items,
            "processed_items": processed_count,
            "failed_items": failed_count,
            "total_batches": len(batches),
            "successful_batches": successful_batches,
            "failed_batches": failed_batches,
            "data": all_results,
            "errors": all_errors,
            "execution_time": total_time,
            "throughput_items_per_second": processed_count / total_time if total_time > 0 else 0,
        }
        
        self.logger.info(
            f"Batch processing completed: {processed_count}/{self._total_items} items "
            f"in {total_time:.2f}s ({results['throughput_items_per_second']:.1f} items/s)"
        )
        
        return results
        
    def process_sequential(
        self,
        items: List[T],
        processor_func: Callable[[List[T]], List[R]],
    ) -> Dict[str, Any]:
        """Process items in sequential batches with thread safety.
        
        Args:
            items: List of items to process
            processor_func: Function that processes a batch and returns results
            
        Returns:
            Dictionary with processing results and statistics
        """
        # Initialize state atomically
        with self._lock:
            self._total_items = len(items)
            self._processed_items = 0
            self._failed_items = 0
            self._batch_results = []
        
        self.logger.info(
            f"Starting sequential batch processing: {self._total_items} items, "
            f"batch_size={self.batch_size}"
        )
        
        start_time = time.time()
        batches = self._create_batches(items)
        all_results = []
        all_errors = []
        
        # Process batches sequentially
        for batch_id, batch in enumerate(batches):
            if self._shutdown_event.is_set():
                break
                
            batch_result = self._process_batch_with_retry(batch, batch_id, processor_func)
            self._update_progress_atomically(batch_result)
            
            if batch_result.success:
                all_results.extend(batch_result.data)
            else:
                all_errors.extend(batch_result.errors)
                
            self.logger.debug(
                f"Batch {batch_id} completed: "
                f"success={batch_result.success}, "
                f"processed={batch_result.processed_count}, "
                f"time={batch_result.execution_time:.2f}s"
            )
        
        total_time = time.time() - start_time
        
        # Get final counts atomically
        with self._lock:
            processed_count = self._processed_items
            failed_count = self._failed_items
            successful_batches = sum(1 for r in self._batch_results if r.success)
            failed_batches = sum(1 for r in self._batch_results if not r.success)
        
        # Compile final results
        results = {
            "success": failed_count == 0,
            "total_items": self._total_items,
            "processed_items": processed_count,
            "failed_items": failed_count,
            "total_batches": len(batches),
            "successful_batches": successful_batches,
            "failed_batches": failed_batches,
            "data": all_results,
            "errors": all_errors,
            "execution_time": total_time,
            "throughput_items_per_second": processed_count / total_time if total_time > 0 else 0,
        }
        
        self.logger.info(
            f"Sequential processing completed: {processed_count}/{self._total_items} items "
            f"in {total_time:.2f}s ({results['throughput_items_per_second']:.1f} items/s)"
        )
        
        return results
        
    def shutdown(self):
        """Initiate graceful shutdown."""
        self._shutdown_event.set()
        with self._lock:
            if self._executor:
                self._executor.shutdown(wait=True)
                self._executor = None


# Maintain backward compatibility with existing code
BatchProcessor = ThreadSafeBatchProcessor


class StreamingJSONProcessor:
    """Memory-efficient JSON processor for large files."""
    
    def __init__(self, chunk_size: int = 8192):
        """Initialize the streaming processor.
        
        Args:
            chunk_size: Size of chunks to read from file
        """
        self.chunk_size = chunk_size
        self.logger = config.logger
        self._shutdown_event = threading.Event()
        
    def process_large_json_file(
        self,
        file_path: Path,
        processor_func: Callable[[Dict[str, Any]], Any],
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> Dict[str, Any]:
        """Process a large JSON file without loading everything into memory.
        
        Args:
            file_path: Path to the JSON file
            processor_func: Function to process each JSON object
            progress_callback: Optional callback for progress updates
            
        Returns:
            Processing results
        """
        import ijson  # Requires: pip install ijson
        
        self.logger.info(f"Starting streaming JSON processing: {file_path}")
        start_time = time.time()
        
        processed_count = 0
        failed_count = 0
        results = []
        errors = []
        
        try:
            with open(file_path, 'rb') as file:
                # Parse JSON objects incrementally
                parser = ijson.parse(file)
                
                for prefix, event, value in parser:
                    if self._shutdown_event.is_set():
                        break
                        
                    try:
                        if event == 'end_map':  # Complete JSON object
                            result = processor_func(value)
                            if result is not None:
                                results.append(result)
                            processed_count += 1
                            
                            if progress_callback and processed_count % 100 == 0:
                                progress_callback(processed_count)
                                
                    except Exception as e:
                        failed_count += 1
                        errors.append(f"Processing error at item {processed_count}: {e}")
                        self.logger.warning(f"Failed to process item {processed_count}: {e}")
                        
        except Exception as e:
            error_msg = f"File processing error: {e}"
            errors.append(error_msg)
            self.logger.error(error_msg)
            
        total_time = time.time() - start_time
        
        result_summary = {
            "success": failed_count == 0,
            "processed_count": processed_count,
            "failed_count": failed_count,
            "results": results,
            "errors": errors,
            "execution_time": total_time,
            "throughput_items_per_second": processed_count / total_time if total_time > 0 else 0,
        }
        
        self.logger.info(
            f"Streaming processing completed: {processed_count} items "
            f"in {total_time:.2f}s ({result_summary['throughput_items_per_second']:.1f} items/s)"
        )
        
        return result_summary
    
    def shutdown(self):
        """Initiate graceful shutdown."""
        self._shutdown_event.set()


def create_default_batch_processor(
    batch_size: Optional[int] = None,
    max_workers: Optional[int] = None,
    enable_rate_limiting: bool = True,
) -> ThreadSafeBatchProcessor:
    """Create a batch processor with default configuration.
    
    Args:
        batch_size: Override default batch size
        max_workers: Override default worker count
        enable_rate_limiting: Whether to enable rate limiting
        
    Returns:
        Configured ThreadSafeBatchProcessor instance
    """
    # Get configuration from migration config
    default_batch_size = config.migration_config.get("batch_size", 100)
    default_workers = config.migration_config.get("max_workers", 4)
    
    # Create rate limiter if enabled
    rate_limiter = None
    if enable_rate_limiting:
        rate_limiter = RateLimiter(
            calls_per_second=config.migration_config.get("rate_limit", 10),
            burst_size=config.migration_config.get("burst_size", 20)
        )
    
    return ThreadSafeBatchProcessor(
        batch_size=batch_size or default_batch_size,
        max_workers=max_workers or default_workers,
        rate_limiter=rate_limiter,
        enable_progress_tracking=True,
        retry_attempts=3,
    )
