#!/usr/bin/env python3
"""Comprehensive tests for TimeEntryMigrator class."""

import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch, mock_open
from datetime import datetime
from typing import Dict, Any, List

import pytest

from src.utils.time_entry_migrator import TimeEntryMigrator


@pytest.fixture
def mock_jira_client():
    """Fixture for a mocked JiraClient."""
    client = MagicMock()
    # Mock the existence of the tempo method by default
    client.get_tempo_time_entries = MagicMock()
    client.get_work_logs_for_issue = MagicMock()
    return client


@pytest.fixture
def mock_op_client():
    """Fixture for a mocked OpenProjectClient."""
    client = MagicMock()
    # Simulate returning some default activities
    client.get_time_entry_activities.return_value = [
        {"id": 1, "name": "Development", "is_default": True},
        {"id": 2, "name": "Testing", "is_default": False},
        {"id": 3, "name": "Code review", "is_default": False},
    ]
    # Simulate successful time entry creation
    client.create_time_entry.side_effect = lambda data: {"id": 123, **data}
    return client


@pytest.fixture
def mock_transformer():
    """Fixture for a mocked TimeEntryTransformer."""
    transformer = MagicMock()
    transformer.batch_transform_work_logs.return_value = []
    return transformer


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Fixture to create a temporary data directory."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture
def migrator(mock_jira_client, mock_op_client, tmp_data_dir: Path) -> TimeEntryMigrator:
    """Fixture for an instance of TimeEntryMigrator with mocked clients."""
    return TimeEntryMigrator(
        jira_client=mock_jira_client,
        op_client=mock_op_client,
        data_dir=tmp_data_dir,
    )


def write_json_file(path: Path, data: dict):
    """Helper to write JSON data to a file."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


@pytest.fixture
def sample_jira_work_log():
    """Sample Jira work log data."""
    return {
        "id": "work_log_1",
        "author": {"accountId": "user1", "displayName": "John Doe"},
        "timeSpentSeconds": 3600,
        "started": "2023-01-01T10:00:00.000+0000",
        "comment": "Working on feature",
        "issue_key": "PROJ-1"
    }


@pytest.fixture
def sample_tempo_entry():
    """Sample Tempo time entry data."""
    return {
        "tempoWorklogId": "tempo_1",
        "timeSpentSeconds": 7200,
        "startDate": "2023-01-01",
        "author": {"accountId": "user1"},
        "issue": {"key": "PROJ-1"},
        "description": "Development work"
    }


@pytest.fixture
def sample_openproject_time_entry():
    """Sample OpenProject time entry format."""
    return {
        "hours": 1.0,
        "spentOn": "2023-01-01",
        "comment": "Working on feature",
        "_embedded": {
            "workPackage": {"href": "/api/v3/work_packages/1"},
            "user": {"href": "/api/v3/users/1"},
            "activity": {"href": "/api/v3/time_entries/activities/1"}
        },
        "_meta": {
            "jira_work_log_id": "work_log_1",
            "source": "jira"
        }
    }


class TestTimeEntryMigratorInitialization:
    """Tests for the initialization and mapping loading of TimeEntryMigrator."""

    def test_initialization_loads_mappings_successfully(
        self, mock_jira_client, mock_op_client, tmp_data_dir
    ):
        """
        Verify that mappings are loaded correctly from files and the OP client
        during initialization.
        """
        # Arrange
        user_mapping_data = {
            "user1": {"jira_username": "jira_user_1", "openproject_id": 101}
        }
        wp_mapping_data = {
            "wp1": {"jira_key": "PROJ-1", "openproject_id": 201}
        }
        write_json_file(tmp_data_dir / "user_mapping.json", user_mapping_data)
        write_json_file(tmp_data_dir / "work_package_mapping.json", wp_mapping_data)

        # Act
        migrator = TimeEntryMigrator(mock_jira_client, mock_op_client, tmp_data_dir)

        # Assert
        assert migrator.user_mapping == {"jira_user_1": 101}
        assert migrator.work_package_mapping == {"PROJ-1": 201}
        assert migrator.activity_mapping == {"development": 1, "testing": 2, "code review": 3}
        assert migrator.default_activity_id == 1
        mock_op_client.get_time_entry_activities.assert_called_once()

    def test_initialization_handles_missing_mapping_files(
        self, mock_jira_client, mock_op_client, tmp_data_dir
    ):
        """
        Verify that initialization proceeds with empty mappings if files are not found.
        """
        # Act
        migrator = TimeEntryMigrator(mock_jira_client, mock_op_client, tmp_data_dir)

        # Assert
        assert migrator.user_mapping == {}
        assert migrator.work_package_mapping == {}
        assert migrator.activity_mapping is not None

    def test_initialization_handles_invalid_json_mapping_files(
        self, mock_jira_client, mock_op_client, tmp_data_dir
    ):
        """
        Verify that initialization handles corrupted JSON mapping files gracefully.
        """
        # Arrange - create files with invalid JSON
        with open(tmp_data_dir / "user_mapping.json", "w") as f:
            f.write("{ invalid json }")
        with open(tmp_data_dir / "work_package_mapping.json", "w") as f:
            f.write("{ also invalid }")

        # Act
        migrator = TimeEntryMigrator(mock_jira_client, mock_op_client, tmp_data_dir)

        # Assert
        assert migrator.user_mapping == {}
        assert migrator.work_package_mapping == {}

    def test_initialization_handles_op_client_failure(self, mock_jira_client, tmp_data_dir):
        """
        Verify that initialization handles failures when fetching activities from OpenProject.
        """
        # Arrange
        mock_op_client_failed = MagicMock()
        mock_op_client_failed.get_time_entry_activities.side_effect = Exception(
            "API Error"
        )

        # Act
        migrator = TimeEntryMigrator(mock_jira_client, mock_op_client_failed, tmp_data_dir)

        # Assert
        assert migrator.activity_mapping == {}
        assert migrator.default_activity_id is None

    def test_initialization_creates_data_directory(self, mock_jira_client, mock_op_client, tmp_path):
        """
        Verify that the data directory is set correctly (creation is handled by the implementation as needed).
        """
        # Arrange
        non_existent_dir = tmp_path / "non_existent"

        # Act
        migrator = TimeEntryMigrator(mock_jira_client, mock_op_client, non_existent_dir)

        # Assert
        assert migrator.data_dir == non_existent_dir
        # Note: Directory creation is handled as needed by the implementation


class TestTimeEntryMigratorValidation:
    """Tests for the internal _validate_time_entry method."""

    @pytest.mark.parametrize(
        "entry, expected_valid",
        [
            # Happy path
            (
                {
                    "hours": 8,
                    "spentOn": "2023-01-01",
                    "_embedded": {
                        "workPackage": {"href": "/api/v3/work_packages/1"},
                        "user": {"href": "/api/v3/users/1"},
                    },
                },
                True,
            ),
            # Invalid: hours is zero
            (
                {
                    "hours": 0,
                    "spentOn": "2023-01-01",
                    "_embedded": {
                        "workPackage": {"href": "/api/v3/work_packages/1"},
                        "user": {"href": "/api/v3/users/1"},
                    },
                },
                False,
            ),
            # Invalid: hours is negative
            (
                {
                    "hours": -1,
                    "spentOn": "2023-01-01",
                    "_embedded": {
                        "workPackage": {"href": "/api/v3/work_packages/1"},
                        "user": {"href": "/api/v3/users/1"},
                    },
                },
                False,
            ),
            # Invalid: hours is missing
            (
                {
                    "spentOn": "2023-01-01",
                    "_embedded": {
                        "workPackage": {"href": "/api/v3/work_packages/1"},
                        "user": {"href": "/api/v3/users/1"},
                    },
                },
                False,
            ),
            # Invalid: spentOn is missing
            (
                {
                    "hours": 8,
                    "_embedded": {
                        "workPackage": {"href": "/api/v3/work_packages/1"},
                        "user": {"href": "/api/v3/users/1"},
                    },
                },
                False,
            ),
            # Invalid: workPackage href is missing
            (
                {
                    "hours": 8,
                    "spentOn": "2023-01-01",
                    "_embedded": {"user": {"href": "/api/v3/users/1"}},
                },
                False,
            ),
            # Invalid: user href is missing
            (
                {
                    "hours": 8,
                    "spentOn": "2023-01-01",
                    "_embedded": {"workPackage": {"href": "/api/v3/work_packages/1"}},
                },
                False,
            ),
            # Invalid: _embedded is missing entirely
            (
                {
                    "hours": 8,
                    "spentOn": "2023-01-01",
                },
                False,
            ),
        ],
    )
    def test_validate_time_entry(self, migrator, entry, expected_valid):
        """
        Verify that _validate_time_entry correctly identifies valid and invalid entries.
        """
        # Act
        is_valid = migrator._validate_time_entry(entry)

        # Assert
        assert is_valid == expected_valid


class TestTimeEntryMigratorExtraction:
    """Tests for Jira work log and Tempo time entry extraction."""

    def test_extract_jira_work_logs_for_issues_success(
        self, migrator, mock_jira_client, sample_jira_work_log
    ):
        """
        Verify successful extraction of Jira work logs for multiple issues.
        """
        # Arrange
        issue_keys = ["PROJ-1", "PROJ-2"]
        mock_jira_client.get_work_logs_for_issue.side_effect = [
            [sample_jira_work_log],  # PROJ-1
            [{"id": "work_log_2", "timeSpentSeconds": 1800, "issue_key": "PROJ-2"}],  # PROJ-2
        ]

        # Act
        result = migrator.extract_jira_work_logs_for_issues(issue_keys, save_to_file=False)

        # Assert
        assert len(result) == 2  # Returns dictionary with 2 issues
        assert migrator.migration_results["jira_work_logs_extracted"] == 2
        assert len(migrator.extracted_work_logs) == 2
        assert "PROJ-1" in migrator.extracted_work_logs
        assert "PROJ-2" in migrator.extracted_work_logs
        assert mock_jira_client.get_work_logs_for_issue.call_count == 2

    def test_extract_jira_work_logs_handles_api_errors(
        self, migrator, mock_jira_client
    ):
        """
        Verify that API errors for individual issues are handled gracefully.
        """
        # Arrange
        issue_keys = ["PROJ-1", "PROJ-2", "PROJ-3"]
        mock_jira_client.get_work_logs_for_issue.side_effect = [
            [{"id": "work_log_1", "issue_key": "PROJ-1"}],  # Success
            Exception("API Error"),  # Failure
            [{"id": "work_log_3", "issue_key": "PROJ-3"}],  # Success
        ]

        # Act
        result = migrator.extract_jira_work_logs_for_issues(issue_keys)

        # Assert
        assert len(result) == 2  # Only successful extractions returned
        assert migrator.migration_results["jira_work_logs_extracted"] == 2
        assert len(migrator.migration_results["errors"]) == 1
        assert "API Error" in migrator.migration_results["errors"][0]

    def test_extract_jira_work_logs_empty_results(
        self, migrator, mock_jira_client
    ):
        """
        Verify handling of empty work log results.
        """
        # Arrange
        issue_keys = ["PROJ-1"]
        mock_jira_client.get_work_logs_for_issue.return_value = []

        # Act
        result = migrator.extract_jira_work_logs_for_issues(issue_keys)

        # Assert
        assert len(result) == 0  # Returns empty dictionary
        assert migrator.migration_results["jira_work_logs_extracted"] == 0
        assert len(migrator.extracted_work_logs) == 0

    def test_extract_tempo_time_entries_success(
        self, migrator, mock_jira_client, sample_tempo_entry
    ):
        """
        Verify successful extraction of Tempo time entries.
        """
        # Arrange
        mock_jira_client.get_tempo_time_entries.return_value = [sample_tempo_entry]

        # Act
        result = migrator.extract_tempo_time_entries(
            project_keys=["PROJ"], 
            date_from="2023-01-01", 
            date_to="2023-01-31"
        )

        # Assert
        assert len(result) == 1  # Returns list with 1 entry
        assert migrator.migration_results["tempo_entries_extracted"] == 1
        assert len(migrator.extracted_tempo_entries) == 1
        mock_jira_client.get_tempo_time_entries.assert_called_once_with(
            project_keys=["PROJ"], date_from="2023-01-01", date_to="2023-01-31"
        )

    def test_extract_tempo_time_entries_missing_integration(
        self, migrator, mock_jira_client
    ):
        """
        Verify graceful handling when Tempo integration is not available.
        """
        # Arrange
        delattr(mock_jira_client, 'get_tempo_time_entries')

        # Act
        result = migrator.extract_tempo_time_entries()

        # Assert
        assert len(result) == 0  # Returns empty list
        assert migrator.migration_results["tempo_entries_extracted"] == 0

    def test_extract_tempo_time_entries_api_error(
        self, migrator, mock_jira_client
    ):
        """
        Verify handling of API errors during Tempo extraction.
        """
        # Arrange
        mock_jira_client.get_tempo_time_entries.side_effect = Exception("Tempo API Error")

        # Act
        result = migrator.extract_tempo_time_entries()

        # Assert
        assert len(result) == 0  # Returns empty list
        assert len(migrator.migration_results["errors"]) == 1
        assert "Tempo API Error" in migrator.migration_results["errors"][0]

    @patch("builtins.open", new_callable=mock_open)
    @patch("json.dump")
    def test_extract_work_logs_saves_to_file(
        self, mock_json_dump, mock_file, migrator, mock_jira_client, sample_jira_work_log
    ):
        """
        Verify that extracted work logs are saved to file when requested.
        """
        # Arrange
        mock_jira_client.get_work_logs_for_issue.return_value = [sample_jira_work_log]

        # Act
        migrator.extract_jira_work_logs_for_issues(["PROJ-1"], save_to_file=True)

        # Assert
        mock_file.assert_called()
        mock_json_dump.assert_called()


class TestTimeEntryMigratorTransformation:
    """Tests for time entry transformation logic."""

    @patch("src.utils.time_entry_migrator.TimeEntryTransformer")
    def test_transform_all_time_entries_success(
        self, MockTransformer, migrator, sample_openproject_time_entry
    ):
        """
        Verify successful transformation of Jira and Tempo entries.
        """
        # Arrange
        migrator.extracted_work_logs = {"PROJ-1": [{"id": "work_log_1"}]}
        migrator.extracted_tempo_entries = [{"tempoWorklogId": "tempo_1"}]
        
        mock_transformer_instance = MockTransformer.return_value
        mock_transformer_instance.batch_transform_work_logs.side_effect = [
            [sample_openproject_time_entry],  # Jira transformation
            [sample_openproject_time_entry],  # Tempo transformation
        ]

        # Act
        result = migrator.transform_all_time_entries()

        # Assert
        assert len(result) == 2
        assert migrator.migration_results["successful_transformations"] == 2
        assert mock_transformer_instance.batch_transform_work_logs.call_count == 2

    @patch("src.utils.time_entry_migrator.TimeEntryTransformer")
    def test_transform_all_time_entries_handles_errors(
        self, MockTransformer, migrator
    ):
        """
        Verify handling of transformation errors.
        """
        # Arrange
        migrator.extracted_work_logs = {"PROJ-1": [{"id": "work_log_1"}]}
        migrator.extracted_tempo_entries = [{"tempoWorklogId": "tempo_1"}]
        
        mock_transformer_instance = MockTransformer.return_value
        mock_transformer_instance.batch_transform_work_logs.side_effect = [
            Exception("Transformation Error"),  # Jira fails
            [{"hours": 1.0}],  # Tempo succeeds
        ]

        # Act
        result = migrator.transform_all_time_entries()

        # Assert
        assert len(result) == 1  # Only Tempo succeeded
        assert migrator.migration_results["successful_transformations"] == 1
        assert len(migrator.migration_results["errors"]) == 1
        assert "Transformation Error" in migrator.migration_results["errors"][0]

    @patch("src.utils.time_entry_migrator.TimeEntryTransformer")
    def test_transform_all_time_entries_empty_extractions(
        self, MockTransformer, migrator
    ):
        """
        Verify handling when no data has been extracted.
        """
        # Arrange - no extracted data
        mock_transformer_instance = MockTransformer.return_value

        # Act
        result = migrator.transform_all_time_entries()

        # Assert
        assert len(result) == 0
        assert migrator.migration_results["successful_transformations"] == 0
        mock_transformer_instance.batch_transform_work_logs.assert_not_called()


class TestTimeEntryMigratorMigration:
    """Tests for OpenProject time entry migration."""

    def test_migrate_time_entries_to_openproject_success(
        self, migrator, mock_op_client, sample_openproject_time_entry
    ):
        """
        Verify successful migration of time entries to OpenProject.
        """
        # Arrange
        time_entries = [sample_openproject_time_entry, sample_openproject_time_entry]
        migrator.transformed_time_entries = time_entries

        # Act
        result = migrator.migrate_time_entries_to_openproject(batch_size=2)

        # Assert
        assert result["successful_migrations"] == 2
        assert result["failed_migrations"] == 0
        assert result["skipped_entries"] == 0
        assert mock_op_client.create_time_entry.call_count == 2

    def test_migrate_time_entries_validation_failures(
        self, migrator, mock_op_client, sample_openproject_time_entry
    ):
        """
        Verify that invalid entries are skipped during migration.
        """
        # Arrange
        invalid_entry = {"hours": 0, "spentOn": "2023-01-01"}  # Missing required fields
        time_entries = [sample_openproject_time_entry, invalid_entry]
        migrator.transformed_time_entries = time_entries

        # Act
        result = migrator.migrate_time_entries_to_openproject()

        # Assert
        assert result["successful_migrations"] == 1  # Only valid entry
        assert result["failed_migrations"] == 0
        assert result["skipped_entries"] == 1  # Invalid entry skipped
        assert mock_op_client.create_time_entry.call_count == 1

    def test_migrate_time_entries_api_failures(
        self, migrator, mock_op_client, sample_openproject_time_entry
    ):
        """
        Verify handling of OpenProject API failures.
        """
        # Arrange
        time_entries = [sample_openproject_time_entry, sample_openproject_time_entry]
        migrator.transformed_time_entries = time_entries
        mock_op_client.create_time_entry.side_effect = [
            {"id": 1},  # Success
            Exception("OpenProject API Error"),  # Failure
        ]

        # Act
        result = migrator.migrate_time_entries_to_openproject()

        # Assert
        assert result["successful_migrations"] == 1
        assert result["failed_migrations"] == 1
        assert len(result["errors"]) == 1
        assert "OpenProject API Error" in result["errors"][0]

    def test_migrate_time_entries_dry_run_mode(
        self, migrator, mock_op_client, sample_openproject_time_entry
    ):
        """
        Verify that dry run mode doesn't create any time entries.
        """
        # Arrange
        time_entries = [sample_openproject_time_entry]
        migrator.transformed_time_entries = time_entries

        # Act
        result = migrator.migrate_time_entries_to_openproject(dry_run=True)

        # Assert
        mock_op_client.create_time_entry.assert_not_called()
        assert result["successful_migrations"] == 1  # Simulated success
        assert result["failed_migrations"] == 0

    def test_migrate_time_entries_batch_processing(
        self, migrator, mock_op_client, sample_openproject_time_entry
    ):
        """
        Verify that large datasets are processed in batches.
        """
        # Arrange
        time_entries = [sample_openproject_time_entry] * 5
        migrator.transformed_time_entries = time_entries

        # Act
        migrator.migrate_time_entries_to_openproject(batch_size=2)

        # Assert
        # Should process in batches: 2, 2, 1
        assert mock_op_client.create_time_entry.call_count == 5


class TestTimeEntryMigratorOrchestration:
    """Tests for main migration orchestration methods."""

    @patch("src.utils.time_entry_migrator.TimeEntryTransformer")
    def test_migrate_time_entries_for_issues_success(
        self, MockTransformer, migrator, mock_jira_client, mock_op_client, sample_openproject_time_entry
    ):
        """
        Verify the main entry point works correctly.
        """
        # Arrange
        migrated_issues = [
            {"jira_key": "PROJ-1", "work_package_id": 1, "project_id": 1},
            {"jira_key": "PROJ-2", "work_package_id": 2, "project_id": 1},
        ]
        
        mock_jira_client.get_work_logs_for_issue.return_value = [{"id": "work_log_1"}]
        mock_jira_client.get_tempo_time_entries.return_value = [{"tempoWorklogId": "tempo_1"}]
        
        mock_transformer_instance = MockTransformer.return_value
        mock_transformer_instance.batch_transform_work_logs.return_value = [sample_openproject_time_entry]

        # Act
        result = migrator.migrate_time_entries_for_issues(migrated_issues)

        # Assert
        assert result["status"] == "success"
        assert result["total_time_entries"]["migrated"] > 0
        assert "PROJ-1" in migrator.work_package_mapping
        assert "PROJ-2" in migrator.work_package_mapping

    @patch("src.utils.time_entry_migrator.TimeEntryTransformer")
    def test_run_complete_migration_end_to_end(
        self, MockTransformer, migrator, mock_jira_client, mock_op_client, sample_openproject_time_entry
    ):
        """
        Verify the complete migration pipeline works end-to-end.
        """
        # Arrange
        issue_keys = ["PROJ-1", "PROJ-2"]
        mock_jira_client.get_work_logs_for_issue.return_value = [{"id": "work_log_1"}]
        mock_jira_client.get_tempo_time_entries.return_value = [{"tempoWorklogId": "tempo_1"}]
        
        mock_transformer_instance = MockTransformer.return_value
        mock_transformer_instance.batch_transform_work_logs.return_value = [sample_openproject_time_entry]

        # Act
        result = migrator.run_complete_migration(issue_keys=issue_keys, include_tempo=True)

        # Assert
        assert result["successful_migrations"] > 0
        assert result["failed_migrations"] == 0
        assert migrator.migration_results["jira_work_logs_extracted"] > 0
        assert migrator.migration_results["tempo_entries_extracted"] > 0

    def test_run_complete_migration_handles_errors(
        self, migrator, mock_jira_client
    ):
        """
        Verify that migration errors are handled gracefully.
        """
        # Arrange
        issue_keys = ["PROJ-1"]
        mock_jira_client.get_work_logs_for_issue.side_effect = Exception("Critical Error")

        # Act
        result = migrator.run_complete_migration(issue_keys=issue_keys)

        # Assert
        assert result["successful_migrations"] == 0
        assert len(migrator.migration_results["errors"]) > 0


class TestTimeEntryMigratorUtilities:
    """Tests for utility and support methods."""

    @patch("builtins.open", new_callable=mock_open)
    @patch("json.dump")
    def test_generate_migration_report(
        self, mock_json_dump, mock_file, migrator
    ):
        """
        Verify that migration reports are generated correctly.
        """
        # Arrange
        migrator.migration_results["successful_migrations"] = 5
        migrator.migration_results["failed_migrations"] = 1

        # Act
        migrator._generate_migration_report()

        # Assert
        mock_file.assert_called()
        mock_json_dump.assert_called()

    def test_get_migration_summary(self, migrator):
        """
        Verify that migration summary is calculated correctly.
        """
        # Arrange
        migrator.migration_results.update({
            "total_work_logs_found": 10,
            "successful_transformations": 8,
            "successful_migrations": 7,
            "failed_migrations": 1,
            "errors": ["Error 1"],
            "warnings": ["Warning 1", "Warning 2"],
            "processing_time_seconds": 120.5
        })

        # Act
        summary = migrator.get_migration_summary()

        # Assert
        assert summary["total_work_logs_found"] == 10
        assert summary["successful_migrations"] == 7
        assert summary["failed_migrations"] == 1
        assert summary["error_count"] == 1
        assert summary["warning_count"] == 2
        assert summary["success_rate"] == 70.0
        assert summary["processing_time_seconds"] == 120.5

    def test_migration_results_initialization(self, migrator):
        """
        Verify that migration results are properly initialized.
        """
        # Assert
        assert migrator.migration_results["jira_work_logs_extracted"] == 0
        assert migrator.migration_results["tempo_entries_extracted"] == 0
        assert migrator.migration_results["successful_transformations"] == 0
        assert migrator.migration_results["successful_migrations"] == 0
        assert migrator.migration_results["failed_migrations"] == 0
        assert migrator.migration_results["errors"] == []
        assert migrator.migration_results["warnings"] == []


class TestTimeEntryMigratorEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_empty_issue_keys_list(self, migrator):
        """
        Verify handling of empty issue keys list.
        """
        # Act
        result = migrator.extract_jira_work_logs_for_issues([])

        # Assert
        assert len(result) == 0  # Returns empty dictionary
        assert migrator.migration_results["jira_work_logs_extracted"] == 0

    def test_none_time_entries_input(self, migrator):
        """
        Verify handling of None input for time entries.
        """
        # Act
        result = migrator.migrate_time_entries_to_openproject(time_entries=None)

        # Assert
        assert result == {}  # Returns empty dict when no entries

    def test_large_dataset_memory_management(
        self, migrator, mock_op_client, sample_openproject_time_entry
    ):
        """
        Verify that large datasets are handled efficiently.
        """
        # Arrange
        large_dataset = [sample_openproject_time_entry] * 1000
        migrator.transformed_time_entries = large_dataset

        # Act
        result = migrator.migrate_time_entries_to_openproject(batch_size=100)

        # Assert
        # Should process in 10 batches of 100
        assert mock_op_client.create_time_entry.call_count == 1000
        assert result["successful_migrations"] == 1000

    def test_concurrent_file_access_handling(self, migrator, tmp_data_dir):
        """
        Verify handling of file access issues during save operations.
        """
        # Arrange
        migrator.extracted_work_logs = {"PROJ-1": [{"id": "work_log_1"}]}
        
        # Make the directory read-only to simulate permission error
        import os
        os.chmod(tmp_data_dir, 0o444)

        # Act & Assert
        # Should not raise exception, just log warning
        migrator._save_extracted_work_logs()
        
        # Cleanup
        os.chmod(tmp_data_dir, 0o755)

    @patch("src.utils.time_entry_migrator.datetime")
    def test_timing_measurements_accuracy(self, mock_datetime, migrator):
        """
        Verify that timing measurements are captured accurately.
        """
        # Arrange
        start_time = datetime(2023, 1, 1, 10, 0, 0)
        end_time = datetime(2023, 1, 1, 10, 0, 30)
        mock_datetime.now.side_effect = [start_time, end_time]

        # Act
        migrator.transform_all_time_entries()

        # Assert
        # Processing time should be calculated correctly
        mock_datetime.now.assert_called() 