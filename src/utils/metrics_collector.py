#!/usr/bin/env python3
"""Metrics collection for monitoring staleness detection and cache operations.

This module provides a thread-safe metrics collector for tracking:
- Cache hits and misses
- Staleness detection events
- Refresh attempts and outcomes
- Fallback strategy executions

FIXED ISSUES:
- PII exposure: Usernames are hashed for privacy compliance
- Memory management: Limited unique tag combinations to prevent unbounded growth
- Input validation: Metric names and tag values are validated
- Side effects: Getter methods don't mutate state
"""

import hashlib
import logging
import re
import threading
from collections import OrderedDict, defaultdict
from typing import Any

logger = logging.getLogger(__name__)

# Constants for validation and memory management
METRIC_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")
MAX_TAG_COMBINATIONS = 1000  # Prevent memory growth
MAX_TAG_STRING_LENGTH = 200


class MetricsCollector:
    """Thread-safe metrics collector with privacy protection and memory management.

    Key features:
    - Username hashing for PII protection
    - Bounded memory usage with LRU eviction
    - Input validation for security
    - No side effects in getter methods
    """

    def __init__(self) -> None:
        """Initialize the metrics collector."""
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._tagged_counters: dict[str, OrderedDict[str, int]] = defaultdict(
            lambda: OrderedDict(),
        )

        # Initialize idempotency metrics
        self._idempotency_metrics = {
            "cache_hits": 0,
            "cache_misses": 0,
            "keys_generated": 0,
            "keys_cached": 0,
            "redis_errors": 0,
            "fallback_used": 0,
        }

        logger.debug(
            "MetricsCollector initialized with privacy protection and memory management",
        )

    def _hash_username(self, username: str) -> str:
        """Hash username for privacy compliance.

        Args:
            username: Raw username that may contain PII

        Returns:
            8-character hash for metrics storage

        """
        if not username:
            return "unknown"
        return hashlib.sha256(username.encode()).hexdigest()[:8]

    def _create_safe_tag_string(self, tags: dict[str, str]) -> str:
        """Create a safe tag string with privacy protection.

        Args:
            tags: Dictionary of tags

        Returns:
            Safe tag string for storage

        """
        safe_tags = {}
        for key, value in tags.items():
            if key == "username":
                safe_tags[key] = self._hash_username(str(value))
            else:
                safe_tags[key] = str(value)

        tag_string = ",".join(f"{k}:{v}" for k, v in sorted(safe_tags.items()))

        # Truncate if too long
        if len(tag_string) > MAX_TAG_STRING_LENGTH:
            tag_string = tag_string[:MAX_TAG_STRING_LENGTH]

        return tag_string

    def increment_counter(
        self,
        metric_name: str,
        tags: dict[str, str] | None = None,
    ) -> None:
        """Increment a counter metric with optional tags.

        Args:
            metric_name: Name of the metric to increment
            tags: Optional dictionary of tags for metric categorization

        """
        try:
            # Basic input validation
            if not metric_name or not isinstance(metric_name, str):
                logger.warning("Invalid metric name: %s", metric_name)
                return

            if not METRIC_NAME_PATTERN.match(metric_name):
                logger.warning(
                    "Metric name contains invalid characters: %s",
                    metric_name,
                )
                return

            with self._lock:
                # Always increment basic counter
                self._counters[metric_name] += 1

                # Handle tagged counter if tags provided
                if tags and isinstance(tags, dict):
                    try:
                        tag_string = self._create_safe_tag_string(tags)

                        # LRU behavior: move to end if exists
                        if tag_string in self._tagged_counters[metric_name]:
                            self._tagged_counters[metric_name].move_to_end(tag_string)
                            self._tagged_counters[metric_name][tag_string] += 1
                        else:
                            # Create new entry
                            self._tagged_counters[metric_name][tag_string] = 1

                        # Memory management: remove oldest if too many combinations
                        while (
                            len(self._tagged_counters[metric_name])
                            > MAX_TAG_COMBINATIONS
                        ):
                            oldest = next(iter(self._tagged_counters[metric_name]))
                            self._tagged_counters[metric_name].pop(oldest)
                            logger.debug(
                                "Removed old metric entry for memory management",
                            )
                    except Exception as tag_error:
                        logger.warning(
                            "Failed to process tags for metric %s: %s",
                            metric_name,
                            tag_error,
                        )
                        # Continue execution - basic counter was already incremented

                logger.debug("Incremented metric %s", metric_name)

        except Exception as e:
            # Don't let metrics collection failures break core functionality
            logger.warning("Failed to increment metric %s: %s", metric_name, e)

    def get_metrics(self) -> dict[str, Any]:
        """Get current metrics snapshot without side effects.

        Returns:
            Dictionary containing all current metrics and their tagged variants

        """
        with self._lock:
            return {
                "counters": dict(self._counters),
                "tagged_counters": {
                    metric: dict(tags) for metric, tags in self._tagged_counters.items()
                },
            }

    def get_counter(self, metric_name: str) -> int:
        """Get the current value of a specific counter without side effects.

        Args:
            metric_name: Name of the metric counter

        Returns:
            Current counter value (0 if not found)

        """
        with self._lock:
            return self._counters.get(metric_name, 0)

    def get_tagged_counter(self, metric_name: str, tags: dict[str, str]) -> int:
        """Get the current value of a specific tagged counter without side effects.

        Args:
            metric_name: Name of the metric counter
            tags: Tags to filter by

        Returns:
            Current counter value for the specific tag combination (0 if not found)

        """
        with self._lock:
            try:
                tag_string = self._create_safe_tag_string(tags)
                return self._tagged_counters.get(metric_name, {}).get(tag_string, 0)
            except Exception as e:
                logger.warning("Failed to get tagged counter: %s", e)
                return 0

    def reset_metrics(self) -> None:
        """Reset all metrics to zero."""
        with self._lock:
            self._counters.clear()
            self._tagged_counters.clear()
            # Reset idempotency metrics
            for key in self._idempotency_metrics:
                self._idempotency_metrics[key] = 0

        logger.debug("All metrics reset to zero")

    def increment_idempotency_metric(self, metric_name: str) -> None:
        """Increment an idempotency metric counter.

        Args:
            metric_name: Name of the idempotency metric to increment

        """
        if metric_name not in self._idempotency_metrics:
            logger.warning("Unknown idempotency metric: %s", metric_name)
            return

        with self._lock:
            self._idempotency_metrics[metric_name] += 1

    def get_idempotency_metrics(self) -> dict[str, int]:
        """Get all idempotency metrics.

        Returns:
            Dictionary of idempotency metric names to their current values

        """
        with self._lock:
            return self._idempotency_metrics.copy()

    def get_summary(self) -> dict[str, Any]:
        """Get a summary of metrics activity.

        Returns:
            Summary statistics about collected metrics

        """
        with self._lock:
            total_tagged_combinations = sum(
                len(tags) for tags in self._tagged_counters.values()
            )

            return {
                "total_metrics": len(self._counters),
                "total_count": sum(self._counters.values()),
                "tagged_metrics": len(self._tagged_counters),
                "total_tagged_combinations": total_tagged_combinations,
                "tagged_counters": {
                    metric: dict(tags)
                    for metric, tags in self._tagged_counters.items()
                },
                "metric_names": list(self._counters.keys()),
                "memory_usage": {
                    "max_tag_combinations": MAX_TAG_COMBINATIONS,
                    "current_tagged_combinations": total_tagged_combinations,
                },
            }
