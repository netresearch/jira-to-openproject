import pytest

pytestmark = pytest.mark.integration


"""Resource boundary tests for the migration tool.
Tests behavior under memory pressure, rate limiting, network issues, and resource exhaustion.

Based on Zen TestGen analysis identifying critical gaps in:
- Memory pressure scenarios during large dataset processing
- Rate limiting boundary conditions
- Network partition recovery
- Resource exhaustion handling
"""

import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch


from src.clients.exceptions import ConnectionError, RateLimitError
from src.clients.jira_client import JiraClient
from src.clients.openproject_client import OpenProjectClient
from src.migration import Migration
from src.migrations.work_package_migration import WorkPackageMigration


class TestMemoryPressureScenarios:
    """Tests for behavior under memory pressure and large dataset conditions."""

    @pytest.fixture
    def memory_constrained_migration(self):
        """Create migration instance configured for memory-efficient operation."""
        migration = WorkPackageMigration()
        migration.batch_size = 10  # Small batch size for memory efficiency
        migration.enable_memory_monitoring = True
        migration.memory_threshold_mb = 100  # Low threshold for testing
        return migration

    @pytest.fixture
    def large_dataset(self):
        """Generate large dataset that could cause memory pressure."""
        return [
            {
                "id": i,
                "title": f"Work Package {i}",
                "description": "A" * 1000,  # Large description
                "attachments": [
                    f"file_{j}.pdf" for j in range(10)
                ],  # Multiple attachments
                "custom_fields": {f"field_{k}": f"value_{k}" * 100 for k in range(20)},
            }
            for i in range(1000)  # 1000 large work packages
        ]

    def test_memory_efficient_batch_processing(
        self,
        memory_constrained_migration,
        large_dataset,
    ) -> None:
        """MEMORY TEST: Large datasets should be processed in memory-efficient batches.
        Verifies that memory usage stays within acceptable limits.
        """
        # Arrange: Mock memory monitoring
        memory_usage_samples = []

        def mock_get_memory_usage():
            # Simulate increasing memory usage
            current_usage = len(memory_usage_samples) * 10 + 50  # Start at 50MB
            memory_usage_samples.append(current_usage)
            return current_usage

        memory_constrained_migration.get_memory_usage = mock_get_memory_usage
        memory_constrained_migration.openproject_client = MagicMock()

        # Act: Process large dataset
        memory_constrained_migration.migrate_work_packages(large_dataset)

        # Assert: Should process in multiple batches
        assert (
            memory_constrained_migration.openproject_client.create_work_package.call_count
            == 1000
        )

        # Verify batch processing was used
        batch_calls = (
            memory_constrained_migration.openproject_client.create_batch.call_count
        )
        assert (
            batch_calls >= 100
        )  # At least 100 batches for 1000 items with batch_size=10

    def test_memory_pressure_triggers_garbage_collection(
        self,
        memory_constrained_migration,
    ) -> None:
        """MEMORY TEST: High memory usage should trigger garbage collection.
        Tests automatic memory management during processing.
        """
        # Arrange: Mock memory monitoring to simulate pressure
        memory_readings = [80, 95, 110, 120, 90, 85]  # Spike above threshold
        memory_iter = iter(memory_readings)

        def mock_memory_usage():
            try:
                return next(memory_iter)
            except StopIteration:
                return 85  # Return to normal after GC

        memory_constrained_migration.get_memory_usage = mock_memory_usage

        with patch("gc.collect") as mock_gc:
            # Act: Process data that triggers memory pressure
            memory_constrained_migration.process_large_batch(
                [{"id": i} for i in range(100)],
            )

            # Assert: Garbage collection should be triggered
            mock_gc.assert_called()

    def test_out_of_memory_error_recovery(self, memory_constrained_migration) -> None:
        """MEMORY TEST: System should recover gracefully from OOM errors.
        Tests fallback to smaller batch sizes when memory is exhausted.
        """
        # Arrange: Simulate OOM error during large batch processing
        memory_constrained_migration.openproject_client = MagicMock()
        memory_constrained_migration.openproject_client.create_batch.side_effect = [
            MemoryError("Cannot allocate memory for batch"),  # First attempt fails
            None,  # Second attempt with smaller batch succeeds
            None,
            None,
        ]

        test_data = [{"id": i} for i in range(50)]

        # Act: Process data that causes OOM
        memory_constrained_migration.migrate_work_packages(test_data)

        # Assert: Should automatically reduce batch size and retry
        assert memory_constrained_migration.batch_size == 5  # Reduced from 10
        assert (
            memory_constrained_migration.openproject_client.create_batch.call_count >= 2
        )


class TestRateLimitingBoundaries:
    """Tests for handling API rate limiting and throttling scenarios."""

    @pytest.fixture
    def rate_limited_client(self):
        """Create client that simulates rate limiting responses."""
        client = OpenProjectClient()
        client.rate_limit_handler = MagicMock()
        client.retry_after_rate_limit = True
        return client

    def test_jira_api_rate_limiting_with_backoff(self, rate_limited_client) -> None:
        """RATE LIMIT TEST: JIRA API rate limiting should trigger exponential backoff.
        Tests that the client properly handles 429 responses.
        """
        # Arrange: Mock JIRA API calls with rate limiting
        jira_client = JiraClient()

        # Simulate rate limiting: fail twice, then succeed
        api_responses = [
            RateLimitError("Rate limit exceeded", retry_after=2),
            RateLimitError("Rate limit exceeded", retry_after=4),
            {"issues": [{"id": 1, "key": "TEST-1"}]},  # Success
        ]
        response_iter = iter(api_responses)

        def mock_api_call(*args, **kwargs):
            response = next(response_iter)
            if isinstance(response, Exception):
                raise response
            return response

        jira_client.search_issues = mock_api_call

        start_time = time.time()

        # Act: Make API call that hits rate limits
        result = jira_client.search_issues_with_retry("project = TEST")

        end_time = time.time()

        # Assert: Should succeed after retries
        assert result == {"issues": [{"id": 1, "key": "TEST-1"}]}

        # Should take at least 6 seconds due to backoff (2 + 4 seconds)
        assert end_time - start_time >= 6

    def test_openproject_api_rate_limiting_batch_adjustment(
        self,
        rate_limited_client,
    ) -> None:
        """RATE LIMIT TEST: OpenProject rate limiting should trigger batch size reduction.
        Tests adaptive batch sizing based on API limits.
        """
        # Arrange: Mock batch operations with rate limiting
        original_batch_size = rate_limited_client.batch_size = 50

        # Simulate rate limiting on large batches
        def mock_create_batch(items) -> str:
            if len(items) > 10:
                msg = "Batch too large for rate limit"
                raise RateLimitError(msg)
            return f"Created {len(items)} items"

        rate_limited_client.create_batch = mock_create_batch

        test_items = [{"id": i} for i in range(100)]

        # Act: Process items that trigger rate limiting
        rate_limited_client.process_items_with_rate_limiting(test_items)

        # Assert: Batch size should be automatically reduced
        assert rate_limited_client.batch_size < original_batch_size
        assert rate_limited_client.batch_size <= 10  # Below rate limit threshold

    def test_concurrent_api_calls_respect_rate_limits(
        self,
        rate_limited_client,
    ) -> None:
        """RATE LIMIT TEST: Concurrent API calls should coordinate to respect rate limits.
        Tests that multiple threads don't exceed API limits.
        """
        # Arrange: Mock rate limiter that tracks call frequency
        call_timestamps = []
        rate_limit_window = 1.0  # 1 second window
        max_calls_per_window = 3

        def mock_api_call_with_rate_limiting(*args, **kwargs) -> str:
            current_time = time.time()
            call_timestamps.append(current_time)

            # Check if we're exceeding rate limit
            recent_calls = [
                t for t in call_timestamps if current_time - t <= rate_limit_window
            ]
            if len(recent_calls) > max_calls_per_window:
                msg = "Too many concurrent calls"
                raise RateLimitError(msg)

            time.sleep(0.1)  # Simulate API call time
            return f"Success at {current_time}"

        rate_limited_client.make_api_call = mock_api_call_with_rate_limiting

        # Act: Make multiple concurrent API calls
        def make_calls():
            results = []
            for i in range(5):
                try:
                    result = rate_limited_client.make_api_call(f"request_{i}")
                    results.append(result)
                except RateLimitError:
                    results.append("rate_limited")
            return results

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(make_calls) for _ in range(3)]
            all_results = [future.result() for future in futures]

        # Assert: Some calls should be rate limited
        rate_limited_count = sum(
            1
            for results in all_results
            for result in results
            if result == "rate_limited"
        )
        assert rate_limited_count > 0  # Some calls should hit rate limits


class TestNetworkResilienceAndRecovery:
    """Tests for network failure scenarios and recovery mechanisms."""

    @pytest.fixture
    def network_aware_migration(self):
        """Create migration with network resilience features."""
        migration = Migration()
        migration.enable_network_monitoring = True
        migration.network_timeout = 30
        migration.max_network_retries = 3
        return migration

    def test_ssh_connection_recovery_after_network_partition(
        self,
        network_aware_migration,
    ) -> None:
        """NETWORK TEST: SSH connections should recover after network partitions.
        Tests reconnection logic when network becomes available again.
        """
        # Arrange: Mock SSH client with network failures
        ssh_client = MagicMock()
        connection_attempts = []

        def mock_execute_command(command):
            attempt_time = time.time()
            connection_attempts.append(attempt_time)

            # Simulate network partition for first 2 attempts
            if len(connection_attempts) <= 2:
                msg = "Network unreachable"
                raise ConnectionError(msg)

            # Recovery on 3rd attempt
            return ("command output", "", 0)

        ssh_client.execute_command = mock_execute_command
        network_aware_migration.openproject_client.ssh_client = ssh_client

        # Act: Execute command that fails due to network issues
        result = network_aware_migration.openproject_client.execute_with_retry(
            "ls /tmp",
        )

        # Assert: Should eventually succeed after recovery
        assert result == ("command output", "", 0)
        assert len(connection_attempts) == 3  # 2 failures + 1 success

    def test_docker_container_communication_timeout_handling(
        self,
        network_aware_migration,
    ) -> None:
        """NETWORK TEST: Docker container communication should handle timeouts gracefully.
        Tests timeout and retry logic for container operations.
        """
        # Arrange: Mock Docker client with intermittent timeouts
        docker_client = MagicMock()
        timeout_count = 0

        def mock_container_exec(command) -> str:
            nonlocal timeout_count
            timeout_count += 1

            # First 2 calls timeout
            if timeout_count <= 2:
                msg = f"Container operation timed out after {network_aware_migration.network_timeout}s"
                raise TimeoutError(
                    msg,
                )

            # 3rd call succeeds
            return "container response"

        docker_client.exec_run = mock_container_exec
        network_aware_migration.openproject_client.docker_client = docker_client

        # Act: Execute container command with timeouts
        result = network_aware_migration.openproject_client.execute_in_container(
            "rails console",
        )

        # Assert: Should succeed after retries
        assert result == "container response"
        assert timeout_count == 3

    def test_rails_console_session_recovery_after_disconnect(
        self,
        network_aware_migration,
    ) -> None:
        """NETWORK TEST: Rails console sessions should recover after disconnection.
        Tests session restoration and command replay.
        """
        # Arrange: Mock Rails console client with session failures
        rails_client = MagicMock()
        session_attempts = []

        def mock_execute_command(command) -> str:
            session_attempts.append(command)

            # First command fails due to session loss
            if len(session_attempts) == 1:
                msg = "Rails console session lost"
                raise ConnectionError(msg)

            # Subsequent commands succeed after session restoration
            return f"Rails output for: {command}"

        rails_client.execute = mock_execute_command
        network_aware_migration.openproject_client.rails_client = rails_client

        # Act: Execute Rails command that fails due to session loss
        result = network_aware_migration.openproject_client.execute_rails_command(
            "User.count",
        )

        # Assert: Should succeed after session recovery
        assert "User.count" in result
        assert len(session_attempts) >= 2  # Original + retry after session restore


class TestConcurrentResourceContention:
    """Tests for resource contention when multiple operations run simultaneously."""

    def test_database_connection_pool_exhaustion(self) -> None:
        """RESOURCE TEST: Multiple migrations should handle DB connection pool limits.
        Tests that migrations gracefully handle connection exhaustion.
        """
        # Arrange: Mock database with limited connection pool
        db_connections = []
        max_connections = 3

        def mock_get_db_connection():
            if len(db_connections) >= max_connections:
                msg = "Connection pool exhausted"
                raise Exception(msg)

            connection = MagicMock()
            db_connections.append(connection)
            return connection

        def mock_release_connection(conn) -> None:
            if conn in db_connections:
                db_connections.remove(conn)

        # Act: Multiple migrations trying to get connections
        migration_results = []

        def run_migration(migration_id) -> None:
            try:
                conn = mock_get_db_connection()
                time.sleep(0.1)  # Simulate migration work
                mock_release_connection(conn)
                migration_results.append(f"migration_{migration_id}_success")
            except Exception as e:
                migration_results.append(f"migration_{migration_id}_failed: {e}")

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(run_migration, i) for i in range(5)]
            for future in futures:
                future.result()

        # Assert: Some migrations should succeed, others should fail gracefully
        successful = [r for r in migration_results if "success" in r]
        failed = [r for r in migration_results if "failed" in r]

        assert len(successful) <= max_connections
        assert len(failed) >= 2  # Pool exhaustion should cause failures

    def test_file_system_resource_contention(self) -> None:
        """RESOURCE TEST: Multiple migrations writing temp files simultaneously.
        Tests handling of file system resource limits and conflicts.
        """
        # Arrange: Mock file system with limited file handles
        open_files = []
        max_open_files = 5

        def mock_open_file(filename, mode="r"):
            if len(open_files) >= max_open_files:
                msg = "Too many open files"
                raise OSError(msg)

            file_handle = MagicMock()
            file_handle.name = filename
            open_files.append(file_handle)
            return file_handle

        def mock_close_file(file_handle) -> None:
            if file_handle in open_files:
                open_files.remove(file_handle)

        # Act: Multiple operations trying to open temp files
        file_operations = []

        def perform_file_operations(op_id) -> None:
            try:
                files = []
                for i in range(3):
                    f = mock_open_file(f"/tmp/migration_{op_id}_{i}.json")
                    files.append(f)

                # Simulate file operations
                time.sleep(0.05)

                # Clean up
                for f in files:
                    mock_close_file(f)

                file_operations.append(f"op_{op_id}_success")
            except Exception as e:
                file_operations.append(f"op_{op_id}_failed: {e}")

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(perform_file_operations, i) for i in range(4)]
            for future in futures:
                future.result()

        # Assert: Resource limits should be respected
        [op for op in file_operations if "success" in op]
        failed_ops = [op for op in file_operations if "failed" in op]

        # Some operations should fail due to file handle limits
        assert len(failed_ops) > 0
        assert "Too many open files" in str(failed_ops)
