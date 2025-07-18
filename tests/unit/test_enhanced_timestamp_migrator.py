#!/usr/bin/env python3
"""Unit tests for EnhancedTimestampMigrator."""

import json
from datetime import UTC, datetime, timezone
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.utils.enhanced_timestamp_migrator import (
    EnhancedTimestampMigrator,
    TimestampMapping,
    TimestampMigrationResult,
)
from tests.utils.mock_factory import create_mock_jira_client, create_mock_openproject_client


class TestEnhancedTimestampMigrator:
    """Test suite for EnhancedTimestampMigrator."""

    @pytest.fixture
    def mock_clients(self):
        """Create mock clients."""
        jira_client = create_mock_jira_client()
        op_client = create_mock_openproject_client()
        
        # Ensure jira client has the jira property
        jira_client.jira = Mock()
        jira_client.jira.server_info = Mock()
        
        return jira_client, op_client

    @pytest.fixture 
    def migrator_with_mocks(self, mock_clients):
        """Create EnhancedTimestampMigrator with mocked dependencies."""
        jira_client, op_client = mock_clients
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.jira_config.get.return_value = "UTC"
            mock_config.logger = Mock()
            
            return EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client
            )

    def test_initialization(self, mock_clients):
        """Test basic initialization of EnhancedTimestampMigrator."""
        jira_client, op_client = mock_clients
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.jira_config.get.return_value = "UTC"
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client
            )
            
            assert migrator.jira_client == jira_client
            assert migrator.op_client == op_client
            assert migrator.target_timezone == "UTC"

    def test_detect_jira_timezone_from_server_info(self, mock_clients):
        """Test successful Jira timezone detection from server info."""
        jira_client, op_client = mock_clients
        
        # Mock successful server_info response
        jira_client.jira.server_info.return_value = {
            "serverTimeZone": {"timeZoneId": "Europe/Berlin"}
        }
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.jira_config.get.return_value = "UTC"
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client
            )
            
            assert migrator.jira_timezone == "Europe/Berlin"

    def test_detect_jira_timezone_mapping_fallback(self, mock_clients):
        """Test timezone detection with common abbreviation mapping."""
        jira_client, op_client = mock_clients
        
        # Mock server_info returning EST (should be mapped)
        jira_client.jira.server_info.return_value = {
            "serverTimeZone": {"timeZoneId": "EST"}
        }
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.jira_config.get.return_value = "UTC"
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client
            )
            
            assert migrator.jira_timezone == "America/New_York"

    def test_detect_jira_timezone_error_fallback(self, mock_clients):
        """Test Jira timezone detection error fallback to UTC."""
        jira_client, op_client = mock_clients
        
        # Simulate server_info() failure
        jira_client.jira.server_info.side_effect = Exception("Connection failed")
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.jira_config.get.return_value = "UTC"
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client
            )
            
            # Should fallback to UTC (from config)
            assert migrator.jira_timezone == "UTC"

    def test_detect_jira_timezone_no_timezone_field(self, mock_clients):
        """Test timezone detection when server_info lacks serverTimeZone field."""
        jira_client, op_client = mock_clients
        
        # Return server info without timezone field
        jira_client.jira.server_info.return_value = {
            "version": "8.0.0",
            "build": "800000"
        }
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.jira_config.get.return_value = "UTC"
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client
            )
            
            # Should fallback to UTC
            assert migrator.jira_timezone == "UTC"

    def test_detect_jira_timezone_malformed_response(self, mock_clients):
        """Test timezone detection with malformed server response."""
        jira_client, op_client = mock_clients
        
        # Return malformed timezone info
        jira_client.jira.server_info.return_value = {
            "serverTimeZone": {"invalidField": "someValue"}
        }
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.jira_config.get.return_value = "UTC"
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client
            )
            
            assert migrator.jira_timezone == "UTC"

    def test_detect_jira_timezone_various_timezones(self, mock_clients):
        """Test timezone detection with various timezone formats."""
        jira_client, op_client = mock_clients
        
        test_cases = [
            ("America/New_York", "America/New_York"),
            ("Asia/Tokyo", "Asia/Tokyo"),
            ("UTC", "UTC"),
            ("GMT", "GMT"),  # Should remain GMT according to mapping
            ("EST", "America/New_York"),  # Mapped
            ("CET", "Europe/Paris"),  # Mapped
        ]
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.jira_config.get.return_value = "UTC"
            mock_config.logger = Mock()
            
            for input_tz, expected_tz in test_cases:
                # Reset mock
                jira_client.jira.server_info.reset_mock()
                jira_client.jira.server_info.return_value = {
                    "serverTimeZone": {"timeZoneId": input_tz}
                }
                
                migrator = EnhancedTimestampMigrator(
                    jira_client=jira_client,
                    op_client=op_client
                )
                
                assert migrator.jira_timezone == expected_tz, f"Failed for {input_tz}"

    def test_detect_jira_timezone_client_not_connected(self, mock_clients):
        """Test timezone detection when JiraClient.jira is None."""
        jira_client, op_client = mock_clients
        
        # Simulate client not connected
        jira_client.jira = None
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.jira_config.get.return_value = "UTC"
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client
            )
            
            # Should fallback to UTC
            assert migrator.jira_timezone == "UTC"

    def test_normalize_timestamp_iso_format(self, migrator_with_mocks):
        """Test timestamp normalization with ISO format."""
        timestamp = "2023-10-15T14:30:00.000Z"
        
        result = migrator_with_mocks._normalize_timestamp(timestamp)
        
        assert result is not None
        assert result.endswith("+00:00")  # UTC timezone

    def test_normalize_timestamp_various_formats(self, migrator_with_mocks):
        """Test timestamp normalization with various input formats."""
        test_cases = [
            "2023-10-15T14:30:00.000Z",
            "2023-10-15T14:30:00Z",
            "2023-10-15 14:30:00",
            "2023-10-15T14:30:00+02:00",
        ]
        
        for timestamp in test_cases:
            with patch.object(migrator_with_mocks, '_parse_timestamp') as mock_parse:
                mock_parse.return_value = datetime(2023, 10, 15, 14, 30, 0, tzinfo=UTC)
                
                result = migrator_with_mocks._normalize_timestamp(timestamp)
                assert result is not None
                mock_parse.assert_called_once_with(timestamp)

    def test_normalize_timestamp_timezone_conversion(self, migrator_with_mocks):
        """Test timezone conversion during normalization."""
        # Set up migrator with Berlin timezone
        migrator_with_mocks.jira_timezone = "Europe/Berlin"
        
        # Test timestamp without timezone info
        timestamp = "2023-10-15 14:30:00"
        
        with patch.object(migrator_with_mocks, '_parse_timestamp') as mock_parse:
            # Mock as naive datetime (no timezone)
            naive_dt = datetime(2023, 10, 15, 14, 30, 0)
            mock_parse.return_value = naive_dt
            
            result = migrator_with_mocks._normalize_timestamp(timestamp)
            
            assert result is not None
            # Should have timezone information added
            assert result != timestamp

    def test_normalize_timestamp_dst_handling(self, migrator_with_mocks):
        """Test DST handling in timestamp normalization."""
        migrator_with_mocks.jira_timezone = "Europe/Berlin"
        
        # Test both DST and non-DST dates
        dst_timestamp = "2023-07-15T14:30:00"  # Summer (DST)
        non_dst_timestamp = "2023-01-15T14:30:00"  # Winter (no DST)
        
        for timestamp in [dst_timestamp, non_dst_timestamp]:
            with patch.object(migrator_with_mocks, '_parse_timestamp') as mock_parse:
                mock_parse.return_value = datetime(2023, 7, 15, 14, 30, 0, tzinfo=UTC)
                
                result = migrator_with_mocks._normalize_timestamp(timestamp)
                assert result is not None

    def test_normalize_timestamp_invalid_format(self, migrator_with_mocks):
        """Test handling of invalid timestamp formats."""
        invalid_timestamps = [
            "not-a-timestamp",
            "2023-13-40T25:70:80",  # Invalid date/time
            "",
            None,
        ]
        
        for invalid_timestamp in invalid_timestamps:
            with patch.object(migrator_with_mocks, '_parse_timestamp') as mock_parse:
                mock_parse.side_effect = ValueError("Invalid timestamp format")
                
                result = migrator_with_mocks._normalize_timestamp(invalid_timestamp)
                assert result is None

    def test_extract_all_timestamps_complete_issue(self, migrator_with_mocks, sample_jira_issue):
        """Test extraction of all timestamps from a complete Jira issue."""
        # Mock all timestamp fields
        sample_issue = {
            "fields": {
                "created": "2023-10-15T10:00:00.000Z",
                "updated": "2023-10-15T12:00:00.000Z",
                "duedate": "2023-10-20",
                "resolutiondate": "2023-10-18T16:00:00.000Z",
                "customfield_12345": "2023-10-16T14:00:00.000Z",  # Custom date field
            }
        }
        
        with patch.object(migrator_with_mocks, '_is_date_field', return_value=True):
            result = migrator_with_mocks._extract_all_timestamps(sample_issue)
            
            assert "created" in result
            assert "updated" in result
            assert "duedate" in result
            assert "resolutiondate" in result

    def test_extract_all_timestamps_minimal_issue(self, migrator_with_mocks):
        """Test extraction from minimal Jira issue (only required fields)."""
        minimal_issue = {
            "fields": {
                "created": "2023-10-15T10:00:00.000Z",
                "summary": "Test issue",
                "issuetype": {"name": "Task"},
            }
        }
        
        result = migrator_with_mocks._extract_all_timestamps(minimal_issue)
        
        assert "created" in result
        assert len(result) >= 1  # At least created timestamp

    def test_is_date_field_validation(self, migrator_with_mocks):
        """Test date field validation logic."""
        date_fields = ["created", "updated", "duedate", "resolutiondate"]
        non_date_fields = ["summary", "description", "priority", "status"]
        
        for field in date_fields:
            assert migrator_with_mocks._is_date_field(field, "2023-10-15T10:00:00.000Z")
            
        for field in non_date_fields:
            assert not migrator_with_mocks._is_date_field(field, "Some text value")

    def test_migrate_creation_timestamp_with_rails(self, migrator_with_mocks):
        """Test creation timestamp migration using Rails backend."""
        jira_issue = {
            "fields": {"created": "2023-10-15T10:00:00.000Z"}
        }
        work_package = {"id": 123}
        
        with patch.object(migrator_with_mocks, '_normalize_timestamp') as mock_normalize:
            mock_normalize.return_value = "2023-10-15T10:00:00+00:00"
            
            result = migrator_with_mocks.migrate_creation_timestamp(
                jira_issue, work_package, use_rails=True
            )
            
            assert result.success
            assert result.field_name == "created_at"
            assert result.jira_value == "2023-10-15T10:00:00.000Z"

    def test_migrate_creation_timestamp_without_rails(self, migrator_with_mocks):
        """Test creation timestamp migration without Rails backend."""
        jira_issue = {
            "fields": {"created": "2023-10-15T10:00:00.000Z"}
        }
        work_package = {"id": 123}
        
        with patch.object(migrator_with_mocks, '_normalize_timestamp') as mock_normalize:
            mock_normalize.return_value = "2023-10-15T10:00:00+00:00"
            
            result = migrator_with_mocks.migrate_creation_timestamp(
                jira_issue, work_package, use_rails=False
            )
            
            assert result.success
            assert result.field_name == "created_at"

    def test_migrate_creation_timestamp_missing_data(self, migrator_with_mocks):
        """Test creation timestamp migration with missing created field."""
        jira_issue = {"fields": {}}  # No created field
        work_package = {"id": 123}
        
        result = migrator_with_mocks.migrate_creation_timestamp(
            jira_issue, work_package
        )
        
        assert not result.success
        assert "Missing 'created'" in result.error_message

    def test_migrate_creation_timestamp_normalization_failure(self, migrator_with_mocks):
        """Test creation timestamp migration with normalization failure."""
        jira_issue = {
            "fields": {"created": "invalid-timestamp"}
        }
        work_package = {"id": 123}
        
        with patch.object(migrator_with_mocks, '_normalize_timestamp') as mock_normalize:
            mock_normalize.return_value = None  # Normalization failed
            
            result = migrator_with_mocks.migrate_creation_timestamp(
                jira_issue, work_package
            )
            
            assert not result.success
            assert "Failed to normalize" in result.error_message

    def test_migrate_update_timestamp(self, migrator_with_mocks):
        """Test update timestamp migration."""
        jira_issue = {
            "fields": {"updated": "2023-10-15T12:00:00.000Z"}
        }
        work_package = {"id": 123}
        
        with patch.object(migrator_with_mocks, '_normalize_timestamp') as mock_normalize:
            mock_normalize.return_value = "2023-10-15T12:00:00+00:00"
            
            result = migrator_with_mocks.migrate_update_timestamp(
                jira_issue, work_package
            )
            
            assert result.success
            assert result.field_name == "updated_at"

    def test_migrate_due_date(self, migrator_with_mocks):
        """Test due date migration."""
        jira_issue = {
            "fields": {"duedate": "2023-10-20"}
        }
        work_package = {"id": 123}
        
        with patch.object(migrator_with_mocks, '_normalize_timestamp') as mock_normalize:
            mock_normalize.return_value = "2023-10-20T00:00:00+00:00"
            
            result = migrator_with_mocks.migrate_due_date(
                jira_issue, work_package
            )
            
            assert result.success
            assert result.field_name == "due_date"

    def test_migrate_resolution_date_with_rails(self, migrator_with_mocks):
        """Test resolution date migration using Rails."""
        jira_issue = {
            "fields": {"resolutiondate": "2023-10-18T16:00:00.000Z"}
        }
        work_package = {"id": 123}
        
        with patch.object(migrator_with_mocks, '_normalize_timestamp') as mock_normalize:
            mock_normalize.return_value = "2023-10-18T16:00:00+00:00"
            
            result = migrator_with_mocks.migrate_resolution_date(
                jira_issue, work_package, use_rails=True
            )
            
            assert result.success
            assert result.field_name == "closed_at"

    def test_migrate_custom_date_fields(self, migrator_with_mocks):
        """Test migration of custom date fields."""
        jira_issue = {
            "fields": {
                "customfield_12345": "2023-10-16T14:00:00.000Z",
                "customfield_67890": "2023-10-17",
            }
        }
        work_package = {"id": 123}
        
        with patch.object(migrator_with_mocks, '_is_date_field') as mock_is_date:
            mock_is_date.return_value = True
            
            with patch.object(migrator_with_mocks, '_normalize_timestamp') as mock_normalize:
                mock_normalize.return_value = "2023-10-16T14:00:00+00:00"
                
                results = migrator_with_mocks.migrate_custom_date_fields(
                    jira_issue, work_package
                )
                
                assert len(results) == 2
                assert all(result.success for result in results)

    def test_queue_rails_operation(self, migrator_with_mocks):
        """Test Rails operation queuing."""
        operation = {
            "table": "work_packages",
            "id": 123,
            "field": "created_at",
            "value": "2023-10-15T10:00:00+00:00"
        }
        
        migrator_with_mocks._queue_rails_operation(operation)
        
        assert len(migrator_with_mocks.rails_operations_cache) == 1
        assert migrator_with_mocks.rails_operations_cache[0] == operation

    def test_generate_timestamp_preservation_script(self, migrator_with_mocks):
        """Test timestamp preservation script generation."""
        operations = [
            {
                "table": "work_packages",
                "id": 123,
                "field": "created_at",
                "value": "2023-10-15T10:00:00+00:00"
            }
        ]
        
        script_content = migrator_with_mocks._generate_timestamp_preservation_script(operations)
        
        assert "UPDATE work_packages" in script_content
        assert "created_at" in script_content
        assert "2023-10-15T10:00:00+00:00" in script_content

    def test_execute_rails_timestamp_operations_success(self, migrator_with_mocks, mock_clients):
        """Test successful Rails timestamp operations execution."""
        jira_client, op_client = mock_clients
        
        # Add operations to cache
        migrator_with_mocks.rails_operations_cache = [
            {
                "table": "work_packages",
                "id": 123,
                "field": "created_at",
                "value": "2023-10-15T10:00:00+00:00"
            }
        ]
        
        # Mock successful Rails execution
        op_client.execute_rails_script.return_value = {"success": True}
        
        result = migrator_with_mocks._execute_rails_timestamp_operations()
        
        assert result["success"]
        assert len(migrator_with_mocks.rails_operations_cache) == 0  # Cache cleared

    def test_execute_rails_timestamp_operations_failure(self, migrator_with_mocks, mock_clients):
        """Test Rails timestamp operations execution failure."""
        jira_client, op_client = mock_clients
        
        # Add operations to cache
        migrator_with_mocks.rails_operations_cache = [
            {
                "table": "work_packages",
                "id": 123,
                "field": "created_at",
                "value": "2023-10-15T10:00:00+00:00"
            }
        ]
        
        # Mock Rails execution failure
        op_client.execute_rails_script.side_effect = Exception("Rails error")
        
        result = migrator_with_mocks._execute_rails_timestamp_operations()
        
        assert not result["success"]
        assert "Rails error" in result["error"]

    def test_execute_rails_timestamp_operations_empty_cache(self, migrator_with_mocks):
        """Test Rails operations execution with empty cache."""
        result = migrator_with_mocks._execute_rails_timestamp_operations()
        
        assert result["success"]
        assert "No operations" in result["message"]

    def test_generate_timestamp_report(self, migrator_with_mocks):
        """Test timestamp migration report generation."""
        results = [
            TimestampMigrationResult(
                success=True,
                field_name="created_at",
                jira_value="2023-10-15T10:00:00.000Z",
                normalized_value="2023-10-15T10:00:00+00:00",
                migration_method="direct"
            ),
            TimestampMigrationResult(
                success=False,
                field_name="invalid_date",
                jira_value="invalid",
                error_message="Invalid timestamp format"
            )
        ]
        
        report = migrator_with_mocks._generate_timestamp_report(results)
        
        assert "Migration Report" in report
        assert "1 successful" in report
        assert "1 failed" in report
        assert "created_at" in report
        assert "invalid_date" in report

    @patch("src.utils.enhanced_timestamp_migrator.config")
    def test_save_migration_results(self, mock_config, migrator_with_mocks, tmp_path):
        """Test saving migration results to file."""
        mock_config.output_dir = str(tmp_path)
        
        results = [
            TimestampMigrationResult(
                success=True,
                field_name="created_at",
                jira_value="2023-10-15T10:00:00.000Z",
                normalized_value="2023-10-15T10:00:00+00:00",
                migration_method="direct"
            )
        ]
        
        output_file = migrator_with_mocks._save_migration_results(results, "TEST-123")
        
        assert output_file.exists()
        
        # Verify content
        with open(output_file, 'r') as f:
            content = json.load(f)
            
        assert content["jira_key"] == "TEST-123"
        assert len(content["timestamp_migrations"]) == 1

    def test_migrate_timestamps_full_workflow(self, migrator_with_mocks, sample_jira_issue):
        """Test complete timestamp migration workflow."""
        jira_issue = {
            "key": "TEST-123",
            "fields": {
                "created": "2023-10-15T10:00:00.000Z",
                "updated": "2023-10-15T12:00:00.000Z",
                "duedate": "2023-10-20"
            }
        }
        work_package = {"id": 123}
        
        with patch.object(migrator_with_mocks, '_normalize_timestamp') as mock_normalize:
            mock_normalize.return_value = "2023-10-15T10:00:00+00:00"
            
            results = migrator_with_mocks.migrate_timestamps(
                jira_issue, work_package
            )
            
            assert len(results) >= 2  # At least created and updated
            assert all(isinstance(result, TimestampMigrationResult) for result in results)

    def test_migrate_timestamps_with_warnings(self, migrator_with_mocks):
        """Test timestamp migration with warnings for invalid timestamps."""
        jira_issue = {
            "key": "TEST-123",
            "fields": {
                "created": "2023-10-15T10:00:00.000Z",  # Valid
                "invalid_date": "not-a-date",  # Invalid
            }
        }
        work_package = {"id": 123}
        
        def mock_normalize(timestamp):
            if timestamp == "not-a-date":
                return None
            return "2023-10-15T10:00:00+00:00"
        
        with patch.object(migrator_with_mocks, '_normalize_timestamp', side_effect=mock_normalize):
            with patch.object(migrator_with_mocks, '_is_date_field', return_value=True):
                results = migrator_with_mocks.migrate_timestamps(
                    jira_issue, work_package
                )
                
                successful_results = [r for r in results if r.success]
                failed_results = [r for r in results if not r.success]
                
                assert len(successful_results) >= 1
                assert len(failed_results) >= 1

    def test_migrate_timestamps_exception_handling(self, migrator_with_mocks, mock_clients):
        """Test timestamp migration exception handling."""
        jira_client, op_client = mock_clients
        
        jira_issue = {
            "key": "TEST-123",
            "fields": {"created": "2023-10-15T10:00:00.000Z"}
        }
        work_package = {"id": 123}
        
        # Mock an exception during migration
        with patch.object(migrator_with_mocks, 'migrate_creation_timestamp') as mock_migrate:
            mock_migrate.side_effect = Exception("Unexpected error")
            
            results = migrator_with_mocks.migrate_timestamps(
                jira_issue, work_package
            )
            
            # Should handle exception gracefully
            assert isinstance(results, list)

    def test_timezone_edge_cases(self, mock_clients):
        """Test timezone detection edge cases."""
        jira_client, op_client = mock_clients
        
        edge_cases = [
            # String format instead of dict
            {"serverTimeZone": "Europe/Berlin"},
            # Alternative field names
            {"timeZone": "America/New_York"},
            {"timezone": "Asia/Tokyo"},
            {"serverTz": "Australia/Sydney"},
        ]
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.jira_config.get.return_value = "UTC"
            mock_config.logger = Mock()
            
            for server_info in edge_cases:
                jira_client.jira.server_info.return_value = server_info
                
                migrator = EnhancedTimestampMigrator(
                    jira_client=jira_client,
                    op_client=op_client
                )
                
                # Should detect timezone successfully
                assert migrator.jira_timezone != "UTC"

    def test_normalize_timestamp_with_datetime_object(self, migrator_with_mocks):
        """Test timestamp normalization with datetime object input."""
        dt = datetime(2023, 10, 15, 14, 30, 0, tzinfo=UTC)
        
        result = migrator_with_mocks._normalize_timestamp(dt)
        assert result is not None

    def test_custom_field_filtering(self, migrator_with_mocks):
        """Test custom field filtering logic."""
        jira_issue = {
            "fields": {
                "customfield_12345": "2023-10-15T14:00:00.000Z",
                "customfield_text": "Not a date",
                "summary": "Test issue"
            }
        }
        
        with patch.object(migrator_with_mocks, '_is_date_field') as mock_is_date:
            # Only return True for the first custom field
            mock_is_date.side_effect = lambda field, value: field == "customfield_12345"
            
            timestamps = migrator_with_mocks._extract_all_timestamps(jira_issue)
            
            # Should only include the date custom field
            custom_fields = [field for field in timestamps.keys() if field.startswith("customfield")]
            assert "customfield_12345" in custom_fields
            assert "customfield_text" not in timestamps 