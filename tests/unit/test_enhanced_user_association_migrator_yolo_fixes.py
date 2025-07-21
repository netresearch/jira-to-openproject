#!/usr/bin/env python3
"""Tests for YOLO fixes in Enhanced User Association Migrator.

This module tests the YOLO improvements applied to the migrator including:
- Defensive metrics collection (_safe_metrics_increment)
- JSON serialization of complex objects (_make_json_serializable) 
- Security-focused error message sanitization
- Enhanced exception handling patterns
"""

import json
import logging
import pytest
import threading
import time
from datetime import datetime, UTC
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch, call
from concurrent.futures import ThreadPoolExecutor

from src.utils.enhanced_user_association_migrator import EnhancedUserAssociationMigrator
from tests.utils.mock_factory import create_mock_jira_client, create_mock_openproject_client


class TestYoloMetricsHelpers:
    """Test suite for defensive metrics collection (_safe_metrics_increment)."""

    @pytest.fixture
    def migrator_instance(self):
        """Create migrator instance with mocked clients."""
        mock_jira_client = create_mock_jira_client()
        mock_op_client = create_mock_openproject_client()
        
        migrator = EnhancedUserAssociationMigrator(
            jira_client=mock_jira_client,
            op_client=mock_op_client
        )
        
        # Mock config to prevent AttributeError
        migrator.config = Mock()
        migrator.config.get_path.return_value = Path("/tmp/test")
        
        return migrator

    def test_safe_metrics_increment_with_none_collector(self, migrator_instance, caplog):
        """Test _safe_metrics_increment when metrics_collector is None."""
        migrator_instance.metrics_collector = None
        
        # Should not raise exception
        migrator_instance._safe_metrics_increment("test_counter", {"tag": "value"})
        
        # Should not log any failure messages
        assert "Metrics collection failed" not in caplog.text

    def test_safe_metrics_increment_missing_collector_attribute(self, migrator_instance, caplog):
        """Test _safe_metrics_increment when metrics_collector attribute is missing."""
        # Remove the attribute entirely
        if hasattr(migrator_instance, 'metrics_collector'):
            delattr(migrator_instance, 'metrics_collector')
        
        # Should not raise exception
        migrator_instance._safe_metrics_increment("test_counter", {"tag": "value"})
        
        # Should not log any failure messages
        assert "Metrics collection failed" not in caplog.text

    def test_safe_metrics_increment_failing_collector(self, migrator_instance, caplog):
        """Test _safe_metrics_increment when metrics collector raises exception."""
        mock_collector = Mock()
        mock_collector.increment_counter.side_effect = Exception("Metrics service down")
        migrator_instance.metrics_collector = mock_collector
        
        # Set log level to DEBUG to capture the debug message
        caplog.set_level(logging.DEBUG)
        
        # Should not raise exception
        migrator_instance._safe_metrics_increment("test_counter", {"tag": "value"})
        
        # Should log debug message about failure
        assert "Metrics collection failed for test_counter" in caplog.text
        assert "Metrics service down" in caplog.text

    def test_safe_metrics_increment_successful_call(self, migrator_instance):
        """Test _safe_metrics_increment with successful metrics collection."""
        mock_collector = Mock()
        migrator_instance.metrics_collector = mock_collector
        
        migrator_instance._safe_metrics_increment("staleness_detected_total", 
                                                {"reason": "expired", "username": "testuser"})
        
        mock_collector.increment_counter.assert_called_once_with(
            "staleness_detected_total", 
            tags={"reason": "expired", "username": "testuser"}
        )

    def test_safe_metrics_increment_with_none_tags(self, migrator_instance):
        """Test _safe_metrics_increment with None tags (should default to empty dict)."""
        mock_collector = Mock()
        migrator_instance.metrics_collector = mock_collector
        
        migrator_instance._safe_metrics_increment("test_counter", None)
        
        mock_collector.increment_counter.assert_called_once_with("test_counter", tags={})

    def test_safe_metrics_increment_preserves_debug_level(self, migrator_instance, caplog):
        """Test that metrics failures are logged at DEBUG level only."""
        mock_collector = Mock()
        mock_collector.increment_counter.side_effect = RuntimeError("Network error")
        migrator_instance.metrics_collector = mock_collector
        
        # Set log level to DEBUG to capture the message
        caplog.set_level(logging.DEBUG)
        
        migrator_instance._safe_metrics_increment("test_counter")
        
        # Verify debug level logging
        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("Metrics collection failed" in r.message for r in debug_records)


class TestYoloJsonSerialization:
    """Test suite for JSON serialization of complex objects (_make_json_serializable)."""

    @pytest.fixture
    def migrator_instance(self):
        """Create migrator instance for testing."""
        return EnhancedUserAssociationMigrator(
            jira_client=create_mock_jira_client(),
            op_client=create_mock_openproject_client()
        )

    def test_make_json_serializable_basic_types(self, migrator_instance):
        """Test _make_json_serializable with basic JSON-compatible types."""
        test_cases = [
            ("string", "string"),
            (42, 42),
            (3.14, 3.14),
            (True, True),
            (False, False),
            (None, None)
        ]
        
        for input_val, expected in test_cases:
            result = migrator_instance._make_json_serializable(input_val)
            assert result == expected

    def test_make_json_serializable_mock_objects_with_name(self, migrator_instance):
        """Test _make_json_serializable with Mock objects that have _mock_name."""
        mock_obj = Mock()
        mock_obj._mock_name = "test_mock"
        
        result = migrator_instance._make_json_serializable(mock_obj)
        assert result == "<Mock: test_mock>"

    def test_make_json_serializable_mock_objects_without_name(self, migrator_instance):
        """Test _make_json_serializable with Mock objects without _mock_name."""
        mock_obj = Mock()
        if hasattr(mock_obj, '_mock_name'):
            delattr(mock_obj, '_mock_name')
        
        result = migrator_instance._make_json_serializable(mock_obj)
        # The actual implementation returns the type name when _mock_name is missing
        assert result == "<Mock: Mock>"

    def test_make_json_serializable_magicmock_objects(self, migrator_instance):
        """Test _make_json_serializable with MagicMock objects."""
        mock_obj = MagicMock()
        
        result = migrator_instance._make_json_serializable(mock_obj)
        # Should detect it's a mock and handle appropriately
        assert result.startswith("<Mock:")

    def test_make_json_serializable_datetime_objects(self, migrator_instance):
        """Test _make_json_serializable with datetime objects."""
        dt = datetime(2023, 1, 15, 14, 30, 45, tzinfo=UTC)
        
        result = migrator_instance._make_json_serializable(dt)
        assert result == "2023-01-15T14:30:45+00:00"

    def test_make_json_serializable_path_objects(self, migrator_instance):
        """Test _make_json_serializable with Path objects."""
        path_obj = Path("/tmp/test/file.json")
        
        result = migrator_instance._make_json_serializable(path_obj)
        assert result == "/tmp/test/file.json"

    def test_make_json_serializable_nested_dict(self, migrator_instance):
        """Test _make_json_serializable with nested dictionaries containing complex objects."""
        mock_user = Mock()
        mock_user._mock_name = "jira_user"
        
        complex_dict = {
            "timestamp": datetime(2023, 1, 15, tzinfo=UTC),
            "user": mock_user,
            "path": Path("/tmp/test"),
            "nested": {
                "more_data": mock_user,
                "number": 42
            }
        }
        
        result = migrator_instance._make_json_serializable(complex_dict)
        
        expected = {
            "timestamp": "2023-01-15T00:00:00+00:00",
            "user": "<Mock: jira_user>",
            "path": "/tmp/test",
            "nested": {
                "more_data": "<Mock: jira_user>",
                "number": 42
            }
        }
        
        assert result == expected

    def test_make_json_serializable_nested_list(self, migrator_instance):
        """Test _make_json_serializable with lists containing complex objects."""
        mock_obj = Mock()
        mock_obj._mock_name = "list_mock"
        
        complex_list = [
            datetime(2023, 1, 15, tzinfo=UTC),
            mock_obj,
            {"nested": mock_obj},
            [mock_obj, "string"]
        ]
        
        result = migrator_instance._make_json_serializable(complex_list)
        
        expected = [
            "2023-01-15T00:00:00+00:00",
            "<Mock: list_mock>",
            {"nested": "<Mock: list_mock>"},
            ["<Mock: list_mock>", "string"]
        ]
        
        assert result == expected

    def test_make_json_serializable_tuple_handling(self, migrator_instance):
        """Test _make_json_serializable with tuples (should convert to lists)."""
        mock_obj = Mock()
        mock_obj._mock_name = "tuple_mock"
        
        test_tuple = (datetime(2023, 1, 15, tzinfo=UTC), mock_obj, "string")
        
        result = migrator_instance._make_json_serializable(test_tuple)
        
        expected = ["2023-01-15T00:00:00+00:00", "<Mock: tuple_mock>", "string"]
        assert result == expected

    def test_make_json_serializable_unknown_objects(self, migrator_instance):
        """Test _make_json_serializable with unknown object types."""
        class CustomObject:
            def __str__(self):
                return "custom_object_string"
        
        custom_obj = CustomObject()
        
        result = migrator_instance._make_json_serializable(custom_obj)
        assert result == "custom_object_string"

    def test_make_json_serializable_produces_valid_json(self, migrator_instance):
        """Test that _make_json_serializable output can be serialized to JSON."""
        complex_data = {
            "user_mappings": {
                "user1": {
                    "jira_user": Mock(_mock_name="jira_user_mock"),
                    "openproject_user": Mock(_mock_name="op_user_mock"),
                    "lastRefreshed": datetime(2023, 1, 15, tzinfo=UTC),
                    "metadata": {
                        "files": [Path("/tmp/file1"), Path("/tmp/file2")],
                        "nested_mock": Mock(_mock_name="nested")
                    }
                }
            }
        }
        
        serializable = migrator_instance._make_json_serializable(complex_data)
        
        # This should not raise an exception
        json_string = json.dumps(serializable)
        
        # And we should be able to parse it back
        parsed = json.loads(json_string)
        assert parsed["user_mappings"]["user1"]["jira_user"] == "<Mock: jira_user_mock>"
        assert parsed["user_mappings"]["user1"]["lastRefreshed"] == "2023-01-15T00:00:00+00:00"


class TestYoloIntegrationScenarios:
    """Test integration scenarios where YOLO fixes are used."""

    @pytest.fixture
    def migrator_instance(self):
        """Create migrator instance with proper mocks."""
        mock_jira_client = create_mock_jira_client()
        mock_op_client = create_mock_openproject_client()
        
        migrator = EnhancedUserAssociationMigrator(
            jira_client=mock_jira_client,
            op_client=mock_op_client
        )
        
        # Mock config
        migrator.config = Mock()
        migrator.config.get_path.return_value = Path("/tmp/test")
        
        return migrator

    @patch('src.utils.enhanced_user_association_migrator.config')
    def test_save_enhanced_mappings_with_mock_objects(self, mock_config, migrator_instance):
        """Test _save_enhanced_mappings handles Mock objects via _make_json_serializable."""
        # Setup enhanced_user_mappings with Mock objects
        mock_jira_user = Mock()
        mock_jira_user._mock_name = "test_jira_user"
        
        migrator_instance.enhanced_user_mappings = {
            "testuser": {
                "mapping_status": "mapped",
                "jira_user": mock_jira_user,
                "openproject_user": {"id": 123},
                "lastRefreshed": datetime(2023, 1, 15, tzinfo=UTC),
                "metadata": {
                    "jira_account_id": "test123",
                    "paths": [Path("/tmp/test1"), Path("/tmp/test2")]
                }
            }
        }
        
        # Mock file operations
        mock_config.get_path.return_value = Path("/tmp/test")
        
        # Create a proper mock file context manager
        mock_file_handle = Mock()
        mock_file_cm = Mock()
        mock_file_cm.__enter__ = Mock(return_value=mock_file_handle)
        mock_file_cm.__exit__ = Mock(return_value=None)
        
        with patch('pathlib.Path.open', return_value=mock_file_cm):
            with patch('json.dump') as mock_json_dump:
                migrator_instance._save_enhanced_mappings()
        
        # Verify json.dump was called with serializable data (no Mock objects)
        assert mock_json_dump.called
        saved_data = mock_json_dump.call_args[0][0]
        
        # Verify Mock objects were serialized properly
        assert saved_data["testuser"]["jira_user"] == "<Mock: test_jira_user>"
        assert saved_data["testuser"]["lastRefreshed"] == "2023-01-15T00:00:00+00:00"
        assert saved_data["testuser"]["metadata"]["paths"] == ["/tmp/test1", "/tmp/test2"]

    def test_staleness_detection_with_metrics_failure(self, migrator_instance, caplog):
        """Test staleness detection continues working when metrics collection fails."""
        # Setup failing metrics collector
        mock_collector = Mock()
        mock_collector.increment_counter.side_effect = ConnectionError("Metrics service unavailable")
        migrator_instance.metrics_collector = mock_collector
        
        # Set log level to DEBUG to capture the debug messages
        caplog.set_level(logging.DEBUG)
        
        # Test staleness detection via get_mapping_with_staleness_check (which triggers metrics)
        result = migrator_instance.get_mapping_with_staleness_check("nonexistent_user")
        
        # Should return None (user doesn't exist, stale)
        assert result is None
        
        # Metrics failure should be logged but not break functionality
        assert "Metrics collection failed" in caplog.text
        assert "staleness_detected_total" in caplog.text

    def test_concurrent_metrics_collection_thread_safety(self, migrator_instance):
        """Test that multiple threads can safely call _safe_metrics_increment concurrently."""
        # Setup mock collector that tracks calls
        call_count = 0
        call_lock = threading.Lock()
        
        def increment_counter(counter_name, tags=None):
            nonlocal call_count
            with call_lock:
                call_count += 1
                time.sleep(0.001)  # Small delay to increase chance of race conditions
        
        mock_collector = Mock()
        mock_collector.increment_counter.side_effect = increment_counter
        migrator_instance.metrics_collector = mock_collector
        
        def worker():
            migrator_instance._safe_metrics_increment("test_counter", {"thread": threading.current_thread().name})
        
        # Run 10 concurrent threads
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker) for _ in range(10)]
            for future in futures:
                future.result()  # Wait for completion
        
        # All calls should have completed successfully
        assert call_count == 10


class TestYoloExceptionHandling:
    """Test enhanced exception handling patterns."""

    @pytest.fixture 
    def migrator_instance(self):
        """Create migrator instance for testing."""
        return EnhancedUserAssociationMigrator(
            jira_client=create_mock_jira_client(),
            op_client=create_mock_openproject_client()
        )

    @patch('src.utils.enhanced_user_association_migrator.config')
    def test_load_user_mapping_specific_exceptions(self, mock_config, migrator_instance):
        """Test that _load_user_mapping handles specific exceptions, not broad Exception."""
        mock_config.get_path.return_value = Path("/tmp/test")
        
        # Test IOError handling
        with patch('pathlib.Path.exists', return_value=True):
            with patch('pathlib.Path.open', side_effect=IOError("Permission denied")):
                result = migrator_instance._load_user_mapping()
                assert result == {}

        # Test JSONDecodeError handling  
        with patch('pathlib.Path.exists', return_value=True):
            with patch('pathlib.Path.open', mock_open="mock_open") as mock_file:
                with patch('json.load', side_effect=json.JSONDecodeError("Invalid JSON", "doc", 0)):
                    result = migrator_instance._load_user_mapping()
                    assert result == {}

        # Test ValueError handling
        with patch('pathlib.Path.exists', return_value=True):
            with patch('pathlib.Path.open'):
                with patch('json.load', side_effect=ValueError("Invalid data")):
                    result = migrator_instance._load_user_mapping()
                    assert result == {}

        # Test OSError handling
        with patch('pathlib.Path.exists', return_value=True):
            with patch('pathlib.Path.open', side_effect=OSError("Disk full")):
                result = migrator_instance._load_user_mapping()
                assert result == {}

    def test_network_error_specific_handling(self, migrator_instance):
        """Test that network operations handle specific ConnectionError, not broad Exception."""
        # This tests the watchers fetching code path that was improved in extract_user_associations
        mock_issue = Mock()
        mock_issue.key = "TEST-123"
        mock_issue.fields.assignee = None
        mock_issue.fields.creator = None
        mock_issue.fields.reporter = None
        mock_issue.fields.watches = Mock()
        mock_issue.fields.watches.watchCount = 2  # Set positive watch count to trigger watchers fetch
        
        # Mock the Jira client to raise ConnectionError when fetching watchers
        migrator_instance.jira_client.get_issue_watchers.side_effect = ConnectionError("Network timeout")
        
        # The method should handle the specific exception gracefully and continue processing
        associations = migrator_instance._extract_user_associations(mock_issue)
        
        # Should return associations with empty watchers list (graceful degradation)
        assert associations["watchers"] == []
        # Since assignee, creator, and reporter are None, they won't be in associations
        # The main test is that the ConnectionError was handled and execution continued
        assert "watchers" in associations  # Watchers key should exist even when empty


if __name__ == "__main__":
    pytest.main([__file__]) 