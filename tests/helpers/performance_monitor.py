"""Test performance monitoring system for tracking runtime metrics."""

import time
import json
import statistics
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict
from datetime import datetime


@dataclass
class TestMetrics:
    """Metrics for a single test."""
    test_name: str
    duration: float
    status: str  # 'passed', 'failed', 'skipped'
    timestamp: float
    memory_usage: Optional[float] = None
    cpu_usage: Optional[float] = None


@dataclass
class TestSuiteMetrics:
    """Metrics for a test suite."""
    suite_name: str
    total_tests: int
    passed_tests: int
    failed_tests: int
    skipped_tests: int
    total_duration: float
    average_duration: float
    slowest_test: str
    slowest_duration: float
    fastest_test: str
    fastest_duration: float
    timestamp: float


class TestPerformanceMonitor:
    """Monitor and track test performance metrics."""
    
    def __init__(self, output_dir: Path = None):
        self.output_dir = output_dir or Path("var/test_metrics")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.metrics: List[TestMetrics] = []
        self.start_time = time.time()
    
    def record_test(self, test_name: str, duration: float, status: str, **kwargs):
        """Record metrics for a single test."""
        metric = TestMetrics(
            test_name=test_name,
            duration=duration,
            status=status,
            timestamp=time.time(),
            **kwargs
        )
        self.metrics.append(metric)
    
    def generate_suite_report(self, suite_name: str) -> TestSuiteMetrics:
        """Generate a report for the test suite."""
        if not self.metrics:
            return TestSuiteMetrics(
                suite_name=suite_name,
                total_tests=0,
                passed_tests=0,
                failed_tests=0,
                skipped_tests=0,
                total_duration=0.0,
                average_duration=0.0,
                slowest_test="",
                slowest_duration=0.0,
                fastest_test="",
                fastest_duration=0.0,
                timestamp=time.time()
            )
        
        passed = len([m for m in self.metrics if m.status == 'passed'])
        failed = len([m for m in self.metrics if m.status == 'failed'])
        skipped = len([m for m in self.metrics if m.status == 'skipped'])
        total_duration = sum(m.duration for m in self.metrics)
        average_duration = total_duration / len(self.metrics)
        
        slowest = max(self.metrics, key=lambda m: m.duration)
        fastest = min(self.metrics, key=lambda m: m.duration)
        
        return TestSuiteMetrics(
            suite_name=suite_name,
            total_tests=len(self.metrics),
            passed_tests=passed,
            failed_tests=failed,
            skipped_tests=skipped,
            total_duration=total_duration,
            average_duration=average_duration,
            slowest_test=slowest.test_name,
            slowest_duration=slowest.duration,
            fastest_test=fastest.test_name,
            fastest_duration=fastest.duration,
            timestamp=time.time()
        )
    
    def save_report(self, suite_name: str):
        """Save the performance report to disk."""
        suite_metrics = self.generate_suite_report(suite_name)
        
        report = {
            'suite_metrics': asdict(suite_metrics),
            'individual_tests': [asdict(m) for m in self.metrics],
            'summary': {
                'total_runtime': time.time() - self.start_time,
                'tests_per_second': len(self.metrics) / (time.time() - self.start_time) if self.metrics else 0,
                'generated_at': datetime.now().isoformat()
            }
        }
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_file = self.output_dir / f"test_performance_{suite_name}_{timestamp}.json"
        
        with open(report_file, 'w') as f:
            json.dump(report, f, indent=2)
        
        return report_file
    
    def get_slow_tests(self, threshold: float = 1.0) -> List[TestMetrics]:
        """Get tests that take longer than the threshold."""
        return [m for m in self.metrics if m.duration > threshold]
    
    def get_fast_tests(self, threshold: float = 0.1) -> List[TestMetrics]:
        """Get tests that take less than the threshold."""
        return [m for m in self.metrics if m.duration < threshold]


# Global monitor instance
_global_monitor = TestPerformanceMonitor()


def get_test_monitor() -> TestPerformanceMonitor:
    """Get the global test performance monitor."""
    return _global_monitor


def record_test_metrics(test_name: str, duration: float, status: str, **kwargs):
    """Record test metrics using the global monitor."""
    _global_monitor.record_test(test_name, duration, status, **kwargs) 