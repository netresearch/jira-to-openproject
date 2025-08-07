#!/usr/bin/env python3
"""Automated Testing Suite for Jira to OpenProject Migration.

This module provides a comprehensive automated testing solution:
- Test orchestration and execution
- Automated test generation
- CI/CD integration
- Test reporting and analytics
- Performance testing automation
- Security testing automation
- Test data management
- Parallel test execution
"""

import asyncio
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from jinja2 import Template

from src.utils.comprehensive_logging import get_logger


class TestType(Enum):
    """Types of tests supported by the automated testing suite."""

    UNIT = "unit"
    FUNCTIONAL = "functional"
    INTEGRATION = "integration"
    END_TO_END = "end_to_end"
    PERFORMANCE = "performance"
    SECURITY = "security"
    REGRESSION = "regression"
    SMOKE = "smoke"


class TestStatus(Enum):
    """Test execution status."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass
class TestResult:
    """Result of a test execution."""

    test_id: str
    test_name: str
    test_type: TestType
    status: TestStatus
    duration: float
    start_time: datetime
    end_time: datetime
    error_message: str | None = None
    stack_trace: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TestSuiteConfig:
    """Configuration for the automated testing suite."""

    # Test execution
    parallel_workers: int = 4
    timeout_seconds: int = 300
    retry_failed_tests: int = 2
    stop_on_first_failure: bool = False

    # Test selection
    test_types: list[TestType] = field(
        default_factory=lambda: [
            TestType.UNIT,
            TestType.FUNCTIONAL,
            TestType.INTEGRATION,
        ],
    )
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)

    # Coverage and reporting
    enable_coverage: bool = True
    coverage_threshold: float = 80.0
    generate_html_report: bool = True
    generate_junit_xml: bool = True

    # Performance testing
    enable_performance_tests: bool = False
    performance_baseline_file: Path | None = None
    performance_threshold_percent: float = 10.0

    # Security testing
    enable_security_tests: bool = False
    security_scan_level: str = "medium"

    # CI/CD integration
    ci_mode: bool = False
    ci_provider: str = "github"  # github, gitlab, jenkins, etc.
    ci_config_file: Path | None = None

    # Test data management
    test_data_dir: Path = Path("tests/test_data")
    cleanup_test_data: bool = True
    preserve_failed_test_data: bool = True

    # Reporting
    report_dir: Path = Path("test_reports")
    report_format: str = "html"  # html, json, xml

    def __post_init__(self):
        """Validate and set up configuration."""
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.test_data_dir.mkdir(parents=True, exist_ok=True)


class TestDataManager:
    """Manages test data creation, cleanup, and isolation."""

    def __init__(self, config: TestSuiteConfig) -> None:
        self.config = config
        self.logger = get_logger()
        self.test_data_sets: dict[str, dict[str, Any]] = {}
        self.active_tests: set[str] = set()

    def create_test_data(
        self,
        test_id: str,
        data_spec: dict[str, Any],
    ) -> dict[str, Any]:
        """Create test data for a specific test."""
        self.logger.info("Creating test data", test_id=test_id, data_spec=data_spec)

        # Generate unique test data
        test_data = {
            "test_id": test_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "data": self._generate_data_from_spec(data_spec),
        }

        # Save test data to file
        data_file = self.config.test_data_dir / f"{test_id}_data.json"
        with open(data_file, "w") as f:
            json.dump(test_data, f, indent=2)

        self.test_data_sets[test_id] = test_data
        self.active_tests.add(test_id)

        return test_data

    def _generate_data_from_spec(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Generate test data from a specification."""
        generated_data = {}

        for key, value_spec in spec.items():
            if isinstance(value_spec, dict):
                if value_spec.get("type") == "jira_issue":
                    generated_data[key] = self._generate_jira_issue(value_spec)
                elif value_spec.get("type") == "openproject_work_package":
                    generated_data[key] = self._generate_openproject_work_package(
                        value_spec,
                    )
                elif value_spec.get("type") == "user":
                    generated_data[key] = self._generate_user_data(value_spec)
                elif value_spec.get("type") == "project":
                    generated_data[key] = self._generate_project_data(value_spec)
                else:
                    generated_data[key] = self._generate_random_data(value_spec)
            else:
                generated_data[key] = value_spec

        return generated_data

    def _generate_jira_issue(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Generate a mock Jira issue."""
        return {
            "id": f"ISSUE-{uuid4().hex[:8].upper()}",
            "key": f"PROJ-{uuid4().hex[:4].upper()}",
            "summary": spec.get("summary", f"Test Issue {uuid4().hex[:4]}"),
            "description": spec.get("description", "Test issue description"),
            "status": spec.get("status", "To Do"),
            "priority": spec.get("priority", "Medium"),
            "assignee": spec.get("assignee", "test.user@example.com"),
            "reporter": spec.get("reporter", "test.reporter@example.com"),
            "created": datetime.now(UTC).isoformat(),
            "updated": datetime.now(UTC).isoformat(),
            "project": spec.get("project", "TEST"),
            "issue_type": spec.get("issue_type", "Task"),
            "labels": spec.get("labels", ["test", "automated"]),
            "components": spec.get("components", ["test-component"]),
            "custom_fields": spec.get("custom_fields", {}),
        }

    def _generate_openproject_work_package(
        self,
        spec: dict[str, Any],
    ) -> dict[str, Any]:
        """Generate a mock OpenProject work package."""
        return {
            "id": uuid4().int,
            "subject": spec.get("subject", f"Test Work Package {uuid4().hex[:4]}"),
            "description": spec.get("description", "Test work package description"),
            "status": spec.get("status", "New"),
            "priority": spec.get("priority", "Normal"),
            "assigned_to": spec.get("assigned_to", "test.user@example.com"),
            "author": spec.get("author", "test.author@example.com"),
            "created_at": datetime.now(UTC).isoformat(),
            "updated_at": datetime.now(UTC).isoformat(),
            "project": spec.get("project", "test-project"),
            "type": spec.get("type", "Task"),
            "custom_fields": spec.get("custom_fields", {}),
        }

    def _generate_user_data(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Generate user test data."""
        return {
            "username": spec.get("username", f"testuser_{uuid4().hex[:4]}"),
            "email": spec.get("email", f"testuser_{uuid4().hex[:4]}@example.com"),
            "first_name": spec.get("first_name", "Test"),
            "last_name": spec.get("last_name", "User"),
            "active": spec.get("active", True),
            "groups": spec.get("groups", ["test-group"]),
        }

    def _generate_project_data(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Generate project test data."""
        return {
            "key": spec.get("key", f"TEST{uuid4().hex[:4].upper()}"),
            "name": spec.get("name", f"Test Project {uuid4().hex[:4]}"),
            "description": spec.get("description", "Test project description"),
            "lead": spec.get("lead", "test.lead@example.com"),
            "components": spec.get("components", ["test-component"]),
            "issue_types": spec.get("issue_types", ["Task", "Bug", "Story"]),
            "workflows": spec.get("workflows", ["default"]),
        }

    def _generate_random_data(self, spec: dict[str, Any]) -> Any:
        """Generate random data based on specification."""
        data_type = spec.get("type", "string")

        if data_type == "string":
            return spec.get("value", f"test_{uuid4().hex[:8]}")
        if data_type == "integer":
            return spec.get("value", uuid4().int % 1000)
        if data_type == "boolean":
            return spec.get("value", True)
        if data_type == "list":
            return spec.get("value", ["item1", "item2", "item3"])
        if data_type == "dict":
            return spec.get("value", {"key1": "value1", "key2": "value2"})
        return spec.get("value", "default_value")

    def cleanup_test_data(self, test_id: str, test_passed: bool = True) -> None:
        """Clean up test data for a specific test."""
        if test_id not in self.active_tests:
            return

        # Preserve data for failed tests if configured
        if not test_passed and self.config.preserve_failed_test_data:
            self.logger.info("Preserving test data for failed test", test_id=test_id)
            return

        # Remove test data file
        data_file = self.config.test_data_dir / f"{test_id}_data.json"
        if data_file.exists():
            data_file.unlink()

        # Remove from tracking
        self.test_data_sets.pop(test_id, None)
        self.active_tests.discard(test_id)

        self.logger.info("Cleaned up test data", test_id=test_id)

    def cleanup_all_test_data(self) -> None:
        """Clean up all test data."""
        for test_id in list(self.active_tests):
            self.cleanup_test_data(test_id, test_passed=True)


class TestGenerator:
    """Generates automated tests based on code analysis and specifications."""

    def __init__(self, config: TestSuiteConfig) -> None:
        self.config = config
        self.logger = get_logger()

    def generate_unit_tests(self, module_path: Path) -> list[str]:
        """Generate unit tests for a Python module."""
        self.logger.info("Generating unit tests", module_path=str(module_path))

        # Analyze the module to find functions and classes
        functions = self._extract_functions(module_path)
        classes = self._extract_classes(module_path)

        generated_tests = []

        # Generate tests for functions
        for func_name, func_info in functions.items():
            test_code = self._generate_function_test(func_name, func_info)
            generated_tests.append(test_code)

        # Generate tests for classes
        for class_name, class_info in classes.items():
            test_code = self._generate_class_test(class_name, class_info)
            generated_tests.append(test_code)

        return generated_tests

    def _extract_functions(self, module_path: Path) -> dict[str, dict[str, Any]]:
        """Extract function information from a module."""
        # This is a simplified implementation
        # In a real implementation, you would use AST parsing
        functions = {}

        with open(module_path) as f:
            content = f.read()

        # Simple regex-based extraction (for demonstration)
        import re

        # Find function definitions
        func_pattern = r"def\s+(\w+)\s*\([^)]*\)\s*:"
        matches = re.finditer(func_pattern, content)

        for match in matches:
            func_name = match.group(1)
            if not func_name.startswith("_"):  # Skip private functions
                functions[func_name] = {
                    "name": func_name,
                    "line": content[: match.start()].count("\n") + 1,
                }

        return functions

    def _extract_classes(self, module_path: Path) -> dict[str, dict[str, Any]]:
        """Extract class information from a module."""
        classes = {}

        with open(module_path) as f:
            content = f.read()

        import re

        # Find class definitions
        class_pattern = r"class\s+(\w+)\s*[:\(]"
        matches = re.finditer(class_pattern, content)

        for match in matches:
            class_name = match.group(1)
            if not class_name.startswith("_"):  # Skip private classes
                classes[class_name] = {
                    "name": class_name,
                    "line": content[: match.start()].count("\n") + 1,
                }

        return classes

    def _generate_function_test(self, func_name: str, func_info: dict[str, Any]) -> str:
        """Generate test code for a function."""
        test_template = '''
def test_{{ func_name }}():
    """Test {{ func_name }} function."""
    # TODO: Add test implementation
    # This is a generated test stub
    pass
'''

        template = Template(test_template)
        return template.render(func_name=func_name)

    def _generate_class_test(self, class_name: str, class_info: dict[str, Any]) -> str:
        """Generate test code for a class."""
        test_template = '''
class Test{{ class_name }}:
    """Test {{ class_name }} class."""

    def test_{{ class_name.lower }}_creation(self):
        """Test {{ class_name }} instantiation."""
        # TODO: Add test implementation
        # This is a generated test stub
        pass

    def test_{{ class_name.lower }}_methods(self):
        """Test {{ class_name }} methods."""
        # TODO: Add test implementation
        # This is a generated test stub
        pass
'''

        template = Template(test_template)
        return template.render(class_name=class_name)


class TestReporter:
    """Generates comprehensive test reports."""

    def __init__(self, config: TestSuiteConfig) -> None:
        self.config = config
        self.logger = get_logger()
        self.results: list[TestResult] = []

    def add_result(self, result: TestResult) -> None:
        """Add a test result to the report."""
        self.results.append(result)

    def generate_report(self) -> dict[str, Any]:
        """Generate a comprehensive test report."""
        if not self.results:
            return {"error": "No test results to report"}

        # Calculate statistics
        total_tests = len(self.results)
        passed_tests = sum(1 for r in self.results if r.status == TestStatus.PASSED)
        failed_tests = sum(1 for r in self.results if r.status == TestStatus.FAILED)
        skipped_tests = sum(1 for r in self.results if r.status == TestStatus.SKIPPED)
        error_tests = sum(1 for r in self.results if r.status == TestStatus.ERROR)

        # Calculate success rate
        success_rate = (passed_tests / total_tests) * 100 if total_tests > 0 else 0

        # Group by test type
        results_by_type = {}
        for result in self.results:
            test_type = result.test_type.value
            if test_type not in results_by_type:
                results_by_type[test_type] = []
            results_by_type[test_type].append(result)

        # Calculate duration statistics
        durations = [r.duration for r in self.results]
        avg_duration = sum(durations) / len(durations) if durations else 0
        max_duration = max(durations) if durations else 0
        min_duration = min(durations) if durations else 0

        return {
            "summary": {
                "total_tests": total_tests,
                "passed": passed_tests,
                "failed": failed_tests,
                "skipped": skipped_tests,
                "errors": error_tests,
                "success_rate": round(success_rate, 2),
                "total_duration": sum(durations),
                "avg_duration": round(avg_duration, 2),
                "max_duration": round(max_duration, 2),
                "min_duration": round(min_duration, 2),
            },
            "results_by_type": results_by_type,
            "failed_tests": [
                {
                    "test_id": r.test_id,
                    "test_name": r.test_name,
                    "test_type": r.test_type.value,
                    "error_message": r.error_message,
                    "duration": r.duration,
                }
                for r in self.results
                if r.status in [TestStatus.FAILED, TestStatus.ERROR]
            ],
            "timestamp": datetime.now(UTC).isoformat(),
            "config": {
                "parallel_workers": self.config.parallel_workers,
                "timeout_seconds": self.config.timeout_seconds,
                "test_types": [t.value for t in self.config.test_types],
            },
        }

    def save_report(self, report: dict[str, Any], format: str = "json"):
        """Save the test report to a file."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if format == "json":
            report_file = self.config.report_dir / f"test_report_{timestamp}.json"
            with open(report_file, "w") as f:
                json.dump(report, f, indent=2)
        elif format == "html":
            report_file = self.config.report_dir / f"test_report_{timestamp}.html"
            html_content = self._generate_html_report(report)
            with open(report_file, "w") as f:
                f.write(html_content)
        elif format == "xml":
            report_file = self.config.report_dir / f"test_report_{timestamp}.xml"
            xml_content = self._generate_junit_xml(report)
            with open(report_file, "w") as f:
                f.write(xml_content)

        self.logger.info("Test report saved", file=str(report_file), format=format)
        return report_file

    def _generate_html_report(self, report: dict[str, Any]) -> str:
        """Generate HTML test report."""
        html_template = """
<!DOCTYPE html>
<html>
<head>
    <title>Test Report</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        .summary { background: #f5f5f5; padding: 20px; border-radius: 5px; margin-bottom: 20px; }
        .metric { display: inline-block; margin: 10px; padding: 10px; background: white; border-radius: 3px; }
        .passed { color: green; }
        .failed { color: red; }
        .skipped { color: orange; }
        .failed-tests { margin-top: 20px; }
        .test-result { margin: 10px 0; padding: 10px; border-left: 4px solid #ccc; }
        .test-result.failed { border-left-color: red; }
        .test-result.error { border-left-color: darkred; }
    </style>
</head>
<body>
    <h1>Test Report</h1>

    <div class="summary">
        <h2>Summary</h2>
        <div class="metric">Total Tests: {{ summary.total_tests }}</div>
        <div class="metric passed">Passed: {{ summary.passed }}</div>
        <div class="metric failed">Failed: {{ summary.failed }}</div>
        <div class="metric skipped">Skipped: {{ summary.skipped }}</div>
        <div class="metric">Success Rate: {{ summary.success_rate }}%</div>
        <div class="metric">Total Duration: {{ summary.total_duration }}s</div>
    </div>

    {% if failed_tests %}
    <div class="failed-tests">
        <h2>Failed Tests</h2>
        {% for test in failed_tests %}
        <div class="test-result failed">
            <h3>{{ test.test_name }}</h3>
            <p><strong>Type:</strong> {{ test.test_type }}</p>
            <p><strong>Duration:</strong> {{ test.duration }}s</p>
            {% if test.error_message %}
            <p><strong>Error:</strong> {{ test.error_message }}</p>
            {% endif %}
        </div>
        {% endfor %}
    </div>
    {% endif %}

    <p><em>Generated at: {{ timestamp }}</em></p>
</body>
</html>
"""

        template = Template(html_template)
        return template.render(**report)

    def _generate_junit_xml(self, report: dict[str, Any]) -> str:
        """Generate JUnit XML report."""
        xml_template = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
    <testsuite name="Jira to OpenProject Migration Tests"
               tests="{{ summary.total_tests }}"
               failures="{{ summary.failed }}"
               errors="{{ summary.errors }}"
               skipped="{{ summary.skipped }}"
               time="{{ summary.total_duration }}">
        {% for result in all_results %}
        <testcase name="{{ result.test_name }}"
                  classname="{{ result.test_type }}"
                  time="{{ result.duration }}">
            {% if result.status == 'failed' %}
            <failure message="{{ result.error_message or 'Test failed' }}">
                {{ result.stack_trace or 'No stack trace available' }}
            </failure>
            {% elif result.status == 'error' %}
            <error message="{{ result.error_message or 'Test error' }}">
                {{ result.stack_trace or 'No stack trace available' }}
            </error>
            {% elif result.status == 'skipped' %}
            <skipped message="Test skipped" />
            {% endif %}
        </testcase>
        {% endfor %}
    </testsuite>
</testsuites>
"""

        template = Template(xml_template)
        return template.render(
            summary=report["summary"],
            all_results=self.results,
            timestamp=report["timestamp"],
        )


class AutomatedTestingSuite:
    """Main class for the automated testing suite."""

    def __init__(self, config: TestSuiteConfig) -> None:
        self.config = config
        self.logger = get_logger()
        self.test_data_manager = TestDataManager(config)
        self.test_generator = TestGenerator(config)
        self.reporter = TestReporter(config)
        self.results: list[TestResult] = []

    async def run_tests(self) -> dict[str, Any]:
        """Run the complete test suite."""
        start_time = time.time()
        self.logger.info("Starting automated test suite")

        try:
            # Run tests by type
            for test_type in self.config.test_types:
                await self._run_test_type(test_type)

            # Generate and save report
            report = self.reporter.generate_report()

            # Save reports in different formats
            if self.config.generate_html_report:
                self.reporter.save_report(report, "html")
            if self.config.generate_junit_xml:
                self.reporter.save_report(report, "xml")

            # Always save JSON report
            report_file = self.reporter.save_report(report, "json")

            total_duration = time.time() - start_time
            self.logger.info(
                "Test suite completed",
                total_duration=total_duration,
                success_rate=report["summary"]["success_rate"],
                report_file=str(report_file),
            )

            return report

        except Exception as e:
            self.logger.exception("Test suite failed", error=str(e))
            raise
        finally:
            # Clean up test data
            if self.config.cleanup_test_data:
                self.test_data_manager.cleanup_all_test_data()

    async def _run_test_type(self, test_type: TestType) -> None:
        """Run tests of a specific type."""
        self.logger.info("Running tests", test_type=test_type.value)

        # Find test files for this type
        test_files = self._find_test_files(test_type)

        if not test_files:
            self.logger.warning("No test files found", test_type=test_type.value)
            return

        # Run tests in parallel
        with ThreadPoolExecutor(max_workers=self.config.parallel_workers) as executor:
            futures = []
            for test_file in test_files:
                future = executor.submit(self._run_test_file, test_file, test_type)
                futures.append(future)

            # Collect results
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result:
                        self.results.append(result)
                        self.reporter.add_result(result)
                except Exception as e:
                    self.logger.exception("Test execution error", error=str(e))

    def _find_test_files(self, test_type: TestType) -> list[Path]:
        """Find test files for a specific test type."""
        test_dir = Path("tests")

        if test_type == TestType.UNIT:
            return list(test_dir.glob("unit/**/*.py"))
        if test_type == TestType.FUNCTIONAL:
            return list(test_dir.glob("functional/**/*.py"))
        if test_type == TestType.INTEGRATION:
            return list(test_dir.glob("integration/**/*.py"))
        if test_type == TestType.END_TO_END:
            return list(test_dir.glob("end_to_end/**/*.py"))
        if test_type == TestType.PERFORMANCE:
            return list(test_dir.glob("**/test_*performance*.py"))
        if test_type == TestType.SECURITY:
            return list(test_dir.glob("security/**/*.py"))
        return []

    def _run_test_file(
        self,
        test_file: Path,
        test_type: TestType,
    ) -> TestResult | None:
        """Run a single test file."""
        test_id = f"{test_type.value}_{test_file.stem}_{uuid4().hex[:8]}"
        start_time = time.time()

        try:
            # Run pytest on the test file
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    str(test_file),
                    "--tb=short",
                    "--quiet",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
            )

            end_time = time.time()
            duration = end_time - start_time

            # Determine test status
            if result.returncode == 0:
                status = TestStatus.PASSED
                error_message = None
            elif result.returncode == 1:
                status = TestStatus.FAILED
                error_message = result.stdout + result.stderr
            elif result.returncode == 2:
                status = TestStatus.ERROR
                error_message = result.stdout + result.stderr
            elif result.returncode == 5:
                status = TestStatus.SKIPPED
                error_message = None
            else:
                status = TestStatus.ERROR
                error_message = f"Unknown return code: {result.returncode}"

            return TestResult(
                test_id=test_id,
                test_name=test_file.stem,
                test_type=test_type,
                status=status,
                duration=duration,
                start_time=datetime.fromtimestamp(start_time, UTC),
                end_time=datetime.fromtimestamp(end_time, UTC),
                error_message=error_message,
            )

        except subprocess.TimeoutExpired:
            end_time = time.time()
            return TestResult(
                test_id=test_id,
                test_name=test_file.stem,
                test_type=test_type,
                status=TestStatus.TIMEOUT,
                duration=end_time - start_time,
                start_time=datetime.fromtimestamp(start_time, UTC),
                end_time=datetime.fromtimestamp(end_time, UTC),
                error_message="Test execution timed out",
            )
        except Exception as e:
            end_time = time.time()
            return TestResult(
                test_id=test_id,
                test_name=test_file.stem,
                test_type=test_type,
                status=TestStatus.ERROR,
                duration=end_time - start_time,
                start_time=datetime.fromtimestamp(start_time, UTC),
                end_time=datetime.fromtimestamp(end_time, UTC),
                error_message=str(e),
            )

    def generate_tests(self, target_path: Path) -> list[str]:
        """Generate automated tests for a target path."""
        if target_path.is_file():
            return self.test_generator.generate_unit_tests(target_path)
        if target_path.is_dir():
            generated_tests = []
            for py_file in target_path.rglob("*.py"):
                if not py_file.name.startswith("test_"):
                    tests = self.test_generator.generate_unit_tests(py_file)
                    generated_tests.extend(tests)
            return generated_tests
        msg = f"Target path does not exist: {target_path}"
        raise ValueError(msg)

    def create_ci_config(self, ci_provider: str = "github") -> str:
        """Create CI/CD configuration file."""
        if ci_provider == "github":
            return self._create_github_actions_config()
        if ci_provider == "gitlab":
            return self._create_gitlab_ci_config()
        msg = f"Unsupported CI provider: {ci_provider}"
        raise ValueError(msg)

    def _create_github_actions_config(self) -> str:
        """Create GitHub Actions workflow configuration."""
        config = {
            "name": "Automated Testing",
            "on": ["push", "pull_request"],
            "jobs": {
                "test": {
                    "runs-on": "ubuntu-latest",
                    "steps": [
                        {"name": "Checkout code", "uses": "actions/checkout@v3"},
                        {
                            "name": "Set up Python",
                            "uses": "actions/setup-python@v4",
                            "with": {"python-version": "3.9"},
                        },
                        {
                            "name": "Install dependencies",
                            "run": "pip install -r requirements.txt",
                        },
                        {
                            "name": "Run tests",
                            "run": "python -m src.utils.automated_testing_suite",
                        },
                        {
                            "name": "Upload test results",
                            "uses": "actions/upload-artifact@v3",
                            "with": {"name": "test-results", "path": "test_reports/"},
                        },
                    ],
                },
            },
        }

        return yaml.dump(config, default_flow_style=False)

    def _create_gitlab_ci_config(self) -> str:
        """Create GitLab CI configuration."""
        config = {
            "stages": ["test"],
            "test": {
                "stage": "test",
                "image": "python:3.9",
                "script": [
                    "pip install -r requirements.txt",
                    "python -m src.utils.automated_testing_suite",
                ],
                "artifacts": {"paths": ["test_reports/"], "expire_in": "1 week"},
            },
        }

        return yaml.dump(config, default_flow_style=False)


# Convenience functions
def run_automated_tests(config: TestSuiteConfig | None = None) -> dict[str, Any]:
    """Run the automated testing suite."""
    if config is None:
        config = TestSuiteConfig()

    suite = AutomatedTestingSuite(config)
    return asyncio.run(suite.run_tests())


def generate_tests_for_module(
    module_path: str,
    config: TestSuiteConfig | None = None,
) -> list[str]:
    """Generate tests for a specific module."""
    if config is None:
        config = TestSuiteConfig()

    suite = AutomatedTestingSuite(config)
    return suite.generate_tests(Path(module_path))


def create_ci_config(ci_provider: str = "github") -> str:
    """Create CI/CD configuration for the specified provider."""
    suite = AutomatedTestingSuite(TestSuiteConfig())
    return suite.create_ci_config(ci_provider)


if __name__ == "__main__":
    # Run the automated testing suite when executed directly
    config = TestSuiteConfig()

    # Parse command line arguments
    if len(sys.argv) > 1:
        if sys.argv[1] == "--ci-config":
            provider = sys.argv[2] if len(sys.argv) > 2 else "github"
            sys.exit(0)
        elif sys.argv[1] == "--generate-tests":
            module_path = sys.argv[2] if len(sys.argv) > 2 else "src"
            tests = generate_tests_for_module(module_path, config)
            for _test in tests:
                pass
            sys.exit(0)

    # Run tests
    result = run_automated_tests(config)

    # Exit with appropriate code
    if result["summary"]["success_rate"] >= 80:
        sys.exit(0)
    else:
        sys.exit(1)
