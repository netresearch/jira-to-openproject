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
        return jira_client, op_client

    @pytest.fixture
    def sample_jira_issue(self):
        """Create sample Jira issue with timestamp fields."""
        issue = Mock()
        issue.key = "TEST-123"
        issue.fields = Mock()
        
        # Standard timestamp fields
        issue.fields.created = "2023-01-15T10:30:00.000+0000"
        issue.fields.updated = "2023-01-16T14:45:30.000+0000"
        issue.fields.duedate = "2023-01-20"
        issue.fields.resolutiondate = "2023-01-18T16:20:15.000+0000"
        
        # Custom date fields
        issue.fields.customfield_10001 = "2023-01-17T12:00:00.000+0000"  # custom datetime
        issue.fields.customfield_10002 = "2023-01-19"  # custom date
        issue.fields.customfield_10003 = "Invalid date"  # invalid format
        
        # Time tracking
        issue.fields.timeoriginalestimate = 28800  # 8 hours in seconds
        issue.fields.timeestimate = 14400  # 4 hours remaining
        issue.fields.timespent = 14400  # 4 hours spent
        
        return issue

    @pytest.fixture
    @patch("src.utils.enhanced_timestamp_migrator.config")
    def migrator_with_mocks(self, mock_config, mock_clients):
        """Create migrator instance with mocked dependencies."""
        mock_config.logger = Mock()
        
        jira_client, op_client = mock_clients
        
        # Mock Jira server info for timezone detection
        jira_client.server_info.return_value = {
            "serverTime": "2023-01-15T10:30:00.000+0000",
            "serverTimeZone": {"timeZoneId": "UTC"}
        }
        
        return EnhancedTimestampMigrator(
            jira_client=jira_client,
            op_client=op_client,
            target_timezone="UTC"
        )

    def test_initialization_with_utc_timezone(self, migrator_with_mocks):
        """Test migrator initialization with UTC timezone."""
        assert migrator_with_mocks.target_timezone == "UTC"
        assert migrator_with_mocks.jira_timezone == "UTC"
        assert len(migrator_with_mocks._rails_operations_cache) == 0
        assert len(migrator_with_mocks.migration_results) == 0

    @patch("src.utils.enhanced_timestamp_migrator.config")
    def test_initialization_with_custom_timezone(self, mock_config, mock_clients):
        """Test migrator initialization with custom timezone."""
        mock_config.logger = Mock()
        jira_client, op_client = mock_clients
        
        jira_client.server_info.return_value = {
            "serverTime": "2023-01-15T10:30:00.000-0800",
            "serverTimeZone": {"timeZoneId": "America/Los_Angeles"}
        }
        
        migrator = EnhancedTimestampMigrator(
            jira_client=jira_client,
            op_client=op_client,
            target_timezone="America/New_York"
        )
        
        assert migrator.target_timezone == "America/New_York"
        assert migrator.jira_timezone == "America/Los_Angeles"

    def test_detect_jira_timezone_from_server_info(self, mock_clients):
        """Test Jira timezone detection from server info."""
        jira_client, op_client = mock_clients
        
        jira_client.server_info.return_value = {
            "serverTimeZone": {"timeZoneId": "Europe/London"}
        }
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client
            )
            
            assert migrator.jira_timezone == "Europe/London"

    def test_detect_jira_timezone_mapping_fallback(self, mock_clients):
        """Test Jira timezone detection with mapping fallback."""
        jira_client, op_client = mock_clients
        
        jira_client.server_info.return_value = {
            "serverTimeZone": {"timeZoneId": "PST"}  # Common abbreviation
        }
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client
            )
            
            assert migrator.jira_timezone == "America/Los_Angeles"  # Mapped value

    def test_detect_jira_timezone_error_fallback(self, mock_clients):
        """Test Jira timezone detection error fallback to UTC."""
        jira_client, op_client = mock_clients
        
        jira_client.server_info.side_effect = Exception("API error")
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client
            )
            
            assert migrator.jira_timezone == "UTC"  # Default fallback

    def test_normalize_timestamp_iso_format(self, migrator_with_mocks):
        """Test timestamp normalization with ISO format."""
        timestamp_str = "2023-01-15T10:30:00.000+0000"
        
        normalized = migrator_with_mocks._normalize_timestamp(timestamp_str)
        
        assert normalized == "2023-01-15T10:30:00+00:00"

    def test_normalize_timestamp_various_formats(self, migrator_with_mocks):
        """Test timestamp normalization with various formats."""
        test_cases = [
            ("2023-01-15T10:30:00Z", "2023-01-15T10:30:00+00:00"),
            ("2023-01-15T10:30:00.123456+0000", "2023-01-15T10:30:00.123456+00:00"),
            ("2023-01-15 10:30:00", "2023-01-15T10:30:00+00:00"),  # Assume Jira timezone
            ("2023-01-15", "2023-01-15T00:00:00+00:00"),  # Date only
        ]
        
        for input_ts, expected in test_cases:
            normalized = migrator_with_mocks._normalize_timestamp(input_ts)
            assert normalized == expected, f"Failed for input: {input_ts}"

    def test_normalize_timestamp_timezone_conversion(self, mock_clients):
        """Test timestamp normalization with timezone conversion."""
        jira_client, op_client = mock_clients
        
        # Set Jira timezone to PST
        jira_client.server_info.return_value = {
            "serverTimeZone": {"timeZoneId": "America/Los_Angeles"}
        }
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client,
                target_timezone="UTC"
            )
            
            # PST time without timezone info (should be interpreted as PST)
            timestamp_str = "2023-01-15 10:30:00"
            normalized = migrator._normalize_timestamp(timestamp_str)
            
            # Should be converted to UTC (PST is UTC-8, so 10:30 PST = 18:30 UTC)
            assert "18:30:00" in normalized

    def test_normalize_timestamp_dst_handling(self, mock_clients):
        """Test timestamp normalization during DST transition."""
        jira_client, op_client = mock_clients
        
        jira_client.server_info.return_value = {
            "serverTimeZone": {"timeZoneId": "America/New_York"}
        }
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client,
                target_timezone="UTC"
            )
            
            # Test during DST (EDT = UTC-4)
            dst_timestamp = "2023-07-15 10:30:00"
            normalized_dst = migrator._normalize_timestamp(dst_timestamp)
            assert "14:30:00" in normalized_dst  # 10:30 EDT = 14:30 UTC
            
            # Test during standard time (EST = UTC-5)
            std_timestamp = "2023-01-15 10:30:00"
            normalized_std = migrator._normalize_timestamp(std_timestamp)
            assert "15:30:00" in normalized_std  # 10:30 EST = 15:30 UTC

    def test_normalize_timestamp_invalid_format(self, migrator_with_mocks):
        """Test timestamp normalization with invalid format."""
        invalid_timestamps = [
            "",
            None,
            "Invalid date",
            "2023-13-45T25:70:80",
            "Not a date at all",
        ]
        
        for invalid_ts in invalid_timestamps:
            normalized = migrator_with_mocks._normalize_timestamp(invalid_ts)
            assert normalized is None

    def test_extract_all_timestamps_complete_issue(self, migrator_with_mocks, sample_jira_issue):
        """Test extracting all timestamps from complete Jira issue."""
        timestamps = migrator_with_mocks._extract_all_timestamps(sample_jira_issue)
        
        # Standard fields
        assert "created_at" in timestamps
        assert timestamps["created_at"]["raw_value"] == "2023-01-15T10:30:00.000+0000"
        assert timestamps["created_at"]["normalized_utc"] == "2023-01-15T10:30:00+00:00"
        
        assert "updated_at" in timestamps
        assert timestamps["updated_at"]["raw_value"] == "2023-01-16T14:45:30.000+0000"
        
        assert "due_date" in timestamps
        assert timestamps["due_date"]["raw_value"] == "2023-01-20"
        
        assert "resolution_date" in timestamps
        assert timestamps["resolution_date"]["raw_value"] == "2023-01-18T16:20:15.000+0000"
        
        # Custom fields
        assert "customfield_10001" in timestamps
        assert timestamps["customfield_10001"]["field_type"] == "datetime"
        
        assert "customfield_10002" in timestamps
        assert timestamps["customfield_10002"]["field_type"] == "date"
        
        # Invalid custom field should be filtered out
        assert "customfield_10003" not in timestamps
        
        # Time tracking
        assert "time_original_estimate" in timestamps
        assert timestamps["time_original_estimate"]["raw_value"] == 28800
        assert timestamps["time_original_estimate"]["hours"] == 8.0

    def test_extract_all_timestamps_minimal_issue(self, migrator_with_mocks):
        """Test extracting timestamps from minimal Jira issue."""
        issue = Mock()
        issue.key = "TEST-456"
        issue.fields = Mock()
        
        # Only required created field
        issue.fields.created = "2023-01-15T10:30:00.000+0000"
        issue.fields.updated = None
        issue.fields.duedate = None
        issue.fields.resolutiondate = None
        
        timestamps = migrator_with_mocks._extract_all_timestamps(issue)
        
        assert "created_at" in timestamps
        assert "updated_at" not in timestamps
        assert "due_date" not in timestamps
        assert "resolution_date" not in timestamps

    def test_is_date_field_validation(self, migrator_with_mocks):
        """Test date field validation logic."""
        test_cases = [
            ("2023-01-15T10:30:00.000+0000", True),  # ISO datetime
            ("2023-01-15", True),  # Date only
            ("10:30:00", False),  # Time only
            ("2023-01-15T10:30:00", True),  # Datetime without timezone
            ("Invalid date", False),  # Invalid string
            (12345, False),  # Number
            (None, False),  # None
            ("", False),  # Empty string
        ]
        
        for value, expected in test_cases:
            result = migrator_with_mocks._is_date_field(value)
            assert result == expected, f"Failed for value: {value}"

    def test_migrate_creation_timestamp_with_rails(self, migrator_with_mocks):
        """Test creation timestamp migration with Rails console."""
        extracted = {
            "created_at": {
                "raw_value": "2023-01-15T10:30:00.000+0000",
                "normalized_utc": "2023-01-15T10:30:00+00:00"
            }
        }
        work_package_data = {"jira_key": "TEST-123"}
        
        result = migrator_with_mocks._migrate_creation_timestamp(
            extracted, work_package_data, use_rails=True
        )
        
        assert result["rails_operation"] is not None
        assert result["rails_operation"]["type"] == "set_created_at"
        assert result["rails_operation"]["jira_key"] == "TEST-123"
        assert result["rails_operation"]["timestamp"] == "2023-01-15T10:30:00+00:00"
        assert result["warnings"] == []

    def test_migrate_creation_timestamp_without_rails(self, migrator_with_mocks):
        """Test creation timestamp migration without Rails console."""
        extracted = {
            "created_at": {
                "raw_value": "2023-01-15T10:30:00.000+0000",
                "normalized_utc": "2023-01-15T10:30:00+00:00"
            }
        }
        work_package_data = {}
        
        result = migrator_with_mocks._migrate_creation_timestamp(
            extracted, work_package_data, use_rails=False
        )
        
        assert result["rails_operation"] is None
        assert work_package_data["created_at"] == "2023-01-15T10:30:00+00:00"
        assert result["warnings"] == []

    def test_migrate_creation_timestamp_missing_data(self, migrator_with_mocks):
        """Test creation timestamp migration with missing data."""
        extracted = {}  # No created_at
        work_package_data = {}
        
        result = migrator_with_mocks._migrate_creation_timestamp(
            extracted, work_package_data, use_rails=True
        )
        
        assert result["rails_operation"] is None
        assert "created_at" not in work_package_data
        assert len(result["warnings"]) == 1
        assert "No creation timestamp found" in result["warnings"][0]

    def test_migrate_creation_timestamp_normalization_failure(self, migrator_with_mocks):
        """Test creation timestamp migration with normalization failure."""
        extracted = {
            "created_at": {
                "raw_value": "Invalid date",
                "normalized_utc": None  # Normalization failed
            }
        }
        work_package_data = {}
        
        result = migrator_with_mocks._migrate_creation_timestamp(
            extracted, work_package_data, use_rails=True
        )
        
        assert result["rails_operation"] is None
        assert "created_at" not in work_package_data
        assert len(result["warnings"]) == 1
        assert "Could not normalize creation timestamp" in result["warnings"][0]

    def test_migrate_update_timestamp(self, migrator_with_mocks):
        """Test update timestamp migration."""
        extracted = {
            "updated_at": {
                "raw_value": "2023-01-16T14:45:30.000+0000",
                "normalized_utc": "2023-01-16T14:45:30+00:00"
            }
        }
        work_package_data = {"jira_key": "TEST-123"}
        
        result = migrator_with_mocks._migrate_update_timestamp(
            extracted, work_package_data, use_rails=True
        )
        
        assert result["rails_operation"] is not None
        assert result["rails_operation"]["type"] == "set_updated_at"
        assert result["warnings"] == []

    def test_migrate_due_date(self, migrator_with_mocks):
        """Test due date migration."""
        extracted = {
            "due_date": {
                "raw_value": "2023-01-20",
                "normalized_utc": "2023-01-20T00:00:00+00:00"
            }
        }
        work_package_data = {}
        
        result = migrator_with_mocks._migrate_due_date(extracted, work_package_data)
        
        assert work_package_data["due_date"] == "2023-01-20T00:00:00+00:00"
        assert result["warnings"] == []

    def test_migrate_resolution_date_with_rails(self, migrator_with_mocks):
        """Test resolution date migration with Rails console."""
        extracted = {
            "resolution_date": {
                "raw_value": "2023-01-18T16:20:15.000+0000",
                "normalized_utc": "2023-01-18T16:20:15+00:00"
            }
        }
        work_package_data = {"jira_key": "TEST-123"}
        
        result = migrator_with_mocks._migrate_resolution_date(
            extracted, work_package_data, use_rails=True
        )
        
        assert result["rails_operation"] is not None
        assert result["rails_operation"]["type"] == "set_resolution_date"
        assert result["warnings"] == []

    def test_migrate_custom_date_fields(self, migrator_with_mocks):
        """Test custom date fields migration."""
        extracted = {
            "customfield_10001": {
                "raw_value": "2023-01-17T12:00:00.000+0000",
                "normalized_utc": "2023-01-17T12:00:00+00:00",
                "field_type": "datetime"
            },
            "customfield_10002": {
                "raw_value": "2023-01-19",
                "normalized_utc": "2023-01-19T00:00:00+00:00",
                "field_type": "date"
            }
        }
        work_package_data = {}
        
        result = migrator_with_mocks._migrate_custom_date_fields(extracted, work_package_data)
        
        assert work_package_data["customfield_10001"] == "2023-01-17T12:00:00+00:00"
        assert work_package_data["customfield_10002"] == "2023-01-19T00:00:00+00:00"
        assert result["warnings"] == []

    def test_queue_rails_operation(self, migrator_with_mocks):
        """Test queuing Rails operation."""
        operation = {
            "type": "set_created_at",
            "jira_key": "TEST-123",
            "timestamp": "2023-01-15T10:30:00+00:00"
        }
        
        migrator_with_mocks.queue_rails_operation(operation)
        
        assert len(migrator_with_mocks._rails_operations_cache) == 1
        assert migrator_with_mocks._rails_operations_cache[0] == operation

    def test_generate_timestamp_preservation_script(self, migrator_with_mocks):
        """Test generation of Rails script for timestamp preservation."""
        # Queue some operations
        operations = [
            {
                "type": "set_created_at",
                "jira_key": "TEST-123",
                "timestamp": "2023-01-15T10:30:00+00:00"
            },
            {
                "type": "set_updated_at",
                "jira_key": "TEST-456",
                "timestamp": "2023-01-16T14:45:30+00:00"
            }
        ]
        
        for op in operations:
            migrator_with_mocks.queue_rails_operation(op)
        
        work_package_mapping = {
            "wp1": {"jira_key": "TEST-123", "openproject_id": 1001},
            "wp2": {"jira_key": "TEST-456", "openproject_id": 1002},
        }
        
        script = migrator_with_mocks._generate_timestamp_preservation_script(work_package_mapping)
        
        # Verify script contains expected operations
        assert "WorkPackage.find(1001)" in script
        assert "wp.created_at = Time.parse('2023-01-15T10:30:00+00:00')" in script
        assert "WorkPackage.find(1002)" in script
        assert "wp.updated_at = Time.parse('2023-01-16T14:45:30+00:00')" in script
        assert "wp.save(validate: false)" in script
        assert "Enhanced Timestamp Preservation Script" in script

    def test_execute_rails_timestamp_operations_success(self, migrator_with_mocks, mock_clients):
        """Test successful execution of Rails timestamp operations."""
        jira_client, op_client = mock_clients
        
        # Queue an operation
        operation = {
            "type": "set_created_at",
            "jira_key": "TEST-123",
            "timestamp": "2023-01-15T10:30:00+00:00"
        }
        migrator_with_mocks.queue_rails_operation(operation)
        
        # Mock successful Rails execution
        op_client.rails_client.execute_script.return_value = {"status": "success"}
        
        work_package_mapping = {
            "wp1": {"jira_key": "TEST-123", "openproject_id": 1001}
        }
        
        result = migrator_with_mocks.execute_rails_timestamp_operations(work_package_mapping)
        
        assert result["processed"] == 1
        assert result["errors"] == []
        assert len(migrator_with_mocks._rails_operations_cache) == 0  # Cache cleared

    def test_execute_rails_timestamp_operations_failure(self, migrator_with_mocks, mock_clients):
        """Test Rails timestamp operations execution failure."""
        jira_client, op_client = mock_clients
        
        # Queue an operation
        operation = {
            "type": "set_created_at",
            "jira_key": "TEST-123",
            "timestamp": "2023-01-15T10:30:00+00:00"
        }
        migrator_with_mocks.queue_rails_operation(operation)
        
        # Mock Rails execution failure
        op_client.rails_client.execute_script.side_effect = Exception("Rails connection failed")
        
        work_package_mapping = {
            "wp1": {"jira_key": "TEST-123", "openproject_id": 1001}
        }
        
        result = migrator_with_mocks.execute_rails_timestamp_operations(work_package_mapping)
        
        assert result["processed"] == 0
        assert len(result["errors"]) == 1
        assert "Rails connection failed" in result["errors"][0]

    def test_execute_rails_timestamp_operations_empty_cache(self, migrator_with_mocks):
        """Test Rails operations execution with empty cache."""
        result = migrator_with_mocks.execute_rails_timestamp_operations({})
        
        assert result["processed"] == 0
        assert result["errors"] == []

    def test_generate_timestamp_report(self, migrator_with_mocks):
        """Test generation of timestamp migration report."""
        # Add some migration results
        migrator_with_mocks.migration_results = {
            "TEST-123": TimestampMigrationResult(
                jira_key="TEST-123",
                extracted_timestamps={"created_at": {"raw_value": "2023-01-15T10:30:00+00:00"}},
                migrated_timestamps={"created_at": "2023-01-15T10:30:00+00:00"},
                rails_operations=[],
                warnings=[],
                errors=[],
                status="success"
            ),
            "TEST-456": TimestampMigrationResult(
                jira_key="TEST-456",
                extracted_timestamps={"created_at": {"raw_value": "Invalid"}},
                migrated_timestamps={},
                rails_operations=[],
                warnings=["Could not normalize timestamp"],
                errors=[],
                status="partial"
            )
        }
        
        # Queue a Rails operation
        migrator_with_mocks.queue_rails_operation({
            "type": "set_created_at",
            "jira_key": "TEST-789",
            "timestamp": "2023-01-15T10:30:00+00:00"
        })
        
        report = migrator_with_mocks.generate_timestamp_report()
        
        assert report["summary"]["total_work_packages"] == 2
        assert report["summary"]["successful_migrations"] == 1
        assert report["summary"]["partial_migrations"] == 1
        assert report["summary"]["failed_migrations"] == 0
        assert report["rails_operations_pending"] == 1
        assert "generated_at" in report

    @patch("src.utils.enhanced_timestamp_migrator.config")
    def test_save_migration_results(self, mock_config, migrator_with_mocks, tmp_path):
        """Test saving migration results to file."""
        mock_config.get_path.return_value = tmp_path
        
        # Add migration result
        migrator_with_mocks.migration_results = {
            "TEST-123": TimestampMigrationResult(
                jira_key="TEST-123",
                extracted_timestamps={"created_at": {"raw_value": "2023-01-15T10:30:00+00:00"}},
                migrated_timestamps={"created_at": "2023-01-15T10:30:00+00:00"},
                rails_operations=[],
                warnings=[],
                errors=[],
                status="success"
            )
        }
        
        migrator_with_mocks.save_migration_results()
        
        # Verify file was created
        results_file = tmp_path / "timestamp_migration_results.json"
        assert results_file.exists()
        
        # Verify content
        with results_file.open() as f:
            saved_data = json.load(f)
        
        assert "TEST-123" in saved_data
        assert saved_data["TEST-123"]["jira_key"] == "TEST-123"
        assert saved_data["TEST-123"]["status"] == "success"

    def test_migrate_timestamps_full_workflow(self, migrator_with_mocks, sample_jira_issue):
        """Test complete timestamp migration workflow."""
        work_package_data = {"jira_key": "TEST-123"}
        
        result = migrator_with_mocks.migrate_timestamps(
            sample_jira_issue, work_package_data, use_rails_for_immutable=True
        )
        
        # Verify timestamps were extracted and migrated
        assert result["jira_key"] == "TEST-123"
        assert result["status"] == "success"
        assert "extracted_timestamps" in result
        assert "migrated_timestamps" in result
        
        # Should have Rails operations for immutable fields
        assert len(result["rails_operations"]) >= 2  # created_at and updated_at
        
        # Verify result stored
        assert "TEST-123" in migrator_with_mocks.migration_results

    def test_migrate_timestamps_with_warnings(self, migrator_with_mocks):
        """Test timestamp migration with warnings for invalid data."""
        issue = Mock()
        issue.key = "TEST-456"
        issue.fields = Mock()
        
        # Invalid timestamp data
        issue.fields.created = "Invalid date"
        issue.fields.updated = "2023-01-16T14:45:30.000+0000"
        issue.fields.duedate = None
        issue.fields.resolutiondate = None
        
        work_package_data = {"jira_key": "TEST-456"}
        
        result = migrator_with_mocks.migrate_timestamps(
            issue, work_package_data, use_rails_for_immutable=True
        )
        
        # Should have warnings for invalid creation timestamp
        assert result["status"] == "partial"
        assert len(result["warnings"]) >= 1
        
        # Should still process valid fields
        assert len(result["rails_operations"]) >= 1  # updated_at

    def test_migrate_timestamps_exception_handling(self, migrator_with_mocks, mock_clients):
        """Test timestamp migration with exception during processing."""
        jira_client, _ = mock_clients
        
        # Create issue that will cause exception
        issue = Mock()
        issue.key = "TEST-ERROR"
        issue.fields = Mock()
        
        # Mock an exception during extraction
        with patch.object(migrator_with_mocks, '_extract_all_timestamps', side_effect=Exception("Extraction failed")):
            work_package_data = {"jira_key": "TEST-ERROR"}
            
            result = migrator_with_mocks.migrate_timestamps(
                issue, work_package_data, use_rails_for_immutable=False
            )
            
            assert result["status"] == "failed"
            assert len(result["errors"]) == 1
            assert "Extraction failed" in result["errors"][0]

    def test_timezone_edge_cases(self, mock_clients):
        """Test various timezone edge cases."""
        jira_client, op_client = mock_clients
        
        # Test with invalid timezone
        jira_client.server_info.return_value = {
            "serverTimeZone": {"timeZoneId": "Invalid/Timezone"}
        }
        
        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.logger = Mock()
            
            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client
            )
            
            # Should fallback to UTC
            assert migrator.jira_timezone == "UTC"

    def test_normalize_timestamp_with_datetime_object(self, migrator_with_mocks):
        """Test timestamp normalization with datetime object input."""
        dt = datetime(2023, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        
        normalized = migrator_with_mocks._normalize_timestamp(dt)
        
        assert normalized == "2023-01-15T10:30:00+00:00"

    def test_custom_field_filtering(self, migrator_with_mocks):
        """Test that only date/datetime custom fields are processed."""
        issue = Mock()
        issue.key = "TEST-CUSTOM"
        issue.fields = Mock()
        
        # Mix of date and non-date custom fields
        issue.fields.customfield_10001 = "2023-01-15T10:30:00+00:00"  # datetime
        issue.fields.customfield_10002 = "2023-01-15"  # date
        issue.fields.customfield_10003 = "Some text value"  # text
        issue.fields.customfield_10004 = 12345  # number
        issue.fields.customfield_10005 = None  # null
        
        timestamps = migrator_with_mocks._extract_all_timestamps(issue)
        
        # Should only include valid date fields
        assert "customfield_10001" in timestamps
        assert "customfield_10002" in timestamps
        assert "customfield_10003" not in timestamps
        assert "customfield_10004" not in timestamps
        assert "customfield_10005" not in timestamps 