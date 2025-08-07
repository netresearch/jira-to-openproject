#!/usr/bin/env python3
"""Configuration settings for error recovery system."""

from dataclasses import dataclass


@dataclass
class ErrorRecoveryConfig:
    """Configuration for error recovery system."""

    # Retry settings
    max_retries: int = 3
    retry_delay_base: float = 1.0  # seconds
    retry_delay_max: float = 60.0  # seconds
    retry_exponential_base: float = 2.0

    # Circuit breaker settings
    circuit_breaker_threshold: int = 5  # failures before opening
    circuit_breaker_timeout: int = 60  # seconds before half-open
    circuit_breaker_expected_exception: type | None = None

    # Checkpointing settings
    checkpoint_frequency: int = 10  # entities per checkpoint
    checkpoint_retention_days: int = 30  # days to keep checkpoints

    # Logging settings
    enable_structured_logging: bool = True
    log_error_details: bool = True
    log_performance_metrics: bool = True

    # Recovery settings
    enable_auto_resume: bool = True
    enable_rollback: bool = True
    max_rollback_attempts: int = 3

    # Performance settings
    batch_size: int = 50
    concurrent_operations: int = 5
    timeout_seconds: int = 30

    # Database settings
    db_connection_timeout: int = 10
    db_max_connections: int = 20
    db_connection_retries: int = 3


# Default configuration
DEFAULT_ERROR_RECOVERY_CONFIG = ErrorRecoveryConfig()


def load_error_recovery_config(
    config_path: str | None = None,
) -> ErrorRecoveryConfig:
    """Load error recovery configuration from file or use defaults."""
    if not config_path:
        return DEFAULT_ERROR_RECOVERY_CONFIG

    # TODO: Implement configuration file loading
    # For now, return default configuration
    return DEFAULT_ERROR_RECOVERY_CONFIG
