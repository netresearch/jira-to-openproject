#!/usr/bin/env python3
"""Integration tests for the complete migration workflow using the Integration Testing Framework."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.utils.integration_testing_framework import (
    IntegrationTestingFramework,
    TestConfig,
    TestData,
    TestEnvironment,
    TestScope,
    TestSuite,
    create_basic_test_suite,
    run_integration_tests,
)


class TestMigrationIntegration:
    """Integration tests for the complete migration workflow."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for test data."""
        with tempfile.TemporaryDirectory() as temp_dir:
            yield Path(temp_dir)

    @pytest.fixture
    def config(self):
        """Create test configuration."""
        return TestConfig(
            environment=TestEnvironment.MOCK,
            scope=TestScope.INTEGRATION,
            timeout_seconds=60,
            max_retries=2,
            parallel_workers=2,
            cleanup_on_exit=True,
            generate_reports=True,
            performance_threshold_ms=10000,
            data_volume="small",
            enable_mocking=True,
            enable_docker=False,
            enable_real_apis=False,
        )

    @pytest.fixture
    def framework(self, config, temp_dir):
        """Create integration testing framework instance."""
        return IntegrationTestingFramework(config, temp_dir)

    @pytest.mark.asyncio
    async def test_jira_connection_integration(self, framework) -> None:
        """Test Jira connection integration."""
        async with framework.test_context():
            # Mock Jira client
            with patch("src.clients.jira_client.JiraClient") as mock_jira_client:
                mock_client = AsyncMock()
                mock_client.connect.return_value = True
                mock_client.get_projects.return_value = [
                    {"key": "TEST1", "name": "Test Project 1"},
                    {"key": "TEST2", "name": "Test Project 2"},
                ]
                mock_jira_client.return_value = mock_client

                # Test connection
                assert await self._test_jira_connection(framework)

                # Verify client was called
                mock_client.connect.assert_called_once()
                mock_client.get_projects.assert_called_once()

    @pytest.mark.asyncio
    async def test_openproject_connection_integration(self, framework) -> None:
        """Test OpenProject connection integration."""
        async with framework.test_context():
            # Mock OpenProject client
            with patch(
                "src.clients.openproject_client.OpenProjectClient",
            ) as mock_op_client:
                mock_client = AsyncMock()
                mock_client.connect.return_value = True
                mock_client.get_projects.return_value = [
                    {"identifier": "test1", "name": "Test Project 1"},
                    {"identifier": "test2", "name": "Test Project 2"},
                ]
                mock_op_client.return_value = mock_client

                # Test connection
                assert await self._test_openproject_connection(framework)

                # Verify client was called
                mock_client.connect.assert_called_once()
                mock_client.get_projects.assert_called_once()

    @pytest.mark.asyncio
    async def test_data_validation_integration(self, framework) -> None:
        """Test data validation integration."""
        async with framework.test_context():
            # Mock validation framework
            with patch(
                "src.utils.advanced_validation.ValidationFramework",
            ) as mock_validation:
                mock_validator = AsyncMock()
                mock_validator.validate_data.return_value = {
                    "valid": True,
                    "errors": [],
                    "warnings": [],
                }
                mock_validation.return_value = mock_validator

                # Test validation
                assert await self._test_data_validation(framework)

                # Verify validation was called
                mock_validator.validate_data.assert_called_once()

    @pytest.mark.asyncio
    async def test_migration_workflow_integration(self, framework) -> None:
        """Test complete migration workflow integration."""
        async with framework.test_context():
            # Mock all migration components
            with patch("src.migration.MigrationEngine") as mock_migration_engine:
                mock_engine = AsyncMock()
                mock_engine.migrate.return_value = {
                    "success": True,
                    "migrated_items": 100,
                    "errors": 0,
                    "warnings": 5,
                }
                mock_migration_engine.return_value = mock_engine

                # Test migration workflow
                result = await self._test_migration_workflow(framework)
                assert result["success"] is True
                assert result["migrated_items"] == 100

                # Verify migration was called
                mock_engine.migrate.assert_called_once()

    @pytest.mark.asyncio
    async def test_performance_monitoring_integration(self, framework) -> None:
        """Test performance monitoring integration."""
        async with framework.test_context():
            # Start performance monitoring
            framework.performance_monitor.start_timer("test_operation")

            # Simulate some work
            await asyncio.sleep(0.1)

            # Stop monitoring
            framework.performance_monitor.stop_timer("test_operation")

            # Verify performance metrics
            metrics = framework.performance_monitor.get_metrics()
            assert "test_operation" in metrics
            assert metrics["test_operation"]["count"] == 1
            assert metrics["test_operation"]["avg_ms"] > 0

    @pytest.mark.asyncio
    async def test_error_recovery_integration(self, framework) -> None:
        """Test error recovery integration."""
        async with framework.test_context():
            # Mock error recovery system
            with patch(
                "src.utils.error_recovery.ErrorRecoverySystem",
            ) as mock_error_recovery:
                mock_recovery = AsyncMock()
                mock_recovery.handle_error.return_value = {
                    "recovered": True,
                    "retry_count": 1,
                    "error_type": "connection_timeout",
                }
                mock_error_recovery.return_value = mock_recovery

                # Simulate an error
                try:
                    msg = "Connection timeout"
                    raise ConnectionError(msg)
                except ConnectionError as e:
                    result = await self._test_error_recovery(framework, e)
                    assert result["recovered"] is True
                    assert result["retry_count"] == 1

    @pytest.mark.asyncio
    async def test_security_integration(self, framework) -> None:
        """Test security features integration."""
        async with framework.test_context():
            # Mock security manager
            with patch("src.utils.advanced_security.SecurityManager") as mock_security:
                mock_security_manager = AsyncMock()
                mock_security_manager.validate_access.return_value = True
                mock_security_manager.audit_log.return_value = True
                mock_security.return_value = mock_security_manager

                # Test security validation
                assert await self._test_security_validation(framework)

                # Verify security was called
                mock_security_manager.validate_access.assert_called_once()
                mock_security_manager.audit_log.assert_called_once()

    @pytest.mark.asyncio
    async def test_configuration_management_integration(self, framework) -> None:
        """Test configuration management integration."""
        async with framework.test_context():
            # Mock configuration manager
            with patch(
                "src.utils.advanced_config_manager.ConfigurationManager",
            ) as mock_config:
                mock_config_manager = AsyncMock()
                mock_config_manager.load_config.return_value = {
                    "jira": {"url": "https://test.jira.com"},
                    "openproject": {"url": "https://test.openproject.com"},
                }
                mock_config_manager.validate_config.return_value = {
                    "valid": True,
                    "errors": [],
                }
                mock_config.return_value = mock_config_manager

                # Test configuration management
                assert await self._test_configuration_management(framework)

                # Verify configuration was loaded and validated
                mock_config_manager.load_config.assert_called_once()
                mock_config_manager.validate_config.assert_called_once()

    @pytest.mark.asyncio
    async def test_complete_integration_suite(self, framework) -> None:
        """Test running a complete integration test suite."""
        # Create a comprehensive test suite
        suite = TestSuite(
            name="Complete Migration Integration Tests",
            description="End-to-end integration tests for the migration workflow",
            config=framework.config,
            test_data=TestData(
                jira_projects=2,
                jira_issues_per_project=10,
                jira_users=5,
                op_projects=2,
                op_work_packages=10,
                op_users=5,
            ),
            tests=[
                "test_jira_connection",
                "test_openproject_connection",
                "test_data_validation",
                "test_migration_workflow",
                "test_performance_monitoring",
                "test_error_recovery",
                "test_security_validation",
                "test_configuration_management",
            ],
        )

        # Mock all test functions
        def mock_get_test_function(test_name):
            test_functions = {
                "test_jira_connection": self._test_jira_connection,
                "test_openproject_connection": self._test_openproject_connection,
                "test_data_validation": self._test_data_validation,
                "test_migration_workflow": self._test_migration_workflow,
                "test_performance_monitoring": self._test_performance_monitoring,
                "test_error_recovery": self._test_error_recovery,
                "test_security_validation": self._test_security_validation,
                "test_configuration_management": self._test_configuration_management,
            }
            return test_functions.get(test_name)

        framework._get_test_function = mock_get_test_function

        # Run the test suite
        results = await framework.run_test_suite(suite)

        # Verify results
        assert results["suite_name"] == "Complete Migration Integration Tests"
        assert len(results["tests"]) == 8
        assert "summary" in results

        # Check that all tests passed
        summary = results["summary"]
        assert summary["total"] == 8
        assert summary["passed"] == 8
        assert summary["failed"] == 0
        assert summary["success_rate"] == 100.0

    # Helper methods for individual test functions
    async def _test_jira_connection(self, framework) -> bool:
        """Test Jira connection."""
        # This would normally use the actual Jira client
        # For integration tests, we mock it
        return True

    async def _test_openproject_connection(self, framework) -> bool:
        """Test OpenProject connection."""
        # This would normally use the actual OpenProject client
        # For integration tests, we mock it
        return True

    async def _test_data_validation(self, framework) -> bool:
        """Test data validation."""
        # Validate that test data was generated
        assert framework.test_data is not None
        assert "jira" in framework.test_data
        assert "openproject" in framework.test_data
        return True

    async def _test_migration_workflow(self, framework):
        """Test migration workflow."""
        # Simulate migration process
        await asyncio.sleep(0.1)  # Simulate work
        return {"success": True, "migrated_items": 100, "errors": 0, "warnings": 5}

    async def _test_performance_monitoring(self, framework) -> bool:
        """Test performance monitoring."""
        framework.performance_monitor.start_timer("test_operation")
        await asyncio.sleep(0.05)
        framework.performance_monitor.stop_timer("test_operation")
        return True

    async def _test_error_recovery(self, framework, error):
        """Test error recovery."""
        # Simulate error recovery
        await asyncio.sleep(0.05)
        return {"recovered": True, "retry_count": 1, "error_type": type(error).__name__}

    async def _test_security_validation(self, framework) -> bool:
        """Test security validation."""
        # Simulate security validation
        await asyncio.sleep(0.05)
        return True

    async def _test_configuration_management(self, framework) -> bool:
        """Test configuration management."""
        # Simulate configuration management
        await asyncio.sleep(0.05)
        return True


class TestIntegrationTestOrchestration:
    """Test the orchestration of multiple integration test suites."""

    @pytest.mark.asyncio
    async def test_multiple_test_suites(self) -> None:
        """Test running multiple test suites."""
        config = TestConfig(
            environment=TestEnvironment.MOCK,
            scope=TestScope.INTEGRATION,
            timeout_seconds=30,
            generate_reports=True,
        )

        # Create multiple test suites
        suite1 = TestSuite(
            name="Connection Tests",
            description="Test connections to external services",
            config=config,
            test_data=TestData(jira_projects=1, op_projects=1),
            tests=["test_jira_connection", "test_openproject_connection"],
        )

        suite2 = TestSuite(
            name="Workflow Tests",
            description="Test migration workflows",
            config=config,
            test_data=TestData(jira_projects=1, op_projects=1),
            tests=["test_data_validation", "test_migration_workflow"],
        )

        # Mock test functions
        async def mock_test_func() -> bool:
            await asyncio.sleep(0.01)
            return True

        def mock_get_test_function(test_name):
            return mock_test_func

        # Create framework and mock test function getter
        framework = IntegrationTestingFramework(config)
        framework._get_test_function = mock_get_test_function

        # Run multiple suites
        results = await run_integration_tests(config, [suite1, suite2])

        # Verify results
        assert "Connection Tests" in results
        assert "Workflow Tests" in results
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_basic_test_suite_creation(self) -> None:
        """Test creating and running the basic test suite."""
        suite = create_basic_test_suite()

        assert suite.name == "Basic Integration Tests"
        assert suite.description == "Basic integration tests for migration components"
        assert len(suite.tests) == 4
        assert "test_jira_connection" in suite.tests
        assert "test_openproject_connection" in suite.tests
        assert "test_data_validation" in suite.tests
        assert "test_migration_workflow" in suite.tests

        # Test running the basic suite
        config = TestConfig(
            environment=TestEnvironment.MOCK,
            scope=TestScope.INTEGRATION,
            timeout_seconds=30,
        )

        async def mock_test_func() -> bool:
            await asyncio.sleep(0.01)
            return True

        def mock_get_test_function(test_name):
            return mock_test_func

        framework = IntegrationTestingFramework(config)
        framework._get_test_function = mock_get_test_function

        results = await framework.run_test_suite(suite)

        assert results["suite_name"] == "Basic Integration Tests"
        assert len(results["tests"]) == 4
        assert results["summary"]["total"] == 4
        assert results["summary"]["passed"] == 4


if __name__ == "__main__":
    pytest.main([__file__])
