"""Advanced retry mechanisms with exponential backoff for migration operations.

This module provides robust retry logic for handling transient failures in API calls,
network operations, and other potentially unreliable operations during migration.
"""

import asyncio
import functools
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from secrets import SystemRandom
from typing import Any, ParamSpec, TypeVar

from src.display import configure_logging


class RetryStrategy(Enum):
    """Available retry strategies."""

    EXPONENTIAL_BACKOFF = "exponential_backoff"
    LINEAR_BACKOFF = "linear_backoff"
    FIXED_DELAY = "fixed_delay"
    FIBONACCI_BACKOFF = "fibonacci_backoff"


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    strategy: RetryStrategy = RetryStrategy.EXPONENTIAL_BACKOFF
    jitter: bool = True
    jitter_range: float = 0.1
    backoff_multiplier: float = 2.0

    # Exception handling
    retryable_exceptions: tuple = (Exception,)
    non_retryable_exceptions: tuple = (KeyboardInterrupt, SystemExit)

    # Condition-based retries
    retry_on_status_codes: list[int] | None = None
    retry_on_result: Callable[[Any], bool] | None = None


@dataclass
class RetryResult:
    """Result of a retry operation."""

    success: bool
    result: Any = None
    exception: Exception | None = None
    attempts_made: int = 0
    total_delay: float = 0.0
    last_delay: float = 0.0


class RetryManager:
    """Advanced retry manager with multiple backoff strategies."""

    def __init__(self, config_param: RetryConfig | None = None) -> None:
        """Initialize the retry manager.

        Args:
            config_param: Retry configuration. Uses defaults if not provided.

        """
        self.config = config_param or RetryConfig()
        self.logger = configure_logging("INFO", None)
        # Non-crypto jitter generator that satisfies linting rules
        self._rng = SystemRandom()

    def _calculate_delay(self, attempt: int) -> float:
        """Calculate delay for the given attempt number.

        Args:
            attempt: Current attempt number (0-based)

        Returns:
            Delay in seconds

        """
        if self.config.strategy == RetryStrategy.EXPONENTIAL_BACKOFF:
            delay = self.config.base_delay * (self.config.backoff_multiplier**attempt)
        elif self.config.strategy == RetryStrategy.LINEAR_BACKOFF:
            delay = self.config.base_delay * (attempt + 1)
        elif self.config.strategy == RetryStrategy.FIXED_DELAY:
            delay = self.config.base_delay
        elif self.config.strategy == RetryStrategy.FIBONACCI_BACKOFF:
            # Fibonacci sequence for backoff
            if attempt <= 1:
                delay = self.config.base_delay
            else:
                fib_a, fib_b = 1, 1
                for _ in range(attempt - 1):
                    fib_a, fib_b = fib_b, fib_a + fib_b
                delay = self.config.base_delay * fib_b
        else:
            delay = self.config.base_delay

        # Apply max delay cap
        delay = min(delay, self.config.max_delay)

        # Add jitter to prevent thundering herd
        if self.config.jitter:
            jitter_amount = delay * self.config.jitter_range
            delay += self._rng.uniform(-jitter_amount, jitter_amount)
            delay = max(0, delay)  # Ensure non-negative

        return delay

    def _should_retry(self, exception: Exception, result: object, attempt: int) -> bool:
        """Determine if an operation should be retried.

        Args:
            exception: Exception that occurred (None if no exception)
            result: Result of the operation (None if exception occurred)
            attempt: Current attempt number

        Returns:
            True if should retry, False otherwise

        """
        # Check attempt limit
        if attempt >= self.config.max_attempts:
            return False

        # Check for non-retryable exceptions
        if exception and isinstance(exception, self.config.non_retryable_exceptions):
            return False

        # Check for retryable exceptions
        if exception and not isinstance(exception, self.config.retryable_exceptions):
            return False

        # Check HTTP status codes if configured
        if (
            self.config.retry_on_status_codes
            and hasattr(exception, "response")
            and hasattr(exception.response, "status_code")
        ):
            return exception.response.status_code in self.config.retry_on_status_codes

        # Check result-based retry condition
        if self.config.retry_on_result and result is not None:
            return self.config.retry_on_result(result)

        # Default: retry on any exception
        return exception is not None

    def execute_with_retry(
        self,
        func: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> RetryResult:
        """Execute a function with retry logic.

        Args:
            func: Function to execute
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function

        Returns:
            RetryResult with execution outcome

        """
        total_delay = 0.0
        last_exception = None

        for attempt in range(self.config.max_attempts):
            try:
                # Execute the function
                result = func(*args, **kwargs)

                # Check if result indicates retry needed
                if self.config.retry_on_result and self.config.retry_on_result(result):
                    _msg = f"Retry condition met for result: {result!r}"
                    raise RuntimeError(_msg)

                # Success!
                return RetryResult(
                    success=True,
                    result=result,
                    attempts_made=attempt + 1,
                    total_delay=total_delay,
                )

            except self.config.retryable_exceptions as e:
                last_exception = e

                # Check if we should retry
                if not self._should_retry(e, None, attempt):
                    break

                # Calculate delay for next attempt
                if (
                    attempt < self.config.max_attempts - 1
                ):  # Don't delay after last attempt
                    delay = self._calculate_delay(attempt)
                    total_delay += delay

                    self.logger.debug(
                        "Attempt %d failed: %s. Retrying in %.2fs...",
                        attempt + 1,
                        e,
                        delay,
                    )

                    time.sleep(delay)
                else:
                    self.logger.debug(
                        "Final attempt %d failed: %s",
                        attempt + 1,
                        e,
                    )

        # All attempts failed
        return RetryResult(
            success=False,
            exception=last_exception,
            attempts_made=self.config.max_attempts,
            total_delay=total_delay,
        )

    def get_metrics(self) -> dict[str, Any]:
        """Get retry manager metrics.

        Returns:
            Dictionary with retry statistics

        """
        return {
            "max_attempts": self.config.max_attempts,
            "base_delay": self.config.base_delay,
            "max_delay": self.config.max_delay,
            "strategy": self.config.strategy.value,
            "jitter_enabled": self.config.jitter,
        }


# Decorator for easy retry functionality
P = ParamSpec("P")
R = TypeVar("R")


def retry(  # noqa: PLR0913
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    strategy: RetryStrategy = RetryStrategy.EXPONENTIAL_BACKOFF,
    *,
    jitter: bool = True,
    retryable_exceptions: tuple = (Exception,),
    non_retryable_exceptions: tuple = (KeyboardInterrupt, SystemExit),
    retry_on_status_codes: list[int] | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Add retry logic to a function.

    Args:
        max_attempts: Maximum number of retry attempts
        base_delay: Base delay between retries
        max_delay: Maximum delay cap
        strategy: Retry strategy to use
        jitter: Whether to add jitter to delays
        retryable_exceptions: Tuple of exceptions that should trigger retries
        non_retryable_exceptions: Tuple of exceptions that should not be retried
        retry_on_status_codes: HTTP status codes that should trigger retries

    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            retry_config = RetryConfig(
                max_attempts=max_attempts,
                base_delay=base_delay,
                max_delay=max_delay,
                strategy=strategy,
                jitter=jitter,
                retryable_exceptions=retryable_exceptions,
                non_retryable_exceptions=non_retryable_exceptions,
                retry_on_status_codes=retry_on_status_codes,
            )

            retry_manager = RetryManager(retry_config)
            result = retry_manager.execute_with_retry(func, *args, **kwargs)

            if result.success:
                return result.result  # type: ignore[return-value]
            raise result.exception  # type: ignore[misc]

        return wrapper

    return decorator


# Async version for async operations
class AsyncRetryManager:
    """Async retry manager for async operations."""

    def __init__(self, config: RetryConfig | None = None) -> None:
        """Initialize the async retry manager.

        Args:
            config: Retry configuration. Uses defaults if not provided.

        """
        self.config = config or RetryConfig()
        self.logger = configure_logging("INFO", None)
        self._rng = SystemRandom()

    def _calculate_delay(self, attempt: int) -> float:
        """Calculate delay for the given attempt number (duplicated from sync version)."""
        cfg = self.config
        if cfg.strategy == RetryStrategy.EXPONENTIAL_BACKOFF:
            delay = cfg.base_delay * (cfg.backoff_multiplier**attempt)
        elif cfg.strategy == RetryStrategy.LINEAR_BACKOFF:
            delay = cfg.base_delay * (attempt + 1)
        elif cfg.strategy == RetryStrategy.FIXED_DELAY:
            delay = cfg.base_delay
        elif cfg.strategy == RetryStrategy.FIBONACCI_BACKOFF:
            if attempt <= 1:
                delay = cfg.base_delay
            else:
                fib_a, fib_b = 1, 1
                for _ in range(attempt - 1):
                    fib_a, fib_b = fib_b, fib_a + fib_b
                delay = cfg.base_delay * fib_b
        else:
            delay = cfg.base_delay

        delay = min(delay, cfg.max_delay)
        if cfg.jitter:
            jitter_amount = delay * cfg.jitter_range
            delay += self._rng.uniform(-jitter_amount, jitter_amount)
            delay = max(0, delay)
        return delay

    def _should_retry(self, exception: Exception, result: object, attempt: int) -> bool:
        """Determine if an operation should be retried (duplicated from sync version)."""
        cfg = self.config
        if attempt >= cfg.max_attempts:
            return False
        if exception and isinstance(exception, cfg.non_retryable_exceptions):
            return False
        if exception and not isinstance(exception, cfg.retryable_exceptions):
            return False
        if (
            cfg.retry_on_status_codes
            and hasattr(exception, "response")
            and hasattr(exception.response, "status_code")
        ):
            return exception.response.status_code in cfg.retry_on_status_codes
        if cfg.retry_on_result and result is not None:
            return cfg.retry_on_result(result)  # type: ignore[arg-type]
        return exception is not None

    async def execute_with_retry(
        self,
        coro_func: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> RetryResult:
        """Execute an async function with retry logic.

        Args:
            coro_func: Async function to execute
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function

        Returns:
            RetryResult with execution outcome

        """
        total_delay = 0.0
        last_exception = None

        for attempt in range(self.config.max_attempts):
            try:
                # Execute the async function
                result = await coro_func(*args, **kwargs)

                # Check if result indicates retry needed
                if self.config.retry_on_result and self.config.retry_on_result(result):
                    _msg = f"Retry condition met for result: {result!r}"
                    raise RuntimeError(_msg)

                # Success!
                return RetryResult(
                    success=True,
                    result=result,
                    attempts_made=attempt + 1,
                    total_delay=total_delay,
                )

            except self.config.retryable_exceptions as e:
                last_exception = e

                # Check if we should retry
                if not self._should_retry(e, None, attempt):
                    break

                # Calculate delay for next attempt
                if (
                    attempt < self.config.max_attempts - 1
                ):  # Don't delay after last attempt
                    delay = self._calculate_delay(attempt)
                    total_delay += delay

                    self.logger.debug(
                        "Async attempt %d failed: %s. Retrying in %.2fs...",
                        attempt + 1,
                        e,
                        delay,
                    )

                    await asyncio.sleep(delay)
                else:
                    self.logger.debug(
                        "Final async attempt %d failed: %s",
                        attempt + 1,
                        e,
                    )

        # All attempts failed
        return RetryResult(
            success=False,
            exception=last_exception,
            attempts_made=self.config.max_attempts,
            total_delay=total_delay,
        )


# Common retry configurations for different scenarios
class CommonRetryConfigs:
    """Pre-configured retry settings for common scenarios."""

    # For API calls with potential rate limiting
    API_CALLS = RetryConfig(
        max_attempts=5,
        base_delay=1.0,
        max_delay=30.0,
        strategy=RetryStrategy.EXPONENTIAL_BACKOFF,
        jitter=True,
        retry_on_status_codes=[429, 500, 502, 503, 504],
    )

    # For network operations
    NETWORK_OPERATIONS = RetryConfig(
        max_attempts=3,
        base_delay=2.0,
        max_delay=20.0,
        strategy=RetryStrategy.EXPONENTIAL_BACKOFF,
        jitter=True,
    )

    # For database operations
    DATABASE_OPERATIONS = RetryConfig(
        max_attempts=3,
        base_delay=0.5,
        max_delay=5.0,
        strategy=RetryStrategy.EXPONENTIAL_BACKOFF,
        jitter=False,
    )

    # For file operations
    FILE_OPERATIONS = RetryConfig(
        max_attempts=2,
        base_delay=0.1,
        max_delay=1.0,
        strategy=RetryStrategy.FIXED_DELAY,
        jitter=False,
    )


# Quick decorator instances for common use cases
def api_retry(func: Callable) -> Callable:
    """Decorate API-call functions with a sensible retry policy."""
    return retry(
        max_attempts=5,
        base_delay=1.0,
        max_delay=30.0,
        strategy=RetryStrategy.EXPONENTIAL_BACKOFF,
        retry_on_status_codes=[429, 500, 502, 503, 504],
    )(func)


def network_retry(func: Callable) -> Callable:
    """Decorate network operations with a conservative retry policy."""
    return retry(
        max_attempts=3,
        base_delay=2.0,
        max_delay=20.0,
        strategy=RetryStrategy.EXPONENTIAL_BACKOFF,
    )(func)


T = TypeVar("T")


def db_retry(func: Callable[P, T]) -> Callable[P, T]:  # noqa: UP047
    """Decorate database operations with conservative retries."""
    return retry(
        max_attempts=3,
        base_delay=0.5,
        max_delay=5.0,
        strategy=RetryStrategy.EXPONENTIAL_BACKOFF,
        jitter=False,
    )(func)
