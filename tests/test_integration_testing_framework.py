#!/usr/bin/env python3
"""Tests for the Integration Testing Framework."""

import asyncio
import json
import tempfile
import time
from datetime import datetime, UTC
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.utils.integration_testing_framework import (
    IntegrationTestingFramework,
    PerformanceMonitor,
    TestConfig,
    TestData,
    TestDataGenerator,
    TestEnvironment,
    TestEnvironmentManager,
    TestReporter,
    TestReport,
    TestResult,
    TestScope,
    TestSuite,
    create_basic_test_suite,
    run_integration_tests,
)


class TestTestConfig:
    """Test TestConfig dataclass."""

    def test_default_config(self):
        """Test default configuration values."""
        config = TestConfig()
        
        assert config.environment == TestEnvironment.MOCK
        assert config.scope == TestScope.INTEGRATION
        assert config.timeout_seconds == 300
        assert config.max_retries == 3
        assert config.parallel_workers == 4
        assert config.cleanup_on_exit is True
        assert config.generate_reports is True
        assert config.performance_threshold_ms == 5000
        assert config.data_volume == "medium"
        assert config.enable_mocking is True
        assert config.enable_docker is False
        assert config.enable_real_apis is False

    def test_custom_config(self):
        """Test custom configuration values."""
        config = TestConfig(
            environment=TestEnvironment.DOCKER,
            scope=TestScope.END_TO_END,
            timeout_seconds=600,
            max_retries=5,
            parallel_workers=8,
            cleanup_on_exit=False,
            generate_reports=False,
            performance_threshold_ms=10000,
            data_volume="large",
            enable_mocking=False,
            enable_docker=True,
            enable_real_apis=True
        )
        
        assert config.environment == TestEnvironment.DOCKER
        assert config.scope == TestScope.END_TO_END
        assert config.timeout_seconds == 600
        assert config.max_retries == 5
        assert config.parallel_workers == 8
        assert config.cleanup_on_exit is False
        assert config.generate_reports is False
        assert config.performance_threshold_ms == 10000
        assert config.data_volume == "large"
        assert config.enable_mocking is False
        assert config.enable_docker is True
        assert config.enable_real_apis is True


class TestTestData:
    """Test TestData dataclass."""

    def test_default_data(self):
        """Test default test data values."""
        data = TestData()
        
        assert data.jira_projects == 2
        assert data.jira_issues_per_project == 50
        assert data.jira_users == 10
        assert data.jira_attachments == 20
        assert data.jira_comments == 100
        assert data.op_projects == 2
        assert data.op_work_packages == 50
        assert data.op_users == 10
        assert data.custom_fields == 5
        assert data.workflows == 3

    def test_custom_data(self):
        """Test custom test data values."""
        data = TestData(
            jira_projects=5,
            jira_issues_per_project=100,
            jira_users=20,
            jira_attachments=50,
            jira_comments=200,
            op_projects=5,
            op_work_packages=100,
            op_users=20,
            custom_fields=10,
            workflows=5
        )
        
        assert data.jira_projects == 5
        assert data.jira_issues_per_project == 100
        assert data.jira_users == 20
        assert data.jira_attachments == 50
        assert data.jira_comments == 200
        assert data.op_projects == 5
        assert data.op_work_packages == 100
        assert data.op_users == 20
        assert data.custom_fields == 10
        assert data.workflows == 5


class TestTestReport:
    """Test TestReport dataclass."""

    def test_test_report_creation(self):
        """Test creating a test report."""
        start_time = datetime.now(UTC)
        report = TestReport(
            test_id="test-123",
            test_name="test_migration",
            scope=TestScope.INTEGRATION,
            environment=TestEnvironment.MOCK,
            start_time=start_time
        )
        
        assert report.test_id == "test-123"
        assert report.test_name == "test_migration"
        assert report.scope == TestScope.INTEGRATION
        assert report.environment == TestEnvironment.MOCK
        assert report.start_time == start_time
        assert report.end_time is None
        assert report.duration_ms is None
        assert report.result == TestResult.PASSED
        assert report.error_message is None
        assert report.performance_metrics == {}
        assert report.data_metrics == {}
        assert report.logs == []

    def test_test_report_with_results(self):
        """Test creating a test report with results."""
        start_time = datetime.now(UTC)
        end_time = datetime.now(UTC)
        
        report = TestReport(
            test_id="test-123",
            test_name="test_migration",
            scope=TestScope.INTEGRATION,
            environment=TestEnvironment.MOCK,
            start_time=start_time,
            end_time=end_time,
            duration_ms=1500,
            result=TestResult.FAILED,
            error_message="Test failed due to timeout",
            performance_metrics={"response_time": 1500},
            data_metrics={"items_processed": 100},
            logs=["Starting test", "Test completed"]
        )
        
        assert report.test_id == "test-123"
        assert report.test_name == "test_migration"
        assert report.end_time == end_time
        assert report.duration_ms == 1500
        assert report.result == TestResult.FAILED
        assert report.error_message == "Test failed due to timeout"
        assert report.performance_metrics == {"response_time": 1500}
        assert report.data_metrics == {"items_processed": 100}
        assert report.logs == ["Starting test", "Test completed"]


class TestPerformanceMonitor:
    """Test PerformanceMonitor class."""

    def test_performance_monitor_creation(self):
        """Test creating a performance monitor."""
        monitor = PerformanceMonitor()
        
        assert monitor.metrics == {}
        assert monitor.start_times == {}

    def test_timer_start_stop(self):
        """Test starting and stopping a timer."""
        monitor = PerformanceMonitor()
        
        # Start timer
        monitor.start_timer("test_timer")
        assert "test_timer" in monitor.start_times
        
        # Small delay
        time.sleep(0.1)
        
        # Stop timer
        duration = monitor.stop_timer("test_timer")
        assert duration > 0
        assert "test_timer" not in monitor.start_times
        assert "test_timer" in monitor.metrics
        assert len(monitor.metrics["test_timer"]) == 1

    def test_timer_stop_without_start(self):
        """Test stopping a timer that wasn't started."""
        monitor = PerformanceMonitor()
        
        with pytest.raises(ValueError, match="Timer 'test_timer' was not started"):
            monitor.stop_timer("test_timer")

    def test_multiple_timers(self):
        """Test multiple timers."""
        monitor = PerformanceMonitor()
        
        # Start multiple timers
        monitor.start_timer("timer1")
        monitor.start_timer("timer2")
        
        time.sleep(0.1)
        monitor.stop_timer("timer1")
        
        time.sleep(0.1)
        monitor.stop_timer("timer2")
        
        assert "timer1" in monitor.metrics
        assert "timer2" in monitor.metrics
        assert len(monitor.metrics["timer1"]) == 1
        assert len(monitor.metrics["timer2"]) == 1

    def test_get_metrics(self):
        """Test getting performance metrics."""
        monitor = PerformanceMonitor()
        
        # Add some test data
        monitor.metrics["test_timer"] = [1.0, 2.0, 3.0]
        
        metrics = monitor.get_metrics()
        
        assert "test_timer" in metrics
        assert metrics["test_timer"]["count"] == 3
        assert metrics["test_timer"]["total_ms"] == 6000
        assert metrics["test_timer"]["avg_ms"] == 2000
        assert metrics["test_timer"]["min_ms"] == 1000
        assert metrics["test_timer"]["max_ms"] == 3000
        assert metrics["test_timer"]["p95_ms"] == 3000

    def test_check_thresholds(self):
        """Test checking performance thresholds."""
        monitor = PerformanceMonitor()
        
        # Add test data
        monitor.metrics["fast_timer"] = [0.1, 0.2, 0.3]  # 100-300ms
        monitor.metrics["slow_timer"] = [1.0, 2.0, 3.0]  # 1000-3000ms
        
        thresholds = {
            "fast_timer": 500,  # 500ms threshold
            "slow_timer": 1000,  # 1000ms threshold
            "missing_timer": 100  # Missing timer
        }
        
        results = monitor.check_thresholds(thresholds)
        
        assert results["fast_timer"] is True  # avg 200ms < 500ms
        assert results["slow_timer"] is False  # avg 2000ms > 1000ms
        assert results["missing_timer"] is False  # timer doesn't exist


class TestTestDataGenerator:
    """Test TestDataGenerator class."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            yield Path(temp_dir)

    @pytest.fixture
    def generator(self, temp_dir):
        """Create a test data generator."""
        config = TestData(
            jira_projects=2,
            jira_issues_per_project=5,
            jira_users=3,
            op_projects=2,
            op_work_packages=5,
            op_users=3,
            custom_fields=2,
            workflows=1
        )
        return TestDataGenerator(config, temp_dir)

    @pytest.mark.asyncio
    async def test_generate_all_data(self, generator):
        """Test generating all test data."""
        data = await generator.generate_all_data()
        
        assert "jira" in data
        assert "openproject" in data
        assert "migration_config" in data
        assert "metadata" in data
        
        # Check Jira data
        jira_data = data["jira"]
        assert len(jira_data["projects"]) == 2
        assert len(jira_data["issues"]) == 10  # 2 projects * 5 issues
        assert len(jira_data["users"]) == 3
        assert len(jira_data["custom_fields"]) == 2
        assert len(jira_data["workflows"]) == 1
        
        # Check OpenProject data
        op_data = data["openproject"]
        assert len(op_data["projects"]) == 2
        assert len(op_data["work_packages"]) == 10  # 2 projects * 5 work packages
        assert len(op_data["users"]) == 3
        
        # Check migration config
        migration_config = data["migration_config"]
        assert "jira" in migration_config
        assert "openproject" in migration_config
        assert "migration" in migration_config
        assert "validation" in migration_config

    @pytest.mark.asyncio
    async def test_generate_jira_data(self, generator):
        """Test generating Jira test data."""
        jira_data = await generator._generate_jira_data()
        
        assert "projects" in jira_data
        assert "issues" in jira_data
        assert "users" in jira_data
        assert "custom_fields" in jira_data
        assert "workflows" in jira_data
        
        # Check projects
        projects = jira_data["projects"]
        assert len(projects) == 2
        assert projects[0]["key"] == "TEST1"
        assert projects[1]["key"] == "TEST2"
        
        # Check issues
        issues = jira_data["issues"]
        assert len(issues) == 10  # 2 projects * 5 issues
        assert issues[0]["key"] == "TEST1-1"
        assert issues[5]["key"] == "TEST2-1"
        
        # Check users
        users = jira_data["users"]
        assert len(users) == 3
        assert users[0]["name"] == "user1"
        assert users[1]["name"] == "user2"
        assert users[2]["name"] == "user3"

    @pytest.mark.asyncio
    async def test_generate_openproject_data(self, generator):
        """Test generating OpenProject test data."""
        op_data = await generator._generate_openproject_data()
        
        assert "projects" in op_data
        assert "work_packages" in op_data
        assert "users" in op_data
        
        # Check projects
        projects = op_data["projects"]
        assert len(projects) == 2
        assert projects[0]["identifier"] == "test1"
        assert projects[1]["identifier"] == "test2"
        
        # Check work packages
        work_packages = op_data["work_packages"]
        assert len(work_packages) == 10  # 2 projects * 5 work packages
        
        # Check users
        users = op_data["users"]
        assert len(users) == 3
        assert users[0]["login"] == "user1"
        assert users[1]["login"] == "user2"
        assert users[2]["login"] == "user3"
        assert users[0]["admin"] is True  # First user is admin

    def test_generate_custom_fields(self, generator):
        """Test generating custom fields."""
        fields = generator._generate_custom_fields()
        
        assert len(fields) == 2
        
        # Check field types
        field_types = [field["type"] for field in fields]
        assert "text" in field_types
        assert "number" in field_types
        
        # Check field IDs
        assert fields[0]["id"] == "customfield_10000"
        assert fields[1]["id"] == "customfield_10001"

    def test_generate_workflows(self, generator):
        """Test generating workflows."""
        workflows = generator._generate_workflows()
        
        assert len(workflows) == 1
        
        workflow = workflows[0]
        assert workflow["id"] == 1
        assert workflow["name"] == "Workflow 1"
        assert len(workflow["steps"]) == 4
        assert len(workflow["transitions"]) == 3

    @pytest.mark.asyncio
    async def test_generate_migration_config(self, generator):
        """Test generating migration configuration."""
        config = await generator._generate_migration_config()
        
        assert "jira" in config
        assert "openproject" in config
        assert "migration" in config
        assert "validation" in config
        
        # Check Jira config
        jira_config = config["jira"]
        assert jira_config["url"] == "https://jira-test.example.com"
        assert jira_config["username"] == "test_user"
        assert jira_config["project_key"] == "TEST1"
        
        # Check OpenProject config
        op_config = config["openproject"]
        assert op_config["url"] == "https://openproject-test.example.com"
        assert op_config["api_token"] == "test_token"
        assert op_config["project_identifier"] == "test1"
        
        # Check migration config
        migration_config = config["migration"]
        assert migration_config["include_attachments"] is True
        assert migration_config["include_comments"] is True
        assert migration_config["map_users"] is True
        assert migration_config["batch_size"] == 100
        assert migration_config["max_concurrent"] == 4

    @pytest.mark.asyncio
    async def test_save_data(self, generator, temp_dir):
        """Test saving test data to files."""
        data = {
            "jira": {"projects": [], "issues": []},
            "openproject": {"projects": [], "work_packages": []},
            "migration_config": {"jira": {}, "openproject": {}},
            "metadata": {"generated_at": "2024-01-01T00:00:00Z"}
        }
        
        await generator._save_data(data)
        
        # Check that files were created
        assert (generator.data_dir / "jira_data.json").exists()
        assert (generator.data_dir / "openproject_data.json").exists()
        assert (generator.data_dir / "migration_config.json").exists()
        assert (generator.data_dir / "metadata.json").exists()
        
        # Check file contents
        with open(generator.data_dir / "jira_data.json") as f:
            jira_data = json.load(f)
            assert "projects" in jira_data
            assert "issues" in jira_data


class TestTestReporter:
    """Test TestReporter class."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            yield Path(temp_dir)

    @pytest.fixture
    def reporter(self, temp_dir):
        """Create a test reporter."""
        return TestReporter(temp_dir)

    def test_reporter_creation(self, reporter, temp_dir):
        """Test creating a test reporter."""
        assert reporter.output_dir == temp_dir
        assert reporter.reports == []
        assert temp_dir.exists()

    def test_add_report(self, reporter):
        """Test adding a test report."""
        report = TestReport(
            test_id="test-123",
            test_name="test_migration",
            scope=TestScope.INTEGRATION,
            environment=TestEnvironment.MOCK,
            start_time=datetime.now(UTC)
        )
        
        reporter.add_report(report)
        assert len(reporter.reports) == 1
        assert reporter.reports[0] == report

    def test_generate_reports(self, reporter):
        """Test generating all reports."""
        # Add some test reports
        report1 = TestReport(
            test_id="test-1",
            test_name="test_1",
            scope=TestScope.INTEGRATION,
            environment=TestEnvironment.MOCK,
            start_time=datetime.now(UTC),
            end_time=datetime.now(UTC),
            duration_ms=1000,
            result=TestResult.PASSED
        )
        
        report2 = TestReport(
            test_id="test-2",
            test_name="test_2",
            scope=TestScope.INTEGRATION,
            environment=TestEnvironment.MOCK,
            start_time=datetime.now(UTC),
            end_time=datetime.now(UTC),
            duration_ms=2000,
            result=TestResult.FAILED,
            error_message="Test failed"
        )
        
        reporter.add_report(report1)
        reporter.add_report(report2)
        
        report_files = reporter.generate_reports()
        
        assert "json" in report_files
        assert "html" in report_files
        assert "junit" in report_files
        assert "summary" in report_files
        
        # Check that files were created
        for file_path in report_files.values():
            assert Path(file_path).exists()

    def test_get_summary_stats(self, reporter):
        """Test getting summary statistics."""
        # Add test reports
        report1 = TestReport(
            test_id="test-1",
            test_name="test_1",
            scope=TestScope.INTEGRATION,
            environment=TestEnvironment.MOCK,
            start_time=datetime.now(UTC),
            end_time=datetime.now(UTC),
            duration_ms=1000,
            result=TestResult.PASSED
        )
        
        report2 = TestReport(
            test_id="test-2",
            test_name="test_2",
            scope=TestScope.INTEGRATION,
            environment=TestEnvironment.MOCK,
            start_time=datetime.now(UTC),
            end_time=datetime.now(UTC),
            duration_ms=2000,
            result=TestResult.FAILED
        )
        
        report3 = TestReport(
            test_id="test-3",
            test_name="test_3",
            scope=TestScope.INTEGRATION,
            environment=TestEnvironment.MOCK,
            start_time=datetime.now(UTC),
            end_time=datetime.now(UTC),
            duration_ms=1500,
            result=TestResult.SKIPPED
        )
        
        reporter.add_report(report1)
        reporter.add_report(report2)
        reporter.add_report(report3)
        
        stats = reporter._get_summary_stats()
        
        assert stats["total"] == 3
        assert stats["passed"] == 1
        assert stats["failed"] == 1
        assert stats["skipped"] == 1
        assert stats["errors"] == 0
        assert abs(stats["success_rate"] - 33.33333333333333) < 0.01
        assert stats["total_duration"] == 4.5  # (1000 + 2000 + 1500) / 1000


class TestTestEnvironmentManager:
    """Test TestEnvironmentManager class."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            yield Path(temp_dir)

    @pytest.fixture
    def config(self):
        """Create a test configuration."""
        return TestConfig(
            environment=TestEnvironment.MOCK,
            cleanup_on_exit=True
        )

    @pytest.fixture
    def manager(self, config, temp_dir):
        """Create a test environment manager."""
        return TestEnvironmentManager(config, temp_dir)

    @pytest.mark.asyncio
    async def test_manager_creation(self, manager, config, temp_dir):
        """Test creating an environment manager."""
        assert manager.config == config
        assert manager.work_dir == temp_dir
        assert manager.temp_dirs == []
        assert manager.docker_containers == []
        assert manager.processes == []

    @pytest.mark.asyncio
    async def test_setup_mock_environment(self, manager):
        """Test setting up mock environment."""
        await manager._setup_mock_environment()
        
        # Check that mock data directory was created
        mock_data_dir = manager.work_dir / "mock_data"
        assert mock_data_dir.exists()
        assert mock_data_dir in manager.temp_dirs
        
        # Check that mock config file was created
        config_file = mock_data_dir / "mock_config.json"
        assert config_file.exists()
        
        # Check config content
        with open(config_file) as f:
            config = json.load(f)
            assert "jira" in config
            assert "openproject" in config

    @pytest.mark.asyncio
    async def test_setup_docker_environment_disabled(self, manager):
        """Test setting up Docker environment when disabled."""
        with pytest.raises(RuntimeError, match="Docker environment requested but not enabled"):
            await manager._setup_docker_environment()

    @pytest.mark.asyncio
    async def test_setup_real_environment_disabled(self, manager):
        """Test setting up real environment when disabled."""
        with pytest.raises(RuntimeError, match="Real environment requested but not enabled"):
            await manager._setup_real_environment()

    @pytest.mark.asyncio
    async def test_setup_and_teardown_mock(self, manager):
        """Test setting up and tearing down mock environment."""
        await manager.setup()
        
        # Check that environment was set up
        mock_data_dir = manager.work_dir / "mock_data"
        assert mock_data_dir.exists()
        
        await manager.teardown()
        
        # Check that environment was cleaned up
        if manager.config.cleanup_on_exit:
            assert not mock_data_dir.exists()


class TestIntegrationTestingFramework:
    """Test IntegrationTestingFramework class."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            yield Path(temp_dir)

    @pytest.fixture
    def config(self):
        """Create a test configuration."""
        return TestConfig(
            environment=TestEnvironment.MOCK,
            scope=TestScope.INTEGRATION,
            generate_reports=True
        )

    @pytest.fixture
    def framework(self, config, temp_dir):
        """Create a test framework."""
        return IntegrationTestingFramework(config, temp_dir)

    def test_framework_creation(self, framework, config, temp_dir):
        """Test creating an integration testing framework."""
        assert framework.config == config
        assert framework.work_dir == temp_dir
        assert framework.test_data is None
        assert framework.logger is not None

    @pytest.mark.asyncio
    async def test_run_single_test_success(self, framework):
        """Test running a single successful test."""
        async def test_func():
            await asyncio.sleep(0.1)
            return "success"
        
        report = await framework.run_single_test("test_success", test_func)
        
        assert report.test_name == "test_success"
        assert report.result == TestResult.PASSED
        assert report.error_message is None
        assert report.duration_ms is not None
        assert report.duration_ms > 0

    @pytest.mark.asyncio
    async def test_run_single_test_failure(self, framework):
        """Test running a single failing test."""
        async def test_func():
            await asyncio.sleep(0.1)
            raise ValueError("Test failed")
        
        report = await framework.run_single_test("test_failure", test_func)
        
        assert report.test_name == "test_failure"
        assert report.result == TestResult.FAILED
        assert report.error_message == "Test failed"
        assert report.duration_ms is not None
        assert report.duration_ms > 0

    @pytest.mark.asyncio
    async def test_create_test_suite(self, framework):
        """Test creating a test suite."""
        tests = ["test1", "test2", "test3"]
        suite = framework.create_test_suite("Test Suite", "Test description", tests)
        
        assert suite.name == "Test Suite"
        assert suite.description == "Test description"
        assert suite.config == framework.config
        assert suite.tests == tests

    @pytest.mark.asyncio
    async def test_test_context(self, framework):
        """Test the test context manager."""
        async with framework.test_context() as ctx:
            assert ctx == framework
            assert framework.test_data is not None
            assert "jira" in framework.test_data
            assert "openproject" in framework.test_data

    @pytest.mark.asyncio
    async def test_run_test_suite(self, framework):
        """Test running a test suite."""
        # Create a test suite
        suite = TestSuite(
            name="Test Suite",
            description="Test description",
            config=framework.config,
            test_data=TestData(),
            tests=["test1", "test2"]
        )
        
        # Mock the test function getter to return actual test functions
        async def mock_test1():
            return True
        
        async def mock_test2():
            return True
        
        def mock_get_test_function(test_name):
            if test_name == "test1":
                return mock_test1
            elif test_name == "test2":
                return mock_test2
            return None
        
        framework._get_test_function = mock_get_test_function
        
        results = await framework.run_test_suite(suite)
        
        assert results["suite_name"] == "Test Suite"
        assert "start_time" in results
        assert "end_time" in results
        assert "tests" in results
        assert "summary" in results
        assert len(results["tests"]) == 2


class TestConvenienceFunctions:
    """Test convenience functions."""

    def test_create_basic_test_suite(self):
        """Test creating a basic test suite."""
        suite = create_basic_test_suite()
        
        assert suite.name == "Basic Integration Tests"
        assert suite.description == "Basic integration tests for migration components"
        assert suite.config.environment == TestEnvironment.MOCK
        assert suite.config.scope == TestScope.INTEGRATION
        assert len(suite.tests) == 4
        assert "test_jira_connection" in suite.tests
        assert "test_openproject_connection" in suite.tests
        assert "test_data_validation" in suite.tests
        assert "test_migration_workflow" in suite.tests

    @pytest.mark.asyncio
    async def test_run_integration_tests(self):
        """Test running integration tests."""
        config = TestConfig(
            environment=TestEnvironment.MOCK,
            scope=TestScope.INTEGRATION
        )
        
        suite1 = TestSuite(
            name="Suite 1",
            description="Test suite 1",
            config=config,
            test_data=TestData(),
            tests=["test1"]
        )
        
        suite2 = TestSuite(
            name="Suite 2",
            description="Test suite 2",
            config=config,
            test_data=TestData(),
            tests=["test2"]
        )
        
        results = await run_integration_tests(config, [suite1, suite2])
        
        assert "Suite 1" in results
        assert "Suite 2" in results
        # Note: The actual test execution would fail because test functions don't exist
        # but the framework should handle this gracefully


if __name__ == "__main__":
    pytest.main([__file__]) 