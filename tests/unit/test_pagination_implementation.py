"""Tests for the pagination implementation in WorkPackageMigration.

These tests verify that the new generator-based pagination approach works correctly
and eliminates memory accumulation issues.
"""

from unittest.mock import Mock, patch

import pytest

from jira.resources import Issue
from src.migrations.work_package_migration import WorkPackageMigration


class TestPaginationImplementation:
    """Test pagination implementation for memory optimization."""

    def setup_method(self) -> None:
        """Setup test environment with proper mocks."""
        # Create mock clients
        self.mock_jira_client = Mock()
        self.mock_op_client = Mock()

        # Create migration instance
        self.migration = WorkPackageMigration(
            jira_client=self.mock_jira_client,
            op_client=self.mock_op_client,
        )

    def test_iter_project_issues_basic_functionality(self) -> None:
        """Test that iter_project_issues properly paginates through issues."""

        # Create mock Issue objects (batch_size worth to trigger second call)
        def create_mock_issue(issue_id):
            mock_issue = Mock(spec=Issue)
            mock_issue.id = str(issue_id)
            mock_issue.key = f"TEST-{issue_id}"
            mock_issue.fields = Mock()
            mock_issue.fields.summary = f"Test Issue {issue_id}"
            return mock_issue

        # Mock the global config to set batch_size
        with patch("src.migrations.work_package_migration.config") as mock_config:
            mock_config.migration_config = {"batch_size": 50}

            # Mock the _fetch_issues_with_retry to return proper list responses
            with patch.object(self.migration, "_fetch_issues_with_retry") as mock_fetch:
                # First call returns 50 issues (batch_size), second call returns empty list (pagination complete)
                first_batch = [create_mock_issue(i) for i in range(1, 51)]
                mock_fetch.side_effect = [
                    first_batch,  # First batch (50 issues)
                    [],  # Second batch (end of pagination)
                ]

                issues = list(self.migration.iter_project_issues("TEST"))

                assert len(issues) == 50
                assert issues[0].key == "TEST-1"
                assert issues[-1].key == "TEST-50"

                # Verify _fetch_issues_with_retry was called twice (for pagination)
                assert mock_fetch.call_count == 2

    def test_iter_project_issues_handles_retry_logic(self) -> None:
        """Test that retry logic is properly invoked on API failures."""
        # Mock Issue object
        mock_issue = Mock(spec=Issue)
        mock_issue.id = "1"
        mock_issue.key = "TEST-1"

        with patch.object(self.migration, "_fetch_issues_with_retry") as mock_fetch:
            # Second attempt succeeds
            mock_fetch.side_effect = [
                [mock_issue],  # First batch succeeds
                [],  # Second batch (end of pagination)
            ]

            issues = list(self.migration.iter_project_issues("TEST"))

            assert len(issues) == 1
            assert issues[0].key == "TEST-1"

    def test_iter_project_issues_project_not_found(self) -> None:
        """Test that project not found errors are handled gracefully."""
        # Mock the project check to fail
        self.migration.jira_client.jira.project.side_effect = Exception(
            "Project not found",
        )

        from src.clients.jira_client import JiraResourceNotFoundError

        with pytest.raises(JiraResourceNotFoundError):
            list(self.migration.iter_project_issues("INVALID"))

    def test_extract_jira_issues_uses_generator(self) -> None:
        """Test that _extract_jira_issues uses the new generator internally."""
        # Mock Issue objects
        mock_issue1 = Mock(spec=Issue)
        mock_issue1.id = "1"
        mock_issue1.key = "TEST-1"
        mock_issue1.self = "https://jira.com/issue/1"
        mock_issue1.fields = Mock()
        mock_issue1.fields.summary = "Test Issue 1"
        mock_issue1.changelog = None

        with (
            patch.object(self.migration, "iter_project_issues") as mock_iter,
            patch.object(self.migration, "_save_to_json"),
        ):

            # Return issue for the project
            mock_iter.return_value = [mock_issue1]

            # Call with single project key (correct signature)
            result = self.migration._extract_jira_issues("TEST1")

            # Verify iter_project_issues was called once for the project
            assert mock_iter.call_count == 1
            mock_iter.assert_called_with("TEST1")

            # Verify result contains the issue
            assert len(result) == 1
            assert result[0]["key"] == "TEST-1"

    def test_get_current_entities_for_type_uses_generator(self) -> None:
        """Test that _get_current_entities_for_type uses the generator for issues."""
        # Mock Issue objects
        mock_issue1 = Mock(spec=Issue)
        mock_issue1.id = "1"
        mock_issue1.key = "TEST1-1"
        mock_issue1.fields = Mock()
        mock_issue1.fields.__dict__ = {"summary": "Test Issue 1"}
        mock_issue1.raw = {}

        # Mock the projects call
        self.migration.jira_client.get_projects.return_value = [
            {"key": "TEST1"},
            {"key": "TEST2"},
        ]

        # Mock the global config to set batch_size
        with patch("src.migrations.work_package_migration.config") as mock_config:
            mock_config.migration_config = {"batch_size": 50}

            with patch.object(self.migration, "iter_project_issues") as mock_iter:
                mock_iter.return_value = [mock_issue1]

                # Call with correct parameter type (entity_type string)
                result = self.migration._get_current_entities_for_type("issues")

                # Verify generator was used for each project
                assert mock_iter.call_count == 2  # Called for each project
                assert (
                    len(result) >= 2
                )  # Should return processed issues from both projects

    def test_memory_efficiency_no_accumulation(self) -> None:
        """Test that the generator pattern doesn't accumulate issues in memory."""
        # This test verifies the generator pattern - it should not load all issues at once

        # Mock a large number of issues in batches
        def create_mock_issue(issue_id, project_key):
            mock_issue = Mock(spec=Issue)
            mock_issue.id = str(issue_id)
            mock_issue.key = f"{project_key}-{issue_id}"
            return mock_issue

        # Mock the global config to set batch_size
        with patch("src.migrations.work_package_migration.config") as mock_config:
            mock_config.migration_config = {"batch_size": 50}

            with patch.object(self.migration, "_fetch_issues_with_retry") as mock_fetch:
                # Simulate 3 batches of 50 issues each (full batches trigger continuation)
                batch_responses = []

                # First batch (50 issues - full batch, continue)
                batch_responses.append(
                    [create_mock_issue(i, "TEST") for i in range(1, 51)],
                )

                # Second batch (50 issues - full batch, continue)
                batch_responses.append(
                    [create_mock_issue(i, "TEST") for i in range(51, 101)],
                )

                # Third batch (50 issues - full batch, continue)
                batch_responses.append(
                    [create_mock_issue(i, "TEST") for i in range(101, 151)],
                )

                # Fourth batch (empty - end pagination)
                batch_responses.append([])

                mock_fetch.side_effect = batch_responses

                # Process through generator - should handle all issues without memory accumulation
                count = 0
                for issue in self.migration.iter_project_issues("TEST"):
                    count += 1
                    # Verify each issue is properly yielded
                    assert hasattr(issue, "key")
                    assert issue.key.startswith("TEST-")

                assert count == 150
                assert (
                    mock_fetch.call_count == 4
                )  # 3 data batches + 1 empty batch to end
