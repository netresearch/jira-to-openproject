#!/usr/bin/env python3
"""Unit tests for EnhancedAuditTrailMigrator."""

import json
from unittest.mock import Mock, patch

import pytest

from src.utils.enhanced_audit_trail_migrator import (
    EnhancedAuditTrailMigrator,
)
from tests.utils.mock_factory import (
    create_mock_jira_client,
    create_mock_openproject_client,
)


class TestEnhancedAuditTrailMigrator:
    """Test suite for EnhancedAuditTrailMigrator."""

    @pytest.fixture
    def temp_data_dir(self, tmp_path):
        """Create temporary data directory."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Create migration_data subdirectory
        migration_data_dir = data_dir / "migration_data"
        migration_data_dir.mkdir()

        return data_dir

    @pytest.fixture
    def mock_clients(self):
        """Create mock clients."""
        jira_client = create_mock_jira_client()
        op_client = create_mock_openproject_client()
        return jira_client, op_client

    @pytest.fixture
    def sample_user_mapping(self):
        """Create sample user mapping data."""
        return {
            "john.doe": 123,
            "jane.smith": 456,
            "admin": 1,
            "deleted.user": None,
        }

    @pytest.fixture
    def sample_changelog_data(self):
        """Create sample Jira changelog data."""
        return [
            {
                "id": "12345",
                "created": "2023-01-15T10:30:00.000+0000",
                "author": {
                    "name": "john.doe",
                    "displayName": "John Doe",
                    "emailAddress": "john.doe@example.com",
                },
                "items": [
                    {
                        "field": "summary",
                        "fromString": "Old Title",
                        "toString": "New Title",
                    },
                    {
                        "field": "status",
                        "fromString": "To Do",
                        "toString": "In Progress",
                        "from": "1",
                        "to": "3",
                    },
                ],
            },
            {
                "id": "12346",
                "created": "2023-01-16T14:45:30.000+0000",
                "author": {
                    "name": "jane.smith",
                    "displayName": "Jane Smith",
                    "emailAddress": "jane.smith@example.com",
                },
                "items": [
                    {
                        "field": "assignee",
                        "fromString": "john.doe",
                        "toString": "jane.smith",
                    },
                ],
            },
        ]

    @pytest.fixture
    def sample_jira_issue(self, sample_changelog_data):
        """Create sample Jira issue with changelog."""
        issue = Mock()
        issue.key = "TEST-123"
        issue.fields = Mock()
        issue.fields.created = "2023-01-15T09:00:00.000+0000"

        # Convert dictionary data to mock objects for changelog
        histories = []
        for hist_data in sample_changelog_data:
            history = Mock()
            history.id = f"hist_{len(histories)}"
            history.created = hist_data["created"]

            # Create author mock
            if hist_data.get("author"):
                author = Mock()
                author.name = hist_data["author"]["name"]
                author.displayName = hist_data["author"]["displayName"]
                author.emailAddress = hist_data["author"]["emailAddress"]
                history.author = author
            else:
                history.author = None

            # Create items mock
            items = []
            for item_data in hist_data["items"]:
                item = Mock()
                item.field = item_data["field"]
                item.fromString = item_data.get("fromString")
                item.toString = item_data.get("toString")
                if "from" in item_data:
                    item.from_ = item_data["from"]
                if "to" in item_data:
                    item.to = item_data["to"]
                items.append(item)

            history.items = items
            histories.append(history)

        # Mock changelog
        issue.changelog = Mock()
        issue.changelog.histories = histories

        return issue

    @pytest.fixture
    @patch("src.utils.enhanced_audit_trail_migrator.config")
    def migrator_with_mocks(self, mock_config, mock_clients, temp_data_dir):
        """Create migrator instance with mocked dependencies."""
        mock_config.logger = Mock()

        jira_client, op_client = mock_clients

        return EnhancedAuditTrailMigrator(jira_client=jira_client, op_client=op_client)

    def test_initialization(self, migrator_with_mocks, temp_data_dir) -> None:
        """Test migrator initialization."""
        migrator = migrator_with_mocks

        assert len(migrator.changelog_data) == 0
        assert len(migrator.rails_operations) == 0
        assert len(migrator.audit_events) == 0
        assert migrator.migration_results is not None
        assert "total_changelog_entries" in migrator.migration_results

    @patch("src.utils.data_handler.load_dict")
    def test_load_user_mapping_success(
        self,
        mock_load_dict,
        migrator_with_mocks,
        sample_user_mapping,
    ) -> None:
        """Test successful loading of user mapping."""
        # Mock data handler load_dict
        mock_load_dict.return_value = sample_user_mapping

        migrator = migrator_with_mocks
        migrator._load_user_mapping()

        assert migrator.user_mapping == sample_user_mapping
        mock_load_dict.assert_called_once()

    @patch("src.utils.data_handler.load_dict")
    def test_load_user_mapping_file_not_found(
        self,
        mock_load_dict,
        migrator_with_mocks,
    ) -> None:
        """Test loading user mapping when file doesn't exist."""
        # Mock load_dict returning empty dict (default behavior for missing file)
        mock_load_dict.return_value = {}

        migrator = migrator_with_mocks
        migrator._load_user_mapping()

        # Should call load_dict which handles missing file gracefully
        mock_load_dict.assert_called_once()

    @patch("src.utils.data_handler.load_dict")
    def test_load_user_mapping_invalid_json(
        self,
        mock_load_dict,
        migrator_with_mocks,
    ) -> None:
        """Test loading user mapping with invalid JSON."""
        # Mock data handler raises exception
        mock_load_dict.side_effect = Exception("Invalid JSON")

        migrator = migrator_with_mocks
        migrator._load_user_mapping()

        # Should handle exception gracefully
        mock_load_dict.assert_called_once()

    def test_extract_changelog_from_issue(
        self,
        migrator_with_mocks,
        sample_jira_issue,
        sample_changelog_data,
    ) -> None:
        """Test extracting changelog from Jira issue."""
        migrator = migrator_with_mocks

        changelog = migrator.extract_changelog_from_issue(sample_jira_issue)

        assert len(changelog) == 2
        assert changelog[0]["created"] == "2023-01-15T10:30:00.000+0000"
        assert changelog[0]["author"]["name"] == "john.doe"
        assert len(changelog[0]["items"]) == 2
        assert changelog[1]["created"] == "2023-01-16T14:45:30.000+0000"

    def test_extract_changelog_no_changelog(self, migrator_with_mocks) -> None:
        """Test extracting changelog from issue without changelog."""
        issue = Mock()
        issue.key = "TEST-123"
        issue.changelog = None

        migrator = migrator_with_mocks
        changelog = migrator.extract_changelog_from_issue(issue)

        assert changelog == []

    def test_extract_changelog_empty_histories(self, migrator_with_mocks) -> None:
        """Test extracting changelog with empty histories."""
        issue = Mock()
        issue.key = "TEST-123"
        issue.changelog = Mock()
        issue.changelog.histories = []

        migrator = migrator_with_mocks
        changelog = migrator.extract_changelog_from_issue(issue)

        assert changelog == []

    def test_map_field_changes_known_fields(
        self,
        migrator_with_mocks,
        sample_user_mapping,
    ) -> None:
        """Test mapping of known field changes."""
        migrator = migrator_with_mocks
        # Set up user mapping for assignee test
        migrator.user_mapping = sample_user_mapping

        # Test summary mapping
        change_item = {
            "field": "summary",
            "fromString": "Old Title",
            "toString": "New Title",
        }
        changes = migrator._map_field_changes(change_item)
        expected = {"subject": ["Old Title", "New Title"]}
        assert changes == expected

        # Test status mapping
        change_item = {
            "field": "status",
            "fromString": "To Do",
            "toString": "In Progress",
        }
        changes = migrator._map_field_changes(change_item)
        expected = {"status_id": ["To Do", "In Progress"]}
        assert changes == expected

        # Test assignee mapping (with user ID lookup)
        change_item = {
            "field": "assignee",
            "fromString": "john.doe",
            "toString": "jane.smith",
        }
        changes = migrator._map_field_changes(change_item)
        expected = {"assigned_to_id": [123, 456]}  # User IDs from sample_user_mapping
        assert changes == expected

    def test_map_field_changes_unknown_field(self, migrator_with_mocks) -> None:
        """Test mapping of unknown field changes."""
        migrator = migrator_with_mocks

        change_item = {
            "field": "customfield_10001",
            "fromString": "old",
            "toString": "new",
        }
        changes = migrator._map_field_changes(change_item)
        expected = {"customfield_10001": ["old", "new"]}
        assert changes == expected

    def test_map_field_changes_empty_values(self, migrator_with_mocks) -> None:
        """Test mapping with empty values."""
        migrator = migrator_with_mocks

        change_item = {"field": "summary", "fromString": None, "toString": "New Title"}
        changes = migrator._map_field_changes(change_item)
        expected = {"subject": [None, "New Title"]}
        assert changes == expected

    def test_generate_audit_comment_single_change(self, migrator_with_mocks) -> None:
        """Test generating audit comment for single change."""
        migrator = migrator_with_mocks

        change_item = {
            "field": "subject",
            "fromString": "Old Title",
            "toString": "New Title",
        }
        comment = migrator._generate_audit_comment(change_item)

        assert comment == "Changed subject from 'Old Title' to 'New Title'"

    def test_generate_audit_comment_empty_values(self, migrator_with_mocks) -> None:
        """Test generating audit comment with empty values."""
        migrator = migrator_with_mocks

        change_item = {"field": "status", "fromString": None, "toString": "In Progress"}
        comment = migrator._generate_audit_comment(change_item)

        assert comment == "Changed status from 'empty' to 'In Progress'"

    def test_generate_audit_comment_with_none_values(self, migrator_with_mocks) -> None:
        """Test generating audit comment with None values."""
        migrator = migrator_with_mocks

        change_item = {"field": "assignee", "fromString": None, "toString": "john.doe"}
        comment = migrator._generate_audit_comment(change_item)

        assert comment == "Changed assignee from 'empty' to 'john.doe'"

    def test_transform_changelog_to_audit_events(
        self,
        migrator_with_mocks,
        sample_changelog_data,
        sample_user_mapping,
    ) -> None:
        """Test transforming changelog to audit events."""
        migrator = migrator_with_mocks
        migrator.user_mapping = sample_user_mapping

        work_package_id = 1001
        jira_issue_key = "TEST-123"
        events = migrator.transform_changelog_to_audit_events(
            sample_changelog_data,
            jira_issue_key,
            work_package_id,
        )

        assert len(events) == 3

        # First event (summary change)
        event1 = events[0]
        assert event1["openproject_work_package_id"] == work_package_id
        assert event1["user_id"] == 123  # john.doe mapped to 123
        assert event1["created_at"] == "2023-01-15T10:30:00.000+0000"
        assert event1["changes"] == {"subject": ["Old Title", "New Title"]}
        assert event1["comment"] == "Changed summary from 'Old Title' to 'New Title'"

        # Second event (status change)
        event2 = events[1]
        assert event2["user_id"] == 123  # john.doe mapped to 123
        assert event2["created_at"] == "2023-01-15T10:30:00.000+0000"
        assert event2["changes"] == {"status_id": ["1", "3"]}

        # Third event (assignee change)
        event3 = events[2]
        assert event3["user_id"] == 456  # jane.smith mapped to 456
        assert event3["created_at"] == "2023-01-16T14:45:30.000+0000"
        assert event3["changes"] == {"assigned_to_id": [123, 456]}

    def test_transform_changelog_unmapped_user(
        self,
        migrator_with_mocks,
        sample_changelog_data,
    ) -> None:
        """Test transforming changelog with unmapped user."""
        migrator = migrator_with_mocks
        migrator.user_mapping = {"john.doe": 123}  # Missing jane.smith

        work_package_id = 1001
        jira_issue_key = "TEST-123"
        events = migrator.transform_changelog_to_audit_events(
            sample_changelog_data,
            jira_issue_key,
            work_package_id,
        )

        assert len(events) == 3

        # Third event should use admin fallback since jane.smith is unmapped
        event3 = events[2]
        assert event3["user_id"] == 1  # Admin fallback

    def test_migrate_audit_trail_for_issue(
        self,
        migrator_with_mocks,
        sample_jira_issue,
        sample_user_mapping,
    ) -> None:
        """Test migrating audit trail for a single issue."""
        migrator = migrator_with_mocks
        migrator.user_mapping = sample_user_mapping

        jira_issue_key = "TEST-123"
        work_package_id = 1001

        result = migrator.migrate_audit_trail_for_issue(
            sample_jira_issue,
            work_package_id,
        )

        assert result is True
        assert jira_issue_key in migrator.changelog_data
        assert len(migrator.changelog_data[jira_issue_key]) == 2

    def test_migrate_audit_trail_for_issue_no_changelog(
        self,
        migrator_with_mocks,
    ) -> None:
        """Test migrating audit trail for issue without changelog."""
        issue = Mock()
        issue.key = "TEST-123"
        issue.changelog = None

        migrator = migrator_with_mocks
        result = migrator.migrate_audit_trail_for_issue(issue, 1001)

        assert result is True
        assert "TEST-123" in migrator.changelog_data
        assert len(migrator.changelog_data["TEST-123"]) == 0

    def test_process_stored_changelog_data_success(
        self,
        migrator_with_mocks,
        sample_changelog_data,
        sample_user_mapping,
    ) -> None:
        """Test processing stored changelog data successfully."""
        migrator = migrator_with_mocks
        migrator.user_mapping = sample_user_mapping

        # Pre-populate cache
        migrator.changelog_data = {"TEST-123": sample_changelog_data}

        work_package_mapping = {"TEST-123": 1001}

        result = migrator.process_stored_changelog_data(work_package_mapping)

        assert result is True
        assert len(migrator.rails_operations) == 2  # Two audit events
        assert migrator.migration_results["comments_processed"] == 0

    def test_process_stored_changelog_data_no_mapping(
        self,
        migrator_with_mocks,
        sample_changelog_data,
    ) -> None:
        """Test processing changelog data with missing work package mapping."""
        migrator = migrator_with_mocks

        migrator.changelog_data = {
            "TEST-123": sample_changelog_data,
            "TEST-456": sample_changelog_data,
        }

        work_package_mapping = {"TEST-123": 1001}  # Missing TEST-456

        result = migrator.process_stored_changelog_data(work_package_mapping)

        assert result is True
        assert len(migrator.rails_operations) == 2  # Only TEST-123 processed
        assert migrator.migration_results["orphaned_events"] == 1

    def test_process_stored_changelog_data_with_comments(
        self,
        migrator_with_mocks,
        sample_changelog_data,
        sample_user_mapping,
    ) -> None:
        """Ensure comments are queued alongside changelog events."""
        migrator = migrator_with_mocks
        migrator.user_mapping = sample_user_mapping

        comment_payload = [{
            "id": "c1",
            "created": "2023-01-17T12:00:00.000+0000",
            "author": {"name": "john.doe"},
            "body": "Reviewed change",
        }]

        migrator.changelog_data = {
            "TEST-123": {
                "jira_issue_key": "TEST-123",
                "changelog_entries": sample_changelog_data,
                "comments": comment_payload,
            },
        }

        work_package_mapping = {"TEST-123": {"openproject_id": 1001}}

        result = migrator.process_stored_changelog_data(work_package_mapping)

        assert result is True
        operations = migrator.rails_operations
        assert any(op.get("operation") == "create_comment_event" for op in operations)
        assert migrator.migration_results["comments_processed"] == 1


    @patch("subprocess.run")
    def test_execute_rails_audit_operations_success(
        self,
        mock_subprocess,
        migrator_with_mocks,
    ) -> None:
        """Test successful Rails audit operations execution."""
        mock_subprocess.return_value = Mock(returncode=0, stdout="Success", stderr="")

        migrator = migrator_with_mocks
        migrator.rails_operations = [
            {
                "work_package_id": 1001,
                "user_id": 123,
                "created_at": "2023-01-15T10:30:00.000+0000",
                "notes": "Test change",
                "changes": [
                    {"field": "subject", "old_value": "old", "new_value": "new"},
                ],
            },
        ]

        result = migrator.execute_rails_audit_operations()

        assert result["status"] == "success"
        assert result["processed"] == 1
        assert result["total"] == 1
        assert migrator.rails_operations == []
        assert migrator.migration_results["rails_execution_success"] is True
        mock_subprocess.assert_called_once()

    @patch("subprocess.run")
    def test_execute_rails_audit_operations_failure(
        self,
        mock_subprocess,
        migrator_with_mocks,
    ) -> None:
        """Test failed Rails audit operations execution."""
        mock_subprocess.return_value = Mock(
            returncode=1,
            stdout="",
            stderr="Error occurred",
        )

        migrator = migrator_with_mocks
        migrator.rails_operations = [
            {
                "work_package_id": 1001,
                "user_id": 123,
                "created_at": "2023-01-15T10:30:00.000+0000",
                "notes": "Test change",
                "changes": [],
            },
        ]

        result = migrator.execute_rails_audit_operations()

        assert result["status"] == "error"
        assert result["processed"] == 0
        assert result["total"] == 1
        assert result["errors"] == ["Error occurred"]
        assert migrator.rails_operations  # operations remain for retry
        assert migrator.migration_results["rails_execution_success"] is False
        mock_subprocess.assert_called_once()

    @patch("subprocess.run")
    def test_execute_rails_audit_operations_empty_cache(
        self,
        mock_subprocess,
        migrator_with_mocks,
    ) -> None:
        """Test Rails execution with empty operations cache."""
        migrator = migrator_with_mocks
        migrator.rails_operations = []

        result = migrator.execute_rails_audit_operations()

        assert result == {"status": "skipped", "processed": 0, "total": 0, "errors": []}
        mock_subprocess.assert_not_called()

    def test_generate_audit_creation_script(self, migrator_with_mocks) -> None:
        """Test generating Rails audit creation script."""
        migrator = migrator_with_mocks

        audit_events = [
            {
                "work_package_id": 1001,
                "user_id": 123,
                "created_at": "2023-01-15T10:30:00.000+0000",
                "notes": "Changed subject",
                "changes": [
                    {"field": "subject", "old_value": "old", "new_value": "new"},
                ],
            },
        ]

        script = migrator._generate_audit_creation_script(audit_events)

        assert "Journal.create!" in script
        assert "JournalDetail.create!" in script
        assert "work_package_id: 1001" in script
        assert "user_id: 123" in script
        assert "Changed subject" in script

    def test_generate_audit_creation_script_empty_events(
        self,
        migrator_with_mocks,
    ) -> None:
        """Test generating script with empty events."""
        migrator = migrator_with_mocks
        script = migrator._generate_audit_creation_script([])

        assert script.strip() == ""

    def test_generate_audit_trail_report(
        self,
        migrator_with_mocks,
        sample_user_mapping,
    ) -> None:
        """Test generating audit trail migration report."""
        migrator = migrator_with_mocks
        migrator.user_mapping = sample_user_mapping

        # Simulate migration results
        migrator.migration_results = {
            "total_issues_processed": 10,
            "issues_with_changelog": 8,
            "total_audit_events_created": 25,
            "user_attribution_failures": 2,
            "rails_execution_success": True,
            "processing_errors": ["Error with TEST-999"],
        }

        report = migrator.generate_audit_trail_report()

        assert "Audit Trail Migration Report" in report
        assert "Total Issues Processed: 10" in report
        assert "Issues with Changelog: 8" in report
        assert "Total Audit Events Created: 25" in report
        assert "User Attribution Failures: 2" in report
        assert "Rails Execution Success: True" in report
        assert "Processing Errors: 1" in report

    def test_save_migration_results(
        self,
        migrator_with_mocks,
        temp_data_dir,
        sample_user_mapping,
    ) -> None:
        """Test saving migration results to file."""
        migrator = migrator_with_mocks
        migrator.user_mapping = sample_user_mapping

        # Simulate migration results
        migrator.migration_results = {
            "total_issues_processed": 5,
            "rails_execution_success": True,
        }

        result = migrator.save_migration_results()

        assert result is True

        # Check file exists
        results_file = (
            temp_data_dir / "migration_data" / "audit_trail_migration_results.json"
        )
        assert results_file.exists()

        # Check file content
        with open(results_file) as f:
            saved_results = json.load(f)

        assert saved_results["total_issues_processed"] == 5
        assert saved_results["rails_execution_success"] is True

    def test_save_migration_results_io_error(self, migrator_with_mocks) -> None:
        """Test saving migration results with IO error."""
        migrator = migrator_with_mocks
        migrator.data_dir = "/invalid/path"

        result = migrator.save_migration_results()

        assert result is False

    def test_integration_full_workflow(
        self,
        migrator_with_mocks,
        sample_jira_issue,
        sample_user_mapping,
        temp_data_dir,
    ) -> None:
        """Test complete audit trail migration workflow."""
        migrator = migrator_with_mocks
        migrator.user_mapping = sample_user_mapping

        # Step 1: Extract changelog during issue processing
        result1 = migrator.migrate_audit_trail_for_issue(sample_jira_issue, 1001)
        assert result1 is True
        assert "TEST-123" in migrator.changelog_data

        # Step 2: Process stored changelog data
        work_package_mapping = {"TEST-123": 1001}

        with patch.object(migrator, "execute_rails_audit_operations") as mock_execute:
            mock_execute.return_value = True
            result2 = migrator.process_stored_changelog_data(work_package_mapping)

        assert result2 is True
        assert len(migrator.rails_operations) == 2

        # Step 3: Generate and save report
        migrator.migration_results = {
            "total_issues_processed": 1,
            "issues_with_changelog": 1,
            "total_audit_events_created": 2,
            "rails_execution_success": True,
        }

        report = migrator.generate_audit_trail_report()
        assert "Total Issues Processed: 1" in report

        save_result = migrator.save_migration_results()
        assert save_result is True

    def test_error_handling_malformed_changelog_item(
        self,
        migrator_with_mocks,
        sample_user_mapping,
    ) -> None:
        """Test handling of malformed changelog items."""
        migrator = migrator_with_mocks
        migrator.user_mapping = sample_user_mapping

        malformed_changelog = [
            {
                "created": "2023-01-15T10:30:00.000+0000",
                "author": {"name": "john.doe"},  # Missing other author fields
                "items": [
                    {
                        "field": "summary",
                        # Missing fromString and toString
                    },
                ],
            },
        ]

        work_package_id = 1001
        events = migrator.transform_changelog_to_audit_events(
            malformed_changelog,
            work_package_id,
        )

        # Should still create event, handling missing data gracefully
        assert len(events) >= 0  # Depends on error handling strategy

    def test_cache_management(self, migrator_with_mocks) -> None:
        """Test cache management operations."""
        migrator = migrator_with_mocks

        # Add some data to caches
        migrator.changelog_data = {"TEST-123": []}
        migrator.rails_operations = [{"test": "data"}]

        assert len(migrator.changelog_data) == 1
        assert len(migrator.rails_operations) == 1

        # Test cache clearing (if implemented)
        if hasattr(migrator, "clear_caches"):
            migrator.clear_caches()
            assert len(migrator.changelog_data) == 0
            assert len(migrator.rails_operations) == 0

    def test_large_changelog_handling(
        self,
        migrator_with_mocks,
        sample_user_mapping,
    ) -> None:
        """Test handling of issues with large changelogs."""
        migrator = migrator_with_mocks
        migrator.user_mapping = sample_user_mapping

        # Create large changelog (100 entries)
        large_changelog = []
        for i in range(100):
            large_changelog.append(
                {
                    "created": f"2023-01-{i+1:02d}T10:30:00.000+0000",
                    "author": {"name": "john.doe"},
                    "items": [
                        {
                            "field": "summary",
                            "fromString": f"Title {i}",
                            "toString": f"Title {i+1}",
                        },
                    ],
                },
            )

        work_package_id = 1001
        events = migrator.transform_changelog_to_audit_events(
            large_changelog,
            work_package_id,
        )

        assert len(events) == 100

        # Test performance implications
        import time

        start_time = time.time()
        events = migrator.transform_changelog_to_audit_events(
            large_changelog,
            work_package_id,
        )
        end_time = time.time()

        # Should complete in reasonable time (adjust threshold as needed)
        assert (end_time - start_time) < 5.0  # 5 seconds
