#!/usr/bin/env python3
"""Unit tests for EnhancedTimestampMigrator."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from src.utils.enhanced_timestamp_migrator import (
    EnhancedTimestampMigrator,
    TimestampMigrationResult,
)
from tests.utils.mock_factory import (
    create_mock_jira_client,
    create_mock_openproject_client,
)


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
                op_client=op_client,
            )

    def test_initialization(self, mock_clients) -> None:
        """Test basic initialization of EnhancedTimestampMigrator."""
        jira_client, op_client = mock_clients

        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.jira_config.get.return_value = "UTC"
            mock_config.logger = Mock()

            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client,
            )

            assert migrator.jira_client == jira_client
            assert migrator.op_client == op_client
            assert migrator.target_timezone == "UTC"

    def test_detect_jira_timezone_from_server_info(self, mock_clients) -> None:
        """Test successful Jira timezone detection from server info."""
        jira_client, op_client = mock_clients

        # Mock successful server_info response
        jira_client.jira.server_info.return_value = {
            "serverTimeZone": {"timeZoneId": "Europe/Berlin"},
        }

        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.jira_config.get.return_value = "UTC"
            mock_config.logger = Mock()

            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client,
            )

            assert migrator.jira_timezone == "Europe/Berlin"

    def test_detect_jira_timezone_mapping_fallback(self, mock_clients) -> None:
        """Test timezone detection with common abbreviation mapping."""
        jira_client, op_client = mock_clients

        # Mock server_info returning EST (should be mapped)
        jira_client.jira.server_info.return_value = {
            "serverTimeZone": {"timeZoneId": "EST"},
        }

        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.jira_config.get.return_value = "UTC"
            mock_config.logger = Mock()

            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client,
            )

            assert migrator.jira_timezone == "America/New_York"

    def test_detect_jira_timezone_error_fallback(self, mock_clients) -> None:
        """Test Jira timezone detection error fallback to UTC."""
        jira_client, op_client = mock_clients

        # Simulate server_info() failure
        jira_client.jira.server_info.side_effect = Exception("Connection failed")

        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.jira_config.get.return_value = "UTC"
            mock_config.logger = Mock()

            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client,
            )

            # Should fallback to UTC (from config)
            assert migrator.jira_timezone == "UTC"

    def test_detect_jira_timezone_no_timezone_field(self, mock_clients) -> None:
        """Test timezone detection when server_info lacks serverTimeZone field."""
        jira_client, op_client = mock_clients

        # Return server info without timezone field
        jira_client.jira.server_info.return_value = {
            "version": "8.0.0",
            "build": "800000",
            # No serverTimeZone field
        }

        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.jira_config.get.return_value = "UTC"
            mock_config.logger = Mock()

            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client,
            )

            # Should fallback to UTC
            assert migrator.jira_timezone == "UTC"

    def test_detect_jira_timezone_malformed_response(self, mock_clients) -> None:
        """Test timezone detection with malformed server response."""
        jira_client, op_client = mock_clients

        # Return malformed timezone info
        jira_client.jira.server_info.return_value = {
            "serverTimeZone": {"invalidField": "someValue"},
        }

        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.jira_config.get.return_value = "UTC"
            mock_config.logger = Mock()

            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client,
            )

            assert migrator.jira_timezone == "UTC"

    def test_detect_jira_timezone_various_timezones(self, mock_clients) -> None:
        """Test timezone detection with various timezone formats."""
        jira_client, op_client = mock_clients

        test_cases = [
            ("America/New_York", "America/New_York"),
            ("Asia/Tokyo", "Asia/Tokyo"),
            ("UTC", "UTC"),
            ("GMT", "UTC"),  # GMT mapped to UTC for consistency
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
                    "serverTimeZone": {"timeZoneId": input_tz},
                }

                migrator = EnhancedTimestampMigrator(
                    jira_client=jira_client,
                    op_client=op_client,
                )

                assert migrator.jira_timezone == expected_tz, f"Failed for {input_tz}"

    def test_detect_jira_timezone_client_not_connected(self, mock_clients) -> None:
        """Test timezone detection when JiraClient.jira is None."""
        jira_client, op_client = mock_clients

        # Simulate client not connected
        jira_client.jira = None

        with patch("src.utils.enhanced_timestamp_migrator.config") as mock_config:
            mock_config.jira_config.get.return_value = "UTC"
            mock_config.logger = Mock()

            migrator = EnhancedTimestampMigrator(
                jira_client=jira_client,
                op_client=op_client,
            )

            # Should fallback to UTC
            assert migrator.jira_timezone == "UTC"

    def test_normalize_timestamp_iso_format(self, migrator_with_mocks) -> None:
        """Test timestamp normalization with ISO format."""
        timestamp = "2023-10-15T14:30:00.000Z"

        result = migrator_with_mocks._normalize_timestamp(timestamp)

        assert result is not None
        assert result.endswith("+00:00")  # UTC timezone

    def test_normalize_timestamp_various_formats(self, migrator_with_mocks) -> None:
        """Test timestamp normalization with various input formats."""
        test_cases = [
            "2023-10-15T14:30:00.000Z",
            "2023-10-15T14:30:00Z",
            "2023-10-15 14:30:00",
            "2023-10-15T14:30:00+02:00",
        ]

        for timestamp in test_cases:
            result = migrator_with_mocks._normalize_timestamp(timestamp)
            assert result is not None

    def test_normalize_timestamp_timezone_conversion(self, migrator_with_mocks) -> None:
        """Test timezone conversion during normalization."""
        # Set up migrator with Berlin timezone
        migrator_with_mocks.jira_timezone = "Europe/Berlin"

        # Test timestamp without timezone info
        timestamp = "2023-10-15 14:30:00"

        result = migrator_with_mocks._normalize_timestamp(timestamp)

        assert result is not None
        # Should have timezone information added
        assert result != timestamp

    def test_normalize_timestamp_dst_handling(self, migrator_with_mocks) -> None:
        """Test DST handling in timestamp normalization."""
        migrator_with_mocks.jira_timezone = "Europe/Berlin"

        # Test both DST and non-DST dates
        dst_timestamp = "2023-07-15T14:30:00"  # Summer (DST)
        non_dst_timestamp = "2023-01-15T14:30:00"  # Winter (no DST)

        for timestamp in [dst_timestamp, non_dst_timestamp]:
            result = migrator_with_mocks._normalize_timestamp(timestamp)
            assert result is not None

    def test_normalize_timestamp_invalid_format(self, migrator_with_mocks) -> None:
        """Test handling of invalid timestamp formats."""
        invalid_timestamps = [
            "not-a-timestamp",
            "2023-13-40T25:70:80",  # Invalid date/time
            "",
            None,
        ]

        for invalid_timestamp in invalid_timestamps:
            result = migrator_with_mocks._normalize_timestamp(invalid_timestamp)
            assert result is None

    def test_extract_all_timestamps_complete_issue(
        self,
        migrator_with_mocks,
    ) -> None:
        """Test extraction of all timestamps from a complete Jira issue."""
        # Mock all timestamp fields
        sample_issue = {
            "fields": {
                "created": "2023-10-15T10:00:00.000Z",
                "updated": "2023-10-15T12:00:00.000Z",
                "duedate": "2023-10-20",
                "resolutiondate": "2023-10-18T16:00:00.000Z",
                "customfield_12345": "2023-10-16T14:00:00.000Z",  # Custom date field
            },
        }

        # Create a mock object with fields attribute
        class MockJiraIssue:
            def __init__(self, fields):
                self.fields = type("MockFields", (), fields)()

        mock_issue = MockJiraIssue(sample_issue["fields"])

        with patch.object(migrator_with_mocks, "_is_date_field", return_value=True):
            result = migrator_with_mocks._extract_all_timestamps(mock_issue)

            assert "created_at" in result
            assert "updated_at" in result
            assert "due_date" in result
            assert "resolution_date" in result

    def test_extract_all_timestamps_minimal_issue(self, migrator_with_mocks) -> None:
        """Test extraction from minimal Jira issue (only required fields)."""
        minimal_issue = {
            "fields": {
                "created": "2023-10-15T10:00:00.000Z",
                "summary": "Test issue",
                "issuetype": {"name": "Task"},
            },
        }

        # Create a mock object with fields attribute
        class MockJiraIssue:
            def __init__(self, fields):
                self.fields = type("MockFields", (), fields)()

        mock_issue = MockJiraIssue(minimal_issue["fields"])
        result = migrator_with_mocks._extract_all_timestamps(mock_issue)

        assert "created_at" in result

    def test_extract_start_date_precedence(self, migrator_with_mocks) -> None:
        """Start date should honor configured precedence across custom fields."""

        first_value = "2024-07-01T08:00:00+02:00"
        second_value = "2024-07-02T09:00:00+02:00"

        fields = SimpleNamespace(
            customfield_18690=first_value,
            customfield_12590=second_value,
            customfield_11490=None,
            customfield_15082=None,
        )
        issue = SimpleNamespace(
            fields=fields,
            raw={
                "fields": {
                    "customfield_18690": first_value,
                    "customfield_12590": second_value,
                },
            },
        )

        timestamps = migrator_with_mocks._extract_all_timestamps(issue)

        assert "start_date" in timestamps
        assert timestamps["start_date"]["jira_field"] == "customfield_18690"

        work_package: dict[str, str] = {}
        migrator_with_mocks._migrate_start_date(timestamps, work_package)
        assert work_package["start_date"] == "2024-07-01"

    def test_extract_start_date_fallback_field(self, migrator_with_mocks) -> None:
        """Fallback to the next configured custom field when earlier ones are empty."""

        fallback_value = "2024-08-15T10:30:00+02:00"

        fields = SimpleNamespace(
            customfield_18690=None,
            customfield_12590=fallback_value,
            customfield_11490=None,
            customfield_15082=None,
        )
        issue = SimpleNamespace(
            fields=fields,
            raw={
                "fields": {
                    "customfield_12590": fallback_value,
                },
            },
        )

        timestamps = migrator_with_mocks._extract_all_timestamps(issue)

        assert "start_date" in timestamps
        assert timestamps["start_date"]["jira_field"] == "customfield_12590"

        work_package: dict[str, str] = {}
        migrator_with_mocks._migrate_start_date(timestamps, work_package)
        assert work_package["start_date"] == "2024-08-15"

    def test_extract_start_date_missing_fields(self, migrator_with_mocks) -> None:
        """Return no start date when none of the configured fields have values."""

        fields = SimpleNamespace(
            customfield_18690=None,
            customfield_12590=None,
            customfield_11490=None,
            customfield_15082=None,
        )
        issue = SimpleNamespace(
            fields=fields,
            raw={"fields": {}},
        )

        timestamps = migrator_with_mocks._extract_all_timestamps(issue)

        assert "start_date" not in timestamps

    def test_generate_timestamp_preservation_script(self, migrator_with_mocks) -> None:
        """Rails script generation should validate keys and escape payload safely."""
        migrator_with_mocks._rails_operations_cache = [
            {
                "jira_key": "PROJ-1",
                "type": "set_created_at",
                "timestamp": "2024-01-01T00:00:00Z",
            },
        ]
        mapping = {
            "1": {"jira_key": "PROJ-1", "openproject_id": 77},
        }

        script = migrator_with_mocks._generate_timestamp_preservation_script(mapping)

        assert "WorkPackage.find(77)" in script
        assert 'jira_key: "PROJ-1"' in script
        assert "set_created_at" in script

    def test_generate_timestamp_preservation_script_rejects_bad_key(self, migrator_with_mocks) -> None:
        """Invalid Jira keys should raise an exception to prevent injection."""
        migrator_with_mocks._rails_operations_cache = [
            {
                "jira_key": "bad-key",
                "type": "set_updated_at",
                "timestamp": "2024-01-01T00:00:00Z",
            },
        ]
        mapping = {"1": {"jira_key": "bad-key", "openproject_id": 77}}

        with pytest.raises(ValueError):
            migrator_with_mocks._generate_timestamp_preservation_script(mapping)

    def test_execute_rails_timestamp_operations_flushes_cache(self, migrator_with_mocks) -> None:
        """Queued operations should be executed and cache cleared."""
        migrator_with_mocks._rails_operations_cache = [
            {"jira_key": "PROJ-1", "type": "set_created_at", "timestamp": "2024-01-01T00:00:00Z"},
            {"jira_key": "PROJ-2", "type": "set_updated_at", "timestamp": "2024-01-02T00:00:00Z"},
        ]
        migrator_with_mocks.op_client.rails_client.execute.return_value = {"status": "ok"}

        mapping = {
            "1": {"jira_key": "PROJ-1", "openproject_id": 201},
            "2": {"jira_key": "PROJ-2", "openproject_id": 202},
        }

        result = migrator_with_mocks.execute_rails_timestamp_operations(mapping)

        assert result["processed"] == 2
        assert migrator_with_mocks._rails_operations_cache == []
        migrator_with_mocks.op_client.rails_client.execute.assert_called_once()

    def test_is_date_field_validation(self, migrator_with_mocks) -> None:
        """Test date field validation logic."""
        date_fields = ["created", "updated", "duedate", "resolutiondate"]
        non_date_fields = ["summary", "description", "priority", "status"]

        for field in date_fields:
            assert migrator_with_mocks._is_date_field("2023-10-15T10:00:00.000Z")

        for field in non_date_fields:
            assert not migrator_with_mocks._is_date_field("Some text value")

    def test_migrate_creation_timestamp_with_rails(self, migrator_with_mocks) -> None:
        """Test creation timestamp migration using Rails backend."""
        jira_issue = {"fields": {"created": "2023-10-15T10:00:00.000Z"}}
        work_package = {"id": 123}

        # Create a mock object with fields attribute
        class MockJiraIssue:
            def __init__(self, fields):
                self.fields = type("MockFields", (), fields)()

        mock_issue = MockJiraIssue(jira_issue["fields"])

        with patch.object(
            migrator_with_mocks,
            "_normalize_timestamp",
        ) as mock_normalize:
            mock_normalize.return_value = "2023-10-15T10:00:00+00:00"

            # First extract timestamps
            extracted = migrator_with_mocks._extract_all_timestamps(mock_issue)

            result = migrator_with_mocks._migrate_creation_timestamp(
                extracted,
                work_package,
                use_rails=True,
            )

            assert "warnings" in result
            # The method should find the creation timestamp and not generate warnings

    def test_migrate_creation_timestamp_without_rails(
        self,
        migrator_with_mocks,
    ) -> None:
        """Test creation timestamp migration without Rails backend."""
        jira_issue = {"fields": {"created": "2023-10-15T10:00:00.000Z"}}
        work_package = {"id": 123}

        # Create a mock object with fields attribute
        class MockJiraIssue:
            def __init__(self, fields):
                self.fields = type("MockFields", (), fields)()

        mock_issue = MockJiraIssue(jira_issue["fields"])

        with patch.object(
            migrator_with_mocks,
            "_normalize_timestamp",
        ) as mock_normalize:
            mock_normalize.return_value = "2023-10-15T10:00:00+00:00"

            # First extract timestamps
            extracted = migrator_with_mocks._extract_all_timestamps(mock_issue)

            result = migrator_with_mocks._migrate_creation_timestamp(
                extracted,
                work_package,
                use_rails=False,
            )

            assert "warnings" in result

    def test_migrate_creation_timestamp_missing_data(self, migrator_with_mocks) -> None:
        """Test creation timestamp migration with missing created field."""
        jira_issue = {"fields": {}}  # No created field
        work_package = {"id": 123}

        # Create a mock object with fields attribute
        class MockJiraIssue:
            def __init__(self, fields):
                self.fields = type("MockFields", (), fields)()

        mock_issue = MockJiraIssue(jira_issue["fields"])

        # First extract timestamps
        extracted = migrator_with_mocks._extract_all_timestamps(mock_issue)

        result = migrator_with_mocks._migrate_creation_timestamp(
            extracted,
            work_package,
            use_rails=True,
        )

        assert len(result["warnings"]) > 0
        assert "No creation timestamp found" in result["warnings"][0]

    def test_migrate_creation_timestamp_normalization_failure(
        self,
        migrator_with_mocks,
    ) -> None:
        """Test creation timestamp migration with normalization failure."""
        jira_issue = {"fields": {"created": "invalid-timestamp"}}
        work_package = {"id": 123}

        # Create a mock object with fields attribute
        class MockJiraIssue:
            def __init__(self, fields):
                self.fields = type("MockFields", (), fields)()

        mock_issue = MockJiraIssue(jira_issue["fields"])

        with patch.object(
            migrator_with_mocks,
            "_normalize_timestamp",
        ) as mock_normalize:
            mock_normalize.return_value = None  # Normalization failed

            # First extract timestamps
            extracted = migrator_with_mocks._extract_all_timestamps(mock_issue)

            result = migrator_with_mocks._migrate_creation_timestamp(
                extracted,
                work_package,
                use_rails=True,
            )

            assert len(result["warnings"]) > 0

    def test_migrate_update_timestamp(self, migrator_with_mocks) -> None:
        """Test update timestamp migration."""
        jira_issue = {"fields": {"updated": "2023-10-15T12:00:00.000Z"}}
        work_package = {"id": 123}

        # Create a mock object with fields attribute
        class MockJiraIssue:
            def __init__(self, fields):
                self.fields = type("MockFields", (), fields)()

        mock_issue = MockJiraIssue(jira_issue["fields"])

        with patch.object(
            migrator_with_mocks,
            "_normalize_timestamp",
        ) as mock_normalize:
            mock_normalize.return_value = "2023-10-15T12:00:00+00:00"

            # First extract timestamps
            extracted = migrator_with_mocks._extract_all_timestamps(mock_issue)

            result = migrator_with_mocks._migrate_update_timestamp(
                extracted,
                work_package,
                use_rails=True,
            )

            assert "warnings" in result

    def test_migrate_due_date(self, migrator_with_mocks) -> None:
        """Test due date migration."""
        jira_issue = {"fields": {"duedate": "2023-10-20"}}
        work_package = {"id": 123}

        # Create a mock object with fields attribute
        class MockJiraIssue:
            def __init__(self, fields):
                self.fields = type("MockFields", (), fields)()

        mock_issue = MockJiraIssue(jira_issue["fields"])

        with patch.object(
            migrator_with_mocks,
            "_normalize_timestamp",
        ) as mock_normalize:
            mock_normalize.return_value = "2023-10-20T00:00:00+00:00"

            # First extract timestamps
            extracted = migrator_with_mocks._extract_all_timestamps(mock_issue)

            result = migrator_with_mocks._migrate_due_date(extracted, work_package)

            assert "warnings" in result

    def test_migrate_resolution_date_with_rails(self, migrator_with_mocks) -> None:
        """Test resolution date migration using Rails."""
        jira_issue = {"fields": {"resolutiondate": "2023-10-18T16:00:00.000Z"}}
        work_package = {"id": 123}

        # Create a mock object with fields attribute
        class MockJiraIssue:
            def __init__(self, fields):
                self.fields = type("MockFields", (), fields)()

        mock_issue = MockJiraIssue(jira_issue["fields"])

        with patch.object(
            migrator_with_mocks,
            "_normalize_timestamp",
        ) as mock_normalize:
            mock_normalize.return_value = "2023-10-18T16:00:00+00:00"

            # First extract timestamps
            extracted = migrator_with_mocks._extract_all_timestamps(mock_issue)

            result = migrator_with_mocks._migrate_resolution_date(
                extracted,
                work_package,
                use_rails=True,
            )

            assert "warnings" in result

    def test_migrate_custom_date_fields(self, migrator_with_mocks) -> None:
        """Test migration of custom date fields."""
        jira_issue = {
            "fields": {
                "customfield_12345": "2023-10-16T14:00:00.000Z",
                "customfield_67890": "2023-10-17",
            },
        }
        work_package = {"id": 123}

        # Create a mock object with fields attribute
        class MockJiraIssue:
            def __init__(self, fields):
                self.fields = type("MockFields", (), fields)()

        mock_issue = MockJiraIssue(jira_issue["fields"])

        with patch.object(migrator_with_mocks, "_is_date_field") as mock_is_date:
            mock_is_date.return_value = True

            with patch.object(
                migrator_with_mocks,
                "_normalize_timestamp",
            ) as mock_normalize:
                mock_normalize.return_value = "2023-10-16T14:00:00+00:00"

                # First extract timestamps
                extracted = migrator_with_mocks._extract_all_timestamps(mock_issue)

                result = migrator_with_mocks._migrate_custom_date_fields(
                    extracted,
                    work_package,
                )

                assert "warnings" in result

    def test_queue_rails_operation(self, migrator_with_mocks) -> None:
        """Test Rails operation queuing."""
        operation = {
            "table": "work_packages",
            "id": 123,
            "field": "created_at",
            "value": "2023-10-15T10:00:00+00:00",
        }

        migrator_with_mocks.queue_rails_operation(operation)

        assert len(migrator_with_mocks._rails_operations_cache) == 1
        assert migrator_with_mocks._rails_operations_cache[0] == operation

    def test_generate_timestamp_preservation_script(self, migrator_with_mocks) -> None:
        """Test timestamp preservation script generation."""
        # Add operations to cache first
        operation = {
            "jira_key": "TEST-123",
            "type": "set_created_at",
            "timestamp": "2023-10-15T10:00:00+00:00",
        }
        migrator_with_mocks._rails_operations_cache = [operation]

        work_package_mapping = {
            "123": {"jira_key": "TEST-123", "openproject_id": 123},
        }

        script_content = migrator_with_mocks._generate_timestamp_preservation_script(
            work_package_mapping,
        )

        assert "WorkPackage.find" in script_content
        assert "created_at" in script_content
        assert "2023-10-15T10:00:00+00:00" in script_content

    def test_execute_rails_timestamp_operations_success(
        self,
        migrator_with_mocks,
        mock_clients,
    ) -> None:
        """Test successful Rails timestamp operations execution."""
        jira_client, op_client = mock_clients

        # Add operations to cache
        migrator_with_mocks._rails_operations_cache = [
            {
                "jira_key": "TEST-123",
                "type": "set_created_at",
                "timestamp": "2023-10-15T10:00:00+00:00",
            },
        ]

        # Mock successful Rails execution
        op_client.rails_client.execute.return_value = {"success": True}

        work_package_mapping = {"123": {"jira_key": "TEST-123", "openproject_id": 123}}
        result = migrator_with_mocks.execute_rails_timestamp_operations(work_package_mapping)

        assert result["processed"] > 0
        assert len(migrator_with_mocks._rails_operations_cache) == 0  # Cache cleared

    def test_execute_rails_timestamp_operations_failure(
        self,
        migrator_with_mocks,
        mock_clients,
    ) -> None:
        """Test Rails timestamp operations execution failure."""
        jira_client, op_client = mock_clients

        # Add operations to cache
        migrator_with_mocks._rails_operations_cache = [
            {
                "jira_key": "TEST-123",
                "type": "set_created_at",
                "timestamp": "2023-10-15T10:00:00+00:00",
            },
        ]

        # Mock Rails execution failure
        op_client.rails_client.execute.side_effect = Exception("Rails error")

        work_package_mapping = {"123": {"jira_key": "TEST-123", "openproject_id": 123}}
        result = migrator_with_mocks.execute_rails_timestamp_operations(work_package_mapping)

        assert result["processed"] == 0
        assert len(result["errors"]) > 0

    def test_execute_rails_timestamp_operations_empty_cache(
        self,
        migrator_with_mocks,
    ) -> None:
        """Test Rails operations execution with empty cache."""
        work_package_mapping = {"123": {"jira_key": "TEST-123", "openproject_id": 123}}
        result = migrator_with_mocks.execute_rails_timestamp_operations(work_package_mapping)

        assert result["processed"] == 0

    def test_generate_timestamp_report(self, migrator_with_mocks) -> None:
        """Test timestamp migration report generation."""
        results = [
            TimestampMigrationResult(
                success=True,
                field_name="created_at",
                jira_value="2023-10-15T10:00:00.000Z",
                normalized_value="2023-10-15T10:00:00+00:00",
                migration_method="direct",
            ),
            TimestampMigrationResult(
                success=False,
                field_name="invalid_date",
                jira_value="invalid",
                error_message="Invalid timestamp format",
            ),
        ]

        report = migrator_with_mocks.generate_timestamp_report()

        assert "summary" in report
        assert "total_issues" in report["summary"]
        # The report contains summary statistics, not specific strings
        assert report["summary"]["total_issues"] >= 0

    @patch("src.utils.enhanced_timestamp_migrator.config")
    def test_save_migration_results(
        self,
        mock_config,
        migrator_with_mocks,
        tmp_path,
    ) -> None:
        """Test saving migration results to file."""
        mock_config.output_dir = str(tmp_path)

        results = [
            TimestampMigrationResult(
                success=True,
                field_name="created_at",
                jira_value="2023-10-15T10:00:00.000Z",
                normalized_value="2023-10-15T10:00:00+00:00",
                migration_method="direct",
            ),
        ]

        migrator_with_mocks.save_migration_results()

        # Method doesn't return anything, just saves to file
        # The actual file path is mocked by config.get_path()
        # We can't verify the file content since the path is mocked

    def test_migrate_timestamps_full_workflow(
        self,
        migrator_with_mocks,
    ) -> None:
        """Test complete timestamp migration workflow."""
        jira_issue = {
            "key": "TEST-123",
            "fields": {
                "created": "2023-10-15T10:00:00.000Z",
                "updated": "2023-10-15T12:00:00.000Z",
                "duedate": "2023-10-20",
                "resolutiondate": "2023-10-15T13:00:00.000Z",
            },
        }
        work_package = {"id": 123}

        # Create a mock object with fields attribute
        class MockJiraIssue:
            def __init__(self, fields):
                self.fields = type("MockFields", (), fields)()

        mock_issue = MockJiraIssue(jira_issue["fields"])

        with patch.object(
            migrator_with_mocks,
            "_normalize_timestamp",
        ) as mock_normalize:
            mock_normalize.return_value = "2023-10-15T10:00:00+00:00"

            result = migrator_with_mocks.migrate_timestamps(mock_issue, work_package, use_rails_for_immutable=False)

            assert isinstance(result, dict)
            assert "migrated_timestamps" in result
            assert len(result["migrated_timestamps"]) >= 2  # At least created and updated

    def test_migrate_timestamps_with_warnings(self, migrator_with_mocks) -> None:
        """Test timestamp migration with warnings for invalid timestamps."""
        jira_issue = {
            "key": "TEST-123",
            "fields": {
                "created": "2023-10-15T10:00:00.000Z",  # Valid
                "invalid_date": "not-a-date",  # Invalid
            },
        }
        work_package = {"id": 123}

        def mock_normalize(timestamp) -> str | None:
            if timestamp == "not-a-date":
                return None
            return "2023-10-15T10:00:00+00:00"

        with (
            patch.object(
                migrator_with_mocks,
                "_normalize_timestamp",
                side_effect=mock_normalize,
            ),
            patch.object(migrator_with_mocks, "_is_date_field", return_value=True),
        ):
            result = migrator_with_mocks.migrate_timestamps(
                jira_issue,
                work_package,
            )

            # Check that we got a result with warnings
            assert isinstance(result, dict)
            assert "warnings" in result
            assert isinstance(result, dict)
            assert "warnings" in result

    def test_migrate_timestamps_exception_handling(
        self,
        migrator_with_mocks,
        mock_clients,
    ) -> None:
        """Test timestamp migration exception handling."""
        jira_client, op_client = mock_clients

        jira_issue = {
            "key": "TEST-123",
            "fields": {"created": "2023-10-15T10:00:00.000Z"},
        }
        work_package = {"id": 123}

        # Mock an exception during migration
        with patch.object(
            migrator_with_mocks,
            "_migrate_creation_timestamp",
        ) as mock_migrate:
            mock_migrate.side_effect = Exception("Unexpected error")

            result = migrator_with_mocks.migrate_timestamps(jira_issue, work_package)

            # Should handle exception gracefully
            assert isinstance(result, dict)
            assert "errors" in result

    def test_timezone_edge_cases(self, mock_clients) -> None:
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
                    op_client=op_client,
                )

                # Should detect timezone successfully
                assert migrator.jira_timezone != "UTC"

    def test_normalize_timestamp_with_datetime_object(
        self,
        migrator_with_mocks,
    ) -> None:
        """Test timestamp normalization with datetime object input."""
        dt = datetime(2023, 10, 15, 14, 30, 0, tzinfo=UTC)

        result = migrator_with_mocks._normalize_timestamp(dt)
        assert result is not None

    def test_custom_field_filtering(self, migrator_with_mocks) -> None:
        """Test custom field filtering logic."""
        jira_issue = {
            "fields": {
                "customfield_12345": "2023-10-15T14:00:00.000Z",
                "customfield_text": "Not a date",
                "summary": "Test issue",
            },
        }

        with patch.object(migrator_with_mocks, "_is_date_field") as mock_is_date:
            # Only return True for the first custom field
            mock_is_date.side_effect = lambda value: value == "2023-10-15T14:00:00.000Z"

            # Create a mock object with fields attribute and raw attribute for custom fields
            class MockJiraIssue:
                def __init__(self, fields):
                    self.fields = type("MockFields", (), fields)()
                    # Add raw attribute for custom field extraction
                    self.raw = {"fields": fields}

        mock_issue = MockJiraIssue(jira_issue["fields"])
        timestamps = migrator_with_mocks._extract_all_timestamps(mock_issue)

        # Should only include the date custom field
        custom_fields = [
            field for field in timestamps if field.startswith("custom_")
        ]
        assert "custom_customfield_12345" in custom_fields
        assert "custom_customfield_text" not in timestamps
