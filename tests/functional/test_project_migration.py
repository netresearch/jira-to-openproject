"""Tests for the project migration component."""

import json
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, mock_open, patch

import pytest

from src import config
from src.migrations.project_migration import ProjectMigration


class TestProjectMigration(unittest.TestCase):
    """Test cases for the ProjectMigration class."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        # Sample Jira projects data
        self.jira_projects = [
            {
                "id": "10001",
                "key": "PROJ1",
                "name": "Project One",
                "description": "First test project",
            },
            {
                "id": "10002",
                "key": "PROJ2",
                "name": "Project Two",
                "description": "Second test project",
            },
            {
                "id": "10003",
                "key": "PROJ3",
                "name": "Project Three",
                "description": "Third test project with no account",
            },
        ]

        # Sample OpenProject projects data
        self.op_projects = [
            {
                "id": 1,
                "name": "Project One",
                "identifier": "proj1",
                "description": {"raw": "First test project"},
            },
            {
                "id": 3,
                "name": "Existing Project",
                "identifier": "existing-project",
                "description": {"raw": "This already exists"},
            },
        ]

        # Sample project account mapping
        self.project_account_mapping = {
            "PROJ1": 101,
            "PROJ2": 102,
        }

        # Sample account mapping
        self.account_mapping = {
            "101": {"tempo_id": "101", "tempo_name": "Account One"},
            "102": {"tempo_id": "102", "tempo_name": "Account Two"},
        }

        # Expected project mapping
        self.expected_mapping = {
            "PROJ1": {
                "jira_key": "PROJ1",
                "jira_name": "Project One",
                "openproject_id": 1,
                "openproject_identifier": "proj1",
                "openproject_name": "Project One",
                "account_id": 101,
                "account_name": "Account One",
                "created_new": False,
            },
            "PROJ2": {
                "jira_key": "PROJ2",
                "jira_name": "Project Two",
                "openproject_id": 2,
                "openproject_identifier": "proj2",
                "openproject_name": "Project Two",
                "account_id": 102,
                "account_name": "Account Two",
                "created_new": True,
            },
            "PROJ3": {
                "jira_key": "PROJ3",
                "jira_name": "Project Three",
                "openproject_id": 4,
                "openproject_identifier": "proj3",
                "openproject_name": "Project Three",
                "account_id": None,
                "account_name": None,
                "created_new": True,
            },
        }

    @patch("src.clients.jira_client.JiraClient")
    @patch("src.clients.openproject_client.OpenProjectClient")
    @patch("src.migrations.project_migration.config.get_path")
    @patch("src.migrations.project_migration.config.migration_config")
    @patch("os.path.exists")
    def test_extract_jira_projects(
        self,
        mock_exists: MagicMock,
        mock_migration_config: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test extracting projects from Jira."""
        # Create instance with mocked clients
        jira_client = mock_jira_client.return_value

        # Setup the mock return value
        jira_client.get_projects = MagicMock(return_value=self.jira_projects)

        # Mock migration config to force extraction
        mock_migration_config.get.return_value = True  # Force extraction
        mock_exists.return_value = False  # No cached file exists

        # Create the migration instance
        migration = ProjectMigration(jira_client, mock_op_client.return_value)

        # We'll directly patch the _save_to_json method to avoid serialization issues
        with patch.object(migration, "_save_to_json"):
            # Call the method
            result = migration.extract_jira_projects()

            # Assertions - we can't use direct equality because the mock has changed
            assert len(result) == len(self.jira_projects)
            for i, project in enumerate(result):
                assert project["id"] == self.jira_projects[i]["id"]
                assert project["key"] == self.jira_projects[i]["key"]
                assert project["name"] == self.jira_projects[i]["name"]

            # Verify the right method was called
            jira_client.get_projects.assert_called_once()

    @patch("src.clients.jira_client.JiraClient")
    @patch("src.clients.openproject_client.OpenProjectClient")
    @patch("src.migrations.project_migration.config.get_path")
    @patch("src.migrations.project_migration.config.migration_config")
    @patch("os.path.exists")
    def test_extract_openproject_projects(
        self,
        mock_exists: MagicMock,
        mock_migration_config: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test extracting projects from OpenProject."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value
        mock_op_instance.get_projects.return_value = self.op_projects

        mock_get_path.return_value = Path("/tmp/test_data")
        mock_exists.return_value = False

        # Mock the config to return force=True
        mock_migration_config.get.side_effect = lambda key, default=None: True if key == "force" else default

        # Create instance and patch the _save_to_json method to avoid serialization issues
        migration = ProjectMigration(mock_jira_instance, mock_op_instance)
        with patch.object(migration, "_save_to_json") as mock_save_to_json:
            # Call method
            result = migration.extract_openproject_projects()

            # Assertions
            assert result == self.op_projects
            assert mock_op_instance.get_projects.call_count == 1
            assert mock_save_to_json.call_count == 1

    @patch("src.clients.jira_client.JiraClient")
    @patch("src.clients.openproject_client.OpenProjectClient")
    @patch("src.migrations.project_migration.config.get_path")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_analyze_project_mapping(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_get_path: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the analyze_project_mapping method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        mock_get_path.return_value = Path("/tmp/test_data")
        mock_exists.return_value = True

        # Mock file reads
        mock_file.return_value.__enter__.return_value.read.return_value = json.dumps(self.expected_mapping)

        # Create instance
        migration = ProjectMigration(mock_jira_instance, mock_op_instance)
        migration.project_mapping = self.expected_mapping

        # Call method
        result = migration.analyze_project_mapping()

        # Assertions
        assert result["total_projects"] == 3
        assert result["migrated_projects"] == 3
        assert result["new_projects"] == 2  # PROJ2 and PROJ3 are new
        assert result["existing_projects"] == 1  # PROJ1 already existed
        assert result["projects_with_accounts"] == 2  # PROJ1 and PROJ2 have accounts

    def test_find_parent_company_for_project(self) -> None:
        """Test that we resolve parent company via default Tempo account."""
        migration = ProjectMigration(MagicMock(), MagicMock())
        # Stub mappings
        migration.project_account_mapping = {"ACMEWEB": [{"id": "42", "key": "ACC-42", "name": "Q1 Review"}]}
        migration.account_mapping = {"42": {"tempo_id": "42", "company_id": "7", "tempo_name": "Account42"}}
        migration.company_mapping = {
            "7": {
                "tempo_id": "7",
                "openproject_id": 123,
                "tempo_key": "CUST7",
                "tempo_name": "AcmeCorp",
            },
        }
        parent = migration.find_parent_company_for_project({"key": "ACMEWEB"})
        assert parent is not None
        assert parent.get("openproject_id") == 123
        assert parent.get("tempo_name") == "AcmeCorp"

    def test_find_parent_company_warns_on_missing(self) -> None:
        """Test that missing mappings return None and log a warning."""
        migration = ProjectMigration(MagicMock(), MagicMock())
        migration.project_account_mapping = {}
        # Make sure we can capture warnings
        with self.assertLogs(config.logger.name, level="DEBUG") as cm:
            parent = migration.find_parent_company_for_project({"key": "UNKNOWN"})
        assert parent is None
        # Should log a debug about missing account mapping
        assert any("No account mapping found for project UNKNOWN" in msg for msg in cm.output)

    @patch("src.clients.jira_client.JiraClient")
    @patch("src.clients.openproject_client.OpenProjectClient")
    def test_get_existing_project_details_security_injection_prevention(
        self, mock_op_client: MagicMock, mock_jira_client: MagicMock,
    ) -> None:
        """Test that _get_existing_project_details prevents Ruby code injection attacks."""
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        # Create migration instance
        migration = ProjectMigration(mock_jira_instance, mock_op_instance)

        # Test malicious identifiers that could execute code
        malicious_identifiers = [
            # Ruby string interpolation attacks
            "#{system('rm -rf /')}",
            "${system('rm -rf /')}",
            "'; system('rm -rf /'); #",
            # Ruby variable reference attacks
            "#{Rails.application.secrets}",
            "#{ENV['DATABASE_PASSWORD']}",
            # Backtick command execution
            "`rm -rf /`",
            "test`whoami`",
            # Method chaining attacks
            "'}; User.delete_all; Project.create!({identifier: 'evil",
            "'; Project.delete_all; '",
            # Ruby code blocks
            "proc { system('evil') }.call",
            "lambda { |x| system(x) }.call('rm -rf /')",
            # Special characters and escaping
            "test\\\\; system('evil'); \\\"",
            "test'; exec('evil'); #",
            'test"; system("evil"); #',
            # Bracket and brace attacks
            "test}; system('evil'); {",
            "test]; system('evil'); [",
        ]

        for malicious_id in malicious_identifiers:
            with self.subTest(identifier=malicious_id):
                # Mock the Rails query execution to return 'false' (project doesn't exist)
                mock_op_instance.execute_query_to_json_file.return_value = [False, None, None, None]

                # The method should not raise an exception and should handle the input safely
                result = migration._get_existing_project_details(malicious_id)

                # Should return None for non-existent project
                assert result is None

                # Verify the query was executed (proving the method didn't crash)
                mock_op_instance.execute_query_to_json_file.assert_called()

                # Get the actual query that was executed
                call_args = mock_op_instance.execute_query_to_json_file.call_args[0][0]

                # Verify the malicious content is properly escaped in %q{} literal
                # The identifier should be sanitized (closing braces escaped) and wrapped in %q{}
                sanitized = malicious_id.replace("}", "\\}")
                expected_pattern = f"Project.find_by(identifier: %q{{{sanitized}}})"
                assert expected_pattern in call_args

                # Reset mock for next iteration
                mock_op_instance.reset_mock()

    @patch("src.clients.jira_client.JiraClient")
    @patch("src.clients.openproject_client.OpenProjectClient")
    def test_get_existing_project_details_input_validation(
        self, mock_op_client: MagicMock, mock_jira_client: MagicMock,
    ) -> None:
        """Test that _get_existing_project_details validates input types."""
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        migration = ProjectMigration(mock_jira_instance, mock_op_instance)

        # Test non-string inputs
        invalid_inputs = [
            123,
            None,
            [],
            {},
            True,
            False,
        ]

        for invalid_input in invalid_inputs:
            with self.subTest(input_value=invalid_input):
                with pytest.raises(ValueError) as cm:
                    migration._get_existing_project_details(invalid_input)

                # Should raise ValueError with appropriate message
                assert "Identifier must be a string" in str(cm.value)

    @patch("src.clients.jira_client.JiraClient")
    @patch("src.clients.openproject_client.OpenProjectClient")
    def test_get_existing_project_details_special_characters(
        self, mock_op_client: MagicMock, mock_jira_client: MagicMock,
    ) -> None:
        """Test that _get_existing_project_details handles special characters safely."""
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        migration = ProjectMigration(mock_jira_instance, mock_op_instance)

        # Test identifiers with special characters that need proper escaping
        special_identifiers = [
            "project-with-dashes",
            "project_with_underscores",
            "project.with.dots",
            "project with spaces",
            "project\\with\\backslashes",
            "project'with'quotes",
            'project"with"double"quotes',
            "project\nwith\nnewlines",
            "project\rwith\rcarriagereturn",
            "project}with}braces",
            "project{with{open{braces",
            "project[with]brackets",
            "project(with)parentheses",
            "project@with#special$chars%",
            "αβγ-unicode-τεστ",
            "🚀-emoji-project-⭐",
        ]

        for identifier in special_identifiers:
            with self.subTest(identifier=identifier):
                # Mock successful project lookup
                mock_op_instance.execute_query_to_json_file.return_value = [
                    True, 123, "Test Project", identifier,
                ]

                # Should handle special characters without error
                result = migration._get_existing_project_details(identifier)

                # Should return project details
                assert result is not None
                assert result["id"] == 123
                assert result["name"] == "Test Project"
                assert result["identifier"] == identifier

                # Verify the query was executed with proper escaping
                mock_op_instance.execute_query_to_json_file.assert_called()
                call_args = mock_op_instance.execute_query_to_json_file.call_args[0][0]

                # The identifier should be properly escaped (only closing braces need escaping)
                sanitized = identifier.replace("}", "\\}")
                expected_pattern = f"Project.find_by(identifier: %q{{{sanitized}}})"
                assert expected_pattern in call_args

                # Reset mock for next iteration
                mock_op_instance.reset_mock()

    @patch("src.clients.jira_client.JiraClient")
    @patch("src.clients.openproject_client.OpenProjectClient")
    def test_get_existing_project_details_query_failure_handling(
        self, mock_op_client: MagicMock, mock_jira_client: MagicMock,
    ) -> None:
        """Test that _get_existing_project_details handles query failures appropriately."""
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        migration = ProjectMigration(mock_jira_instance, mock_op_instance)

        # Test various failure scenarios
        test_cases = [
            # Rails console exception
            Exception("Rails console connection failed"),
            # Timeout exception
            TimeoutError("Query timed out"),
            # Invalid response format
            "invalid_json_response",
            # Empty response
            [],
            # Malformed response
            [True],  # Missing required fields
        ]

        for exception_or_response in test_cases:
            with self.subTest(scenario=type(exception_or_response).__name__):
                if isinstance(exception_or_response, Exception):
                    # Mock exception being raised
                    mock_op_instance.execute_query_to_json_file.side_effect = exception_or_response

                    # Should re-raise the exception
                    with pytest.raises(Exception) as cm:
                        migration._get_existing_project_details("test-project")

                    assert "Rails query failed" in str(cm.value)
                else:
                    # Mock invalid response format
                    mock_op_instance.execute_query_to_json_file.return_value = exception_or_response
                    mock_op_instance.execute_query_to_json_file.side_effect = None

                    # Should return None for invalid response
                    result = migration._get_existing_project_details("test-project")
                    assert result is None

                # Reset mock for next iteration
                mock_op_instance.reset_mock()
                mock_op_instance.execute_query_to_json_file.side_effect = None

    @patch("src.clients.jira_client.JiraClient")
    @patch("src.clients.openproject_client.OpenProjectClient")
    def test_get_existing_project_details_empty_identifier_handling(
        self, mock_op_client: MagicMock, mock_jira_client: MagicMock,
    ) -> None:
        """Test that _get_existing_project_details handles empty identifiers safely."""
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        migration = ProjectMigration(mock_jira_instance, mock_op_instance)

        # Test empty string (valid string type but empty content)
        mock_op_instance.execute_query_to_json_file.return_value = [False, None, None, None]

        result = migration._get_existing_project_details("")

        # Should handle empty string gracefully
        assert result is None

        # Verify query was executed with empty identifier properly escaped
        mock_op_instance.execute_query_to_json_file.assert_called()
        call_args = mock_op_instance.execute_query_to_json_file.call_args[0][0]
        assert "Project.find_by(identifier: %q{})" in call_args


# Define testing steps for project migration validation


def project_migration_test_steps() -> Any:
    """Testing steps for project migration validation.

    These steps should be executed in a real environment to validate
    the project migration functionality:

    1. Verify project extraction from Jira:
       - Check that all expected Jira projects are extracted
       - Verify key project attributes (key, name, description)

    2. Verify project extraction from OpenProject:
       - Check that existing OpenProject projects are correctly identified
       - Verify key project attributes

    3. Test project creation:
       - Create a new test Jira project
       - Run the migration for just this project
       - Verify the project is created in OpenProject with correct attributes
       - Check that the project identifier follows naming conventions

    4. Test project mapping:
       - Verify projects with the same name are correctly mapped
       - Verify the mapping file contains correct information
       - Check that account associations are correctly maintained

    5. Test project hierarchy (if applicable):
       - Create test Jira projects with parent-child relationships
       - Run the migration
       - Verify the hierarchy is preserved in OpenProject

    6. Test project with custom fields:
       - Verify custom fields like 'Tempo Account' are correctly set on projects
       - Test projects with and without account associations

    7. Test the analysis functionality:
       - Run the analyze_project_mapping method
       - Verify it correctly reports on new vs. existing projects
       - Check it accurately reports on account associations

    8. Test idempotency:
       - Run the migration twice
       - Verify no duplicate projects are created
       - Check that the mapping is correctly updated

    9. Test edge cases:
       - Project with very long name/identifier
       - Project with special characters in name
       - Project with no description
    """
    return "Project migration test steps defined"
