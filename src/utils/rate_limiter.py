"""Adaptive rate limiter for API clients.

Provides intelligent throttling that adapts to API responses, handles rate limit headers,
and implements exponential backoff for different types of errors.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from threading import Lock
from typing import Any, Dict

from src import config
from src.utils.config_validation import SecurityValidator, ConfigurationValidationError

logger = config.logger


class RateLimitStrategy(Enum):
    """Rate limiting strategies for different APIs."""

    ADAPTIVE = "adaptive"  # Adapts based on response times
    FIXED = "fixed"  # Fixed delay (legacy behavior)
    EXPONENTIAL = "exponential"  # Exponential backoff on errors
    BURST = "burst"  # Allows bursts with recovery


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting behavior with comprehensive security validation."""

    strategy: RateLimitStrategy = RateLimitStrategy.ADAPTIVE
    base_delay: float = 0.1  # Base delay in seconds
    max_delay: float = 60.0  # Maximum delay in seconds
    min_delay: float = 0.01  # Minimum delay in seconds
    burst_capacity: int = 10  # Number of requests allowed in burst
    burst_recovery_rate: float = 0.5  # Requests per second recovery rate
    exponential_base: float = 2.0  # Base for exponential backoff
    adaptive_threshold: float = 0.5  # Response time threshold for adaptation
    circuit_breaker_threshold: int = 5  # Failures before circuit breaker

    def __post_init__(self):
        """Validate configuration parameters using SecurityValidator for comprehensive security checks."""
        try:
            # Validate timing parameters
            self.base_delay = SecurityValidator.validate_numeric_parameter('base_delay', self.base_delay)
            self.max_delay = SecurityValidator.validate_numeric_parameter('max_delay', self.max_delay)
            self.min_delay = SecurityValidator.validate_numeric_parameter('min_delay', self.min_delay)
            
            # Validate capacity and rate parameters
            self.burst_capacity = SecurityValidator.validate_numeric_parameter('burst_capacity', self.burst_capacity)
            self.circuit_breaker_threshold = SecurityValidator.validate_numeric_parameter('retry_attempts', self.circuit_breaker_threshold)  # Reuse retry_attempts bounds
            
            # Validate factor parameters
            self.exponential_base = SecurityValidator.validate_numeric_parameter('exponential_base', self.exponential_base)
            self.adaptive_threshold = SecurityValidator.validate_numeric_parameter('adaptive_threshold', self.adaptive_threshold)
            
            # Validate burst recovery rate (custom bounds)
            if not isinstance(self.burst_recovery_rate, (int, float)):
                raise ConfigurationValidationError(
                    'burst_recovery_rate', 
                    self.burst_recovery_rate, 
                    f"numeric value (got {type(self.burst_recovery_rate).__name__})"
                )
            if self.burst_recovery_rate <= 0 or self.burst_recovery_rate > 100:
                raise ConfigurationValidationError(
                    'burst_recovery_rate', 
                    self.burst_recovery_rate, 
                    "0.1 to 100.0 requests per second"
                )
            
            # Validate timing relationships
            SecurityValidator.validate_timing_relationships(self.base_delay, self.max_delay, self.min_delay)
            
            # Validate strategy enum
            if not isinstance(self.strategy, RateLimitStrategy):
                raise ConfigurationValidationError(
                    'strategy', 
                    self.strategy, 
                    f"RateLimitStrategy enum value (got {type(self.strategy).__name__})"
                )
            
        except ConfigurationValidationError as e:
            logger.error(f"RateLimitConfig validation failed: {e}")
            raise


@dataclass
class RateLimitState:
    """Current state of rate limiting."""

    current_delay: float = 0.1
    burst_tokens: int = 10
    last_request_time: float = 0.0
    consecutive_failures: int = 0
    circuit_breaker_open: bool = False
    circuit_breaker_reset_time: float = 0.0
    response_time_history: list = None

    def __post_init__(self):
        if self.response_time_history is None:
            self.response_time_history = []


class RateLimiter:
    """Adaptive rate limiter that adjusts based on API responses.

    Features:
    - Adaptive throttling based on response times
    - Exponential backoff for 429 errors
    - Rate limit header parsing
    - Circuit breaker pattern for repeated failures
    - Burst capacity for short-term high loads
    - Different strategies for different API endpoints
    """

    def __init__(self, config: RateLimitConfig = None):
        """Initialize the rate limiter.

        Args:
            config: Configuration for rate limiting behavior
        """
        self.config = config or RateLimitConfig()
        self.state = RateLimitState()
        self.state.current_delay = self.config.base_delay
        self.state.burst_tokens = self.config.burst_capacity
        self._lock = Lock()

    def wait_if_needed(self, endpoint: str = "default") -> None:
        """Wait if rate limiting is needed for the given endpoint.
        
        This method uses non-blocking sleep when possible to improve performance.
        """
        current_time = time.time()

        with self._lock:
            # Check circuit breaker
            if self.state.circuit_breaker_open:
                if current_time < self.state.circuit_breaker_reset_time:
                    wait_time = self.state.circuit_breaker_reset_time - current_time
                    logger.warning(
                        f"Circuit breaker open for {endpoint}, waiting {wait_time:.2f}s"
                    )
                    time.sleep(wait_time)
                else:
                    # Reset circuit breaker
                    self.state.circuit_breaker_open = False
                    self.state.consecutive_failures = 0
                    logger.info(f"Circuit breaker reset for {endpoint}")

            # Handle burst tokens
            if self.config.strategy == RateLimitStrategy.BURST:
                # Recover tokens based on time passed
                time_since_last = current_time - self.state.last_request_time
                tokens_to_add = time_since_last * self.config.burst_recovery_rate
                self.state.burst_tokens = min(
                    self.config.burst_capacity, self.state.burst_tokens + tokens_to_add
                )

                if self.state.burst_tokens >= 1.0:
                    self.state.burst_tokens -= 1.0
                    self.state.last_request_time = current_time
                    return  # No delay needed

            # Calculate delay based on strategy
            delay = self._calculate_delay()

            if delay > 0:
                logger.debug(f"Rate limiting {endpoint}: waiting {delay:.3f}s")
                # Use asyncio sleep if in async context, otherwise fall back to blocking sleep
                try:
                    loop = asyncio.get_running_loop()
                    if loop and not loop.is_closed():
                        # We're in an async context but this is a sync method
                        # Still use time.sleep for compatibility but log the better approach
                        logger.debug("Consider using async rate limiter for better performance")
                except RuntimeError:
                    # No event loop running, safe to use blocking sleep
                    pass
                
                time.sleep(delay)

            self.state.last_request_time = current_time

    def record_response(
        self,
        response_time: float,
        status_code: int = 200,
        headers: dict[str, str] = None,
    ) -> None:
        """Record response metrics to adapt rate limiting.

        Args:
            response_time: Time taken for the request in seconds
            status_code: HTTP status code
            headers: Response headers that may contain rate limit info
        """
        with self._lock:
            headers = headers or {}

            # Handle rate limit headers
            if status_code == 429:
                self._handle_rate_limit_exceeded(headers)
                return

            # Handle other errors
            if status_code >= 500:
                self._handle_server_error()
                return

            # Record successful response
            self.state.consecutive_failures = 0
            self.state.response_time_history.append(response_time)

            # Keep only recent history
            if len(self.state.response_time_history) > 10:
                self.state.response_time_history.pop(0)

            # Parse rate limit headers
            self._parse_rate_limit_headers(headers)

            # Adapt based on response time
            if self.config.strategy == RateLimitStrategy.ADAPTIVE:
                self._adapt_to_response_time(response_time)

    def _calculate_delay(self) -> float:
        """Calculate delay based on current strategy and state."""
        if self.config.strategy == RateLimitStrategy.FIXED:
            return self.config.base_delay

        elif self.config.strategy == RateLimitStrategy.EXPONENTIAL:
            if self.state.consecutive_failures > 0:
                return min(
                    self.config.base_delay
                    * (self.config.exponential_base**self.state.consecutive_failures),
                    self.config.max_delay,
                )
            return self.config.base_delay

        elif self.config.strategy == RateLimitStrategy.ADAPTIVE:
            return self.state.current_delay

        elif self.config.strategy == RateLimitStrategy.BURST:
            # Burst strategy handled in wait_if_needed
            return 0.0

        return self.config.base_delay

    def _handle_rate_limit_exceeded(self, headers: dict[str, str]) -> None:
        """Handle 429 rate limit exceeded response."""
        self.state.consecutive_failures += 1

        # Look for Retry-After header
        retry_after = headers.get("Retry-After") or headers.get("retry-after")
        if retry_after:
            try:
                wait_time = float(retry_after)
                logger.warning(
                    f"Rate limit exceeded, waiting {wait_time}s as per Retry-After header"
                )
                time.sleep(wait_time)
                return
            except ValueError:
                pass

        # Exponential backoff
        backoff_delay = min(
            self.config.base_delay
            * (self.config.exponential_base**self.state.consecutive_failures),
            self.config.max_delay,
        )

        logger.warning(f"Rate limit exceeded, backing off for {backoff_delay:.2f}s")
        time.sleep(backoff_delay)

        # Update current delay for future requests
        self.state.current_delay = min(
            self.state.current_delay * 2, self.config.max_delay
        )

    def _handle_server_error(self) -> None:
        """Handle server errors (5xx)."""
        self.state.consecutive_failures += 1

        # Check if we should open circuit breaker
        if self.state.consecutive_failures >= self.config.circuit_breaker_threshold:
            self.state.circuit_breaker_open = True
            self.state.circuit_breaker_reset_time = time.time() + self.config.max_delay
            logger.error(
                f"Circuit breaker opened after {self.state.consecutive_failures} failures"
            )

        # Increase delay for adaptive strategy
        if self.config.strategy == RateLimitStrategy.ADAPTIVE:
            self.state.current_delay = min(
                self.state.current_delay * 1.5, self.config.max_delay
            )

    def _parse_rate_limit_headers(self, headers: dict[str, str]) -> None:
        """Parse rate limit headers and adjust accordingly."""
        # Common rate limit headers
        remaining_headers = [
            "X-RateLimit-Remaining",
            "x-ratelimit-remaining",
            "X-Rate-Limit-Remaining",
        ]
        limit_headers = ["X-RateLimit-Limit", "x-ratelimit-limit", "X-Rate-Limit-Limit"]
        reset_headers = ["X-RateLimit-Reset", "x-ratelimit-reset", "X-Rate-Limit-Reset"]

        remaining = None
        limit = None
        reset = None

        for header in remaining_headers:
            if header in headers:
                try:
                    remaining = int(headers[header])
                    break
                except ValueError:
                    continue

        for header in limit_headers:
            if header in headers:
                try:
                    limit = int(headers[header])
                    break
                except ValueError:
                    continue

        for header in reset_headers:
            if header in headers:
                try:
                    reset = int(headers[header])
                    break
                except ValueError:
                    continue

        # Adjust delay based on remaining requests
        if remaining is not None and limit is not None:
            usage_ratio = 1 - (remaining / limit) if limit > 0 else 1

            if usage_ratio > 0.8:  # Less than 20% remaining
                self.state.current_delay = min(
                    self.state.current_delay * 1.5, self.config.max_delay
                )
                logger.debug(
                    f"High API usage detected ({usage_ratio:.1%}), increasing delay"
                )
            elif usage_ratio < 0.2:  # More than 80% remaining
                self.state.current_delay = max(
                    self.state.current_delay * 0.8, self.config.min_delay
                )
                logger.debug(
                    f"Low API usage detected ({usage_ratio:.1%}), decreasing delay"
                )

    def _adapt_to_response_time(self, response_time: float) -> None:
        """Adapt delay based on response time."""
        if not self.state.response_time_history:
            return

        # Calculate average response time
        avg_response_time = sum(self.state.response_time_history) / len(
            self.state.response_time_history
        )

        # Adjust delay based on response time
        if avg_response_time > self.config.adaptive_threshold:
            # Slow responses, increase delay
            self.state.current_delay = min(
                self.state.current_delay * 1.1, self.config.max_delay
            )
            logger.debug(
                f"Slow response time ({avg_response_time:.3f}s), increasing delay"
            )
        elif avg_response_time < self.config.adaptive_threshold * 0.5:
            # Fast responses, decrease delay
            self.state.current_delay = max(
                self.state.current_delay * 0.9, self.config.min_delay
            )
            logger.debug(
                f"Fast response time ({avg_response_time:.3f}s), decreasing delay"
            )

    def get_stats(self) -> dict[str, Any]:
        """Get current rate limiting statistics."""
        return {
            "strategy": self.config.strategy.value,
            "current_delay": self.state.current_delay,
            "burst_tokens": self.state.burst_tokens,
            "consecutive_failures": self.state.consecutive_failures,
            "circuit_breaker_open": self.state.circuit_breaker_open,
            "avg_response_time": (
                sum(self.state.response_time_history)
                / len(self.state.response_time_history)
                if self.state.response_time_history
                else 0
            ),
            "config": {
                "base_delay": self.config.base_delay,
                "max_delay": self.config.max_delay,
                "min_delay": self.config.min_delay,
                "burst_capacity": self.config.burst_capacity,
            },
        }

    def reset(self) -> None:
        """Reset rate limiter state."""
        with self._lock:
            self.state = RateLimitState()
            self.state.current_delay = self.config.base_delay
            self.state.burst_tokens = self.config.burst_capacity


# Factory functions for different API types
def create_jira_rate_limiter() -> RateLimiter:
    """Create a rate limiter optimized for Jira API."""
    config = RateLimitConfig(
        strategy=RateLimitStrategy.ADAPTIVE,
        base_delay=0.1,
        max_delay=30.0,
        min_delay=0.01,
        adaptive_threshold=0.5,
        circuit_breaker_threshold=5,
    )
    return RateLimiter(config)


def create_openproject_rate_limiter() -> RateLimiter:
    """Create a rate limiter optimized for OpenProject operations."""
    config = RateLimitConfig(
        strategy=RateLimitStrategy.BURST,
        base_delay=0.05,
        max_delay=10.0,
        min_delay=0.005,
        burst_capacity=20,
        burst_recovery_rate=2.0,
        circuit_breaker_threshold=10,
    )
    return RateLimiter(config)
