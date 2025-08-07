#!/usr/bin/env python3
"""Comprehensive tests for idempotency decorators.

Tests cover:
- @batch_idempotent decorator behavior
- @api_idempotent decorator behavior
- Header extraction functions
- Result processors
- Decorator composition and edge cases
- Error handling and recovery
"""

import json
import time
from typing import Never
from unittest.mock import Mock, patch
from uuid import uuid4

from src.utils.idempotency_decorators import (
    api_idempotent,
    batch_idempotent,
    create_batch_result_processor,
    extract_headers_from_kwargs,
    extract_headers_from_request,
    simple_idempotent,
    with_idempotency,
)


class TestIdempotencyDecorators:
    """Test suite for idempotency decorators."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        # Reset any global state
        from src.utils.idempotency_manager import reset_idempotency_manager

        reset_idempotency_manager()

    def test_batch_idempotent_decorator_cache_miss(self) -> None:
        """Test @batch_idempotent decorator with cache miss."""
        call_count = 0

        @batch_idempotent()
        def test_function(data, headers=None):
            nonlocal call_count
            call_count += 1
            return {"processed": data, "count": call_count}

        # First call - should execute function
        valid_uuid = str(uuid4())
        result1 = test_function(
            ["item1", "item2"],
            headers={"X-Idempotency-Key": valid_uuid},
        )
        # The batch processor transforms the result
        assert result1["data"]["processed"] == ["item1", "item2"]
        assert result1["data"]["count"] == 1
        assert result1["success"]
        assert result1["idempotent"]
        assert call_count == 1

    def test_batch_idempotent_decorator_cache_hit(self) -> None:
        """Test @batch_idempotent decorator with cache hit."""
        call_count = 0

        @batch_idempotent()
        def test_function(data, headers=None):
            nonlocal call_count
            call_count += 1
            return {"processed": data, "count": call_count}

        valid_uuid = str(uuid4())
        headers = {"X-Idempotency-Key": valid_uuid}

        # First call - should execute function
        result1 = test_function(["item1"], headers=headers)
        assert call_count == 1

        # Second call with same key - should return cached result
        result2 = test_function(["different_item"], headers=headers)
        assert call_count == 1  # Function should not be called again

        # Results should be identical (cached)
        assert result1 == result2
        assert result2["data"]["processed"] == ["item1"]  # Original data, not new data

    def test_api_idempotent_flask_headers(self) -> None:
        """Test @api_idempotent decorator with Flask-style request."""
        call_count = 0

        @api_idempotent()
        def test_endpoint(request):
            nonlocal call_count
            call_count += 1
            return {"result": "success", "call": call_count}

        # Mock Flask request
        mock_request = Mock()
        valid_uuid = str(uuid4())
        mock_request.headers = {"X-Idempotency-Key": valid_uuid}

        # First call
        result1 = test_endpoint(mock_request)
        assert call_count == 1

        # Second call - should be cached
        result2 = test_endpoint(mock_request)
        assert call_count == 1  # No additional call
        assert result1 == result2

    def test_api_idempotent_django_headers(self) -> None:
        """Test @api_idempotent decorator with Django-style request."""
        call_count = 0

        @api_idempotent()
        def test_view(request):
            nonlocal call_count
            call_count += 1
            return {"result": "django", "call": call_count}

        # Mock Django request
        mock_request = Mock()
        valid_uuid = str(uuid4())
        mock_request.META = {"HTTP_X_IDEMPOTENCY_KEY": valid_uuid}

        # First call
        result1 = test_view(mock_request)
        assert call_count == 1

        # Second call - should be cached
        result2 = test_view(mock_request)
        assert call_count == 1  # No additional call
        assert result1 == result2

    def test_simple_idempotent_no_headers(self) -> None:
        """Test @simple_idempotent decorator without header extraction."""
        call_count = 0

        @simple_idempotent()
        def test_function(data):
            nonlocal call_count
            call_count += 1
            return {"processed": data, "count": call_count}

        # Each call should generate a new UUID, so no caching benefit
        result1 = test_function("data1")
        result2 = test_function("data2")

        assert call_count == 2  # Both calls executed
        assert result1["count"] == 1
        assert result2["count"] == 2

    def test_custom_header_extractor(self) -> None:
        """Test custom header extractor function."""

        def custom_extractor(*args, **kwargs):
            return kwargs.get("custom_headers", {})

        call_count = 0

        @with_idempotency(header_extractor=custom_extractor)
        def test_function(data, custom_headers=None):
            nonlocal call_count
            call_count += 1
            return {"processed": data, "count": call_count}

        valid_uuid = str(uuid4())
        headers = {"X-Idempotency-Key": valid_uuid}

        # First call
        result1 = test_function("data1", custom_headers=headers)
        assert call_count == 1

        # Second call - should be cached
        result2 = test_function("data2", custom_headers=headers)
        assert call_count == 1  # No additional call
        assert result1 == result2

    def test_custom_result_processor(self) -> None:
        """Test custom result processor function."""

        def custom_processor(result):
            return {
                "processed_result": result,
                "processed_at": "2024-01-01T00:00:00Z",
                "processor": "custom",
            }

        @with_idempotency(result_processor=custom_processor)
        def test_function(data, headers=None):
            return {"original": data}

        result = test_function("test", headers={"X-Idempotency-Key": "processor-test"})

        assert "processed_result" in result
        assert result["processed_result"]["original"] == "test"
        assert result["processor"] == "custom"

    def test_batch_result_processor(self) -> None:
        """Test batch result processor functionality."""
        processor = create_batch_result_processor()

        # Test successful result
        input_result = {"success": True, "data": [{"id": 1}, {"id": 2}], "errors": []}

        processed = processor(input_result)

        assert processed["success"] is True
        assert processed["data"] == [{"id": 1}, {"id": 2}]
        assert processed["errors"] == []
        assert processed["idempotent"] is True

    def test_batch_result_processor_with_errors(self) -> None:
        """Test batch result processor with errors."""
        processor = create_batch_result_processor()

        # Test result with errors
        input_result = {
            "success": False,
            "data": [{"id": 1}],
            "errors": ["Error processing item 2"],
        }

        processed = processor(input_result)

        assert processed["success"] is False
        assert processed["data"] == [{"id": 1}]
        assert processed["errors"] == ["Error processing item 2"]
        assert processed["idempotent"] is True

    def test_result_processor_error_handling(self) -> None:
        """Test error handling in result processors."""

        def failing_processor(result) -> Never:
            msg = "Processor failed"
            raise ValueError(msg)

        call_count = 0

        @with_idempotency(result_processor=failing_processor)
        def test_function(data, headers=None):
            nonlocal call_count
            call_count += 1
            return {"data": data, "count": call_count}

        # Should handle processor failure gracefully
        result = test_function("test", headers={"X-Idempotency-Key": "error-test"})

        assert call_count == 1
        assert result["data"] == "test"  # Original result returned

    def test_header_extractor_error_handling(self) -> None:
        """Test error handling in header extractors."""

        def failing_extractor(*args, **kwargs) -> Never:
            msg = "Header extraction failed"
            raise KeyError(msg)

        call_count = 0

        @with_idempotency(header_extractor=failing_extractor)
        def test_function(data):
            nonlocal call_count
            call_count += 1
            return {"data": data, "count": call_count}

        # Should handle extractor failure gracefully and generate UUID
        test_function("test1")
        test_function("test2")

        assert (
            call_count == 2
        )  # Both calls executed (no caching due to different UUIDs)

    def test_decorator_metadata(self) -> None:
        """Test that decorators preserve function metadata."""

        @batch_idempotent()
        def original_function(data, headers=None):
            """Original function docstring."""
            return data

        assert hasattr(original_function, "_has_idempotency")
        assert original_function._has_idempotency is True
        assert hasattr(original_function, "_original_function")
        assert original_function.__name__ == "original_function"

    def test_custom_ttl(self) -> None:
        """Test custom TTL configuration."""

        @batch_idempotent(ttl=1800)  # 30 minutes
        def test_function(data, headers=None):
            return {"data": data, "timestamp": time.time()}

        # Function should work normally (TTL testing requires time-based verification)
        result = test_function("test", headers={"X-Idempotency-Key": "ttl-test"})
        assert result["data"] == "test"

    def test_extract_headers_from_kwargs_default(self) -> None:
        """Test extract_headers_from_kwargs with default key."""
        extractor = extract_headers_from_kwargs()

        headers = {"X-Idempotency-Key": "test"}
        result = extractor(arg1="value", headers=headers)

        assert result == headers

    def test_extract_headers_from_kwargs_custom_key(self) -> None:
        """Test extract_headers_from_kwargs with custom key."""
        extractor = extract_headers_from_kwargs(key="request_headers")

        headers = {"X-Idempotency-Key": "custom"}
        result = extractor(arg1="value", request_headers=headers)

        assert result == headers

    def test_extract_headers_from_kwargs_missing(self) -> None:
        """Test extract_headers_from_kwargs with missing headers."""
        extractor = extract_headers_from_kwargs()

        result = extractor(arg1="value", other_arg="other")

        assert result == {}

    def test_extract_headers_from_request_no_request(self) -> None:
        """Test extract_headers_from_request with no request object."""
        extractor = extract_headers_from_request()

        result = extractor("not_a_request", other_arg="value")

        assert result == {}

    def test_extract_headers_from_request_flask_style(self) -> None:
        """Test extract_headers_from_request with Flask request."""
        extractor = extract_headers_from_request()

        mock_request = Mock()
        mock_request.headers = {
            "X-Idempotency-Key": "flask",
            "Content-Type": "application/json",
        }

        result = extractor(mock_request)

        assert "X-Idempotency-Key" in result
        assert result["X-Idempotency-Key"] == "flask"

    def test_extract_headers_from_request_django_style(self) -> None:
        """Test extract_headers_from_request with Django request."""
        extractor = extract_headers_from_request()

        mock_request = Mock()
        mock_request.META = {
            "HTTP_X_IDEMPOTENCY_KEY": "django",
            "HTTP_CONTENT_TYPE": "application/json",
            "SERVER_NAME": "localhost",  # Non-HTTP header
        }

        result = extractor(mock_request)

        assert "X-Idempotency-Key" in result
        assert result["X-Idempotency-Key"] == "django"
        assert "Content-Type" in result
        assert "Server-Name" not in result  # Non-HTTP headers should be filtered

    def test_nested_decorator_calls(self) -> None:
        """Test behavior with nested decorated function calls."""
        call_order = []

        @batch_idempotent()
        def outer_function(data, headers=None):
            call_order.append("outer")
            return inner_function(data + "_inner", headers=headers)

        @batch_idempotent()
        def inner_function(data, headers=None):
            call_order.append("inner")
            return {"result": data}

        valid_uuid = str(uuid4())
        headers = {"X-Idempotency-Key": valid_uuid}

        # First call
        result1 = outer_function("test", headers=headers)
        assert call_order == ["outer", "inner"]

        # Second call - outer should be cached
        call_order.clear()
        result2 = outer_function("test", headers=headers)
        assert call_order == []  # No calls should be made
        assert result1 == result2

    def test_concurrent_decorator_access(self) -> None:
        """Test concurrent access to decorated functions."""
        import threading

        call_counts = {"count": 0}
        results = {}
        errors = []

        @batch_idempotent()
        def concurrent_function(data, headers=None):
            call_counts["count"] += 1
            time.sleep(0.1)  # Simulate some work
            return {"data": data, "count": call_counts["count"]}

        # Use the same idempotency key for all workers to test caching
        shared_uuid = str(uuid4())

        def worker(worker_id) -> None:
            try:
                headers = {"X-Idempotency-Key": shared_uuid}
                # Use the same data for all workers to test idempotency
                result = concurrent_function("shared-data", headers=headers)
                results[worker_id] = result
            except Exception as e:
                errors.append((worker_id, e))

        # Start multiple threads with same idempotency key
        threads = []
        for i in range(5):
            thread = threading.Thread(target=worker, args=(i,))
            threads.append(thread)
            thread.start()

        # Wait for all threads
        for thread in threads:
            thread.join()

        assert not errors, f"Unexpected errors: {errors}"
        assert len(results) == 5

        # Check that the function was only called once due to idempotency
        assert (
            call_counts["count"] == 1
        ), f"Function should only be called once, but was called {call_counts['count']} times"

        # All should get the same result due to idempotency
        unique_results = {
            json.dumps(result, sort_keys=True) for result in results.values()
        }
        assert (
            len(unique_results) == 1
        ), "All concurrent calls should return same result"

    def test_decorator_with_no_idempotency_manager(self) -> None:
        """Test decorator behavior when idempotency manager fails to initialize."""
        with patch(
            "src.utils.idempotency_decorators.get_idempotency_manager",
        ) as mock_get_manager:
            mock_get_manager.side_effect = Exception("Manager initialization failed")

            @batch_idempotent()
            def test_function(data, headers=None):
                return {"data": data}

            # Should handle initialization failure gracefully
            valid_uuid = str(uuid4())
            result = test_function("test", headers={"X-Idempotency-Key": valid_uuid})
            assert result["data"] == "test"

    def test_result_serialization_in_decorator(self) -> None:
        """Test handling of complex result types in decorators."""

        @batch_idempotent()
        def complex_result_function(data, headers=None):
            return {
                "data": data,
                "timestamp": time.time(),
                "complex": {"nested": {"data": [1, 2, 3]}},
                "none_value": None,
                "boolean": True,
            }

        valid_uuid = str(uuid4())
        headers = {"X-Idempotency-Key": valid_uuid}

        result1 = complex_result_function("test", headers=headers)
        result2 = complex_result_function("different", headers=headers)

        # Should get exact same cached result
        assert result1 == result2
        assert result2["data"] == "test"  # Original data, not "different"
