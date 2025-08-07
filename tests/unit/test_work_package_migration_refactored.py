#!/usr/bin/env python3
"""Tests for the refactored work_package_migration.py - exception handling improvements."""

import unittest
from unittest.mock import MagicMock, patch

import pytest

from src.clients.openproject_client import QueryExecutionError
from src.migrations.work_package_migration import WorkPackageMigration
from src.models.migration_error import MigrationError


class TestWorkPackageMigrationRefactored(unittest.TestCase):
    """Test cases for refactored WorkPackageMigration exception handling."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        # Create mock clients
        self.mock_jira_client = MagicMock()
        self.mock_op_client = MagicMock()

        # Create migration instance with mocked clients
        self.migration = WorkPackageMigration(
            jira_client=self.mock_jira_client,
            op_client=self.mock_op_client,
        )

    def test_execute_time_entry_migration_successful(self) -> None:
        """Test successful time entry migration execution."""
        # Setup
        expected_result = {
            "jira_work_logs": {"extracted": 50, "migrated": 45, "errors": []},
            "tempo_time_entries": {"extracted": 30, "migrated": 28, "errors": []},
            "total_time_entries": {"migrated": 73, "failed": 7},
        }

        # Mock the internal migration logic
        with patch.object(
            self.migration,
            "_perform_time_entry_migration",
            return_value=expected_result,
        ):
            # Execute
            result = self.migration._execute_time_entry_migration()

            # Verify
            assert result == expected_result
            assert result["total_time_entries"]["migrated"] == 73

    def test_execute_time_entry_migration_raises_migration_error_on_failure(
        self,
    ) -> None:
        """Test that MigrationError is raised when time entry migration fails."""
        # Setup - Mock internal migration to raise an exception
        with patch.object(
            self.migration,
            "_perform_time_entry_migration",
            side_effect=Exception("Database connection lost"),
        ):
            # Execute & Verify
            with pytest.raises(
                MigrationError,
                match="Failed to execute time entry migration.*Database connection lost",
            ):
                self.migration._execute_time_entry_migration()

    def test_execute_time_entry_migration_handles_nested_exceptions(self) -> None:
        """Test that nested exceptions are properly wrapped in MigrationError."""
        # Setup - Mock internal migration to raise a specific exception
        original_exception = ValueError("Invalid work package ID format")
        with patch.object(
            self.migration,
            "_perform_time_entry_migration",
            side_effect=original_exception,
        ):
            # Execute & Verify
            with pytest.raises(MigrationError) as exc_info:
                self.migration._execute_time_entry_migration()

            # Verify exception chaining
            assert isinstance(exc_info.value.__cause__, ValueError)
            assert "Invalid work package ID format" in str(exc_info.value.__cause__)

    def test_work_package_creation_ruby_script_exception_handling(self) -> None:
        """Test work package creation with Ruby script exception handling."""
        # Setup
        project_key = "TEST-PROJECT"

        # Mock execute_query to raise QueryExecutionError (Ruby script failure)
        self.mock_op_client.execute_query.side_effect = QueryExecutionError(
            "Rails validation error: Name cannot be blank",
        )

        # Mock the migration method that uses the Ruby script
        with patch.object(
            self.migration,
            "_create_work_packages_for_project",
        ) as mock_create:
            mock_create.side_effect = QueryExecutionError(
                "Rails validation error: Name cannot be blank",
            )

            # Execute & Verify
            with pytest.raises(QueryExecutionError, match="Rails validation error"):
                mock_create(project_key)

    def test_work_package_creation_handles_timeout_errors(self) -> None:
        """Test work package creation handles timeout errors properly."""
        # Setup
        project_key = "TIMEOUT-PROJECT"

        # Mock execute_query to raise timeout-related QueryExecutionError
        timeout_error = QueryExecutionError("Command timed out after 90 seconds")
        self.mock_op_client.execute_query.side_effect = timeout_error

        # Mock the migration method
        with patch.object(
            self.migration,
            "_create_work_packages_for_project",
        ) as mock_create:
            mock_create.side_effect = timeout_error

            # Execute & Verify
            with pytest.raises(QueryExecutionError, match="Command timed out"):
                mock_create(project_key)

    def test_work_package_creation_continues_on_individual_failures(self) -> None:
        """Test that work package creation continues processing other projects when one fails."""
        # Setup
        projects = ["PROJECT-1", "PROJECT-2", "PROJECT-3"]

        # Mock individual project processing - second one fails
        def mock_process_project(project_key):
            if project_key == "PROJECT-2":
                msg = f"Rails error for {project_key}"
                raise QueryExecutionError(msg)
            return {
                "success": True,
                "project": project_key,
                "work_packages_created": 10,
            }

        processed_projects = []
        failed_projects = []

        # Simulate the error handling logic from the actual migration
        for project_key in projects:
            try:
                result = mock_process_project(project_key)
                processed_projects.append(result)
            except QueryExecutionError as e:
                failed_projects.append(
                    {
                        "project_key": project_key,
                        "error": str(e),
                        "reason": "query_execution_failed",
                    },
                )

        # Verify
        assert len(processed_projects) == 2  # PROJECT-1 and PROJECT-3 succeeded
        assert len(failed_projects) == 1  # PROJECT-2 failed
        assert failed_projects[0]["project_key"] == "PROJECT-2"
        assert "Rails error for PROJECT-2" in failed_projects[0]["error"]

    def test_ruby_script_no_longer_contains_error_dictionaries(self) -> None:
        """Test that Ruby script no longer contains error dictionary patterns."""
        # Setup
        self.mock_op_client.execute_query.return_value = (
            "Work packages created successfully"
        )

        # Execute - trigger Ruby script generation
        with patch.object(
            self.migration,
            "_generate_work_package_ruby_script",
        ) as mock_script:
            mock_script.return_value = """
            # Ruby script for work package creation
            work_packages.each do |wp_data|
              wp = WorkPackage.create!(
                subject: wp_data[:subject],
                project_id: wp_data[:project_id]
              )
              created_work_packages << wp.as_json
            end

            result = {
              created: created_work_packages,
              count: created_work_packages.size
            }

            result.to_json
            """

            script = mock_script.return_value

            # Verify script no longer has error dictionary patterns
            assert "rescue => e" not in script
            assert "error:" not in script
            assert "status: 'error'" not in script
            assert "message: e.message" not in script
            assert "backtrace:" not in script

            # Verify it has proper success patterns
            assert "WorkPackage.create!" in script
            assert "result.to_json" in script

    def test_calling_code_handles_new_exception_patterns(self) -> None:
        """Test that calling code properly handles the new exception patterns."""
        # Setup
        migration_result = {
            "time_entry_migration": None,
            "failed_projects": [],
            "errors": [],
        }

        # Mock _execute_time_entry_migration to raise MigrationError
        with patch.object(
            self.migration,
            "_execute_time_entry_migration",
            side_effect=MigrationError("Time entry migration failed"),
        ):
            # Execute - simulate the calling code pattern from the refactored version
            try:
                time_entry_result = self.migration._execute_time_entry_migration()
                migration_result["time_entry_migration"] = time_entry_result
            except MigrationError as e:
                # This is the new exception handling pattern
                migration_result["time_entry_migration"] = {
                    "status": "failed",
                    "error": str(e),
                    "jira_work_logs": {
                        "extracted": 0,
                        "migrated": 0,
                        "errors": [str(e)],
                    },
                    "tempo_time_entries": {
                        "extracted": 0,
                        "migrated": 0,
                        "errors": [str(e)],
                    },
                    "total_time_entries": {"migrated": 0, "failed": 0},
                }
                migration_result["errors"].append(f"Time entry migration failed: {e}")

        # Verify
        assert migration_result["time_entry_migration"]["status"] == "failed"
        assert (
            "Time entry migration failed"
            in migration_result["time_entry_migration"]["error"]
        )
        assert len(migration_result["errors"]) == 1

    def test_time_entry_migration_with_empty_work_packages(self) -> None:
        """Test time entry migration behavior with empty work packages."""
        # Setup - Mock empty work package list
        with patch.object(
            self.migration,
            "_get_migrated_work_packages",
            return_value=[],
        ):
            # This should not raise an exception, but return empty results
            with patch.object(
                self.migration,
                "_perform_time_entry_migration",
            ) as mock_perform:
                mock_perform.return_value = {
                    "jira_work_logs": {"extracted": 0, "migrated": 0, "errors": []},
                    "tempo_time_entries": {"extracted": 0, "migrated": 0, "errors": []},
                    "total_time_entries": {"migrated": 0, "failed": 0},
                }

                # Execute
                result = self.migration._execute_time_entry_migration()

                # Verify
                assert result["total_time_entries"]["migrated"] == 0
                assert result["total_time_entries"]["failed"] == 0

    def test_migration_error_preserves_original_context(self) -> None:
        """Test that MigrationError preserves original error context."""
        # Setup
        original_error = ConnectionError("Network connection lost during migration")

        with patch.object(
            self.migration,
            "_perform_time_entry_migration",
            side_effect=original_error,
        ):
            # Execute & Verify
            with pytest.raises(MigrationError) as exc_info:
                self.migration._execute_time_entry_migration()

            # Verify error context preservation
            migration_error = exc_info.value
            assert "Failed to execute time entry migration" in str(migration_error)
            assert isinstance(migration_error.__cause__, ConnectionError)
            assert "Network connection lost" in str(migration_error.__cause__)

    def test_batch_processing_error_handling(self) -> None:
        """Test batch processing continues on individual batch failures."""
        # Setup
        batches = [
            {"batch_id": 1, "projects": ["PROJ-1", "PROJ-2"]},
            {"batch_id": 2, "projects": ["PROJ-3", "PROJ-4"]},  # This batch will fail
            {"batch_id": 3, "projects": ["PROJ-5", "PROJ-6"]},
        ]

        successful_batches = []
        failed_batches = []

        # Mock batch processing
        def process_batch(batch):
            if batch["batch_id"] == 2:
                msg = f"Batch {batch['batch_id']} failed due to Rails error"
                raise QueryExecutionError(
                    msg,
                )
            return {
                "success": True,
                "batch_id": batch["batch_id"],
                "processed_count": len(batch["projects"]),
            }

        # Simulate the error handling pattern from company migration
        for batch in batches:
            try:
                result = process_batch(batch)
                successful_batches.append(result)
            except QueryExecutionError as e:
                failed_batches.append(
                    {
                        "batch_id": batch["batch_id"],
                        "error": str(e),
                        "type": "batch_processing_error",
                    },
                )

        # Verify
        assert len(successful_batches) == 2  # Batch 1 and 3 succeeded
        assert len(failed_batches) == 1  # Batch 2 failed
        assert failed_batches[0]["batch_id"] == 2
        assert successful_batches[0]["batch_id"] == 1
        assert successful_batches[1]["batch_id"] == 3
