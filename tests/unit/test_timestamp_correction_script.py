#!/usr/bin/env python3
"""Unit tests for the timestamp correction script."""

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest

# Add scripts directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from fix_timestamp_timezones import TimestampCorrectionScript


class TestTimestampCorrectionScript:
    """Test suite for TimestampCorrectionScript."""

    @pytest.fixture
    def mock_clients(self):
        """Create mock clients for the correction script."""
        jira_client = Mock()
        jira_client.jira = Mock()
        jira_client.jira.server_info = Mock()

        op_client = Mock()

        return jira_client, op_client

    @pytest.fixture
    def sample_work_packages(self):
        """Create sample work packages with various timestamp formats."""
        return [
            {
                "id": 1,
                "subject": "Test WP 1",
                "createdAt": "2023-01-15T10:30:00Z",  # UTC timestamp needing correction
                "updatedAt": "2023-01-16T14:45:30Z",
                "dueDate": "2023-01-20",
                "customFields": {"29": {"value": "PROJ-123"}},  # Jira key
            },
            {
                "id": 2,
                "subject": "Test WP 2",
                "createdAt": "2023-01-17T08:15:00+01:00",  # Already has timezone
                "updatedAt": "2023-01-18T12:30:00+01:00",
                "customFields": {
                    "29": {"value": "PROJ-124"},
                    "10001": {
                        "value": "2023-01-19T16:00:00Z",
                        "type": "datetime",
                    },  # Custom datetime field
                },
            },
            {
                "id": 3,
                "subject": "Test WP 3",
                "createdAt": "2023-01-20T15:45:00",  # Naive timestamp
                "updatedAt": "2023-01-21T09:00:00",
                "customFields": {"29": {"value": "PROJ-125"}},
            },
        ]

    @pytest.fixture
    def script_with_mocks(self, mock_clients):
        """Create script instance with mocked dependencies."""
        jira_client, op_client = mock_clients

        # Mock timezone detection
        jira_client.jira.server_info.return_value = {
            "serverTimeZone": {"timeZoneId": "Europe/Berlin"},
        }

        with (
            patch("fix_timestamp_timezones.JiraClient", return_value=jira_client),
            patch("fix_timestamp_timezones.OpenProjectClient", return_value=op_client),
            patch("fix_timestamp_timezones.config") as mock_config,
        ):

            mock_config.logger = Mock()

            script = TimestampCorrectionScript(dry_run=True, batch_size=2)
            script.jira_client = jira_client
            script.op_client = op_client

            return script

    def test_initialization_dry_run(self, mock_clients) -> None:
        """Test script initialization in dry run mode."""
        jira_client, op_client = mock_clients

        jira_client.jira.server_info.return_value = {
            "serverTimeZone": {"timeZoneId": "America/New_York"},
        }

        with (
            patch("fix_timestamp_timezones.JiraClient", return_value=jira_client),
            patch("fix_timestamp_timezones.OpenProjectClient", return_value=op_client),
            patch("fix_timestamp_timezones.config") as mock_config,
        ):

            mock_config.logger = Mock()

            script = TimestampCorrectionScript(dry_run=True, batch_size=50)

            assert script.dry_run is True
            assert script.batch_size == 50
            assert script.correct_timezone == "America/New_York"
            assert script.stats["total_packages"] == 0

    def test_initialization_apply_mode(self, mock_clients) -> None:
        """Test script initialization in apply mode."""
        jira_client, op_client = mock_clients

        jira_client.jira.server_info.return_value = {
            "serverTimeZone": {"timeZoneId": "Asia/Tokyo"},
        }

        with (
            patch("fix_timestamp_timezones.JiraClient", return_value=jira_client),
            patch("fix_timestamp_timezones.OpenProjectClient", return_value=op_client),
            patch("fix_timestamp_timezones.config") as mock_config,
        ):

            mock_config.logger = Mock()

            script = TimestampCorrectionScript(dry_run=False, batch_size=100)

            assert script.dry_run is False
            assert script.batch_size == 100
            assert script.correct_timezone == "Asia/Tokyo"

    def test_get_migrated_work_packages_success(
        self,
        script_with_mocks,
        sample_work_packages,
    ) -> None:
        """Test successful retrieval of migrated work packages."""
        script = script_with_mocks

        # Mock successful API response
        script.op_client.get.return_value = {
            "_embedded": {"elements": sample_work_packages},
        }

        result = script._get_migrated_work_packages()

        assert len(result) == 3
        assert result[0]["id"] == 1
        assert result[1]["id"] == 2
        assert result[2]["id"] == 3

        # Verify API call was made with correct filters
        script.op_client.get.assert_called_once()
        call_args = script.op_client.get.call_args
        assert "/api/v3/work_packages" in call_args[0]

    def test_get_migrated_work_packages_empty_response(self, script_with_mocks) -> None:
        """Test handling of empty work packages response."""
        script = script_with_mocks

        # Mock empty response
        script.op_client.get.return_value = {"_embedded": {"elements": []}}

        result = script._get_migrated_work_packages()

        assert result == []

    def test_get_migrated_work_packages_api_error(self, script_with_mocks) -> None:
        """Test handling of API error when fetching work packages."""
        script = script_with_mocks

        # Mock API error
        script.op_client.get.side_effect = Exception("API connection failed")

        result = script._get_migrated_work_packages()

        assert result == []
        script.logger.error.assert_called()

    def test_needs_timezone_correction_utc_timestamp(self, script_with_mocks) -> None:
        """Test detection of UTC timestamps that need correction."""
        script = script_with_mocks
        script.correct_timezone = "Europe/Berlin"  # Non-UTC timezone

        # UTC timestamp should need correction if Jira timezone is not UTC
        assert script._needs_timezone_correction("2023-01-15T10:30:00Z") is True

        # Set timezone to UTC - should not need correction
        script.correct_timezone = "UTC"
        assert script._needs_timezone_correction("2023-01-15T10:30:00Z") is False

    def test_needs_timezone_correction_timezone_aware(self, script_with_mocks) -> None:
        """Test detection of timezone-aware timestamps."""
        script = script_with_mocks
        script.correct_timezone = "Europe/Berlin"

        # Timestamp with correct timezone should not need correction
        assert script._needs_timezone_correction("2023-01-15T10:30:00+01:00") is False

        # Timestamp with different timezone should need correction
        assert script._needs_timezone_correction("2023-01-15T10:30:00-05:00") is True

    def test_needs_timezone_correction_naive_timestamp(self, script_with_mocks) -> None:
        """Test detection of naive timestamps."""
        script = script_with_mocks

        # Naive timestamp should always need correction
        assert script._needs_timezone_correction("2023-01-15T10:30:00") is True

    def test_needs_timezone_correction_invalid_timestamp(
        self,
        script_with_mocks,
    ) -> None:
        """Test handling of invalid timestamp formats."""
        script = script_with_mocks

        # Invalid formats should be marked for correction
        assert script._needs_timezone_correction("Invalid date") is True
        assert script._needs_timezone_correction("") is False
        assert script._needs_timezone_correction(None) is False

    def test_correct_timestamp_utc_to_timezone(self, script_with_mocks) -> None:
        """Test correcting UTC timestamp to target timezone."""
        script = script_with_mocks
        script.correct_timezone = "Europe/Berlin"

        result = script._correct_timestamp("2023-01-15T10:30:00Z")

        # Should convert UTC to Berlin time
        expected_dt = datetime(2023, 1, 15, 11, 30, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        assert result == expected_dt.isoformat()

    def test_correct_timestamp_naive_to_timezone(self, script_with_mocks) -> None:
        """Test correcting naive timestamp by adding timezone."""
        script = script_with_mocks
        script.correct_timezone = "America/New_York"

        result = script._correct_timestamp("2023-01-15T10:30:00")

        # Should assume naive timestamp was in Jira's timezone
        expected_dt = datetime(
            2023,
            1,
            15,
            10,
            30,
            0,
            tzinfo=ZoneInfo("America/New_York"),
        )
        assert result == expected_dt.isoformat()

    def test_correct_timestamp_convert_between_timezones(
        self,
        script_with_mocks,
    ) -> None:
        """Test converting between different timezones."""
        script = script_with_mocks
        script.correct_timezone = "Asia/Tokyo"

        # Timestamp in EST, should be converted to Tokyo time
        result = script._correct_timestamp("2023-01-15T10:30:00-05:00")

        # EST 10:30 = UTC 15:30 = Tokyo 00:30 (next day)
        expected_dt = datetime(2023, 1, 15, 15, 30, 0, tzinfo=UTC)
        tokyo_dt = expected_dt.astimezone(ZoneInfo("Asia/Tokyo"))
        assert result == tokyo_dt.isoformat()

    def test_correct_timestamp_invalid_format(self, script_with_mocks) -> None:
        """Test handling of invalid timestamp format in correction."""
        script = script_with_mocks

        # Should return original string and log warning
        result = script._correct_timestamp("Invalid date format")

        assert result == "Invalid date format"
        script.logger.warning.assert_called()

    def test_is_timestamp_field_by_type(self, script_with_mocks) -> None:
        """Test timestamp field detection by field type."""
        script = script_with_mocks

        # Field with date/time in type should be detected
        assert script._is_timestamp_field({"type": "datetime", "value": "test"}) is True
        assert script._is_timestamp_field({"type": "date", "value": "test"}) is True
        assert (
            script._is_timestamp_field({"type": "timestamp", "value": "test"}) is True
        )

        # Field without date/time in type should not be detected by type alone
        assert script._is_timestamp_field({"type": "string", "value": "test"}) is False

    def test_is_timestamp_field_by_value_format(self, script_with_mocks) -> None:
        """Test timestamp field detection by value format."""
        script = script_with_mocks

        # Valid timestamp values should be detected
        assert (
            script._is_timestamp_field(
                {"type": "string", "value": "2023-01-15T10:30:00Z"},
            )
            is True
        )
        assert (
            script._is_timestamp_field(
                {"type": "string", "value": "2023-01-15T10:30:00+01:00"},
            )
            is True
        )

        # Non-timestamp values should not be detected
        assert (
            script._is_timestamp_field({"type": "string", "value": "Not a timestamp"})
            is False
        )
        assert script._is_timestamp_field({"type": "string", "value": ""}) is False

    def test_process_work_package_with_corrections_dry_run(
        self,
        script_with_mocks,
    ) -> None:
        """Test processing work package with corrections in dry run mode."""
        script = script_with_mocks
        script.correct_timezone = "Europe/Berlin"

        work_package = {
            "id": 1,
            "createdAt": "2023-01-15T10:30:00Z",  # Needs correction
            "updatedAt": "2023-01-16T14:45:30+01:00",  # Already correct
            "customFields": {
                "10001": {
                    "value": "2023-01-17T12:00:00Z",
                    "type": "datetime",
                },  # Needs correction
            },
        }

        with patch.object(script, "_log_corrections") as mock_log:
            script._process_work_package(work_package)

        # Should have identified 2 fields needing correction (createdAt and custom field)
        mock_log.assert_called_once()
        corrections = mock_log.call_args[0][1]
        assert len(corrections) == 2

        # Check statistics were updated
        assert script.stats["packages_with_timestamps"] == 1
        assert script.stats["timestamps_corrected"] == 2

    def test_process_work_package_no_corrections_needed(
        self,
        script_with_mocks,
    ) -> None:
        """Test processing work package that needs no corrections."""
        script = script_with_mocks
        script.correct_timezone = "Europe/Berlin"

        work_package = {
            "id": 1,
            "createdAt": "2023-01-15T10:30:00+01:00",  # Already correct
            "updatedAt": "2023-01-16T14:45:30+01:00",  # Already correct
            "customFields": {},
        }

        with patch.object(script, "_log_corrections") as mock_log:
            script._process_work_package(work_package)

        # Should not have called log_corrections
        mock_log.assert_not_called()

        # Statistics should not be updated
        assert script.stats["packages_with_timestamps"] == 0
        assert script.stats["timestamps_corrected"] == 0

    def test_apply_corrections_success(self, script_with_mocks) -> None:
        """Test applying corrections successfully."""
        script = script_with_mocks
        script.dry_run = False

        corrections = [
            {
                "field": "createdAt",
                "current_value": "2023-01-15T10:30:00Z",
                "corrected_value": "2023-01-15T11:30:00+01:00",
            },
            {
                "field": "customField10001",
                "current_value": "2023-01-17T12:00:00Z",
                "corrected_value": "2023-01-17T13:00:00+01:00",
            },
        ]

        # Mock successful API response
        script.op_client.patch.return_value = {"id": 1}

        script._apply_corrections("1", corrections)

        # Verify API call was made
        script.op_client.patch.assert_called_once()
        call_args = script.op_client.patch.call_args
        assert "/api/v3/work_packages/1" in call_args[0]

        # Verify update data structure
        update_data = call_args[1]["data"]
        assert update_data["createdAt"] == "2023-01-15T11:30:00+01:00"
        assert (
            update_data["customFields"]["10001"]["value"] == "2023-01-17T13:00:00+01:00"
        )

    def test_apply_corrections_api_error(self, script_with_mocks) -> None:
        """Test handling API error when applying corrections."""
        script = script_with_mocks
        script.dry_run = False

        corrections = [
            {
                "field": "createdAt",
                "current_value": "2023-01-15T10:30:00Z",
                "corrected_value": "2023-01-15T11:30:00+01:00",
            },
        ]

        # Mock API error
        script.op_client.patch.side_effect = Exception("API error")

        script._apply_corrections("1", corrections)

        # Should have logged error and incremented error count
        script.logger.error.assert_called()
        assert script.stats["errors"] == 1

    def test_run_script_full_workflow_dry_run(
        self,
        script_with_mocks,
        sample_work_packages,
    ) -> None:
        """Test full script workflow in dry run mode."""
        script = script_with_mocks

        # Mock work packages retrieval
        script.op_client.get.return_value = {
            "_embedded": {"elements": sample_work_packages},
        }

        with patch.object(script, "_process_work_package") as mock_process:
            script.run()

        # Should have processed all work packages
        assert mock_process.call_count == 3
        assert script.stats["total_packages"] == 3

    def test_run_script_empty_work_packages(self, script_with_mocks) -> None:
        """Test script run with no work packages found."""
        script = script_with_mocks

        # Mock empty response
        script.op_client.get.return_value = {"_embedded": {"elements": []}}

        script.run()

        # Should handle empty case gracefully
        assert script.stats["total_packages"] == 0

    def test_run_script_with_batching(
        self,
        script_with_mocks,
        sample_work_packages,
    ) -> None:
        """Test script run with batching logic."""
        script = script_with_mocks
        script.batch_size = 2  # Force multiple batches

        # Mock work packages retrieval
        script.op_client.get.return_value = {
            "_embedded": {
                "elements": sample_work_packages,  # 3 packages, batch size 2 = 2 batches
            },
        }

        with patch.object(script, "_process_batch") as mock_process_batch:
            script.run()

        # Should have processed 2 batches (2 + 1 work packages)
        assert mock_process_batch.call_count == 2

        # Check batch sizes
        batch_calls = mock_process_batch.call_args_list
        assert len(batch_calls[0][0][0]) == 2  # First batch has 2 items
        assert len(batch_calls[1][0][0]) == 1  # Second batch has 1 item
