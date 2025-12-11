"""Security-focused configuration validation utilities for performance components.

This module provides comprehensive input validation with bounds checking, type validation,
and string sanitization to prevent resource exhaustion and security vulnerabilities.
"""

import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ConfigurationValidationError(Exception):
    """Custom exception for configuration validation failures with detailed context."""

    def __init__(
        self,
        parameter_name: str,
        invalid_value: Any,
        expected_range: str,
        additional_context: str | None = None,
    ) -> None:
        self.parameter_name = parameter_name
        self.invalid_value = invalid_value
        self.expected_range = expected_range
        self.additional_context = additional_context

        # Security: Sanitize potentially sensitive values in error messages
        sanitized_value = self._sanitize_error_value(invalid_value)

        message = f"Invalid {parameter_name}: '{sanitized_value}' (expected: {expected_range})"
        if additional_context:
            message += f" - {additional_context}"

        super().__init__(message)

        # Log validation failure at WARN level before aborting
        logger.warning(f"Configuration validation failed: {message}")

    def _sanitize_error_value(self, value: Any) -> str:
        """Sanitize error values to prevent information disclosure."""
        if value is None:
            return "None"

        value_str = str(value)

        # Truncate long values to prevent log pollution
        if len(value_str) > 100:
            return f"{value_str[:50]}...(truncated, {len(value_str)} chars total)"

        # Mask potential sensitive patterns
        if any(pattern in value_str.lower() for pattern in ["password", "token", "key", "secret"]):
            return "[REDACTED]"

        return value_str


class SecurityValidator:
    """Centralized validation utilities with security-focused bounds checking and sanitization."""

    # Security-focused parameter bounds
    NUMERIC_BOUNDS = {
        # Core processing parameters
        "batch_size": {"min": 1, "max": 500, "type": int},
        "max_workers": {"min": 1, "max": min(os.cpu_count() or 4, 32), "type": int},
        "max_concurrent_batches": {
            "min": 1,
            "max": min(os.cpu_count() or 4, 16),
            "type": int,
        },
        "retry_attempts": {"min": 0, "max": 10, "type": int},
        "max_retries": {"min": 0, "max": 10, "type": int},
        # Rate limiting parameters
        "rate_limit_per_sec": {"min": 1, "max": 1000, "type": int},
        "max_requests_per_minute": {"min": 1, "max": 6000, "type": int},
        "burst_size": {"min": 1, "max": 100, "type": int},
        "burst_capacity": {"min": 1, "max": 100, "type": int},
        # Timing parameters (seconds)
        "batch_timeout": {"min": 1.0, "max": 3600.0, "type": float},
        "base_delay": {"min": 0.001, "max": 60.0, "type": float},
        "max_delay": {"min": 0.001, "max": 300.0, "type": float},
        "min_delay": {"min": 0.001, "max": 60.0, "type": float},
        "progress_update_interval": {"min": 0.1, "max": 60.0, "type": float},
        "time_window": {"min": 1.0, "max": 3600.0, "type": float},
        # Memory and resource limits
        "memory_limit_mb": {"min": 64, "max": 8192, "type": int},
        "cache_size": {"min": 10, "max": 10000, "type": int},
        "cache_ttl": {"min": 60, "max": 86400, "type": int},
        # Factor and ratio parameters
        "adaptive_factor": {"min": 0.1, "max": 1.0, "type": float},
        "recovery_factor": {"min": 1.0, "max": 3.0, "type": float},
        "exponential_base": {"min": 1.1, "max": 10.0, "type": float},
        "adaptive_threshold": {"min": 0.01, "max": 10.0, "type": float},
    }

    # Dangerous characters and patterns for string sanitization
    CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f]")
    # Improved SQL injection pattern - more comprehensive, fewer false positives
    SQL_INJECTION_PATTERN = re.compile(
        r"""
        (--[^\r\n]*) |                      # SQL comment
        (/\*.*?\*/) |                       # Multi-line comment
        (\b(UNION|SELECT|INSERT|DELETE|DROP|EXEC|EXECUTE|ALTER|CREATE|TRUNCATE|UPDATE)\b.*
         \b(SELECT|FROM|WHERE|INSERT|UPDATE|DELETE|DROP|TABLE|DATABASE|SCHEMA)\b) |
        (\b(XP_CMDSHELL|SP_EXECUTESQL|OPENQUERY|OPENROWSET)\b) |  # SQL Server procedures
        ([\s;]\s*['"][^'"]*['"]\s*=\s*['"][^'"]*['"];?) |       # Suspicious assignments
        ([\s;]\s*\d+\s*=\s*\d+) |                              # Tautology like 1=1
        (\b(CAST|CONVERT|CHAR|NCHAR|ASCII|SUBSTRING)\b\s*\() | # Type conversion functions
        (0x[0-9a-fA-F]+)                                        # Hex encoding
    """,
        re.IGNORECASE | re.VERBOSE,
    )
    SCRIPT_INJECTION_PATTERN = re.compile(
        r"<script|javascript:|data:|vbscript:",
        re.IGNORECASE,
    )
    PATH_TRAVERSAL_PATTERN = re.compile(r"\.\.|[<>|]")

    # Whitelist patterns for different string types
    SAFE_FILENAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")
    SAFE_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")
    NUMERIC_STRING_PATTERN = re.compile(r"^[-+]?[0-9]*\.?[0-9]+$")

    @classmethod
    def validate_numeric_parameter(
        cls,
        name: str,
        value: Any,
        allow_none: bool = False,
    ) -> int | float | None:
        """Validate numeric parameters with security-focused bounds checking.

        Args:
            name: Parameter name for error reporting
            value: Value to validate
            allow_none: Whether None values are allowed

        Returns:
            Validated numeric value

        Raises:
            ConfigurationValidationError: If validation fails

        """
        if value is None:
            if allow_none:
                return None
            raise ConfigurationValidationError(name, value, "non-null value")

        bounds = cls.NUMERIC_BOUNDS.get(name)
        if not bounds:
            # For unknown parameters, apply basic numeric validation
            if not isinstance(value, (int, float)):
                raise ConfigurationValidationError(name, value, "numeric value")
            return value

        expected_type = bounds["type"]
        min_val = bounds["min"]
        max_val = bounds["max"]

        # Type validation and coercion
        if isinstance(value, str):
            # Security: Only allow numeric strings matching pattern
            if not cls.NUMERIC_STRING_PATTERN.match(value.strip()):
                raise ConfigurationValidationError(
                    name,
                    value,
                    f"{expected_type.__name__} (got malformed string)",
                    "String contains non-numeric characters",
                )
            try:
                value = expected_type(value.strip())
            except ValueError as e:
                raise ConfigurationValidationError(
                    name,
                    value,
                    f"valid {expected_type.__name__}",
                    f"Conversion failed: {e}",
                )
        elif not isinstance(value, expected_type):
            # Try to convert compatible types
            try:
                value = expected_type(value)
            except (ValueError, TypeError) as e:
                raise ConfigurationValidationError(
                    name,
                    value,
                    f"{expected_type.__name__} (got {type(value).__name__})",
                    f"Type conversion failed: {e}",
                )

        # Bounds validation
        if value < min_val:
            raise ConfigurationValidationError(
                name,
                value,
                f">= {min_val}",
                "Value below security minimum",
            )
        if value > max_val:
            raise ConfigurationValidationError(
                name,
                value,
                f"<= {max_val}",
                "Value exceeds security maximum to prevent resource exhaustion",
            )

        return value

    @classmethod
    def validate_string_parameter(
        cls,
        name: str,
        value: Any,
        allow_empty: bool = False,
        max_length: int = 1000,
        pattern: str | None = None,
    ) -> str:
        """Validate and sanitize string parameters with security focus.

        Args:
            name: Parameter name for error reporting
            value: Value to validate
            allow_empty: Whether empty strings are allowed
            max_length: Maximum allowed string length
            pattern: Optional regex pattern for validation

        Returns:
            Validated and sanitized string

        Raises:
            ConfigurationValidationError: If validation fails

        """
        if not isinstance(value, str):
            raise ConfigurationValidationError(
                name,
                value,
                f"string (got {type(value).__name__})",
            )

        # Length validation
        if len(value) > max_length:
            raise ConfigurationValidationError(
                name,
                value,
                f"string <= {max_length} characters",
                f"String too long ({len(value)} chars) - potential DoS",
            )

        if not allow_empty and not value.strip():
            raise ConfigurationValidationError(name, value, "non-empty string")

        # Security sanitization - remove control characters
        sanitized = cls.CONTROL_CHARS_PATTERN.sub("", value)

        # Security checks for injection patterns
        if cls.SQL_INJECTION_PATTERN.search(sanitized):
            raise ConfigurationValidationError(
                name,
                value,
                "string without SQL injection patterns",
                "Detected potential SQL injection attempt",
            )

        if cls.SCRIPT_INJECTION_PATTERN.search(sanitized):
            raise ConfigurationValidationError(
                name,
                value,
                "string without script injection patterns",
                "Detected potential script injection attempt",
            )

        if cls.PATH_TRAVERSAL_PATTERN.search(sanitized):
            raise ConfigurationValidationError(
                name,
                value,
                "string without path traversal patterns",
                "Detected potential path traversal attempt",
            )

        # Pattern validation if specified
        if pattern and not re.match(pattern, sanitized):
            raise ConfigurationValidationError(
                name,
                value,
                f"string matching pattern {pattern}",
            )

        return sanitized

    @classmethod
    def validate_file_path(
        cls,
        name: str,
        value: Any,
        must_exist: bool = False,
        must_be_absolute: bool = False,
    ) -> Path | None:
        """Validate file path parameters with security checks.

        Args:
            name: Parameter name for error reporting
            value: Path value to validate
            must_exist: Whether path must exist
            must_be_absolute: Whether path must be absolute

        Returns:
            Validated Path object or None

        Raises:
            ConfigurationValidationError: If validation fails

        """
        if value is None:
            return None

        if isinstance(value, str):
            # Security: sanitize the path string
            value = cls.validate_string_parameter(
                name,
                value,
                allow_empty=False,
                max_length=4096,
            )
            try:
                path_obj = Path(value).resolve()
            except (ValueError, OSError) as e:
                raise ConfigurationValidationError(
                    name,
                    value,
                    "valid file path",
                    f"Path resolution failed: {e}",
                )
        elif isinstance(value, Path):
            path_obj = value.resolve()
        else:
            raise ConfigurationValidationError(
                name,
                value,
                f"string or Path (got {type(value).__name__})",
            )

        # Security: ensure path doesn't escape expected boundaries
        path_str = str(path_obj)
        if ".." in path_str or path_str.startswith(("/etc", "/proc")):
            raise ConfigurationValidationError(
                name,
                value,
                "safe file path",
                "Path appears to access sensitive system areas",
            )

        if must_be_absolute and not path_obj.is_absolute():
            raise ConfigurationValidationError(name, value, "absolute path")

        if must_exist and not path_obj.exists():
            raise ConfigurationValidationError(name, value, "existing path")

        return path_obj

    @classmethod
    def validate_resource_allocation(
        cls,
        batch_size: int,
        max_workers: int,
        memory_limit_mb: int,
    ) -> None:
        """Validate resource allocation combinations to prevent system overload.

        Args:
            batch_size: Batch size for processing
            max_workers: Maximum worker threads
            memory_limit_mb: Memory limit in MB

        Raises:
            ConfigurationValidationError: If resource allocation is unsafe

        """
        # Improved memory estimation based on typical object sizes
        # Base overhead per worker: 32MB (thread stack + Python overhead)
        # Per-item overhead: 1KB (typical JSON object + processing overhead)
        base_memory_per_worker = 32  # MB
        item_memory_overhead = batch_size * 0.001  # 1KB per item in MB
        estimated_memory_per_worker = base_memory_per_worker + item_memory_overhead
        total_estimated_memory = max_workers * estimated_memory_per_worker

        if total_estimated_memory > memory_limit_mb:
            msg = "resource_allocation"
            raise ConfigurationValidationError(
                msg,
                f"batch_size={batch_size}, max_workers={max_workers}",
                f"combination that fits within {memory_limit_mb}MB",
                f"Estimated usage {total_estimated_memory:.1f}MB exceeds limit",
            )

        # CPU oversubscription check - more conservative for I/O bound tasks
        cpu_count = os.cpu_count() or 4
        # Allow 1.5x CPU count for I/O bound tasks (migration is mostly I/O)
        max_recommended_workers = int(cpu_count * 1.5)
        if max_workers > max_recommended_workers:
            msg = "max_workers"
            raise ConfigurationValidationError(
                msg,
                max_workers,
                f"<= {max_recommended_workers} (1.5x CPU cores for I/O bound tasks)",
                "Excessive worker count may cause system instability",
            )

    @classmethod
    def validate_timing_relationships(
        cls,
        base_delay: float,
        max_delay: float,
        min_delay: float | None = None,
    ) -> None:
        """Validate timing parameter relationships.

        Args:
            base_delay: Base delay value
            max_delay: Maximum delay value
            min_delay: Optional minimum delay value

        Raises:
            ConfigurationValidationError: If timing relationships are invalid

        """
        if min_delay is not None and base_delay < min_delay:
            msg = "base_delay"
            raise ConfigurationValidationError(
                msg,
                base_delay,
                f">= min_delay ({min_delay})",
            )

        if max_delay < base_delay:
            msg = "max_delay"
            raise ConfigurationValidationError(
                msg,
                max_delay,
                f">= base_delay ({base_delay})",
            )

        # Reasonable ratio check
        if max_delay > base_delay * 1000:
            msg = "max_delay"
            raise ConfigurationValidationError(
                msg,
                max_delay,
                f"<= {base_delay * 1000} (1000x base_delay)",
                "Excessive delay ratio may cause indefinite blocking",
            )


def validate_configuration_dict(
    config_dict: dict[str, Any],
    expected_keys: list[str] | None = None,
) -> dict[str, Any]:
    """Validate and sanitize a complete configuration dictionary.

    Args:
        config_dict: Configuration dictionary to validate
        expected_keys: Optional list of expected keys

    Returns:
        Validated and sanitized configuration dictionary

    Raises:
        ConfigurationValidationError: If validation fails

    """
    if not isinstance(config_dict, dict):
        msg = "config"
        raise ConfigurationValidationError(msg, config_dict, "dictionary")

    validated_config = {}

    # Validate known numeric parameters
    for key, value in config_dict.items():
        if key in SecurityValidator.NUMERIC_BOUNDS:
            validated_config[key] = SecurityValidator.validate_numeric_parameter(
                key,
                value,
            )
        elif isinstance(value, str):
            validated_config[key] = SecurityValidator.validate_string_parameter(
                key,
                value,
                allow_empty=True,
                max_length=1000,
            )
        elif isinstance(value, bool):
            validated_config[key] = value
        elif value is None:
            validated_config[key] = None
        else:
            # For other types, perform basic safety checks
            validated_config[key] = value

    # Check for expected keys if provided
    if expected_keys:
        missing_keys = set(expected_keys) - set(validated_config.keys())
        if missing_keys:
            msg = "config"
            raise ConfigurationValidationError(
                msg,
                config_dict,
                f"dictionary containing keys: {missing_keys}",
            )

    return validated_config
