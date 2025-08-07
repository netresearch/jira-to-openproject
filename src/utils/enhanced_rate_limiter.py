"""Enhanced rate limiter with advanced API throttling capabilities.

This module provides sophisticated rate limiting with burst handling, adaptive rate limiting,
and integration with retry mechanisms for robust API interactions.
"""

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from src import config
from src.utils.config_validation import ConfigurationValidationError, SecurityValidator


class RateLimitStrategy(Enum):
    """Rate limiting strategies."""

    TOKEN_BUCKET = "token_bucket"
    SLIDING_WINDOW = "sliding_window"
    FIXED_WINDOW = "fixed_window"
    ADAPTIVE = "adaptive"


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting behavior with comprehensive security validation."""

    max_requests: int = 100
    time_window: float = 60.0  # seconds
    burst_size: int = 10
    strategy: RateLimitStrategy = RateLimitStrategy.TOKEN_BUCKET
    adaptive_factor: float = 0.8  # Reduce rate when hitting limits
    recovery_factor: float = 1.1  # Increase rate when stable
    min_delay: float = 0.1  # Minimum delay between requests
    max_delay: float = 60.0  # Maximum delay for backoff

    def __post_init__(self):
        """Validate configuration parameters using SecurityValidator for comprehensive security checks."""
        try:
            # Validate core rate limiting parameters
            self.max_requests = SecurityValidator.validate_numeric_parameter(
                "max_requests_per_minute",
                self.max_requests,
            )
            self.time_window = SecurityValidator.validate_numeric_parameter(
                "time_window",
                self.time_window,
            )
            self.burst_size = SecurityValidator.validate_numeric_parameter(
                "burst_size",
                self.burst_size,
            )

            # Validate timing parameters
            self.min_delay = SecurityValidator.validate_numeric_parameter(
                "min_delay",
                self.min_delay,
            )
            self.max_delay = SecurityValidator.validate_numeric_parameter(
                "max_delay",
                self.max_delay,
            )

            # Validate factor parameters
            self.adaptive_factor = SecurityValidator.validate_numeric_parameter(
                "adaptive_factor",
                self.adaptive_factor,
            )
            self.recovery_factor = SecurityValidator.validate_numeric_parameter(
                "recovery_factor",
                self.recovery_factor,
            )

            # Validate timing relationships
            SecurityValidator.validate_timing_relationships(
                self.min_delay,
                self.max_delay,
            )

            # Validate strategy enum
            if not isinstance(self.strategy, RateLimitStrategy):
                msg = "strategy"
                raise ConfigurationValidationError(
                    msg,
                    self.strategy,
                    f"RateLimitStrategy enum value (got {type(self.strategy).__name__})",
                )

        except ConfigurationValidationError as e:
            config.logger.error(f"RateLimitConfig validation failed: {e}")
            raise


@dataclass
class RateLimitMetrics:
    """Metrics for rate limiting performance."""

    total_requests: int = 0
    throttled_requests: int = 0
    average_delay: float = 0.0
    peak_delay: float = 0.0
    rate_limit_hits: int = 0
    current_rate: float = 0.0
    last_reset_time: float = field(default_factory=time.time)


class EnhancedRateLimiter:
    """Enhanced rate limiter with adaptive throttling and burst handling."""

    def __init__(self, config: RateLimitConfig) -> None:
        self.config = config
        self.metrics = RateLimitMetrics()
        self._lock = threading.RLock()

        # Token bucket implementation
        self._tokens = config.max_requests
        self._last_refill = time.time()

        # Sliding window implementation
        self._request_times = deque()

        # Adaptive rate limiting
        self._current_max_requests = config.max_requests
        self._consecutive_successes = 0
        self._consecutive_failures = 0

        # Burst handling
        self._burst_tokens = config.burst_size
        self._burst_start_time = None

    def acquire(self, tokens: int = 1) -> bool:
        """Acquire permission to make API request(s)."""
        with self._lock:
            if self.config.strategy == RateLimitStrategy.TOKEN_BUCKET:
                return self._acquire_token_bucket(tokens)
            if self.config.strategy == RateLimitStrategy.SLIDING_WINDOW:
                return self._acquire_sliding_window(tokens)
            if self.config.strategy == RateLimitStrategy.FIXED_WINDOW:
                return self._acquire_fixed_window(tokens)
            if self.config.strategy == RateLimitStrategy.ADAPTIVE:
                return self._acquire_adaptive(tokens)
            msg = f"Unknown strategy: {self.config.strategy}"
            raise ValueError(msg)

    def wait_if_needed(self, tokens: int = 1) -> float:
        """Wait until permission is available. Returns delay time."""
        start_time = time.time()

        while not self.acquire(tokens):
            delay = self._calculate_wait_delay(tokens)
            time.sleep(delay)
            self.metrics.throttled_requests += 1

        actual_delay = time.time() - start_time
        self._update_delay_metrics(actual_delay)
        return actual_delay

    def _acquire_token_bucket(self, tokens: int) -> bool:
        """Token bucket rate limiting implementation."""
        now = time.time()

        # Refill tokens based on elapsed time
        elapsed = now - self._last_refill
        tokens_to_add = elapsed * (self.config.max_requests / self.config.time_window)
        self._tokens = min(self.config.max_requests, self._tokens + tokens_to_add)
        self._last_refill = now

        # Check if we have enough tokens
        if self._tokens >= tokens:
            self._tokens -= tokens
            self.metrics.total_requests += tokens
            return True

        return False

    def _acquire_sliding_window(self, tokens: int) -> bool:
        """Sliding window rate limiting implementation."""
        now = time.time()
        window_start = now - self.config.time_window

        # Remove old requests from the window
        while self._request_times and self._request_times[0] < window_start:
            self._request_times.popleft()

        # Check if adding this request would exceed the limit
        if len(self._request_times) + tokens <= self._current_max_requests:
            for _ in range(tokens):
                self._request_times.append(now)
            self.metrics.total_requests += tokens
            return True

        return False

    def _acquire_fixed_window(self, tokens: int) -> bool:
        """Fixed window rate limiting implementation."""
        now = time.time()

        # Reset window if needed
        if now - self.metrics.last_reset_time >= self.config.time_window:
            self._request_times.clear()
            self.metrics.last_reset_time = now

        # Check if we can make the request
        if len(self._request_times) + tokens <= self._current_max_requests:
            for _ in range(tokens):
                self._request_times.append(now)
            self.metrics.total_requests += tokens
            return True

        return False

    def _acquire_adaptive(self, tokens: int) -> bool:
        """Adaptive rate limiting that adjusts based on API responses."""
        # Use token bucket as base, but with adaptive max_requests
        now = time.time()

        # Refill tokens based on current adaptive rate
        elapsed = now - self._last_refill
        rate = self._current_max_requests / self.config.time_window
        tokens_to_add = elapsed * rate
        self._tokens = min(self._current_max_requests, self._tokens + tokens_to_add)
        self._last_refill = now

        if self._tokens >= tokens:
            self._tokens -= tokens
            self.metrics.total_requests += tokens
            return True

        return False

    def _calculate_wait_delay(self, tokens: int) -> float:
        """Calculate appropriate wait delay based on strategy."""
        if self.config.strategy == RateLimitStrategy.TOKEN_BUCKET:
            # Wait for tokens to refill
            tokens_needed = tokens - self._tokens
            delay = tokens_needed * (self.config.time_window / self.config.max_requests)
        elif self.config.strategy in [
            RateLimitStrategy.SLIDING_WINDOW,
            RateLimitStrategy.FIXED_WINDOW,
        ]:
            # Wait for oldest request to age out
            if self._request_times:
                oldest_request = self._request_times[0]
                delay = (oldest_request + self.config.time_window) - time.time()
            else:
                delay = self.config.min_delay
        else:  # Adaptive
            delay = self.config.min_delay * (2**self._consecutive_failures)

        return max(self.config.min_delay, min(self.config.max_delay, delay))

    def _update_delay_metrics(self, delay: float) -> None:
        """Update delay metrics."""
        if delay > 0:
            self.metrics.average_delay = (
                self.metrics.average_delay * self.metrics.throttled_requests + delay
            ) / (self.metrics.throttled_requests + 1)
            self.metrics.peak_delay = max(self.metrics.peak_delay, delay)

    def handle_api_response(
        self,
        success: bool,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Handle API response to adjust adaptive rate limiting."""
        with self._lock:
            if success:
                self._consecutive_successes += 1
                self._consecutive_failures = 0

                # Gradually increase rate if we're being conservative
                if (
                    self._consecutive_successes >= 10
                    and self._current_max_requests < self.config.max_requests
                ):
                    self._current_max_requests = min(
                        self.config.max_requests,
                        int(self._current_max_requests * self.config.recovery_factor),
                    )
                    self._consecutive_successes = 0
            else:
                self._consecutive_failures += 1
                self._consecutive_successes = 0
                self.metrics.rate_limit_hits += 1

                # Reduce rate on failures
                self._current_max_requests = max(
                    1,
                    int(self._current_max_requests * self.config.adaptive_factor),
                )

            # Parse rate limit headers if available
            if headers:
                self._parse_rate_limit_headers(headers)

    def _parse_rate_limit_headers(self, headers: dict[str, str]) -> None:
        """Parse standard rate limit headers and adjust accordingly."""
        # Common rate limit header patterns
        remaining_headers = [
            "x-ratelimit-remaining",
            "x-rate-limit-remaining",
            "ratelimit-remaining",
            "rate-limit-remaining",
        ]

        reset_headers = [
            "x-ratelimit-reset",
            "x-rate-limit-reset",
            "ratelimit-reset",
            "rate-limit-reset",
        ]

        # Check for remaining requests
        for header in remaining_headers:
            if header.lower() in [k.lower() for k in headers]:
                try:
                    remaining = int(headers[header])
                    if remaining < 5:  # Very low, be conservative
                        self._current_max_requests = max(
                            1,
                            self._current_max_requests // 2,
                        )
                except (ValueError, KeyError):
                    pass
                break

        # Check for reset time
        for header in reset_headers:
            if header.lower() in [k.lower() for k in headers]:
                try:
                    reset_time = int(headers[header])
                    now = int(time.time())
                    if reset_time > now:
                        # Adjust our window to match the API's reset cycle
                        self.config.time_window = min(
                            self.config.time_window,
                            reset_time - now,
                        )
                except (ValueError, KeyError):
                    pass
                break

    def get_metrics(self) -> RateLimitMetrics:
        """Get current rate limiting metrics."""
        with self._lock:
            self.metrics.current_rate = self.metrics.total_requests / max(
                1,
                time.time() - self.metrics.last_reset_time,
            )
            return self.metrics

    def reset_metrics(self) -> None:
        """Reset all metrics."""
        with self._lock:
            self.metrics = RateLimitMetrics()

    def adjust_rate(self, new_max_requests: int) -> None:
        """Manually adjust the maximum request rate."""
        with self._lock:
            self.config.max_requests = new_max_requests
            self._current_max_requests = min(
                self._current_max_requests,
                new_max_requests,
            )


class GlobalRateLimiterManager:
    """Manages multiple rate limiters for different API endpoints."""

    def __init__(self) -> None:
        self._limiters: dict[str, EnhancedRateLimiter] = {}
        self._lock = threading.RLock()

    def get_limiter(
        self,
        endpoint: str,
        config: RateLimitConfig = None,
    ) -> EnhancedRateLimiter:
        """Get or create a rate limiter for a specific endpoint."""
        with self._lock:
            if endpoint not in self._limiters:
                if config is None:
                    config = RateLimitConfig()
                self._limiters[endpoint] = EnhancedRateLimiter(config)
            return self._limiters[endpoint]

    def remove_limiter(self, endpoint: str) -> None:
        """Remove a rate limiter for an endpoint."""
        with self._lock:
            self._limiters.pop(endpoint, None)

    def get_all_metrics(self) -> dict[str, RateLimitMetrics]:
        """Get metrics for all rate limiters."""
        with self._lock:
            return {
                endpoint: limiter.get_metrics()
                for endpoint, limiter in self._limiters.items()
            }

    def reset_all_metrics(self) -> None:
        """Reset metrics for all rate limiters."""
        with self._lock:
            for limiter in self._limiters.values():
                limiter.reset_metrics()


# Global instance
global_rate_limiter_manager = GlobalRateLimiterManager()


def rate_limited(endpoint: str = "default", config: RateLimitConfig = None):
    """Decorator for automatic rate limiting of functions."""

    def decorator(func: Callable) -> Callable:
        limiter = global_rate_limiter_manager.get_limiter(endpoint, config)

        def wrapper(*args, **kwargs):
            limiter.wait_if_needed()
            try:
                result = func(*args, **kwargs)
                limiter.handle_api_response(True)
                return result
            except Exception as e:
                # Determine if this is a rate limit error
                is_rate_limit_error = (
                    hasattr(e, "response")
                    and hasattr(e.response, "status_code")
                    and e.response.status_code == 429
                )
                limiter.handle_api_response(not is_rate_limit_error)
                raise

        return wrapper

    return decorator
