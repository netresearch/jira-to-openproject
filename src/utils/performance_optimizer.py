#!/usr/bin/env python3
"""Performance Optimization Utilities for Migration System.

This module provides comprehensive performance optimizations including:
1. API call batching and bulk operations
2. HTTP connection pooling and session reuse  
3. Response caching with TTL expiration
4. Parallel processing for independent operations
5. Memory-efficient streaming and pagination
6. Intelligent rate limiting with adaptive backoff
"""

import asyncio
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache, wraps
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple, Union
from urllib.parse import urljoin
import json
import hashlib

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """Cache entry with TTL and metadata."""
    data: Any
    timestamp: datetime
    ttl_seconds: int
    access_count: int = 0
    last_accessed: Optional[datetime] = None

    def is_expired(self) -> bool:
        """Check if cache entry has expired."""
        return datetime.now() > self.timestamp + timedelta(seconds=self.ttl_seconds)

    def touch(self) -> None:
        """Update access tracking."""
        self.access_count += 1
        self.last_accessed = datetime.now()


class PerformanceCache:
    """Thread-safe LRU cache with TTL and statistics."""
    
    def __init__(self, max_size: int = 1000, default_ttl: int = 3600):
        self.max_size = max_size
        self.default_ttl = default_ttl
        self._cache: Dict[str, CacheEntry] = {}
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: str) -> Optional[Any]:
        """Get cached value if not expired."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            
            if entry.is_expired():
                del self._cache[key]
                self._misses += 1
                return None
            
            entry.touch()
            self._hits += 1
            return entry.data

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Set cached value with TTL."""
        with self._lock:
            if len(self._cache) >= self.max_size:
                self._evict_oldest()
            
            self._cache[key] = CacheEntry(
                data=value,
                timestamp=datetime.now(),
                ttl_seconds=ttl or self.default_ttl
            )

    def _evict_oldest(self) -> None:
        """Evict oldest or least recently used entry."""
        if not self._cache:
            return
        
        # Find oldest entry by timestamp
        oldest_key = min(
            self._cache.keys(),
            key=lambda k: self._cache[k].timestamp
        )
        del self._cache[oldest_key]
        self._evictions += 1

    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._cache.clear()

    def get_stats(self) -> Dict[str, Any]:
        """Get cache performance statistics."""
        with self._lock:
            total_requests = self._hits + self._misses
            hit_rate = (self._hits / total_requests) if total_requests > 0 else 0.0
            
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": hit_rate,
                "evictions": self._evictions,
                "current_size": len(self._cache),
                "max_size": self.max_size
            }


class ConnectionPoolManager:
    """HTTP connection pool manager with session reuse."""
    
    def __init__(self, 
                 pool_connections: int = 20,
                 pool_maxsize: int = 50,
                 max_retries: int = 3,
                 backoff_factor: float = 0.3):
        self.pool_connections = pool_connections
        self.pool_maxsize = pool_maxsize
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self._sessions: Dict[str, requests.Session] = {}
        self._lock = threading.RLock()

    def get_session(self, base_url: str) -> requests.Session:
        """Get or create a pooled session for the base URL."""
        with self._lock:
            session_key = self._get_session_key(base_url)
            
            if session_key not in self._sessions:
                session = requests.Session()
                
                # Configure retry strategy
                retry_strategy = Retry(
                    total=self.max_retries,
                    backoff_factor=self.backoff_factor,
                    status_forcelist=[429, 500, 502, 503, 504],
                    allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS", "TRACE"]
                )
                
                # Configure HTTP adapter with connection pooling
                adapter = HTTPAdapter(
                    pool_connections=self.pool_connections,
                    pool_maxsize=self.pool_maxsize,
                    max_retries=retry_strategy
                )
                
                session.mount("http://", adapter)
                session.mount("https://", adapter)
                
                # Set default headers
                session.headers.update({
                    'User-Agent': 'Migration-Tool/1.0',
                    'Connection': 'keep-alive'
                })
                
                self._sessions[session_key] = session
                logger.debug(f"Created new session for {base_url}")
            
            return self._sessions[session_key]

    def _get_session_key(self, base_url: str) -> str:
        """Generate session key from base URL."""
        return hashlib.md5(base_url.encode()).hexdigest()

    def close_all(self) -> None:
        """Close all managed sessions."""
        with self._lock:
            for session in self._sessions.values():
                session.close()
            self._sessions.clear()


class BatchProcessor:
    """Generic batch processor for API operations."""
    
    def __init__(self, batch_size: int = 100, max_workers: int = 10):
        self.batch_size = batch_size
        self.max_workers = max_workers
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

    def process_batches(self, 
                       items: List[Any],
                       process_func: Callable[[List[Any]], Any],
                       **kwargs) -> List[Any]:
        """Process items in batches using thread pool."""
        if not items:
            return []
        
        # Split into batches
        batches = [
            items[i:i + self.batch_size]
            for i in range(0, len(items), self.batch_size)
        ]
        
        logger.info(f"Processing {len(items)} items in {len(batches)} batches using {self.max_workers} workers")
        
        results = []
        futures = []
        
        # Submit all batches
        for batch_num, batch in enumerate(batches):
            future = self.executor.submit(process_func, batch, batch_num=batch_num, **kwargs)
            futures.append(future)
        
        # Collect results as they complete
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    results.extend(result if isinstance(result, list) else [result])
            except Exception as e:
                logger.error(f"Batch processing failed: {e}")
                
        return results

    def shutdown(self) -> None:
        """Shutdown the thread pool executor."""
        self.executor.shutdown(wait=True)


class AdaptiveRateLimiter:
    """Adaptive rate limiter that adjusts based on response times and errors."""
    
    def __init__(self, 
                 initial_rate: float = 10.0,  # requests per second
                 min_rate: float = 1.0,
                 max_rate: float = 50.0,
                 adjustment_factor: float = 0.1):
        self.current_rate = initial_rate
        self.min_rate = min_rate
        self.max_rate = max_rate
        self.adjustment_factor = adjustment_factor
        self.last_request_time = 0.0
        self._lock = threading.RLock()
        self._recent_response_times: List[float] = []
        self._error_count = 0
        self._success_count = 0

    @contextmanager
    def throttle(self):
        """Context manager that enforces rate limiting."""
        with self._lock:
            # Calculate delay needed
            current_time = time.time()
            time_since_last = current_time - self.last_request_time
            min_interval = 1.0 / self.current_rate
            
            if time_since_last < min_interval:
                delay = min_interval - time_since_last
                time.sleep(delay)
            
            self.last_request_time = time.time()
        
        # Record timing and adjust rate
        start_time = time.time()
        error_occurred = False
        
        try:
            yield
            self._success_count += 1
        except Exception as e:
            error_occurred = True
            self._error_count += 1
            raise
        finally:
            response_time = time.time() - start_time
            self._record_response(response_time, error_occurred)

    def _record_response(self, response_time: float, error_occurred: bool) -> None:
        """Record response time and adjust rate accordingly."""
        with self._lock:
            self._recent_response_times.append(response_time)
            
            # Keep only recent response times (last 20)
            if len(self._recent_response_times) > 20:
                self._recent_response_times = self._recent_response_times[-20:]
            
            # Adjust rate based on performance
            if error_occurred:
                # Slow down on errors
                self.current_rate = max(
                    self.min_rate,
                    self.current_rate * (1 - self.adjustment_factor * 2)
                )
            elif len(self._recent_response_times) >= 5:
                # Adjust based on average response time
                avg_response_time = sum(self._recent_response_times) / len(self._recent_response_times)
                
                if avg_response_time < 0.5:  # Fast responses, can increase rate
                    self.current_rate = min(
                        self.max_rate,
                        self.current_rate * (1 + self.adjustment_factor)
                    )
                elif avg_response_time > 2.0:  # Slow responses, decrease rate
                    self.current_rate = max(
                        self.min_rate,
                        self.current_rate * (1 - self.adjustment_factor)
                    )

    def get_stats(self) -> Dict[str, Any]:
        """Get rate limiter statistics."""
        with self._lock:
            total_requests = self._success_count + self._error_count
            error_rate = (self._error_count / total_requests) if total_requests > 0 else 0.0
            avg_response_time = (
                sum(self._recent_response_times) / len(self._recent_response_times)
                if self._recent_response_times else 0.0
            )
            
            return {
                "current_rate": self.current_rate,
                "total_requests": total_requests,
                "success_count": self._success_count,
                "error_count": self._error_count,
                "error_rate": error_rate,
                "avg_response_time": avg_response_time
            }


class StreamingPaginator:
    """Memory-efficient streaming paginator for large datasets."""
    
    def __init__(self, 
                 fetch_func: Callable,
                 page_size: int = 100,
                 max_pages: Optional[int] = None):
        self.fetch_func = fetch_func
        self.page_size = page_size
        self.max_pages = max_pages

    def iter_items(self, **kwargs) -> Iterator[Any]:
        """Iterate over all items using streaming pagination."""
        page = 0
        start_at = 0
        
        while self.max_pages is None or page < self.max_pages:
            try:
                # Fetch page
                items = self.fetch_func(
                    start_at=start_at,
                    max_results=self.page_size,
                    **kwargs
                )
                
                if not items:
                    break
                
                # Yield individual items
                for item in items:
                    yield item
                
                # Check if this was the last page
                if len(items) < self.page_size:
                    break
                
                # Prepare for next page
                start_at += len(items)
                page += 1
                
                logger.debug(f"Processed page {page}, total items so far: {start_at}")
                
            except Exception as e:
                logger.error(f"Error fetching page {page}: {e}")
                break

    def collect_all(self, **kwargs) -> List[Any]:
        """Collect all items into a list (use with caution for large datasets)."""
        return list(self.iter_items(**kwargs))


class PerformanceOptimizer:
    """Main performance optimization coordinator."""
    
    def __init__(self,
                 cache_size: int = 1000,
                 cache_ttl: int = 3600,
                 batch_size: int = 100,
                 max_workers: int = 10,
                 rate_limit: float = 10.0):
        
        self.cache = PerformanceCache(max_size=cache_size, default_ttl=cache_ttl)
        self.connection_manager = ConnectionPoolManager()
        self.batch_processor = BatchProcessor(batch_size=batch_size, max_workers=max_workers)
        self.rate_limiter = AdaptiveRateLimiter(initial_rate=rate_limit)
        
        self._stats = {
            "operations_cached": 0,
            "operations_batched": 0,
            "connections_reused": 0,
            "rate_limited_calls": 0
        }

    def cached_operation(self, ttl: Optional[int] = None):
        """Decorator for caching expensive operations."""
        def decorator(func: Callable) -> Callable:
            @wraps(func)
            def wrapper(*args, **kwargs):
                # Create cache key from function name and arguments
                cache_key = self._create_cache_key(func.__name__, args, kwargs)
                
                # Try to get from cache first
                cached_result = self.cache.get(cache_key)
                if cached_result is not None:
                    self._stats["operations_cached"] += 1
                    return cached_result
                
                # Execute function and cache result
                result = func(*args, **kwargs)
                self.cache.set(cache_key, result, ttl)
                
                return result
            return wrapper
        return decorator

    def rate_limited_operation(self):
        """Decorator for rate-limited API calls."""
        def decorator(func: Callable) -> Callable:
            @wraps(func)
            def wrapper(*args, **kwargs):
                with self.rate_limiter.throttle():
                    self._stats["rate_limited_calls"] += 1
                    return func(*args, **kwargs)
            return wrapper
        return decorator

    def batch_operation(self, batch_size: Optional[int] = None):
        """Decorator for batching operations."""
        def decorator(func: Callable) -> Callable:
            @wraps(func)
            def wrapper(items: List[Any], **kwargs):
                if batch_size:
                    self.batch_processor.batch_size = batch_size
                
                self._stats["operations_batched"] += 1
                return self.batch_processor.process_batches(items, func, **kwargs)
            return wrapper
        return decorator

    def _create_cache_key(self, func_name: str, args: Tuple, kwargs: Dict) -> str:
        """Create a cache key from function name and arguments."""
        # Convert args and kwargs to a hashable representation
        key_data = {
            "func": func_name,
            "args": str(args),
            "kwargs": json.dumps(kwargs, sort_keys=True, default=str)
        }
        key_string = json.dumps(key_data, sort_keys=True)
        return hashlib.md5(key_string.encode()).hexdigest()

    def get_comprehensive_stats(self) -> Dict[str, Any]:
        """Get comprehensive performance statistics."""
        return {
            "cache": self.cache.get_stats(),
            "rate_limiter": self.rate_limiter.get_stats(),
            "optimizer": self._stats,
            "connections": {
                "active_sessions": len(self.connection_manager._sessions)
            }
        }

    def shutdown(self) -> None:
        """Shutdown all performance optimization components."""
        self.batch_processor.shutdown()
        self.connection_manager.close_all()
        self.cache.clear()


# Global performance optimizer instance
performance_optimizer = PerformanceOptimizer()


# Convenience decorators using global instance
def cached(ttl: Optional[int] = None):
    """Decorator for caching operations using global optimizer."""
    return performance_optimizer.cached_operation(ttl=ttl)


def rate_limited():
    """Decorator for rate-limited operations using global optimizer."""
    return performance_optimizer.rate_limited_operation()


def batched(batch_size: Optional[int] = None):
    """Decorator for batch operations using global optimizer."""
    return performance_optimizer.batch_operation(batch_size=batch_size)


def get_performance_stats() -> Dict[str, Any]:
    """Get performance statistics from global optimizer."""
    return performance_optimizer.get_comprehensive_stats()


def shutdown_performance_optimizer() -> None:
    """Shutdown global performance optimizer."""
    performance_optimizer.shutdown() 