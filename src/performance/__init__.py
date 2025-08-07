"""Performance optimization package for migration operations.

This package provides comprehensive performance optimization features including:
- Batch processing for efficient API calls
- Rate limiting with adaptive throttling
- Retry mechanisms with exponential backoff
- Progress tracking and reporting
- Memory and throughput optimization
"""

from .migration_performance_manager import (
    MigrationMetrics,
    MigrationPerformanceManager,
    PerformanceConfig,
    process_json_files_optimized,
    process_migration_with_optimization,
)

__all__ = [
    "MigrationMetrics",
    "MigrationPerformanceManager",
    "PerformanceConfig",
    "process_json_files_optimized",
    "process_migration_with_optimization",
]
