#!/usr/bin/env python3
"""Comprehensive unit tests for EnhancedOpenProjectClient with performance optimizations.

Tests cover:
1. Batch work package operations - batch_create_work_packages
2. Parallel processing - bulk_get_work_packages
3. Optimized Rails operations - bulk creation scripts
4. Performance caching - @cached decorator integration
5. Rate limiting - @rate_limited decorator integration
6. Backwards compatibility - with base OpenProjectClient
7. Error handling and resilience
8. Performance improvements and validation
"""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from src.clients.enhanced_openproject_client import EnhancedOpenProjectClient
from src.clients.openproject_client import OpenProjectClient
from src.utils.performance_optimizer import PerformanceOptimizer


class TestEnhancedOpenProjectClientInitialization:
    """Test EnhancedOpenProjectClient initialization and configuration."""

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    def test_enhanced_client_initialization(self, mock_session) -> None:
        """Test enhanced client initialization with performance optimizer."""
        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
            cache_size=800,
            cache_ttl=3600,
            batch_size=25,
            max_workers=6,
            rate_limit=8.0,
        )

        # Verify base client initialization
        assert client.server == "https://openproject.example.com"
        assert client.username == "admin"

        # Verify performance optimizer configuration
        assert client.performance_optimizer.cache.max_size == 800
        assert client.performance_optimizer.cache.default_ttl == 3600
        assert client.batch_size == 25
        assert client.parallel_workers == 6
        assert client.performance_optimizer.rate_limiter.current_rate == 8.0

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    def test_enhanced_client_default_values(self, mock_session) -> None:
        """Test enhanced client initialization with default values."""
        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )

        # Verify default performance optimizer settings
        assert client.performance_optimizer.cache.max_size == 1500
        assert client.performance_optimizer.cache.default_ttl == 2400
        assert client.batch_size == 50
        assert client.parallel_workers == 12
        assert client.performance_optimizer.rate_limiter.current_rate == 12.0


class TestBatchCreateWorkPackages:
    """Test batch_create_work_packages functionality."""

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    def test_batch_create_empty_list(self, mock_session) -> None:
        """Test batch create with empty work packages list."""
        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )

        result = client.batch_create_work_packages([])

        expected = {
            "created": [],
            "errors": [],
            "stats": {"total": 0, "created": 0, "failed": 0},
        }
        assert result == expected

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    @patch.object(EnhancedOpenProjectClient, "_create_temp_work_packages_file")
    @patch.object(EnhancedOpenProjectClient, "_execute_optimized_batch_creation")
    def test_batch_create_success(
        self,
        mock_execute,
        mock_create_temp,
        mock_session,
    ) -> None:
        """Test successful batch work package creation."""
        # Mock temp file
        mock_temp_file = Mock()
        mock_temp_file.exists.return_value = True
        mock_create_temp.return_value = mock_temp_file

        # Mock successful execution
        mock_result = {
            "created": [{"id": 1, "subject": "Test 1"}, {"id": 2, "subject": "Test 2"}],
            "errors": [],
            "stats": {"total": 2, "created": 2, "failed": 0},
        }
        mock_execute.return_value = mock_result

        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )

        work_packages = [
            {"subject": "Test 1", "project_id": 1},
            {"subject": "Test 2", "project_id": 1},
        ]

        result = client.batch_create_work_packages(work_packages)

        assert result == mock_result
        mock_create_temp.assert_called_once_with(work_packages)
        mock_execute.assert_called_once_with(mock_temp_file)
        mock_temp_file.unlink.assert_called_once()

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    @patch.object(EnhancedOpenProjectClient, "_create_temp_work_packages_file")
    @patch.object(EnhancedOpenProjectClient, "_execute_optimized_batch_creation")
    def test_batch_create_with_errors(
        self,
        mock_execute,
        mock_create_temp,
        mock_session,
    ) -> None:
        """Test batch creation with some errors."""
        # Mock temp file
        mock_temp_file = Mock()
        mock_temp_file.exists.return_value = True
        mock_create_temp.return_value = mock_temp_file

        # Mock execution with errors
        mock_result = {
            "created": [{"id": 1, "subject": "Test 1"}],
            "errors": [{"subject": "Test 2", "error": "Invalid project"}],
            "stats": {"total": 2, "created": 1, "failed": 1},
        }
        mock_execute.return_value = mock_result

        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )

        work_packages = [
            {"subject": "Test 1", "project_id": 1},
            {"subject": "Test 2", "project_id": 999},  # Invalid project
        ]

        result = client.batch_create_work_packages(work_packages)

        assert result["stats"]["created"] == 1
        assert result["stats"]["failed"] == 1
        assert len(result["errors"]) == 1

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    @patch.object(EnhancedOpenProjectClient, "_create_temp_work_packages_file")
    @patch.object(EnhancedOpenProjectClient, "_execute_optimized_batch_creation")
    def test_batch_create_exception_handling(
        self,
        mock_execute,
        mock_create_temp,
        mock_session,
    ) -> None:
        """Test batch creation exception handling."""
        # Mock temp file
        mock_temp_file = Mock()
        mock_temp_file.exists.return_value = True
        mock_create_temp.return_value = mock_temp_file

        # Mock execution failure
        mock_execute.side_effect = Exception("Rails script failed")

        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )

        work_packages = [{"subject": "Test", "project_id": 1}]

        with pytest.raises(Exception) as exc_info:
            client.batch_create_work_packages(work_packages)

        assert "Rails script failed" in str(exc_info.value)
        # Temp file should still be cleaned up
        mock_temp_file.unlink.assert_called_once()


class TestCreateTempWorkPackagesFile:
    """Test _create_temp_work_packages_file method."""

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    @patch("tempfile.NamedTemporaryFile")
    def test_create_temp_file(self, mock_temp_file, mock_session) -> None:
        """Test temporary file creation for work packages."""
        # Mock temporary file
        mock_file = Mock()
        mock_temp_file.return_value.__enter__.return_value = mock_file
        mock_temp_file.return_value.__enter__.return_value.name = "/tmp/test_file.json"

        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )

        work_packages = [
            {"subject": "Test 1", "project_id": 1},
            {"subject": "Test 2", "project_id": 2},
        ]

        with patch("pathlib.Path") as mock_path_class:
            mock_path = Mock()
            mock_path_class.return_value = mock_path

            result = client._create_temp_work_packages_file(work_packages)

            assert result == mock_path
            # Verify JSON was written
            mock_file.write.assert_called_once()
            written_data = mock_file.write.call_args[0][0]
            assert "Test 1" in written_data
            assert "Test 2" in written_data


class TestExecuteOptimizedBatchCreation:
    """Test _execute_optimized_batch_creation method."""

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    @patch("subprocess.run")
    def test_execute_batch_creation_success(
        self,
        mock_subprocess,
        mock_session,
    ) -> None:
        """Test successful batch creation execution."""
        # Mock successful subprocess result
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(
            {
                "created": [{"id": 1, "subject": "Test"}],
                "errors": [],
                "stats": {"total": 1, "created": 1, "failed": 0},
            },
        )
        mock_subprocess.return_value = mock_result

        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )

        temp_file = Path("/tmp/test.json")

        result = client._execute_optimized_batch_creation(temp_file)

        assert result["stats"]["created"] == 1
        assert result["stats"]["failed"] == 0
        assert len(result["created"]) == 1

        # Verify subprocess was called with correct parameters
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args[0][0]
        assert "rails" in call_args[0]
        assert "runner" in call_args

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    @patch("subprocess.run")
    def test_execute_batch_creation_failure(
        self,
        mock_subprocess,
        mock_session,
    ) -> None:
        """Test batch creation execution failure."""
        # Mock failed subprocess result
        mock_result = Mock()
        mock_result.returncode = 1
        mock_result.stderr = "Rails error"
        mock_subprocess.return_value = mock_result

        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )

        temp_file = Path("/tmp/test.json")

        with pytest.raises(Exception) as exc_info:
            client._execute_optimized_batch_creation(temp_file)

        assert "Rails script failed" in str(exc_info.value)
        assert "Rails error" in str(exc_info.value)

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    @patch("subprocess.run")
    def test_execute_batch_creation_invalid_json(
        self,
        mock_subprocess,
        mock_session,
    ) -> None:
        """Test handling of invalid JSON response."""
        # Mock subprocess with invalid JSON
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = "invalid json"
        mock_subprocess.return_value = mock_result

        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )

        temp_file = Path("/tmp/test.json")

        with pytest.raises(Exception) as exc_info:
            client._execute_optimized_batch_creation(temp_file)

        assert "Failed to parse" in str(exc_info.value)


class TestBulkGetWorkPackages:
    """Test bulk_get_work_packages functionality."""

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    def test_bulk_get_empty_list(self, mock_session) -> None:
        """Test bulk get with empty work package IDs list."""
        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )

        result = client.bulk_get_work_packages([])
        assert result == {}

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    @patch("src.clients.enhanced_openproject_client.ThreadPoolExecutor")
    def test_bulk_get_success(self, mock_executor_class, mock_session) -> None:
        """Test successful bulk work package retrieval."""
        # Mock executor
        mock_executor = Mock()
        mock_executor_class.return_value.__enter__.return_value = mock_executor

        # Mock work package data
        wp1 = {"id": 1, "subject": "Work Package 1"}
        wp2 = {"id": 2, "subject": "Work Package 2"}

        mock_future1 = Mock()
        mock_future1.result.return_value = wp1
        mock_future2 = Mock()
        mock_future2.result.return_value = wp2

        mock_executor.submit.side_effect = [mock_future1, mock_future2]

        # Mock as_completed
        with patch(
            "src.clients.enhanced_openproject_client.as_completed",
            return_value=[mock_future1, mock_future2],
        ):
            client = EnhancedOpenProjectClient(
                server="https://openproject.example.com",
                username="admin",
                password="password",
            )

            results = client.bulk_get_work_packages([1, 2])

            assert len(results) == 2
            assert results[1] == wp1
            assert results[2] == wp2

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    @patch("src.clients.enhanced_openproject_client.ThreadPoolExecutor")
    def test_bulk_get_partial_failure(self, mock_executor_class, mock_session) -> None:
        """Test bulk get with partial failures."""
        # Mock executor
        mock_executor = Mock()
        mock_executor_class.return_value.__enter__.return_value = mock_executor

        # One successful, one failed
        wp1 = {"id": 1, "subject": "Work Package 1"}

        mock_future1 = Mock()
        mock_future1.result.return_value = wp1
        mock_future2 = Mock()
        mock_future2.result.side_effect = Exception("Not found")

        mock_executor.submit.side_effect = [mock_future1, mock_future2]

        # Mock as_completed
        with patch(
            "src.clients.enhanced_openproject_client.as_completed",
            return_value=[mock_future1, mock_future2],
        ):
            client = EnhancedOpenProjectClient(
                server="https://openproject.example.com",
                username="admin",
                password="password",
            )

            results = client.bulk_get_work_packages([1, 2])

            assert len(results) == 2
            assert results[1] == wp1
            assert results[2] is None


class TestGetWorkPackageSafe:
    """Test _get_work_package_safe internal method."""

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    def test_get_work_package_safe_success(self, mock_session) -> None:
        """Test successful safe work package retrieval."""
        # Mock response
        wp_data = {"id": 1, "subject": "Test Work Package"}
        mock_response = Mock()
        mock_response.json.return_value = wp_data
        mock_response.raise_for_status.return_value = None

        mock_session_instance = Mock()
        mock_session_instance.get.return_value = mock_response
        mock_session.return_value = mock_session_instance

        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )
        client.session = mock_session_instance

        result = client._get_work_package_safe(1)

        assert result == wp_data
        mock_session_instance.get.assert_called_once()

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    def test_get_work_package_safe_not_found(self, mock_session) -> None:
        """Test safe work package retrieval with 404 error."""
        # Mock 404 response
        mock_response = Mock()
        mock_response.raise_for_status.side_effect = Exception("404 Not Found")

        mock_session_instance = Mock()
        mock_session_instance.get.return_value = mock_response
        mock_session.return_value = mock_session_instance

        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )
        client.session = mock_session_instance

        result = client._get_work_package_safe(999)

        assert result is None

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    def test_get_work_package_safe_network_error(self, mock_session) -> None:
        """Test safe work package retrieval with network error."""
        # Mock network error
        mock_session_instance = Mock()
        mock_session_instance.get.side_effect = ConnectionError("Network error")
        mock_session.return_value = mock_session_instance

        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )
        client.session = mock_session_instance

        result = client._get_work_package_safe(1)

        assert result is None


class TestCachedOperations:
    """Test cached operations integration."""

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    def test_cached_priorities_get(self, mock_session) -> None:
        """Test cached priorities retrieval."""
        # Mock priorities data
        mock_priorities = [{"id": 1, "name": "Low"}, {"id": 2, "name": "High"}]

        mock_response = Mock()
        mock_response.json.return_value = {"_embedded": {"elements": mock_priorities}}
        mock_response.raise_for_status.return_value = None

        mock_session_instance = Mock()
        mock_session_instance.get.return_value = mock_response
        mock_session.return_value = mock_session_instance

        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )
        client.session = mock_session_instance

        # First call
        result1 = client.get_priorities_cached()
        assert result1 == mock_priorities

        # Second call should use cache
        result2 = client.get_priorities_cached()
        assert result2 == mock_priorities

        # Should only make one API call due to caching
        assert mock_session_instance.get.call_count == 1

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    def test_cached_types_get(self, mock_session) -> None:
        """Test cached types retrieval."""
        mock_types = [{"id": 1, "name": "Task"}, {"id": 2, "name": "Bug"}]

        mock_response = Mock()
        mock_response.json.return_value = {"_embedded": {"elements": mock_types}}
        mock_response.raise_for_status.return_value = None

        mock_session_instance = Mock()
        mock_session_instance.get.return_value = mock_response
        mock_session.return_value = mock_session_instance

        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )
        client.session = mock_session_instance

        # Test caching behavior
        result1 = client.get_types_cached()
        result2 = client.get_types_cached()

        assert result1 == mock_types
        assert result2 == mock_types
        assert mock_session_instance.get.call_count == 1


class TestBulkUpdateWorkPackages:
    """Test bulk_update_work_packages functionality."""

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    @patch.object(EnhancedOpenProjectClient, "_create_temp_updates_file")
    @patch.object(EnhancedOpenProjectClient, "_execute_optimized_bulk_update")
    def test_bulk_update_success(
        self,
        mock_execute,
        mock_create_temp,
        mock_session,
    ) -> None:
        """Test successful bulk work package updates."""
        # Mock temp file
        mock_temp_file = Mock()
        mock_temp_file.exists.return_value = True
        mock_create_temp.return_value = mock_temp_file

        # Mock successful execution
        mock_result = {
            "updated": [{"id": 1, "subject": "Updated 1"}],
            "errors": [],
            "stats": {"total": 1, "updated": 1, "failed": 0},
        }
        mock_execute.return_value = mock_result

        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )

        updates = [{"id": 1, "subject": "Updated Subject"}]

        result = client.bulk_update_work_packages(updates)

        assert result == mock_result
        mock_create_temp.assert_called_once_with(updates)
        mock_execute.assert_called_once_with(mock_temp_file)
        mock_temp_file.unlink.assert_called_once()


class TestPerformanceIntegration:
    """Test performance optimization integration."""

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    def test_performance_optimizer_integration(self, mock_session) -> None:
        """Test performance optimizer integration in enhanced client."""
        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
            cache_size=200,
            rate_limit=3.0,
        )

        # Verify optimizer is properly configured
        assert isinstance(client.performance_optimizer, PerformanceOptimizer)
        assert client.performance_optimizer.cache.max_size == 200
        assert client.performance_optimizer.rate_limiter.current_rate == 3.0

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    def test_rate_limiting_integration(self, mock_session) -> None:
        """Test rate limiting integration in batch operations."""
        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
            rate_limit=1.0,  # Very slow rate for testing
        )

        # Mock the _get_work_package_safe method to verify rate limiting
        with patch.object(client, "_get_work_package_safe") as mock_get:
            mock_get.return_value = {"id": 1, "subject": "Test"}

            client.bulk_get_work_packages([1])

            # Should have been called (rate limiting is tested in performance_optimizer tests)
            assert mock_get.called


class TestBackwardsCompatibility:
    """Test backwards compatibility with base OpenProjectClient."""

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    def test_base_methods_available(self, mock_session) -> None:
        """Test that base OpenProjectClient methods are still available."""
        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )

        # Should inherit from base client
        assert isinstance(client, OpenProjectClient)

        # Base methods should be available
        assert hasattr(client, "get_work_package")
        assert hasattr(client, "create_work_package")
        assert hasattr(client, "update_work_package")

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    def test_enhanced_methods_added(self, mock_session) -> None:
        """Test that enhanced methods are added."""
        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )

        # Enhanced methods should be available
        assert hasattr(client, "batch_create_work_packages")
        assert hasattr(client, "bulk_get_work_packages")
        assert hasattr(client, "bulk_update_work_packages")
        assert hasattr(client, "get_priorities_cached")
        assert hasattr(client, "get_types_cached")


class TestErrorHandlingAndResilience:
    """Test error handling and resilience features."""

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    def test_network_error_handling(self, mock_session) -> None:
        """Test handling of network errors."""
        mock_session_instance = Mock()
        mock_session_instance.get.side_effect = ConnectionError("Network error")
        mock_session.return_value = mock_session_instance

        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )
        client.session = mock_session_instance

        # Should handle network errors gracefully
        result = client._get_work_package_safe(1)
        assert result is None

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    def test_api_error_handling(self, mock_session) -> None:
        """Test handling of API errors."""
        mock_response = Mock()
        mock_response.raise_for_status.side_effect = Exception("Forbidden")

        mock_session_instance = Mock()
        mock_session_instance.get.return_value = mock_response
        mock_session.return_value = mock_session_instance

        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )
        client.session = mock_session_instance

        # Should handle API errors gracefully
        result = client._get_work_package_safe(1)
        assert result is None

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    @patch("src.clients.enhanced_openproject_client.ThreadPoolExecutor")
    def test_partial_bulk_failure_handling(
        self,
        mock_executor_class,
        mock_session,
    ) -> None:
        """Test handling of partial bulk operation failures."""
        # Mock executor
        mock_executor = Mock()
        mock_executor_class.return_value.__enter__.return_value = mock_executor

        # One successful future, one failed
        mock_future1 = Mock()
        mock_future1.result.return_value = {"id": 1, "subject": "Test"}

        mock_future2 = Mock()
        mock_future2.result.side_effect = Exception("Operation failed")

        mock_executor.submit.side_effect = [mock_future1, mock_future2]

        # Mock as_completed
        with patch(
            "src.clients.enhanced_openproject_client.as_completed",
            return_value=[mock_future1, mock_future2],
        ):
            client = EnhancedOpenProjectClient(
                server="https://openproject.example.com",
                username="admin",
                password="password",
            )

            results = client.bulk_get_work_packages([1, 2])

            # Should have successful result and failed result
            assert 1 in results
            assert 2 in results
            assert results[1] is not None
            assert results[2] is None


class TestOptimizedRailsOperations:
    """Test optimized Rails script operations."""

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    def test_rails_script_generation(self, mock_session) -> None:
        """Test Rails script generation for batch operations."""
        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )

        # Test script generation methods exist
        assert hasattr(client, "_create_temp_work_packages_file")
        assert hasattr(client, "_execute_optimized_batch_creation")

        # Rails-specific methods should be available
        if hasattr(client, "_generate_bulk_creation_script"):
            assert callable(client._generate_bulk_creation_script)

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    @patch("subprocess.run")
    def test_rails_script_execution_timeout(
        self,
        mock_subprocess,
        mock_session,
    ) -> None:
        """Test Rails script execution with timeout."""
        # Mock subprocess timeout
        mock_subprocess.side_effect = TimeoutError("Script timeout")

        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )

        temp_file = Path("/tmp/test.json")

        with pytest.raises(Exception) as exc_info:
            client._execute_optimized_batch_creation(temp_file)

        assert "timeout" in str(exc_info.value).lower()


class TestMemoryEfficiencyAndScalability:
    """Test memory efficiency and scalability features."""

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    @patch.object(EnhancedOpenProjectClient, "_create_temp_work_packages_file")
    @patch.object(EnhancedOpenProjectClient, "_execute_optimized_batch_creation")
    def test_large_batch_handling(
        self,
        mock_execute,
        mock_create_temp,
        mock_session,
    ) -> None:
        """Test handling of large batches efficiently."""
        # Mock temp file
        mock_temp_file = Mock()
        mock_temp_file.exists.return_value = True
        mock_create_temp.return_value = mock_temp_file

        # Mock successful execution for large batch
        mock_result = {
            "created": [{"id": i, "subject": f"Test {i}"} for i in range(1000)],
            "errors": [],
            "stats": {"total": 1000, "created": 1000, "failed": 0},
        }
        mock_execute.return_value = mock_result

        client = EnhancedOpenProjectClient(
            server="https://openproject.example.com",
            username="admin",
            password="password",
        )

        # Create large batch
        large_batch = [{"subject": f"Test {i}", "project_id": 1} for i in range(1000)]

        result = client.batch_create_work_packages(large_batch)

        assert result["stats"]["created"] == 1000
        assert len(result["created"]) == 1000

        # Verify temp file cleanup
        mock_temp_file.unlink.assert_called_once()

    @patch("src.clients.enhanced_openproject_client.requests.Session")
    @patch("src.clients.enhanced_openproject_client.ThreadPoolExecutor")
    def test_bulk_operation_scalability(
        self,
        mock_executor_class,
        mock_session,
    ) -> None:
        """Test bulk operations scale with available workers."""
        # Mock executor
        mock_executor = Mock()
        mock_executor_class.return_value.__enter__.return_value = mock_executor

        # Mock many work packages
        work_package_ids = list(range(1, 101))  # 100 work packages

        # Mock futures
        mock_futures = []
        for wp_id in work_package_ids:
            mock_future = Mock()
            mock_future.result.return_value = {"id": wp_id, "subject": f"WP {wp_id}"}
            mock_futures.append(mock_future)

        mock_executor.submit.side_effect = mock_futures

        # Mock as_completed
        with patch(
            "src.clients.enhanced_openproject_client.as_completed",
            return_value=mock_futures,
        ):
            client = EnhancedOpenProjectClient(
                server="https://openproject.example.com",
                username="admin",
                password="password",
                max_workers=10,
            )

            results = client.bulk_get_work_packages(work_package_ids)

            assert len(results) == 100
            assert all(wp_id in results for wp_id in work_package_ids)

            # Should submit as many tasks as work packages
            assert mock_executor.submit.call_count == 100


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
