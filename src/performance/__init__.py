"""Performance optimization package for migration operations.

This package provides comprehensive performance optimization features including:
- Batch processing for efficient API calls
- Rate limiting with adaptive throttling
- Retry mechanisms with exponential backoff
- Progress tracking and reporting
- Memory and throughput optimization
"""

from .migration_performance_manager import (
    MigrationPerformanceManager,
    PerformanceConfig,
    MigrationMetrics,
    process_migration_with_optimization,
    process_json_files_optimized
)

__all__ = [
    'MigrationPerformanceManager',
    'PerformanceConfig', 
    'MigrationMetrics',
    'process_migration_with_optimization',
    'process_json_files_optimized'
] 