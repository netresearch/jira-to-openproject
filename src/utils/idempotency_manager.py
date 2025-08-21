#!/usr/bin/env python3
"""Idempotency Key Manager for Batch API Operations.

This module provides idempotency key support for all batch API endpoints to prevent
duplicate writes on retries or timeouts by caching and reusing previous results
within a 24-hour window.

Features:
- Header parsing for X-Idempotency-Key
- UUID4 generation for missing keys
- Redis storage with atomic operations
- In-memory cache fallback
- TTL-based expiration (24 hours)
- Thread-safe operations
- Lua script for atomic get-and-set
"""

import json
import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

try:  # Optional dependency: tests may run without redis installed
    import redis  # type: ignore
    from redis.exceptions import RedisError  # type: ignore
except Exception:  # pragma: no cover - test environment without redis
    class RedisError(Exception):
        pass

    redis = None  # type: ignore

from src.utils.performance_optimizer import PerformanceCache

logger = logging.getLogger(__name__)


class SafeJSONEncoder(json.JSONEncoder):
    """Safe JSON encoder that handles common Python types without security risks."""

    def default(self, obj):
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return str(obj)
        if hasattr(obj, "__dict__"):
            # For objects with __dict__, try to serialize their attributes
            try:
                return obj.__dict__
            except Exception:
                pass
        elif hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes, bytearray)):
            # For iterables, convert to list
            try:
                return list(obj)
            except Exception:
                pass

        # Reject objects we can't safely serialize
        msg = f"Object of type {type(obj).__name__} is not JSON serializable"
        raise TypeError(msg)


def safe_json_dumps(obj: Any) -> str:
    """Safely serialize an object to JSON with proper error handling.

    Args:
        obj: Object to serialize

    Returns:
        JSON string

    Raises:
        TypeError: If object cannot be safely serialized

    """
    try:
        return json.dumps(obj, cls=SafeJSONEncoder)
    except (TypeError, ValueError) as e:
        msg = f"Failed to serialize object: {e}"
        raise TypeError(msg)


@dataclass
class IdempotencyResult:
    """Result of an idempotency operation."""

    found: bool
    value: Any | None = None
    is_expired: bool = False
    source: str = "redis"  # "redis" or "memory"


class IdempotencyKeyManager:
    """Manages idempotency keys for batch API operations.

    Provides:
    - X-Idempotency-Key header parsing
    - UUID4 generation for missing keys
    - Redis storage with atomic operations
    - In-memory cache fallback
    - 24-hour TTL expiration
    - Thread-safe operations
    """

    # Default TTL for idempotency keys (24 hours)
    DEFAULT_TTL = 86400  # 24 * 60 * 60 seconds

    # Lua script for atomic get-and-set operations
    LUA_GET_SET_SCRIPT = """
    local key = KEYS[1]
    local value = ARGV[1]
    local ttl = tonumber(ARGV[2])

    -- Try to get existing value
    local existing = redis.call('GET', key)
    if existing then
        -- Key exists, return the existing value
        return {1, existing}
    else
        -- Key doesn't exist, set it with TTL
        redis.call('SETEX', key, ttl, value)
        return {0, value}
    end
    """

    def __init__(
        self,
        redis_url: str = "redis://redis:6379",
        fallback_cache_size: int = 1000,
        default_ttl: int = DEFAULT_TTL,
        key_prefix: str = "idempotency:",
        redis_ssl: bool = False,
        redis_ssl_ca_certs: str | None = None,
        redis_ssl_cert_reqs: str = "required",
    ) -> None:
        """Initialize the IdempotencyKeyManager.

        Args:
            redis_url: Redis connection URL
            fallback_cache_size: Size of in-memory fallback cache
            default_ttl: Default TTL for keys in seconds
            key_prefix: Prefix for Redis keys
            redis_ssl: Enable SSL/TLS for Redis connection
            redis_ssl_ca_certs: Path to CA certificates file
            redis_ssl_cert_reqs: SSL certificate requirements

        """
        self.redis_url = redis_url
        self.default_ttl = default_ttl
        self.key_prefix = key_prefix
        self.redis_ssl = redis_ssl
        self.redis_ssl_ca_certs = redis_ssl_ca_certs
        self.redis_ssl_cert_reqs = redis_ssl_cert_reqs
        self._lock = threading.RLock()

        # Initialize Redis client
        self._redis_client: Any | None = None
        self._redis_available = False
        self._lua_script = None

        # Fallback in-memory cache
        self._fallback_cache = PerformanceCache(
            max_size=fallback_cache_size,
            default_ttl=default_ttl,
        )

        # Metrics
        self._metrics = {
            "redis_hits": 0,
            "redis_misses": 0,
            "redis_errors": 0,
            "fallback_hits": 0,
            "fallback_misses": 0,
            "keys_generated": 0,
            "keys_cached": 0,
        }

        # Initialize Redis connection
        self._init_redis()

    def _init_redis(self) -> None:
        """Initialize Redis connection and Lua script."""
        try:
            redis_params = {
                "decode_responses": True,
                "socket_timeout": 5.0,
                "socket_connect_timeout": 5.0,
                "retry_on_timeout": True,
            }

            # Add SSL/TLS configuration if enabled
            if self.redis_ssl:
                redis_params.update(
                    {
                        "ssl": True,
                        "ssl_cert_reqs": self.redis_ssl_cert_reqs,
                    },
                )
                if self.redis_ssl_ca_certs:
                    redis_params["ssl_ca_certs"] = self.redis_ssl_ca_certs

            if redis is None:
                raise RuntimeError("redis module not available")
            self._redis_client = redis.Redis.from_url(self.redis_url, **redis_params)

            # Test connection
            self._redis_client.ping()

            # Load Lua script
            self._lua_script = self._redis_client.register_script(
                self.LUA_GET_SET_SCRIPT,
            )

            self._redis_available = True
            logger.info("Redis connection established for idempotency management")

        except Exception as e:
            logger.warning("Redis connection failed, using in-memory fallback: %s", e)
            self._redis_available = False
            self._redis_client = None

    def parse_idempotency_key(self, headers: dict[str, str] | None = None) -> str:
        """Parse idempotency key from headers or generate a new one.

        Args:
            headers: Request headers dictionary

        Returns:
            Idempotency key string

        """
        if headers and "X-Idempotency-Key" in headers:
            key = headers["X-Idempotency-Key"].strip()
            if key:
                # Validate key format (basic sanitization)
                if self._is_valid_key(key):
                    return key
                logger.warning("Invalid idempotency key format, generating new one")

        # Generate UUID4 if header missing or invalid
        key = str(uuid.uuid4())
        with self._lock:
            self._metrics["keys_generated"] += 1

        logger.debug("Generated new idempotency key: %s", key)
        return key

    def _is_valid_key(self, key: str) -> bool:
        """Validate idempotency key format.

        Only allows valid UUIDv4 format for security.

        Args:
            key: Key to validate

        Returns:
            True if valid UUIDv4, False otherwise

        """
        if not key or len(key) > 255:
            return False

        try:
            # Parse as UUID and verify it's version 4
            parsed_uuid = uuid.UUID(key)
            return parsed_uuid.version == 4
        except (ValueError, AttributeError):
            return False

    def _safe_json_loads(self, data: str) -> Any:
        """Safely deserialize JSON data with validation.

        Args:
            data: JSON string to deserialize

        Returns:
            Deserialized object

        Raises:
            ValueError: If data is invalid or unsafe

        """
        try:
            # Limit size to prevent DoS attacks
            if len(data) > 10 * 1024 * 1024:  # 10MB limit
                msg = "JSON data too large"
                raise ValueError(msg)

            # Parse with strict validation
            result = json.loads(data)

            # Validate that result is a safe type
            if not isinstance(result, (dict, list, str, int, float, bool, type(None))):
                msg = "Unsafe JSON object type"
                raise ValueError(msg)

            return result

        except json.JSONDecodeError as e:
            msg = f"Invalid JSON: {e}"
            raise ValueError(msg)

    def get_cached_result(self, idempotency_key: str) -> IdempotencyResult:
        """Get cached result for an idempotency key.

        Args:
            idempotency_key: The idempotency key

        Returns:
            IdempotencyResult with the cached value or None

        """
        cache_key = f"{self.key_prefix}{idempotency_key}"

        # Try Redis first
        if self._redis_available and self._redis_client:
            try:
                result = self._redis_client.get(cache_key)
                if result is not None:
                    with self._lock:
                        self._metrics["redis_hits"] += 1

                    try:
                        value = self._safe_json_loads(result)
                        return IdempotencyResult(
                            found=True,
                            value=value,
                            source="redis",
                        )
                    except ValueError as e:
                        logger.warning("Failed to decode cached result: %s", e)
                        # Remove corrupted entry
                        self._redis_client.delete(cache_key)
                else:
                    with self._lock:
                        self._metrics["redis_misses"] += 1

            except RedisError as e:
                logger.warning("Redis error during get: %s", e)
                with self._lock:
                    self._metrics["redis_errors"] += 1
                # Fall through to memory cache

        # Try fallback cache
        result = self._fallback_cache.get(cache_key)
        if result is not None:
            with self._lock:
                self._metrics["fallback_hits"] += 1

            return IdempotencyResult(found=True, value=result, source="memory")
        with self._lock:
            self._metrics["fallback_misses"] += 1

        return IdempotencyResult(found=False)

    def cache_result(
        self,
        idempotency_key: str,
        result: Any,
        ttl: int | None = None,
    ) -> bool:
        """Cache a result for an idempotency key.

        Args:
            idempotency_key: The idempotency key
            result: The result to cache
            ttl: TTL in seconds (defaults to default_ttl)

        Returns:
            True if successfully cached, False otherwise

        """
        cache_key = f"{self.key_prefix}{idempotency_key}"
        ttl = ttl or self.default_ttl

        try:
            serialized_result = safe_json_dumps(result)
        except (TypeError, ValueError) as e:
            logger.warning("Failed to serialize result for caching: %s", e)
            return False

        # Try Redis first, fallback to memory only if Redis fails
        if self._redis_available and self._redis_client:
            try:
                self._redis_client.setex(cache_key, ttl, serialized_result)
                logger.debug("Cached result in Redis for key: %s", idempotency_key)

                with self._lock:
                    self._metrics["keys_cached"] += 1
                return True

            except RedisError as e:
                logger.warning("Redis error during set: %s", e)
                with self._lock:
                    self._metrics["redis_errors"] += 1
                # Fall through to memory cache

        # Fallback to memory cache only if Redis is unavailable
        try:
            # Store the Python object directly to avoid redundant JSON round-trip
            self._fallback_cache.set(cache_key, result, ttl)
            logger.debug("Cached result in memory for key: %s", idempotency_key)

            with self._lock:
                self._metrics["keys_cached"] += 1
            return True

        except Exception as e:
            logger.warning("Failed to cache in memory: %s", e)
            return False

    def atomic_get_or_set(
        self,
        idempotency_key: str,
        result_or_func: Any,
        ttl: int | None = None,
    ) -> IdempotencyResult:
        """Atomically get existing result or set new one.

        Uses Redis Lua script for atomic operations to prevent race conditions.

        Args:
            idempotency_key: The idempotency key
            result_or_func: The result to set if key doesn't exist, or a callable to execute
            ttl: TTL in seconds (defaults to default_ttl)

        Returns:
            IdempotencyResult with either existing or newly set value

        """
        cache_key = f"{self.key_prefix}{idempotency_key}"
        ttl = ttl or self.default_ttl

        # Validate TTL to prevent Redis errors
        if not isinstance(ttl, int) or ttl <= 0 or ttl > 7 * 24 * 60 * 60:  # 7 days max
            logger.warning("Invalid TTL value: %s, using default", ttl)
            ttl = self.default_ttl

        # Check if result_or_func is callable (function to execute)
        is_callable = callable(result_or_func)

        # Try Redis Lua script for atomic operation
        if self._redis_available and self._redis_client and self._lua_script:
            try:
                # Execute function if callable to get the result
                result = result_or_func() if is_callable else result_or_func

                # Serialize the result
                try:
                    serialized_result = safe_json_dumps(result)
                except (TypeError, ValueError) as e:
                    logger.exception("Failed to serialize result for caching: %s", e)
                    # Return the result without caching to avoid data loss
                    return IdempotencyResult(found=False, value=result, source="redis")

                # Use Lua script for atomic get-or-set operation
                try:
                    script_result = self._lua_script(
                        keys=[cache_key],
                        args=[serialized_result, str(ttl)],
                    )

                    if script_result == 1:  # Key was set (new)
                        with self._lock:
                            self._metrics["keys_cached"] += 1
                        return IdempotencyResult(
                            found=False,
                            value=result,
                            source="redis",
                        )
                    # Key already existed (cached)
                    with self._lock:
                        self._metrics["redis_hits"] += 1

                    # Get the cached value
                    cached_data = self._redis_client.get(cache_key)
                    if cached_data:
                        try:
                            cached_value = self._safe_json_loads(cached_data)
                            return IdempotencyResult(
                                found=True,
                                value=cached_value,
                                source="redis",
                            )
                        except ValueError as e:
                            logger.warning("Failed to decode cached result: %s", e)
                            # Remove corrupted entry and return current result
                            self._redis_client.delete(cache_key)
                            return IdempotencyResult(
                                found=False,
                                value=result,
                                source="redis",
                            )

                    # Fallback: return current result if cached data is missing
                    return IdempotencyResult(
                        found=False,
                        value=result,
                        source="redis",
                    )

                except Exception as e:
                    logger.warning("Lua script execution failed: %s", e)
                    # Fall through to fallback cache

            except Exception as e:
                logger.warning("Redis operation failed: %s", e)
                with self._lock:
                    self._metrics["redis_errors"] += 1

        # Fallback to atomic operation using lock
        logger.debug(
            "Using fallback atomic operation with lock for key %s",
            idempotency_key,
        )
        with self._lock:
            existing = self.get_cached_result(idempotency_key)
            if existing.found:
                logger.debug(
                    "Found existing result in fallback cache for key %s",
                    idempotency_key,
                )
                return existing

            # Execute function if callable, otherwise use the result
            result = result_or_func() if is_callable else result_or_func

            # Set the new value
            logger.debug(
                "Setting new result in fallback cache for key %s",
                idempotency_key,
            )
            if self.cache_result(idempotency_key, result, ttl):
                return IdempotencyResult(
                    found=False,
                    value=result,
                    source="memory" if not self._redis_available else "redis",
                )

            return IdempotencyResult(found=False, value=result, source="memory")

    def get_metrics(self) -> dict[str, Any]:
        """Get idempotency manager metrics.

        Returns:
            Dictionary of metrics

        """
        with self._lock:
            base_metrics = self._metrics.copy()

        # Add cache stats
        cache_stats = self._fallback_cache.get_stats()

        return {
            **base_metrics,
            "fallback_cache": cache_stats,
            "redis_available": self._redis_available,
        }

    def clear_cache(self) -> None:
        """Clear all cached idempotency keys."""
        if self._redis_available and self._redis_client:
            try:
                # Use SCAN instead of KEYS to avoid blocking Redis
                cursor = 0
                deleted_count = 0

                while True:
                    cursor, keys = self._redis_client.scan(
                        cursor=cursor,
                        match=f"{self.key_prefix}*",
                        count=100,  # Process in batches
                    )

                    if keys:
                        self._redis_client.delete(*keys)
                        deleted_count += len(keys)

                    if cursor == 0:
                        break

                logger.info("Cleared %d Redis idempotency keys", deleted_count)
            except RedisError as e:
                logger.warning("Failed to clear Redis cache: %s", e)

        self._fallback_cache.clear()
        logger.info("Cleared fallback cache")


# Global idempotency manager instance
_idempotency_manager: IdempotencyKeyManager | None = None
_manager_lock = threading.Lock()


def get_idempotency_manager() -> IdempotencyKeyManager:
    """Get the global idempotency manager instance.

    Returns:
        IdempotencyKeyManager instance

    """
    global _idempotency_manager

    if _idempotency_manager is None:
        with _manager_lock:
            if _idempotency_manager is None:
                _idempotency_manager = IdempotencyKeyManager()

    return _idempotency_manager


def reset_idempotency_manager() -> None:
    """Reset the global idempotency manager (for testing)."""
    global _idempotency_manager

    with _manager_lock:
        _idempotency_manager = None
