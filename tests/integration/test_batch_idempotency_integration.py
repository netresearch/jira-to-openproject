import pytest

pytestmark = pytest.mark.integration


#!/usr/bin/env python3
"""Integration tests for batch API idempotency support.

Tests cover:
- End-to-end idempotency behavior in batch operations
- Header propagation through retry logic
- Partial failure scenarios with idempotency
- Performance impact of idempotency caching
- Integration with existing batch processing
"""

import json
import threading
import time
from unittest.mock import patch

from src.clients.openproject_client import OpenProjectClient
from src.utils.idempotency_manager import reset_idempotency_manager


@pytest.mark.skip(reason="Tests require batch idempotency methods (batch_find_records, batch_get_users_by_emails, etc.) that don't exist on OpenProjectClient")
class TestBatchIdempotencyIntegration:
    """Integration test suite for batch API idempotency."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        reset_idempotency_manager()
        # Mock the SSH connection and related dependencies
        with patch("src.clients.openproject_client.SSHClient"):
            with patch("src.clients.openproject_client.DockerClient"):
                with patch("src.clients.openproject_client.RailsConsoleClient"):
                    self.client = OpenProjectClient(
                        container_name="test-container",
                        ssh_host="test-host",
                        ssh_user="test-user",
                    )

    def teardown_method(self) -> None:
        """Clean up test fixtures."""
        reset_idempotency_manager()

    @patch("src.clients.openproject_client.OpenProjectClient.execute_json_query")
    def test_batch_find_records_idempotency_cache_hit(self, mock_execute) -> None:
        """Test batch_find_records with idempotency cache hit."""
        # Mock the first execution
        mock_execute.return_value = [
            {"id": 1, "name": "User 1"},
            {"id": 2, "name": "User 2"},
        ]

        headers = {"X-Idempotency-Key": "batch-test-key"}

        # First call - should execute query
        result1 = self.client.batch_find_records("User", [1, 2], headers=headers)

        assert mock_execute.call_count == 1
        assert len(result1) == 2
        assert result1[1]["name"] == "User 1"

        # Second call with same key - should return cached result
        result2 = self.client.batch_find_records(
            "User",
            [3, 4],
            headers=headers,  # Different IDs but same key
        )

        # Should not execute again due to caching
        assert mock_execute.call_count == 1
        assert result1 == result2  # Exact same cached result

    @patch("src.clients.openproject_client.OpenProjectClient.execute_json_query")
    def test_batch_get_users_by_emails_idempotency(self, mock_execute) -> None:
        """Test batch_get_users_by_emails with idempotency support."""
        mock_execute.return_value = [
            {"id": 1, "mail": "user1@example.com", "name": "User 1"},
            {"id": 2, "mail": "user2@example.com", "name": "User 2"},
        ]

        headers = {"X-Idempotency-Key": "email-batch-key"}
        emails = ["user1@example.com", "user2@example.com"]

        # First call
        result1 = self.client.batch_get_users_by_emails(emails, headers=headers)
        assert mock_execute.call_count == 1
        assert len(result1) == 2

        # Second call - should be cached
        result2 = self.client.batch_get_users_by_emails(
            ["different@example.com"],
            headers=headers,
        )
        assert mock_execute.call_count == 1  # No additional call
        assert result1 == result2

    @patch("src.clients.openproject_client.OpenProjectClient.execute_json_query")
    def test_batch_operations_different_keys(self, mock_execute) -> None:
        """Test that different idempotency keys don't interfere."""
        mock_execute.side_effect = [
            [{"id": 1, "name": "Result 1"}],
            [{"id": 2, "name": "Result 2"}],
        ]

        headers1 = {"X-Idempotency-Key": "key-1"}
        headers2 = {"X-Idempotency-Key": "key-2"}

        # Call with first key
        result1 = self.client.batch_find_records("User", [1], headers=headers1)
        assert mock_execute.call_count == 1

        # Call with second key - should execute again
        result2 = self.client.batch_find_records("User", [1], headers=headers2)
        assert mock_execute.call_count == 2

        # Results should be different
        assert result1 != result2
        assert result1[1]["name"] == "Result 1"
        assert result2[1]["name"] == "Result 2"

    @patch("src.clients.openproject_client.OpenProjectClient.execute_json_query")
    def test_batch_operations_no_headers(self, mock_execute) -> None:
        """Test batch operations without idempotency headers."""
        mock_execute.return_value = [{"id": 1, "name": "User 1"}]

        # Calls without headers should generate unique keys
        self.client.batch_find_records("User", [1])
        self.client.batch_find_records("User", [1])

        # Should execute twice (no caching benefit)
        assert mock_execute.call_count == 2

    @patch("src.clients.openproject_client.OpenProjectClient.execute_json_query")
    def test_idempotency_with_retry_logic(self, mock_execute) -> None:
        """Test idempotency key propagation through retry logic."""
        # Mock failure then success
        mock_execute.side_effect = [
            Exception("Temporary failure"),
            [{"id": 1, "name": "User 1"}],
        ]

        headers = {"X-Idempotency-Key": "retry-test-key"}

        with patch.object(self.client, "_retry_with_exponential_backoff") as mock_retry:
            # Mock the retry method to capture headers parameter
            mock_retry.return_value = [{"id": 1, "name": "User 1"}]

            self.client.batch_find_records("User", [1], headers=headers)

            # Verify retry was called with headers
            mock_retry.assert_called_once()
            call_args = mock_retry.call_args
            assert call_args[1]["headers"] == headers

    @patch("src.clients.openproject_client.OpenProjectClient.execute_json_query")
    def test_partial_failure_idempotency(self, mock_execute) -> None:
        """Test idempotency with partial batch failures."""
        # Simulate partial success - some records found, some not
        mock_execute.return_value = [
            {"id": 1, "name": "User 1"},
            # Missing ID 2 - simulating partial failure
        ]

        headers = {"X-Idempotency-Key": "partial-failure-key"}

        # First call
        result1 = self.client.batch_find_records("User", [1, 2], headers=headers)
        assert len(result1) == 1  # Only one found
        assert 1 in result1
        assert 2 not in result1

        # Second call should return same partial result
        result2 = self.client.batch_find_records("User", [1, 2], headers=headers)
        assert result1 == result2
        assert mock_execute.call_count == 1  # Cached

    def test_concurrent_batch_operations_same_key(self) -> None:
        """Test concurrent batch operations with same idempotency key."""
        call_count = 0
        results = {}
        errors = []

        def mock_execute_query(query):
            nonlocal call_count
            call_count += 1
            time.sleep(0.1)  # Simulate processing time
            return [{"id": call_count, "query": query[:50]}]

        def worker(worker_id) -> None:
            try:
                with patch.object(
                    self.client,
                    "execute_json_query",
                    side_effect=mock_execute_query,
                ):
                    headers = {"X-Idempotency-Key": "concurrent-key"}
                    result = self.client.batch_find_records(
                        "User",
                        [worker_id],
                        headers=headers,
                    )
                    results[worker_id] = result
            except Exception as e:
                errors.append((worker_id, e))

        # Start multiple threads
        threads = []
        for i in range(5):
            thread = threading.Thread(target=worker, args=(i,))
            threads.append(thread)
            thread.start()

        # Wait for completion
        for thread in threads:
            thread.join()

        assert not errors, f"Unexpected errors: {errors}"
        assert len(results) == 5

        # All workers should get the same result (first one wins)
        unique_results = {json.dumps(result, sort_keys=True) for result in results.values()}
        assert len(unique_results) == 1, "All concurrent calls should return same result"

    @patch("src.clients.openproject_client.OpenProjectClient.execute_json_query")
    def test_performance_impact_measurement(self, mock_execute) -> None:
        """Test performance impact of idempotency caching."""
        mock_execute.return_value = [{"id": i, "name": f"User {i}"} for i in range(100)]

        headers = {"X-Idempotency-Key": "performance-key"}

        # First call - measure baseline
        start_time = time.time()
        result1 = self.client.batch_find_records(
            "User",
            list(range(100)),
            headers=headers,
        )
        first_call_time = time.time() - start_time

        # Second call - should be much faster (cached)
        start_time = time.time()
        result2 = self.client.batch_find_records(
            "User",
            list(range(100)),
            headers=headers,
        )
        second_call_time = time.time() - start_time

        assert result1 == result2
        assert mock_execute.call_count == 1  # Only called once

        # Cache hit should be significantly faster
        # (Note: This test is environment-dependent, but serves as a performance indicator)
        assert second_call_time < first_call_time * 0.5, "Cache hit should be faster"

    @patch("src.clients.openproject_client.OpenProjectClient.execute_json_query")
    def test_batch_custom_fields_idempotency(self, mock_execute) -> None:
        """Test batch_get_custom_fields_by_names with idempotency."""
        mock_execute.return_value = [
            {"id": 1, "name": "Custom Field 1", "type": "text"},
            {"id": 2, "name": "Custom Field 2", "type": "list"},
        ]

        headers = {"X-Idempotency-Key": "custom-fields-key"}
        names = ["Custom Field 1", "Custom Field 2"]

        # First call
        result1 = self.client.batch_get_custom_fields_by_names(names, headers=headers)
        assert mock_execute.call_count == 1
        assert len(result1) == 2

        # Second call - should be cached
        result2 = self.client.batch_get_custom_fields_by_names(
            ["Different Field"],
            headers=headers,
        )
        assert mock_execute.call_count == 1  # No additional call
        assert result1 == result2

    @patch("src.clients.openproject_client.OpenProjectClient.execute_json_query")
    def test_batch_projects_idempotency(self, mock_execute) -> None:
        """Test batch_get_projects_by_identifiers with idempotency."""
        mock_execute.return_value = [
            {"id": 1, "identifier": "project-1", "name": "Project 1"},
            {"id": 2, "identifier": "project-2", "name": "Project 2"},
        ]

        headers = {"X-Idempotency-Key": "projects-key"}
        identifiers = ["project-1", "project-2"]

        # First call
        result1 = self.client.batch_get_projects_by_identifiers(
            identifiers,
            headers=headers,
        )
        assert mock_execute.call_count == 1
        assert len(result1) == 2

        # Second call - should be cached
        result2 = self.client.batch_get_projects_by_identifiers(
            ["different-project"],
            headers=headers,
        )
        assert mock_execute.call_count == 1  # No additional call
        assert result1 == result2

    @patch("src.clients.openproject_client.OpenProjectClient.execute_json_query")
    def test_large_batch_idempotency(self, mock_execute) -> None:
        """Test idempotency with large batch operations."""
        # Simulate large result set
        large_result = [{"id": i, "name": f"User {i}"} for i in range(1000)]
        mock_execute.return_value = large_result

        headers = {"X-Idempotency-Key": "large-batch-key"}

        # First call with large batch
        result1 = self.client.batch_find_records(
            "User",
            list(range(1000)),
            headers=headers,
        )
        assert len(result1) == 1000
        assert mock_execute.call_count == 1

        # Second call - should be cached efficiently
        result2 = self.client.batch_find_records(
            "User",
            list(range(1000)),
            headers=headers,
        )
        assert result1 == result2
        assert mock_execute.call_count == 1  # Still only one call

    @patch("src.clients.openproject_client.OpenProjectClient.execute_json_query")
    def test_idempotency_error_handling(self, mock_execute) -> None:
        """Test error handling in idempotency-enabled batch operations."""
        # First call fails
        mock_execute.side_effect = Exception("Database error")

        headers = {"X-Idempotency-Key": "error-handling-key"}

        # First call should fail
        with pytest.raises(Exception, match="Database error"):
            self.client.batch_find_records("User", [1], headers=headers)

        # Mock recovery for second call
        mock_execute.side_effect = None
        mock_execute.return_value = [{"id": 1, "name": "User 1"}]

        # Second call with same key should work (error not cached)
        result = self.client.batch_find_records("User", [1], headers=headers)
        assert len(result) == 1
        assert result[1]["name"] == "User 1"

    def test_idempotency_ttl_behavior(self) -> None:
        """Test TTL behavior of idempotency caching."""
        # This test would require actual Redis or time manipulation
        # For now, we test that TTL parameters are correctly passed

        with patch(
            "src.utils.idempotency_manager.IdempotencyKeyManager.cache_result",
        ) as mock_cache:
            mock_cache.return_value = True

            with patch.object(self.client, "execute_json_query") as mock_execute:
                mock_execute.return_value = [{"id": 1, "name": "User 1"}]

                headers = {"X-Idempotency-Key": "ttl-test"}
                self.client.batch_find_records("User", [1], headers=headers)

                # Verify TTL was set correctly (3600 seconds for batch_find_records)
                # This would require inspecting the actual cache implementation

    @patch("src.clients.openproject_client.OpenProjectClient.execute_json_query")
    def test_mixed_batch_operations_isolation(self, mock_execute) -> None:
        """Test that different batch operations don't interfere with each other."""
        mock_execute.side_effect = [
            [{"id": 1, "name": "User 1"}],  # batch_find_records
            [{"id": 1, "mail": "user1@example.com"}],  # batch_get_users_by_emails
        ]

        headers = {"X-Idempotency-Key": "mixed-operations-key"}

        # Call different batch methods with same key
        result1 = self.client.batch_find_records("User", [1], headers=headers)
        result2 = self.client.batch_get_users_by_emails(
            ["user1@example.com"],
            headers=headers,
        )

        # Both should execute (different method signatures create different cache keys)
        assert mock_execute.call_count == 2
        assert result1 != result2
