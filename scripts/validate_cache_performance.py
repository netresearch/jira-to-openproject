#!/usr/bin/env python3
"""Cache Performance Validation Script for j2o-50.

This script validates the idempotent workflow caching system by:
1. Running migrations with caching enabled
2. Collecting cache performance metrics
3. Validating cache hit rates meet success criteria
4. Generating performance report

Success Criteria (from j2o-50-testing-strategy.md):
- Cache hit rate > 50% for second run
- API call reduction 30-50%
- Memory usage stays under MAX_TOTAL_CACHE_SIZE
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src import config
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.display import configure_logging
from src.migrations.base_migration import BaseMigration
from src.migrations.issue_type_migration import IssueTypeMigration
from src.migrations.priority_migration import PriorityMigration
from src.migrations.status_migration import StatusMigration

logger = configure_logging("INFO", None)


class CachePerformanceValidator:
    """Validates cache performance for idempotent workflow."""

    def __init__(self) -> None:
        """Initialize validator."""
        self.results: dict[str, Any] = {
            "test_run_timestamp": datetime.now(UTC).isoformat(),
            "migrations_tested": [],
            "overall_metrics": {},
            "validation_status": "pending",
            "success_criteria": {
                "cache_hit_rate_threshold": 0.50,  # 50%
                "api_reduction_min": 0.30,  # 30%
                "api_reduction_max": 0.50,  # 50%
                "memory_limit": BaseMigration.MAX_TOTAL_CACHE_SIZE,
            },
        }
        self.jira_client = None
        self.op_client = None

    def setup_clients(self) -> bool:
        """Set up Jira and OpenProject clients.

        Returns:
            True if clients initialized successfully

        """
        try:
            self.jira_client = JiraClient()
            self.op_client = OpenProjectClient()
            logger.info("✓ Clients initialized successfully")
            return True
        except Exception as e:
            logger.error("✗ Failed to initialize clients: %s", e)
            self.results["validation_status"] = "client_init_failed"
            self.results["error"] = str(e)
            return False

    def run_migration_with_cache_tracking(
        self,
        migration_class: type[BaseMigration],
        entity_type: str,
    ) -> dict[str, Any]:
        """Run a migration twice and collect cache metrics.

        Args:
            migration_class: Migration class to test
            entity_type: Entity type for the migration

        Returns:
            Cache performance metrics

        """
        migration_name = migration_class.__name__
        logger.info("\n" + "=" * 60)
        logger.info("Testing: %s (entity_type: %s)", migration_name, entity_type)
        logger.info("=" * 60)

        metrics = {
            "migration_name": migration_name,
            "entity_type": entity_type,
            "run1_api_calls": 0,
            "run2_api_calls": 0,
            "run2_cache_hits": 0,
            "run2_cache_misses": 0,
            "run2_cache_hit_rate": 0.0,
            "api_reduction_percentage": 0.0,
            "memory_usage": 0,
            "cache_evictions": 0,
            "status": "pending",
        }

        try:
            # Run 1: Fresh migration (no cache)
            logger.info("\nRun 1: Fresh migration (establishing baseline)")
            migration1 = migration_class(
                jira_client=self.jira_client,
                op_client=self.op_client,
            )

            result1 = migration1.run_with_change_detection(entity_type=entity_type)

            if not result1.success:
                logger.warning("Run 1 completed with warnings")

            # Extract cache stats from run 1
            cache_stats1 = result1.details.get("cache_stats", {})
            metrics["run1_api_calls"] = cache_stats1.get("cache_misses", 0)

            logger.info("Run 1 complete:")
            logger.info("  - API calls: %d", metrics["run1_api_calls"])
            logger.info("  - Cache misses: %d", cache_stats1.get("cache_misses", 0))

            # Run 2: With cache (should hit cache)
            logger.info("\nRun 2: With cache (testing cache effectiveness)")
            migration2 = migration_class(
                jira_client=self.jira_client,
                op_client=self.op_client,
            )

            result2 = migration2.run_with_change_detection(entity_type=entity_type)

            # Extract cache stats from run 2
            cache_stats2 = result2.details.get("cache_stats", {})
            metrics["run2_cache_hits"] = cache_stats2.get("cache_hits", 0)
            metrics["run2_cache_misses"] = cache_stats2.get("cache_misses", 0)
            metrics["run2_api_calls"] = metrics["run2_cache_misses"]
            metrics["cache_evictions"] = cache_stats2.get("cache_evictions", 0)
            metrics["memory_usage"] = cache_stats2.get("total_cache_size", 0)

            # Calculate hit rate
            total_requests = metrics["run2_cache_hits"] + metrics["run2_cache_misses"]
            if total_requests > 0:
                metrics["run2_cache_hit_rate"] = metrics["run2_cache_hits"] / total_requests
            else:
                metrics["run2_cache_hit_rate"] = 0.0

            # Calculate API reduction
            if metrics["run1_api_calls"] > 0:
                api_reduction = (metrics["run1_api_calls"] - metrics["run2_api_calls"]) / metrics["run1_api_calls"]
                metrics["api_reduction_percentage"] = api_reduction
            else:
                metrics["api_reduction_percentage"] = 0.0

            logger.info("\nRun 2 complete:")
            logger.info("  - API calls: %d", metrics["run2_api_calls"])
            logger.info("  - Cache hits: %d", metrics["run2_cache_hits"])
            logger.info("  - Cache misses: %d", metrics["run2_cache_misses"])
            logger.info("  - Cache hit rate: %.1f%%", metrics["run2_cache_hit_rate"] * 100)
            logger.info("  - API reduction: %.1f%%", metrics["api_reduction_percentage"] * 100)
            logger.info("  - Memory usage: %d entities", metrics["memory_usage"])

            # Validate against success criteria
            criteria = self.results["success_criteria"]
            passed_checks = []
            failed_checks = []

            # Check 1: Cache hit rate
            if metrics["run2_cache_hit_rate"] >= criteria["cache_hit_rate_threshold"]:
                passed_checks.append("cache_hit_rate")
                logger.info("  ✓ Cache hit rate meets threshold (%.1f%% >= %.1f%%)",
                          metrics["run2_cache_hit_rate"] * 100,
                          criteria["cache_hit_rate_threshold"] * 100)
            else:
                failed_checks.append("cache_hit_rate")
                logger.warning("  ✗ Cache hit rate below threshold (%.1f%% < %.1f%%)",
                             metrics["run2_cache_hit_rate"] * 100,
                             criteria["cache_hit_rate_threshold"] * 100)

            # Check 2: API reduction
            if criteria["api_reduction_min"] <= metrics["api_reduction_percentage"] <= criteria["api_reduction_max"]:
                passed_checks.append("api_reduction")
                logger.info("  ✓ API reduction within target range (%.1f%%)",
                          metrics["api_reduction_percentage"] * 100)
            else:
                failed_checks.append("api_reduction")
                logger.warning("  ✗ API reduction outside target range (%.1f%% not in %.1f%%-%.1f%%)",
                             metrics["api_reduction_percentage"] * 100,
                             criteria["api_reduction_min"] * 100,
                             criteria["api_reduction_max"] * 100)

            # Check 3: Memory usage
            if metrics["memory_usage"] <= criteria["memory_limit"]:
                passed_checks.append("memory_usage")
                logger.info("  ✓ Memory usage within limits (%d <= %d)",
                          metrics["memory_usage"],
                          criteria["memory_limit"])
            else:
                failed_checks.append("memory_usage")
                logger.warning("  ✗ Memory usage exceeds limits (%d > %d)",
                             metrics["memory_usage"],
                             criteria["memory_limit"])

            metrics["passed_checks"] = passed_checks
            metrics["failed_checks"] = failed_checks
            metrics["status"] = "passed" if not failed_checks else "failed"

        except Exception as e:
            logger.exception("Error testing %s: %s", migration_name, e)
            metrics["status"] = "error"
            metrics["error"] = str(e)

        return metrics

    def run_validation(self) -> bool:
        """Run cache performance validation.

        Returns:
            True if all validations passed

        """
        logger.info("\n" + "=" * 70)
        logger.info("Cache Performance Validation - j2o-50 Testing")
        logger.info("=" * 70)

        if not self.setup_clients():
            return False

        # Test Tier 1A migrations
        migrations_to_test = [
            (PriorityMigration, "priorities"),
            (StatusMigration, "statuses"),
            (IssueTypeMigration, "issue_types"),
        ]

        all_passed = True
        total_api_calls_run1 = 0
        total_api_calls_run2 = 0
        total_cache_hits = 0
        total_cache_misses = 0

        for migration_class, entity_type in migrations_to_test:
            metrics = self.run_migration_with_cache_tracking(migration_class, entity_type)
            self.results["migrations_tested"].append(metrics)

            if metrics["status"] != "passed":
                all_passed = False

            # Aggregate metrics
            total_api_calls_run1 += metrics["run1_api_calls"]
            total_api_calls_run2 += metrics["run2_api_calls"]
            total_cache_hits += metrics["run2_cache_hits"]
            total_cache_misses += metrics["run2_cache_misses"]

        # Calculate overall metrics
        total_requests = total_cache_hits + total_cache_misses
        overall_hit_rate = total_cache_hits / total_requests if total_requests > 0 else 0.0
        overall_api_reduction = ((total_api_calls_run1 - total_api_calls_run2) / total_api_calls_run1
                                if total_api_calls_run1 > 0 else 0.0)

        self.results["overall_metrics"] = {
            "total_migrations_tested": len(migrations_to_test),
            "migrations_passed": sum(1 for m in self.results["migrations_tested"] if m["status"] == "passed"),
            "migrations_failed": sum(1 for m in self.results["migrations_tested"] if m["status"] == "failed"),
            "total_api_calls_run1": total_api_calls_run1,
            "total_api_calls_run2": total_api_calls_run2,
            "total_cache_hits": total_cache_hits,
            "total_cache_misses": total_cache_misses,
            "overall_cache_hit_rate": overall_hit_rate,
            "overall_api_reduction": overall_api_reduction,
        }

        self.results["validation_status"] = "passed" if all_passed else "failed"

        return all_passed

    def generate_report(self, output_path: Path) -> None:
        """Generate validation report.

        Args:
            output_path: Path to save report

        """
        logger.info("\n" + "=" * 70)
        logger.info("Validation Report")
        logger.info("=" * 70)

        overall = self.results["overall_metrics"]
        logger.info("\nOverall Results:")
        logger.info("  Status: %s", self.results["validation_status"].upper())
        logger.info("  Migrations tested: %d", overall["total_migrations_tested"])
        logger.info("  Passed: %d", overall["migrations_passed"])
        logger.info("  Failed: %d", overall["migrations_failed"])
        logger.info("\nAggregate Cache Performance:")
        logger.info("  Overall cache hit rate: %.1f%%", overall["overall_cache_hit_rate"] * 100)
        logger.info("  Overall API reduction: %.1f%%", overall["overall_api_reduction"] * 100)
        logger.info("  Run 1 API calls: %d", overall["total_api_calls_run1"])
        logger.info("  Run 2 API calls: %d", overall["total_api_calls_run2"])
        logger.info("  Total cache hits: %d", overall["total_cache_hits"])
        logger.info("  Total cache misses: %d", overall["total_cache_misses"])

        # Save JSON report
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w") as f:
            json.dump(self.results, f, indent=2)

        logger.info("\n✓ Report saved to: %s", output_path)


def main() -> int:
    """Main entry point.

    Returns:
        0 if validation passed, 1 otherwise

    """
    validator = CachePerformanceValidator()

    try:
        success = validator.run_validation()

        # Generate report
        output_dir = config.get_path("output")
        report_path = output_dir / "cache_performance_validation.json"
        validator.generate_report(report_path)

        if success:
            logger.info("\n" + "=" * 70)
            logger.info("✓ ALL VALIDATIONS PASSED")
            logger.info("=" * 70)
            return 0
        logger.warning("\n" + "=" * 70)
        logger.warning("✗ SOME VALIDATIONS FAILED")
        logger.warning("=" * 70)
        return 1

    except Exception as e:
        logger.exception("Validation script failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
