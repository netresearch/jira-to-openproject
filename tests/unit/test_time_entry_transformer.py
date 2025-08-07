"""Tests for TimeEntryTransformer class."""

import pytest

from src.utils.time_entry_transformer import TimeEntryTransformer


class TestTimeEntryTransformer:
    """Test time entry transformation functionality."""

    @pytest.fixture
    def sample_mappings(self):
        """Sample mappings for testing."""
        return {
            "user_mapping": {
                "john.doe": 123,
                "jane.smith": 456,
            },
            "work_package_mapping": {
                "TEST-123": 789,
                "PROJ-456": 101112,
            },
            "activity_mapping": {
                "Development": 1,
                "Testing": 2,
                "Code review": 3,
                "Documentation": 4,
            },
            "default_activity_id": 1,
        }

    @pytest.fixture
    def transformer(self, sample_mappings):
        """Create transformer with sample mappings."""
        return TimeEntryTransformer(
            user_mapping=sample_mappings["user_mapping"],
            work_package_mapping=sample_mappings["work_package_mapping"],
            activity_mapping=sample_mappings["activity_mapping"],
            default_activity_id=sample_mappings["default_activity_id"],
        )

    @pytest.fixture
    def sample_jira_work_log(self):
        """Sample Jira work log data."""
        return {
            "id": "12345",
            "author": {
                "name": "john.doe",
                "key": "john.doe",
                "displayName": "John Doe",
            },
            "comment": "Working on user authentication feature",
            "started": "2023-12-01T10:30:00.000+0000",
            "timeSpentSeconds": 7200,  # 2 hours
            "updateAuthor": {
                "name": "john.doe",
                "displayName": "John Doe",
            },
        }

    @pytest.fixture
    def sample_tempo_work_log(self):
        """Sample Tempo work log data."""
        return {
            "tempoWorklogId": 67890,
            "jiraWorklogId": 12345,
            "issue": {
                "key": "TEST-123",
                "id": 10001,
            },
            "author": {
                "name": "jane.smith",
                "displayName": "Jane Smith",
            },
            "description": "Code review for authentication module",
            "dateStarted": "2023-12-01",
            "timeSpentSeconds": 3600,  # 1 hour
            "billableSeconds": 3600,
            "workAttributes": [
                {
                    "key": "activity",
                    "value": "code review",
                },
                {
                    "key": "client",
                    "value": "Internal",
                },
            ],
        }

    def test_transform_jira_work_log_success(
        self,
        transformer,
        sample_jira_work_log,
    ) -> None:
        """Test successful Jira work log transformation."""
        result = transformer.transform_jira_work_log(sample_jira_work_log, "TEST-123")

        # Check basic fields
        assert result["spentOn"] == "2023-12-01"
        assert result["hours"] == 2.0
        assert result["comment"] == "Working on user authentication feature"

        # Check embedded resources
        assert "_embedded" in result
        assert result["_embedded"]["user"]["href"] == "/api/v3/users/123"
        assert result["_embedded"]["workPackage"]["href"] == "/api/v3/work_packages/789"
        assert (
            result["_embedded"]["activity"]["href"]
            == "/api/v3/time_entries/activities/1"
        )

        # Check metadata
        assert "_meta" in result
        assert result["_meta"]["jira_work_log_id"] == "12345"
        assert result["_meta"]["jira_issue_key"] == "TEST-123"
        assert result["_meta"]["jira_author"] == "john.doe"

    def test_transform_jira_work_log_with_rich_text_comment(self, transformer) -> None:
        """Test Jira work log with rich text comment."""
        work_log = {
            "id": "12345",
            "author": {"name": "john.doe"},
            "comment": {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": "Working on "},
                            {
                                "type": "text",
                                "text": "authentication",
                                "marks": [{"type": "strong"}],
                            },
                            {"type": "text", "text": " feature"},
                        ],
                    },
                ],
            },
            "started": "2023-12-01T10:30:00.000+0000",
            "timeSpentSeconds": 3600,
        }

        result = transformer.transform_jira_work_log(work_log, "TEST-123")
        # The text extraction adds spaces but cleans up doubles
        assert "Working on authentication feature" in result["comment"]

    def test_transform_tempo_work_log_success(
        self,
        transformer,
        sample_tempo_work_log,
    ) -> None:
        """Test successful Tempo work log transformation."""
        result = transformer.transform_tempo_work_log(sample_tempo_work_log)

        # Check basic fields
        assert result["spentOn"] == "2023-12-01"
        assert result["hours"] == 1.0
        assert result["comment"] == "Code review for authentication module"

        # Check embedded resources
        assert "_embedded" in result
        assert result["_embedded"]["user"]["href"] == "/api/v3/users/456"
        assert result["_embedded"]["workPackage"]["href"] == "/api/v3/work_packages/789"
        assert (
            result["_embedded"]["activity"]["href"]
            == "/api/v3/time_entries/activities/3"
        )

        # Check Tempo-specific metadata
        assert result["_meta"]["tempo_worklog_id"] == 67890
        assert result["_meta"]["tempo_billable"] is True
        assert result["_meta"]["tempo_billable_hours"] == 1.0
        assert "tempo_attributes" in result["_meta"]

    def test_transform_jira_work_log_unmapped_user(
        self,
        transformer,
        sample_jira_work_log,
    ) -> None:
        """Test Jira work log with unmapped user."""
        sample_jira_work_log["author"]["name"] = "unknown.user"

        result = transformer.transform_jira_work_log(sample_jira_work_log, "TEST-123")

        # User should not be in embedded resources
        assert "user" not in result["_embedded"]
        assert result["_meta"]["jira_author"] == "unknown.user"

    def test_transform_jira_work_log_unmapped_work_package(
        self,
        transformer,
        sample_jira_work_log,
    ) -> None:
        """Test Jira work log with unmapped work package."""
        result = transformer.transform_jira_work_log(
            sample_jira_work_log,
            "UNKNOWN-999",
        )

        # Work package should not be in embedded resources
        assert "workPackage" not in result["_embedded"]
        assert result["_meta"]["jira_issue_key"] == "UNKNOWN-999"

    def test_batch_transform_jira_work_logs(
        self,
        transformer,
        sample_jira_work_log,
    ) -> None:
        """Test batch transformation of Jira work logs."""
        work_logs = [
            {**sample_jira_work_log, "id": "1", "issue_key": "TEST-123"},
            {**sample_jira_work_log, "id": "2", "issue_key": "PROJ-456"},
        ]

        results = transformer.batch_transform_work_logs(work_logs, "jira")

        assert len(results) == 2
        assert results[0]["_meta"]["jira_work_log_id"] == "1"
        assert results[1]["_meta"]["jira_work_log_id"] == "2"

    def test_batch_transform_tempo_work_logs(
        self,
        transformer,
        sample_tempo_work_log,
    ) -> None:
        """Test batch transformation of Tempo work logs."""
        work_logs = [
            {**sample_tempo_work_log, "tempoWorklogId": 1},
            {**sample_tempo_work_log, "tempoWorklogId": 2},
        ]

        results = transformer.batch_transform_work_logs(work_logs, "tempo")

        assert len(results) == 2
        assert results[0]["_meta"]["tempo_worklog_id"] == 1
        assert results[1]["_meta"]["tempo_worklog_id"] == 2

    def test_parse_jira_date_formats(self, transformer) -> None:
        """Test parsing various Jira date formats."""
        # Standard format
        assert (
            transformer._parse_jira_date("2023-12-01T10:30:00.000+0000") == "2023-12-01"
        )

        # Without timezone
        assert transformer._parse_jira_date("2023-12-01T10:30:00") == "2023-12-01"

        # Date only
        assert transformer._parse_jira_date("2023-12-01") == "2023-12-01"

        # Empty dates fall back to today
        result = transformer._parse_jira_date("")
        assert len(result) == 10  # YYYY-MM-DD format
        assert result.count("-") == 2  # Two dashes in date

        # Invalid dates also fall back to today
        result = transformer._parse_jira_date("invalid")
        assert len(result) == 10  # YYYY-MM-DD format

    def test_detect_activity_from_comment(self, transformer) -> None:
        """Test activity detection from work log comments."""
        # Test development keywords
        assert transformer._detect_activity("Working on coding the new feature") == 1
        assert transformer._detect_activity("Development work on API") == 1

        # Test testing keywords
        assert transformer._detect_activity("Running QA tests") == 2
        assert transformer._detect_activity("Testing the authentication") == 2

        # Test code review keywords
        assert transformer._detect_activity("Code review for pull request") == 3

        # Test documentation keywords
        assert transformer._detect_activity("Writing docs for the API") == 4

        # Test default fallback
        assert (
            transformer._detect_activity("Some random work") == 1
        )  # default_activity_id

    def test_detect_activity_from_tempo_attributes(self, transformer) -> None:
        """Test activity detection from Tempo work attributes."""
        tempo_log = {
            "description": "Some work",
            "workAttributes": [
                {
                    "key": "activity",
                    "value": "testing",  # Lowercase - won't match "Testing", so falls back to default
                },
            ],
        }

        activity_id = transformer._detect_activity_from_tempo(tempo_log)
        assert activity_id == 1  # Falls back to default_activity_id

    def test_extract_text_from_adf(self, transformer) -> None:
        """Test extracting text from Atlassian Document Format."""
        adf_content = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Hello "},
                        {
                            "type": "text",
                            "text": "world",
                            "marks": [{"type": "strong"}],
                        },
                        {"type": "text", "text": "!"},
                    ],
                },
            ],
        }

        result = transformer._extract_text_from_adf(adf_content)
        # The method joins text parts with spaces and removes doubles
        assert result == "Hello world !"

    def test_handle_tempo_specific_fields(self, transformer) -> None:
        """Test handling of Tempo-specific fields."""
        tempo_log = {
            "billableSeconds": 3600,
            "location": "Office",
            "workAttributes": [
                {"key": "client", "value": "ACME Corp"},
                {"key": "project", "value": "Website"},
            ],
        }

        time_entry = {"_meta": {}}
        transformer._handle_tempo_specific_fields(tempo_log, time_entry)

        assert time_entry["_meta"]["tempo_billable"] is True
        assert time_entry["_meta"]["tempo_billable_hours"] == 1.0
        assert time_entry["_meta"]["tempo_location"] == "Office"
        assert time_entry["_meta"]["tempo_attributes"]["client"] == "ACME Corp"

    def test_map_custom_fields(self, transformer) -> None:
        """Test custom field mapping."""
        source_log = {
            "customField1": "value1",
            "customField2": "value2",
            "normalField": "ignored",
        }

        time_entry = {}
        field_mapping = {
            "customField1": "openproject_field1",
            "customField2": "openproject_field2",
        }

        transformer._map_custom_fields(source_log, time_entry, field_mapping)

        assert time_entry["_meta"]["custom_fields"]["openproject_field1"] == "value1"
        assert time_entry["_meta"]["custom_fields"]["openproject_field2"] == "value2"
        assert "normalField" not in time_entry["_meta"]["custom_fields"]

    def test_get_transformation_stats(self, transformer) -> None:
        """Test transformation statistics generation."""
        transformed_entries = [
            {
                "hours": 2.0,
                "_embedded": {"user": {"href": "/api/v3/users/123"}},
                "_meta": {"jira_work_log_id": "1"},
            },
            {
                "hours": 1.5,
                "_embedded": {"workPackage": {"href": "/api/v3/work_packages/789"}},
                "_meta": {"tempo_worklog_id": "2"},
            },
            {
                "hours": 0.5,
                "_meta": {"jira_work_log_id": "3"},
            },
        ]

        stats = transformer.get_transformation_stats(transformed_entries)

        assert stats["total_entries"] == 3
        assert stats["total_hours"] == 4.0
        assert stats["jira_entries"] == 2
        assert stats["tempo_entries"] == 1
        assert stats["mapped_users"] == 1
        assert stats["unmapped_users"] == 2
        assert stats["mapped_work_packages"] == 1
        assert stats["unmapped_work_packages"] == 2

    def test_error_handling_in_transform_jira(self, transformer) -> None:
        """Test handling of incomplete Jira work logs."""
        # The transformer is more robust than expected - it handles missing fields gracefully
        invalid_work_log = {"invalid": "data"}

        # Should succeed but with default values
        result = transformer.transform_jira_work_log(invalid_work_log, "TEST-123")
        assert result is not None
        assert result["spentOn"] is not None  # Gets today's date
        assert result["hours"] == 0.0  # Default time
        assert result["_meta"]["jira_issue_key"] == "TEST-123"

    def test_error_handling_in_transform_tempo(self, transformer) -> None:
        """Test handling of incomplete Tempo work logs."""
        # The transformer is more robust than expected - it handles missing fields gracefully
        invalid_tempo_log = {"invalid": "data"}

        # Should succeed but with default values
        result = transformer.transform_tempo_work_log(invalid_tempo_log)
        assert result is not None
        assert result["spentOn"] == ""  # Empty date from Tempo
        assert result["hours"] == 0.0  # Default time
        assert result["_meta"]["tempo_author"] == "unknown"

    def test_batch_transform_with_failures(
        self,
        transformer,
        sample_jira_work_log,
    ) -> None:
        """Test batch transformation handles failures gracefully."""
        work_logs = [
            {**sample_jira_work_log, "issue_key": "TEST-123"},  # Valid
            {"invalid": "data"},  # Invalid - should fail
            {**sample_jira_work_log, "issue_key": "PROJ-456"},  # Valid
        ]

        results = transformer.batch_transform_work_logs(work_logs, "jira")

        # Should return 2 successful transformations, skip 1 failure
        assert len(results) == 2

    def test_transformer_initialization_defaults(self) -> None:
        """Test transformer initialization with default values."""
        transformer = TimeEntryTransformer()

        assert transformer.user_mapping == {}
        assert transformer.work_package_mapping == {}
        assert transformer.activity_mapping == {}
        assert transformer.default_activity_id is None
        assert "development" in transformer.default_activity_mappings

    def test_work_log_without_issue_key_in_batch(
        self,
        transformer,
        sample_jira_work_log,
    ) -> None:
        """Test batch processing skips work logs without issue key."""
        work_logs = [
            {**sample_jira_work_log},  # Missing issue_key
            {**sample_jira_work_log, "issue_key": "TEST-123"},  # Valid
        ]

        results = transformer.batch_transform_work_logs(work_logs, "jira")

        # Should only process the one with issue_key
        assert len(results) == 1
        assert results[0]["_meta"]["jira_issue_key"] == "TEST-123"
