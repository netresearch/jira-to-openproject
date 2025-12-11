#!/usr/bin/env python3
"""Tests for YOLO fixes in Enhanced User Association Migrator.

This module tests the YOLO improvements applied to the migrator including:
- JSON serialization of complex objects (_make_json_serializable)
- Security-focused error message sanitization
- Enhanced exception handling patterns

Note: Defensive metrics collection tests removed (MetricsCollector deleted as enterprise bloat)
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.utils.enhanced_user_association_migrator import EnhancedUserAssociationMigrator
from tests.utils.mock_factory import (
    create_mock_jira_client,
    create_mock_openproject_client,
)

# TestYoloMetricsHelpers class removed - _safe_metrics_increment deleted with MetricsCollector (enterprise bloat)


class TestYoloJsonSerialization:
    """Test suite for JSON serialization of complex objects (_make_json_serializable)."""

    @pytest.fixture
    def migrator_instance(self):
        """Create migrator instance for testing."""
        return EnhancedUserAssociationMigrator(
            jira_client=create_mock_jira_client(),
            op_client=create_mock_openproject_client(),
        )

    def test_make_json_serializable_basic_types(self, migrator_instance) -> None:
        """Test _make_json_serializable with basic JSON-compatible types."""
        test_cases = [
            ("string", "string"),
            (42, 42),
            (3.14, 3.14),
            (True, True),
            (False, False),
            (None, None),
        ]

        for input_val, expected in test_cases:
            result = migrator_instance._make_json_serializable(input_val)
            assert result == expected

    def test_make_json_serializable_mock_objects_with_name(
        self,
        migrator_instance,
    ) -> None:
        """Test _make_json_serializable with Mock objects that have _mock_name."""
        mock_obj = Mock()
        mock_obj._mock_name = "test_mock"

        result = migrator_instance._make_json_serializable(mock_obj)
        assert result == "<Mock: test_mock>"

    def test_make_json_serializable_mock_objects_without_name(
        self,
        migrator_instance,
    ) -> None:
        """Test _make_json_serializable with Mock objects without _mock_name."""
        mock_obj = Mock()
        if hasattr(mock_obj, "_mock_name"):
            delattr(mock_obj, "_mock_name")

        result = migrator_instance._make_json_serializable(mock_obj)
        # The actual implementation returns the type name when _mock_name is missing
        assert result == "<Mock: Mock>"

    def test_make_json_serializable_magicmock_objects(self, migrator_instance) -> None:
        """Test _make_json_serializable with MagicMock objects."""
        mock_obj = MagicMock()

        result = migrator_instance._make_json_serializable(mock_obj)
        # Should detect it's a mock and handle appropriately
        assert result.startswith("<Mock:")

    def test_make_json_serializable_datetime_objects(self, migrator_instance) -> None:
        """Test _make_json_serializable with datetime objects."""
        dt = datetime(2023, 1, 15, 14, 30, 45, tzinfo=UTC)

        result = migrator_instance._make_json_serializable(dt)
        assert result == "2023-01-15T14:30:45+00:00"

    def test_make_json_serializable_path_objects(self, migrator_instance) -> None:
        """Test _make_json_serializable with Path objects."""
        path_obj = Path("/tmp/test/file.json")

        result = migrator_instance._make_json_serializable(path_obj)
        assert result == "/tmp/test/file.json"

    def test_make_json_serializable_nested_dict(self, migrator_instance) -> None:
        """Test _make_json_serializable with nested dictionaries containing complex objects."""
        mock_user = Mock()
        mock_user._mock_name = "jira_user"

        complex_dict = {
            "timestamp": datetime(2023, 1, 15, tzinfo=UTC),
            "user": mock_user,
            "path": Path("/tmp/test"),
            "nested": {"more_data": mock_user, "number": 42},
        }

        result = migrator_instance._make_json_serializable(complex_dict)

        expected = {
            "timestamp": "2023-01-15T00:00:00+00:00",
            "user": "<Mock: jira_user>",
            "path": "/tmp/test",
            "nested": {"more_data": "<Mock: jira_user>", "number": 42},
        }

        assert result == expected

    def test_make_json_serializable_nested_list(self, migrator_instance) -> None:
        """Test _make_json_serializable with lists containing complex objects."""
        mock_obj = Mock()
        mock_obj._mock_name = "list_mock"

        complex_list = [
            datetime(2023, 1, 15, tzinfo=UTC),
            mock_obj,
            {"nested": mock_obj},
            [mock_obj, "string"],
        ]

        result = migrator_instance._make_json_serializable(complex_list)

        expected = [
            "2023-01-15T00:00:00+00:00",
            "<Mock: list_mock>",
            {"nested": "<Mock: list_mock>"},
            ["<Mock: list_mock>", "string"],
        ]

        assert result == expected

    def test_make_json_serializable_tuple_handling(self, migrator_instance) -> None:
        """Test _make_json_serializable with tuples (should convert to lists)."""
        mock_obj = Mock()
        mock_obj._mock_name = "tuple_mock"

        test_tuple = (datetime(2023, 1, 15, tzinfo=UTC), mock_obj, "string")

        result = migrator_instance._make_json_serializable(test_tuple)

        expected = ["2023-01-15T00:00:00+00:00", "<Mock: tuple_mock>", "string"]
        assert result == expected

    def test_make_json_serializable_unknown_objects(self, migrator_instance) -> None:
        """Test _make_json_serializable with unknown object types."""

        class CustomObject:
            def __str__(self) -> str:
                return "custom_object_string"

        custom_obj = CustomObject()

        result = migrator_instance._make_json_serializable(custom_obj)
        assert result == "custom_object_string"

    def test_make_json_serializable_produces_valid_json(
        self,
        migrator_instance,
    ) -> None:
        """Test that _make_json_serializable output can be serialized to JSON."""
        complex_data = {
            "user_mappings": {
                "user1": {
                    "jira_user": Mock(_mock_name="jira_user_mock"),
                    "openproject_user": Mock(_mock_name="op_user_mock"),
                    "lastRefreshed": datetime(2023, 1, 15, tzinfo=UTC),
                    "metadata": {
                        "files": [Path("/tmp/file1"), Path("/tmp/file2")],
                        "nested_mock": Mock(_mock_name="nested"),
                    },
                },
            },
        }

        serializable = migrator_instance._make_json_serializable(complex_data)

        # This should not raise an exception
        json_string = json.dumps(serializable)

        # And we should be able to parse it back
        parsed = json.loads(json_string)
        assert parsed["user_mappings"]["user1"]["jira_user"] == "<Mock: jira_user_mock>"
        assert parsed["user_mappings"]["user1"]["lastRefreshed"] == "2023-01-15T00:00:00+00:00"

    def test_make_json_serializable_depth_limit_protection(
        self,
        migrator_instance,
    ) -> None:
        """Test that _make_json_serializable prevents stack overflow with depth limits."""

        # Create deeply nested structure
        def create_nested_dict(depth):
            if depth == 0:
                return "leaf_value"
            return {"nested": create_nested_dict(depth - 1)}

        # Create structure deeper than default limit (10)
        deep_data = create_nested_dict(15)

        # Should handle gracefully without stack overflow
        result = migrator_instance._make_json_serializable(deep_data)

        # Should truncate at max depth and indicate truncation
        # Navigate to the max depth and verify truncation marker
        current = result
        for _ in range(10):  # Default max depth is 10
            if isinstance(current, dict) and "nested" in current:
                current = current["nested"]
            else:
                break

        # At max depth, should find the truncation marker
        assert isinstance(current, str)
        assert current.startswith("<MAX_DEPTH_REACHED:")

    def test_make_json_serializable_custom_depth_limit(self, migrator_instance) -> None:
        """Test that _make_json_serializable respects custom max_depth parameter."""
        # Create nested structure
        nested_data = {"level1": {"level2": {"level3": {"level4": "deep_value"}}}}

        # Test with low depth limit
        result = migrator_instance._make_json_serializable(nested_data, max_depth=2)

        # Should truncate at specified depth
        assert result["level1"]["level2"].startswith("<MAX_DEPTH_REACHED:")

    def test_make_json_serializable_depth_limit_with_lists(
        self,
        migrator_instance,
    ) -> None:
        """Test depth limiting works correctly with nested lists."""
        # Create deeply nested list structure
        nested_list = [[[[["deep_value"]]]]]

        result = migrator_instance._make_json_serializable(nested_list, max_depth=3)

        # Should truncate nested lists at specified depth
        # Navigate through the structure
        current = result
        for _ in range(3):
            if isinstance(current, list) and len(current) > 0:
                current = current[0]
            else:
                break

        assert isinstance(current, str)
        assert current.startswith("<MAX_DEPTH_REACHED:")


class TestYoloIntegrationScenarios:
    """Test integration scenarios where YOLO fixes are used."""

    @pytest.fixture
    def migrator_instance(self):
        """Create migrator instance with proper mocks."""
        mock_jira_client = create_mock_jira_client()
        mock_op_client = create_mock_openproject_client()

        migrator = EnhancedUserAssociationMigrator(
            jira_client=mock_jira_client,
            op_client=mock_op_client,
        )

        # Mock config
        migrator.config = Mock()
        migrator.config.get_path.return_value = Path("/tmp/test")

        return migrator

    @patch("src.utils.enhanced_user_association_migrator.config")
    def test_save_enhanced_mappings_with_mock_objects(
        self,
        mock_config,
        migrator_instance,
    ) -> None:
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
                    "paths": [Path("/tmp/test1"), Path("/tmp/test2")],
                },
            },
        }

        # Mock file operations
        mock_config.get_path.return_value = Path("/tmp/test")

        # Create a proper mock file context manager
        mock_file_handle = Mock()
        mock_file_cm = Mock()
        mock_file_cm.__enter__ = Mock(return_value=mock_file_handle)
        mock_file_cm.__exit__ = Mock(return_value=None)

        with (
            patch("pathlib.Path.open", return_value=mock_file_cm),
            patch("json.dump") as mock_json_dump,
        ):
            migrator_instance._save_enhanced_mappings()

        # Verify json.dump was called with serializable data (no Mock objects)
        assert mock_json_dump.called
        saved_data = mock_json_dump.call_args[0][0]

        # Verify Mock objects were serialized properly
        assert saved_data["testuser"]["jira_user"] == "<Mock: test_jira_user>"
        assert saved_data["testuser"]["lastRefreshed"] == "2023-01-15T00:00:00+00:00"
        assert saved_data["testuser"]["metadata"]["paths"] == [
            "/tmp/test1",
            "/tmp/test2",
        ]

    # test_staleness_detection_with_metrics_failure removed - MetricsCollector deleted (enterprise bloat)
    # test_concurrent_metrics_collection_thread_safety removed - _safe_metrics_increment deleted (enterprise bloat)


class TestYoloExceptionHandling:
    """Test enhanced exception handling patterns."""

    @pytest.fixture
    def migrator_instance(self):
        """Create migrator instance for testing."""
        return EnhancedUserAssociationMigrator(
            jira_client=create_mock_jira_client(),
            op_client=create_mock_openproject_client(),
        )

    @patch("src.utils.enhanced_user_association_migrator.config")
    def test_load_user_mapping_specific_exceptions(
        self,
        mock_config,
        migrator_instance,
    ) -> None:
        """Test that _load_user_mapping handles specific exceptions, not broad Exception."""
        mock_config.get_path.return_value = Path("/tmp/test")

        # Test IOError handling
        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.open", side_effect=OSError("Permission denied")):
                result = migrator_instance._load_user_mapping()
                assert result == {}

        # Test JSONDecodeError handling
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.open", mock_open="mock_open"),
        ):
            with patch(
                "json.load",
                side_effect=json.JSONDecodeError("Invalid JSON", "doc", 0),
            ):
                result = migrator_instance._load_user_mapping()
                assert result == {}

        # Test ValueError handling
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.open"),
        ):
            with patch("json.load", side_effect=ValueError("Invalid data")):
                result = migrator_instance._load_user_mapping()
                assert result == {}

        # Test OSError handling
        with patch("pathlib.Path.exists", return_value=True):
            with patch("pathlib.Path.open", side_effect=OSError("Disk full")):
                result = migrator_instance._load_user_mapping()
                assert result == {}

    def test_network_error_specific_handling(self, migrator_instance) -> None:
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
        migrator_instance.jira_client.get_issue_watchers.side_effect = ConnectionError(
            "Network timeout",
        )

        # The method should handle the specific exception gracefully and continue processing
        associations = migrator_instance._extract_user_associations(mock_issue)

        # Should return associations with empty watchers list (graceful degradation)
        assert associations["watchers"] == []
        # Since assignee, creator, and reporter are None, they won't be in associations
        # The main test is that the ConnectionError was handled and execution continued
        assert "watchers" in associations  # Watchers key should exist even when empty


if __name__ == "__main__":
    pytest.main([__file__])
