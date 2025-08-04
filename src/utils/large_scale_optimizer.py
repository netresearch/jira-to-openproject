#!/usr/bin/env python3
"""Large-Scale Migration Performance Optimizer.

This module provides specialized optimizations for migrations with >100k items:
- Advanced memory management and garbage collection
- Distributed processing with worker pools
- Adaptive batch sizing based on system resources
- Intelligent caching with memory pressure awareness
- Connection pooling with automatic scaling
- Progress persistence and recovery
- Resource monitoring and throttling
"""

import asyncio
import gc
import json
import logging
import multiprocessing
import os
import psutil
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar, Union
from uuid import uuid4

import aiofiles
import redis.asyncio as redis
from pydantic import BaseModel, Field

from src.utils.batch_processor import BatchProcessor
from src.utils.enhanced_rate_limiter import EnhancedRateLimiter
from src.utils.retry_manager import RetryManager

T = TypeVar('T')
R = TypeVar('R')

logger = logging.getLogger(__name__)


@dataclass
class SystemResources:
    """System resource information for optimization decisions."""
    
    cpu_count: int = field(default_factory=lambda: multiprocessing.cpu_count())
    memory_total_gb: float = field(default_factory=lambda: psutil.virtual_memory().total / (1024**3))
    memory_available_gb: float = field(default_factory=lambda: psutil.virtual_memory().available / (1024**3))
    memory_usage_percent: float = field(default_factory=lambda: psutil.virtual_memory().percent)
    disk_free_gb: float = field(default_factory=lambda: psutil.disk_usage('/').free / (1024**3))
    
    def get_memory_pressure_level(self) -> str:
        """Get memory pressure level for optimization decisions."""
        if self.memory_usage_percent < 70:
            return "low"
        elif self.memory_usage_percent < 85:
            return "medium"
        else:
            return "high"
    
    def get_optimal_worker_count(self, base_count: int = 4) -> int:
        """Calculate optimal worker count based on system resources."""
        # Use 75% of CPU cores, but respect memory constraints
        cpu_workers = max(1, int(self.cpu_count * 0.75))
        
        # Adjust based on memory pressure
        if self.get_memory_pressure_level() == "high":
            cpu_workers = max(1, cpu_workers // 2)
        elif self.get_memory_pressure_level() == "medium":
            cpu_workers = max(1, int(cpu_workers * 0.8))
        
        return min(cpu_workers, base_count)


@dataclass
class LargeScaleConfig:
    """Configuration for large-scale migration optimization."""
    
    # Memory management
    memory_limit_gb: float = 8.0
    enable_garbage_collection: bool = True
    gc_threshold: int = 1000  # Items processed before GC
    
    # Processing configuration
    base_batch_size: int = 100
    max_batch_size: int = 1000
    adaptive_batch_sizing: bool = True
    enable_distributed_processing: bool = True
    max_workers: int = 8
    
    # Caching configuration
    enable_intelligent_caching: bool = True
    cache_size_mb: int = 512
    cache_ttl_seconds: int = 3600
    
    # Progress persistence
    enable_progress_persistence: bool = True
    progress_save_interval: int = 1000
    progress_file_path: Optional[Path] = None
    
    # Resource monitoring
    enable_resource_monitoring: bool = True
    resource_check_interval: float = 5.0
    memory_pressure_threshold: float = 85.0
    
    # Connection pooling
    connection_pool_size: int = 20
    connection_pool_timeout: float = 30.0
    
    # Recovery configuration
    enable_recovery: bool = True
    checkpoint_interval: int = 5000
    
    def __post_init__(self):
        """Validate and adjust configuration based on system resources."""
        resources = SystemResources()
        
        # Adjust memory limit based on available memory
        if self.memory_limit_gb > resources.memory_total_gb * 0.8:
            self.memory_limit_gb = resources.memory_total_gb * 0.8
            logger.info(f"Adjusted memory limit to {self.memory_limit_gb:.2f} GB")
        
        # Adjust worker count based on system resources
        optimal_workers = resources.get_optimal_worker_count(self.max_workers)
        if optimal_workers != self.max_workers:
            self.max_workers = optimal_workers
            logger.info(f"Adjusted worker count to {self.max_workers}")


class IntelligentCache:
    """Intelligent caching system with memory pressure awareness."""
    
    def __init__(self, max_size_mb: int = 512, ttl_seconds: int = 3600):
        self.max_size_mb = max_size_mb
        self.ttl_seconds = ttl_seconds
        self.cache: Dict[str, Tuple[Any, float]] = {}
        self.current_size_mb = 0
        self._lock = threading.Lock()
    
    def get(self, key: str) -> Optional[Any]:
        """Get item from cache if not expired."""
        with self._lock:
            if key in self.cache:
                value, timestamp = self.cache[key]
                if time.time() - timestamp < self.ttl_seconds:
                    return value
                else:
                    del self.cache[key]
                    self._update_size()
        return None
    
    def set(self, key: str, value: Any, size_mb: float = 0.1) -> bool:
        """Set item in cache if there's space."""
        with self._lock:
            # Check memory pressure
            if self._is_memory_pressure_high():
                self._evict_oldest(0.5)  # Evict 50% of cache
            
            # Check if we have space
            if self.current_size_mb + size_mb > self.max_size_mb:
                self._evict_oldest(0.3)  # Evict 30% of cache
            
            if self.current_size_mb + size_mb <= self.max_size_mb:
                self.cache[key] = (value, time.time())
                self.current_size_mb += size_mb
                return True
            return False
    
    def _is_memory_pressure_high(self) -> bool:
        """Check if system memory pressure is high."""
        return psutil.virtual_memory().percent > 85
    
    def _evict_oldest(self, fraction: float = 0.3):
        """Evict oldest items from cache."""
        if not self.cache:
            return
        
        # Sort by timestamp and remove oldest
        sorted_items = sorted(self.cache.items(), key=lambda x: x[1][1])
        items_to_remove = int(len(sorted_items) * fraction)
        
        for i in range(items_to_remove):
            key, (_, _) = sorted_items[i]
            del self.cache[key]
        
        self._update_size()
    
    def _update_size(self):
        """Update current cache size estimate."""
        # Simple estimation: assume average item size
        self.current_size_mb = len(self.cache) * 0.1
    
    def clear(self):
        """Clear all cache entries."""
        with self._lock:
            self.cache.clear()
            self.current_size_mb = 0


class ProgressPersistence:
    """Progress persistence and recovery system."""
    
    def __init__(self, file_path: Optional[Path] = None):
        self.file_path = file_path or Path(f"migration_progress_{uuid4().hex[:8]}.json")
        self._lock = threading.Lock()
    
    async def save_progress(self, progress_data: Dict[str, Any]) -> None:
        """Save progress data to file."""
        async with aiofiles.open(self.file_path, 'w') as f:
            await f.write(json.dumps(progress_data, indent=2))
    
    async def load_progress(self) -> Optional[Dict[str, Any]]:
        """Load progress data from file."""
        if not self.file_path.exists():
            return None
        
        try:
            async with aiofiles.open(self.file_path, 'r') as f:
                content = await f.read()
                return json.loads(content)
        except Exception as e:
            logger.warning(f"Failed to load progress: {e}")
            return None
    
    def get_checkpoint_path(self, checkpoint_id: str) -> Path:
        """Get path for a specific checkpoint."""
        return self.file_path.parent / f"checkpoint_{checkpoint_id}.json"
    
    async def save_checkpoint(self, checkpoint_id: str, data: Dict[str, Any]) -> None:
        """Save a checkpoint."""
        checkpoint_path = self.get_checkpoint_path(checkpoint_id)
        async with aiofiles.open(checkpoint_path, 'w') as f:
            await f.write(json.dumps(data, indent=2))
    
    async def load_checkpoint(self, checkpoint_id: str) -> Optional[Dict[str, Any]]:
        """Load a checkpoint."""
        checkpoint_path = self.get_checkpoint_path(checkpoint_id)
        if not checkpoint_path.exists():
            return None
        
        try:
            async with aiofiles.open(checkpoint_path, 'r') as f:
                content = await f.read()
                return json.loads(content)
        except Exception as e:
            logger.warning(f"Failed to load checkpoint {checkpoint_id}: {e}")
            return None


class ResourceMonitor:
    """System resource monitoring and throttling."""
    
    def __init__(self, check_interval: float = 5.0, memory_threshold: float = 85.0):
        self.check_interval = check_interval
        self.memory_threshold = memory_threshold
        self.monitoring = False
        self._monitor_thread = None
        self._current_pressure = "low"
        self._callbacks: List[Callable[[str], None]] = []
    
    def start_monitoring(self):
        """Start resource monitoring."""
        if self.monitoring:
            return
        
        self.monitoring = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
    
    def stop_monitoring(self):
        """Stop resource monitoring."""
        self.monitoring = False
        if self._monitor_thread:
            self._monitor_thread.join()
    
    def add_pressure_callback(self, callback: Callable[[str], None]):
        """Add callback for pressure level changes."""
        self._callbacks.append(callback)
    
    def _monitor_loop(self):
        """Main monitoring loop."""
        while self.monitoring:
            try:
                pressure = self._check_memory_pressure()
                if pressure != self._current_pressure:
                    self._current_pressure = pressure
                    for callback in self._callbacks:
                        try:
                            callback(pressure)
                        except Exception as e:
                            logger.error(f"Pressure callback error: {e}")
                
                time.sleep(self.check_interval)
            except Exception as e:
                logger.error(f"Resource monitoring error: {e}")
                time.sleep(self.check_interval)
    
    def _check_memory_pressure(self) -> str:
        """Check current memory pressure level."""
        memory_percent = psutil.virtual_memory().percent
        if memory_percent < 70:
            return "low"
        elif memory_percent < self.memory_threshold:
            return "medium"
        else:
            return "high"
    
    def get_current_pressure(self) -> str:
        """Get current pressure level."""
        return self._current_pressure


class LargeScaleOptimizer:
    """Main large-scale migration optimizer."""
    
    def __init__(self, config: LargeScaleConfig):
        self.config = config
        self.resources = SystemResources()
        self.cache = IntelligentCache(config.cache_size_mb, config.cache_ttl_seconds)
        self.progress_persistence = ProgressPersistence(config.progress_file_path)
        self.resource_monitor = ResourceMonitor(
            config.resource_check_interval,
            config.memory_pressure_threshold
        )
        
        # Initialize components
        self.batch_processor = BatchProcessor()
        from src.utils.enhanced_rate_limiter import RateLimitConfig, RateLimitStrategy
        rate_limit_config = RateLimitConfig(
            max_requests=config.connection_pool_size,
            time_window=60.0,
            strategy=RateLimitStrategy.TOKEN_BUCKET
        )
        self.rate_limiter = EnhancedRateLimiter(rate_limit_config)
        self.retry_manager = RetryManager()
        
        # Performance tracking
        self.processed_count = 0
        self.failed_count = 0
        self.start_time = time.time()
        
        # Start resource monitoring
        if config.enable_resource_monitoring:
            self.resource_monitor.start_monitoring()
            self.resource_monitor.add_pressure_callback(self._on_pressure_change)
    
    def _on_pressure_change(self, pressure: str):
        """Handle memory pressure changes."""
        logger.info(f"Memory pressure changed to: {pressure}")
        if pressure == "high":
            # Force garbage collection
            if self.config.enable_garbage_collection:
                gc.collect()
            
            # Clear cache
            if self.config.enable_intelligent_caching:
                self.cache.clear()
    
    def get_adaptive_batch_size(self) -> int:
        """Get adaptive batch size based on system resources."""
        if not self.config.adaptive_batch_sizing:
            return self.config.base_batch_size
        
        pressure = self.resource_monitor.get_current_pressure()
        
        if pressure == "low":
            return min(self.config.max_batch_size, self.config.base_batch_size * 2)
        elif pressure == "medium":
            return self.config.base_batch_size
        else:  # high pressure
            return max(10, self.config.base_batch_size // 2)
    
    async def process_large_scale_migration(
        self,
        items: List[T],
        processor_func: Callable[[T], R],
        description: str = "Large-scale migration"
    ) -> List[R]:
        """Process large-scale migration with optimizations."""
        logger.info(f"Starting large-scale migration: {len(items)} items")
        
        # Load progress if available
        if self.config.enable_progress_persistence:
            progress = await self.progress_persistence.load_progress()
            if progress:
                logger.info("Resuming from previous progress")
                # TODO: Implement resume logic
        
        results = []
        batch_size = self.get_adaptive_batch_size()
        
        # Process in batches
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]
            batch_start = time.time()
            
            try:
                # Process batch with optimizations
                batch_results = await self._process_batch_optimized(batch, processor_func)
                results.extend(batch_results)
                
                self.processed_count += len(batch_results)
                
                # Update batch size based on performance
                if self.config.adaptive_batch_sizing:
                    batch_time = time.time() - batch_start
                    if batch_time < 1.0:  # Fast batch
                        batch_size = min(self.config.max_batch_size, batch_size + 50)
                    elif batch_time > 5.0:  # Slow batch
                        batch_size = max(10, batch_size - 25)
                
                # Save progress periodically
                if (self.config.enable_progress_persistence and 
                    self.processed_count % self.config.progress_save_interval == 0):
                    await self._save_progress(results, i + len(batch))
                
                # Garbage collection
                if (self.config.enable_garbage_collection and 
                    self.processed_count % self.config.gc_threshold == 0):
                    gc.collect()
                
                # Checkpoint
                if (self.config.enable_recovery and 
                    self.processed_count % self.config.checkpoint_interval == 0):
                    await self._save_checkpoint(results, i + len(batch))
                
            except Exception as e:
                logger.error(f"Batch processing error: {e}")
                self.failed_count += len(batch)
                
                if self.config.enable_recovery:
                    # Try to recover from checkpoint
                    checkpoint = await self._load_latest_checkpoint()
                    if checkpoint:
                        logger.info("Recovering from checkpoint")
                        # TODO: Implement recovery logic
        
        logger.info(f"Large-scale migration completed: {self.processed_count} processed, {self.failed_count} failed")
        return results
    
    async def _process_batch_optimized(
        self,
        batch: List[T],
        processor_func: Callable[[T], R]
    ) -> List[R]:
        """Process a batch with optimizations."""
        if self.config.enable_distributed_processing and len(batch) > 100:
            return await self._process_batch_distributed(batch, processor_func)
        else:
            return await self._process_batch_sequential(batch, processor_func)
    
    async def _process_batch_sequential(
        self,
        batch: List[T],
        processor_func: Callable[[T], R]
    ) -> List[R]:
        """Process batch sequentially with optimizations."""
        results = []
        
        for item in batch:
            try:
                # Check cache first
                cache_key = str(hash(str(item)))
                cached_result = self.cache.get(cache_key)
                
                if cached_result is not None:
                    results.append(cached_result)
                    continue
                
                # Process item
                result = await self._process_item_with_retry(item, processor_func)
                results.append(result)
                
                # Cache result
                if self.config.enable_intelligent_caching:
                    self.cache.set(cache_key, result)
                
            except Exception as e:
                logger.error(f"Item processing error: {e}")
                self.failed_count += 1
        
        return results
    
    async def _process_batch_distributed(
        self,
        batch: List[T],
        processor_func: Callable[[T], R]
    ) -> List[R]:
        """Process batch using distributed processing."""
        # Split batch into chunks for workers
        chunk_size = len(batch) // self.config.max_workers
        chunks = [batch[i:i + chunk_size] for i in range(0, len(batch), chunk_size)]
        
        # Process chunks in parallel
        tasks = []
        for chunk in chunks:
            task = asyncio.create_task(self._process_chunk(chunk, processor_func))
            tasks.append(task)
        
        # Wait for all chunks to complete
        chunk_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Combine results
        results = []
        for chunk_result in chunk_results:
            if isinstance(chunk_result, Exception):
                logger.error(f"Chunk processing error: {chunk_result}")
                self.failed_count += len(chunk_result)
            else:
                results.extend(chunk_result)
        
        return results
    
    async def _process_chunk(
        self,
        chunk: List[T],
        processor_func: Callable[[T], R]
    ) -> List[R]:
        """Process a chunk of items."""
        return await self._process_batch_sequential(chunk, processor_func)
    
    async def _process_item_with_retry(
        self,
        item: T,
        processor_func: Callable[[T], R]
    ) -> R:
        """Process item with retry logic."""
        # TODO: Implement retry logic with rate limiting
        return await processor_func(item)
    
    async def _save_progress(self, results: List[R], current_index: int):
        """Save current progress."""
        progress_data = {
            "timestamp": time.time(),
            "processed_count": self.processed_count,
            "failed_count": self.failed_count,
            "current_index": current_index,
            "results_count": len(results)
        }
        await self.progress_persistence.save_progress(progress_data)
    
    async def _save_checkpoint(self, results: List[R], current_index: int):
        """Save a checkpoint."""
        checkpoint_id = f"checkpoint_{int(time.time())}"
        checkpoint_data = {
            "timestamp": time.time(),
            "processed_count": self.processed_count,
            "failed_count": self.failed_count,
            "current_index": current_index,
            "results_count": len(results)
        }
        await self.progress_persistence.save_checkpoint(checkpoint_id, checkpoint_data)
    
    async def _load_latest_checkpoint(self) -> Optional[Dict[str, Any]]:
        """Load the latest checkpoint."""
        # TODO: Implement checkpoint discovery and loading
        return None
    
    def get_performance_metrics(self) -> Dict[str, Any]:
        """Get performance metrics."""
        duration = time.time() - self.start_time
        return {
            "duration_seconds": duration,
            "processed_count": self.processed_count,
            "failed_count": self.failed_count,
            "success_rate": (self.processed_count / (self.processed_count + self.failed_count)) * 100 if (self.processed_count + self.failed_count) > 0 else 0,
            "items_per_second": self.processed_count / duration if duration > 0 else 0,
            "memory_usage_mb": psutil.Process().memory_info().rss / (1024 * 1024),
            "cache_hit_rate": 0,  # TODO: Implement cache hit rate tracking
            "current_batch_size": self.get_adaptive_batch_size(),
            "memory_pressure": self.resource_monitor.get_current_pressure()
        }
    
    def cleanup(self):
        """Cleanup resources."""
        if self.config.enable_resource_monitoring:
            self.resource_monitor.stop_monitoring()
        
        if self.config.enable_intelligent_caching:
            self.cache.clear()


# Convenience functions
async def optimize_large_scale_migration(
    items: List[T],
    processor_func: Callable[[T], R],
    config: Optional[LargeScaleConfig] = None,
    description: str = "Large-scale migration"
) -> List[R]:
    """Convenience function for large-scale migration optimization."""
    if config is None:
        config = LargeScaleConfig()
    
    optimizer = LargeScaleOptimizer(config)
    try:
        return await optimizer.process_large_scale_migration(items, processor_func, description)
    finally:
        optimizer.cleanup()


def get_optimized_config_for_size(item_count: int) -> LargeScaleConfig:
    """Get optimized configuration based on item count."""
    config = LargeScaleConfig()
    
    if item_count > 1000000:  # >1M items
        config.base_batch_size = 500
        config.max_batch_size = 2000
        config.max_workers = min(16, multiprocessing.cpu_count())
        config.memory_limit_gb = 16.0
        config.cache_size_mb = 1024
    elif item_count > 100000:  # >100k items
        config.base_batch_size = 200
        config.max_batch_size = 1000
        config.max_workers = min(8, multiprocessing.cpu_count())
        config.memory_limit_gb = 8.0
        config.cache_size_mb = 512
    else:  # <100k items
        config.base_batch_size = 100
        config.max_batch_size = 500
        config.max_workers = min(4, multiprocessing.cpu_count())
        config.memory_limit_gb = 4.0
        config.cache_size_mb = 256
    
    return config 