#!/usr/bin/env python3
"""Metrics collection for monitoring staleness detection and cache operations.

This module provides a thread-safe metrics collector for tracking:
- Cache hits and misses
- Staleness detection events
- Refresh attempts and outcomes
- Fallback strategy executions
"""

import logging
import threading
from collections import defaultdict
from typing import Any, Dict, Optional


logger = logging.getLogger(__name__)


class MetricsCollector:
    """Thread-safe metrics collector for staleness detection monitoring.
    
    Provides simple counter-based metrics collection with tag support
    for detailed monitoring and analysis of cache operations, staleness
    detection, refresh attempts, and fallback executions.
    """
    
    def __init__(self) -> None:
        """Initialize the metrics collector with thread-safe storage."""
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = defaultdict(int)
        self._tagged_counters: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        
        logger.debug("MetricsCollector initialized")
    
    def increment_counter(self, metric_name: str, tags: Optional[Dict[str, str]] = None) -> None:
        """Increment a counter metric with optional tags.
        
        Args:
            metric_name: Name of the metric to increment
            tags: Optional dictionary of tags for metric categorization
        """
        try:
            with self._lock:
                # Increment basic counter
                self._counters[metric_name] += 1
                
                # If tags provided, increment tagged counter
                if tags:
                    # Create a tag string for storage (sorted for consistency)
                    tag_string = ",".join(f"{k}:{v}" for k, v in sorted(tags.items()))
                    self._tagged_counters[metric_name][tag_string] += 1
                
                logger.debug("Incremented metric %s with tags %s", metric_name, tags or {})
                
        except Exception as e:
            # Don't let metrics collection failures break core functionality
            logger.warning("Failed to increment metric %s: %s", metric_name, e)
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get current metrics snapshot.
        
        Returns:
            Dictionary containing all current metrics and their tagged variants
        """
        with self._lock:
            return {
                "counters": dict(self._counters),
                "tagged_counters": {
                    metric: dict(tags) for metric, tags in self._tagged_counters.items()
                }
            }
    
    def get_counter(self, metric_name: str) -> int:
        """Get the current value of a specific counter.
        
        Args:
            metric_name: Name of the metric counter
            
        Returns:
            Current counter value
        """
        with self._lock:
            return self._counters[metric_name]
    
    def get_tagged_counter(self, metric_name: str, tags: Dict[str, str]) -> int:
        """Get the current value of a specific tagged counter.
        
        Args:
            metric_name: Name of the metric counter
            tags: Tags to filter by
            
        Returns:
            Current counter value for the specific tag combination
        """
        with self._lock:
            tag_string = ",".join(f"{k}:{v}" for k, v in sorted(tags.items()))
            return self._tagged_counters[metric_name][tag_string]
    
    def reset_metrics(self) -> None:
        """Reset all metrics to zero.
        
        Useful for testing or periodic metric collection.
        """
        with self._lock:
            self._counters.clear()
            self._tagged_counters.clear()
            
        logger.debug("All metrics reset to zero")
    
    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of metrics activity.
        
        Returns:
            Summary statistics about collected metrics
        """
        with self._lock:
            total_metrics = len(self._counters)
            total_counts = sum(self._counters.values())
            tagged_metrics = len(self._tagged_counters)
            
            return {
                "total_metrics": total_metrics,
                "total_count": total_counts,
                "tagged_metrics": tagged_metrics,
                "metric_names": list(self._counters.keys())
            } 