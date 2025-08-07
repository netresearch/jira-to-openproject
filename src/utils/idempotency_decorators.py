#!/usr/bin/env python3
"""Idempotency Decorators for Batch Operations.

This module provides decorators and utilities to add idempotency support
to existing batch API operations without major code changes.
"""

import functools
import logging
from collections.abc import Callable
from typing import Any

from src.utils.idempotency_manager import get_idempotency_manager

logger = logging.getLogger(__name__)


def with_idempotency(
    header_extractor: Callable[..., dict[str, str]] | None = None,
    result_processor: Callable[[Any], Any] | None = None,
    ttl: int | None = None,
):
    """Decorator to add idempotency support to batch operations.

    Args:
        header_extractor: Function to extract headers from function arguments
        result_processor: Function to process results before caching
        ttl: Custom TTL for this operation

    Returns:
        Decorated function with idempotency support

    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Get idempotency manager
            try:
                manager = get_idempotency_manager()
            except Exception as e:
                logger.warning("Failed to initialize idempotency manager: %s", e)
                # Fall back to executing function without idempotency
                return func(*args, **kwargs)

            # Extract headers if available
            headers = None
            if header_extractor:
                try:
                    headers = header_extractor(*args, **kwargs)
                except Exception as e:
                    logger.debug("Failed to extract headers: %s", e)

            # Parse or generate idempotency key
            idempotency_key = manager.parse_idempotency_key(headers)

            # Use atomic get-or-set to handle race conditions
            logger.debug("Attempting atomic get-or-set for key %s", idempotency_key)

            # Define the function to execute if no cached result exists
            def execute_and_process():
                logger.debug(
                    "Executing function %s with idempotency key %s",
                    func.__name__,
                    idempotency_key,
                )
                result = func(*args, **kwargs)

                # Process result if needed
                if result_processor:
                    try:
                        final_result = result_processor(result)
                        logger.debug(
                            "Result processed successfully for key %s",
                            idempotency_key,
                        )
                        return final_result
                    except Exception as e:
                        logger.warning(
                            "Result processor failed for key %s: %s",
                            idempotency_key,
                            e,
                        )
                        # Don't cache if processing fails to avoid inconsistent state
                        return result

                return result

            # Use atomic get-or-set to handle race conditions
            cached_result = manager.atomic_get_or_set(
                idempotency_key,
                execute_and_process,
                ttl,
            )

            if cached_result.found:
                logger.debug(
                    "Idempotent cache hit for key %s from %s",
                    idempotency_key,
                    cached_result.source,
                )
                return cached_result.value
            logger.debug("Cached result for idempotency key %s", idempotency_key)
            return cached_result.value

        # Add idempotency metadata
        wrapper._has_idempotency = True
        wrapper._original_function = func

        return wrapper

    return decorator


def extract_headers_from_kwargs(key: str = "headers") -> Callable:
    """Create a header extractor that gets headers from kwargs.

    Args:
        key: The key name in kwargs containing headers

    Returns:
        Header extractor function

    """

    def extractor(*args, **kwargs) -> dict[str, str]:
        return kwargs.get(key, {})

    return extractor


def extract_headers_from_request() -> Callable:
    """Extract headers from request object (Flask or Django style).

    Returns:
        Function that extracts headers from request object

    """

    def extractor(*args, **kwargs) -> dict[str, str]:
        # Look for request object in args or kwargs
        request = None
        for arg in args:
            if hasattr(arg, "headers") or hasattr(arg, "META"):
                request = arg
                break

        if not request:
            for value in kwargs.values():
                if hasattr(value, "headers") or hasattr(value, "META"):
                    request = value
                    break

        if not request:
            return {}

        headers = {}

        # Try Flask-style headers first (most common)
        if hasattr(request, "headers"):
            try:
                if hasattr(request.headers, "get"):
                    # Flask-style headers object
                    headers = dict(request.headers)
                elif isinstance(request.headers, dict):
                    # Already a dict
                    headers = request.headers
            except (TypeError, AttributeError):
                pass

        # If no Flask headers found, try Django-style
        if not headers and hasattr(request, "META"):
            try:
                for key, value in request.META.items():
                    if key.startswith("HTTP_"):
                        # Convert HTTP_X_IDEMPOTENCY_KEY -> X-Idempotency-Key
                        header_name = key[5:].replace("_", "-").title()
                        headers[header_name] = value
            except (TypeError, AttributeError):
                pass

        return headers

    return extractor


def create_batch_result_processor(
    success_key: str = "success",
    error_key: str = "errors",
    data_key: str = "data",
) -> Callable:
    """Create a result processor for batch operations.

    Args:
        success_key: Key indicating operation success
        error_key: Key containing error information
        data_key: Key containing result data

    Returns:
        Result processor function

    """

    def processor(result: Any) -> Any:
        if isinstance(result, dict):
            # Check if this is already a processed result (has idempotent flag)
            if result.get("idempotent") is True and "cached_at" in result:
                # Already processed, return as-is
                return result

            # Create a composite result structure for partial failures
            from datetime import datetime

            return {
                "success": result.get(success_key, True),
                "data": result.get(data_key, result),
                "errors": result.get(error_key, []),
                "cached_at": datetime.utcnow().isoformat(),
                "idempotent": True,
            }

        return result

    return processor


# Convenience decorators for common patterns
def batch_idempotent(ttl: int | None = None):
    """Simple idempotency decorator for batch operations.

    Args:
        ttl: Custom TTL in seconds

    Returns:
        Idempotency decorator

    """
    return with_idempotency(
        header_extractor=extract_headers_from_kwargs(),
        result_processor=create_batch_result_processor(),
        ttl=ttl,
    )


def api_idempotent(ttl: int | None = None):
    """Idempotency decorator for API endpoints.

    Args:
        ttl: Custom TTL in seconds

    Returns:
        Idempotency decorator

    """
    return with_idempotency(
        header_extractor=extract_headers_from_request(),
        result_processor=create_batch_result_processor(),
        ttl=ttl,
    )


def simple_idempotent(ttl: int | None = None):
    """Simple idempotency decorator without header extraction.

    Args:
        ttl: Custom TTL in seconds

    Returns:
        Idempotency decorator

    """
    return with_idempotency(ttl=ttl)
