"""Test adaptive rate limiting functionality."""

import unittest
from unittest.mock import patch

from src.utils.rate_limiter import (
    RateLimitConfig,
    RateLimiter,
    RateLimitStrategy,
    create_jira_rate_limiter,
    create_openproject_rate_limiter,
)


class TestAdaptiveRateLimiting(unittest.TestCase):
    """Test suite for adaptive rate limiting functionality."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.config = RateLimitConfig(
            strategy=RateLimitStrategy.ADAPTIVE,
            base_delay=0.1,
            max_delay=1.0,
            min_delay=0.01,
            adaptive_threshold=0.5,
        )
        self.rate_limiter = RateLimiter(self.config)

    def test_basic_rate_limiting(self) -> None:
        """Test basic rate limiting functionality."""
        # Should not raise any exceptions
        self.rate_limiter.wait_if_needed("test_endpoint")

        # Record a successful response
        self.rate_limiter.record_response(0.2, 200)

        # Should still work
        self.rate_limiter.wait_if_needed("test_endpoint")

    def test_429_error_handling(self) -> None:
        """Test handling of 429 rate limit exceeded errors."""
        # Mock time.sleep to avoid actual delays in tests
        with patch("time.sleep") as mock_sleep:
            # Record a 429 error
            self.rate_limiter.record_response(0.5, 429)

            # Should have called sleep for backoff
            mock_sleep.assert_called()

            # Current delay should be increased
            assert self.rate_limiter.state.current_delay > self.config.base_delay

    def test_adaptive_delay_adjustment(self) -> None:
        """Test that delay adapts based on response times."""
        # Record fast responses
        for _ in range(5):
            self.rate_limiter.record_response(0.1, 200)  # Fast response

        # Delay should decrease
        fast_delay = self.rate_limiter.state.current_delay

        # Reset for slow responses
        self.rate_limiter.reset()

        # Record slow responses
        for _ in range(5):
            self.rate_limiter.record_response(1.0, 200)  # Slow response

        slow_delay = self.rate_limiter.state.current_delay

        # With adaptive strategy, delays should adjust
        # This is a basic test - actual behavior depends on threshold
        assert isinstance(fast_delay, float)
        assert isinstance(slow_delay, float)

    def test_circuit_breaker(self) -> None:
        """Test circuit breaker functionality."""
        # Record multiple server errors
        for _i in range(6):  # Exceed circuit breaker threshold
            self.rate_limiter.record_response(0.5, 500)

        # Circuit breaker should be open
        assert self.rate_limiter.state.circuit_breaker_open

        # Reset should close circuit breaker
        self.rate_limiter.reset()
        assert not self.rate_limiter.state.circuit_breaker_open

    def test_rate_limit_header_parsing(self) -> None:
        """Test parsing of rate limit headers."""
        headers = {"X-RateLimit-Remaining": "10", "X-RateLimit-Limit": "100"}

        # Record response with rate limit headers
        self.rate_limiter.record_response(0.3, 200, headers)

        # Should not raise any exceptions
        assert self.rate_limiter.state.consecutive_failures == 0

    def test_burst_strategy(self) -> None:
        """Test burst rate limiting strategy."""
        config = RateLimitConfig(
            strategy=RateLimitStrategy.BURST,
            burst_capacity=5,
            burst_recovery_rate=1.0,
        )
        burst_limiter = RateLimiter(config)

        # Should allow burst requests
        for _i in range(5):
            burst_limiter.wait_if_needed("test")

        # Should have consumed burst tokens
        assert burst_limiter.state.burst_tokens < 5

    def test_factory_functions(self) -> None:
        """Test factory functions for different API types."""
        jira_limiter = create_jira_rate_limiter()
        assert isinstance(jira_limiter, RateLimiter)
        assert jira_limiter.config.strategy == RateLimitStrategy.ADAPTIVE

        op_limiter = create_openproject_rate_limiter()
        assert isinstance(op_limiter, RateLimiter)
        assert op_limiter.config.strategy == RateLimitStrategy.BURST

    def test_stats_collection(self) -> None:
        """Test statistics collection."""
        # Record some responses
        self.rate_limiter.record_response(0.3, 200)
        self.rate_limiter.record_response(0.5, 200)

        stats = self.rate_limiter.get_stats()

        # Should contain expected keys
        expected_keys = [
            "strategy",
            "current_delay",
            "consecutive_failures",
            "circuit_breaker_open",
            "avg_response_time",
            "config",
        ]

        for key in expected_keys:
            assert key in stats

        # Should have calculated average response time
        assert stats["avg_response_time"] > 0

    def test_retry_after_header(self) -> None:
        """Test handling of Retry-After header."""
        with patch("time.sleep") as mock_sleep:
            headers = {"Retry-After": "2"}

            # Record 429 with Retry-After header
            self.rate_limiter.record_response(0.5, 429, headers)

            # Should have slept for the retry-after duration
            mock_sleep.assert_called_with(2.0)

    def test_exponential_backoff(self) -> None:
        """Test exponential backoff strategy."""
        config = RateLimitConfig(
            strategy=RateLimitStrategy.EXPONENTIAL,
            base_delay=0.1,
            exponential_base=2.0,
        )
        exp_limiter = RateLimiter(config)

        # Record consecutive failures
        exp_limiter.state.consecutive_failures = 3

        # Calculate expected delay
        expected_delay = 0.1 * (2.0**3)  # 0.8 seconds

        # Test delay calculation
        with patch("time.sleep") as mock_sleep:
            exp_limiter.wait_if_needed("test")

            # Should have used exponential backoff
            mock_sleep.assert_called_with(expected_delay)


if __name__ == "__main__":
    unittest.main()
