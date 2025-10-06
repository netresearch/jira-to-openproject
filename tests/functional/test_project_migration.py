"""Tests for the project migration component."""

import json
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, mock_open, patch

import pytest

from src import config
from src.migrations.project_migration import (
    PROJECT_AVATAR_CF_NAME,
    PROJECT_CATEGORY_CF_NAME,
    PROJECT_LEAD_CF_NAME,
    PROJECT_TYPE_CF_NAME,
    PROJECT_URL_CF_NAME,
    ProjectMigration,
)


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
        mock_migration_config.get.side_effect = lambda key, default=None: (
            True if key == "force" else default
        )

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
        mock_file.return_value.__enter__.return_value.read.return_value = json.dumps(
            self.expected_mapping,
        )

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
        migration.project_account_mapping = {
            "ACMEWEB": [{"id": "42", "key": "ACC-42", "name": "Q1 Review"}],
        }
        migration.account_mapping = {
            "42": {"tempo_id": "42", "company_id": "7", "tempo_name": "Account42"},
        }
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
        assert any(
            "No account mapping found for project UNKNOWN" in msg for msg in cm.output
        )

    @patch("src.clients.jira_client.JiraClient")
    @patch("src.clients.openproject_client.OpenProjectClient")
    def test_get_existing_project_details_security_injection_prevention(
        self,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
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
                mock_op_instance.execute_query_to_json_file.return_value = [
                    False,
                    None,
                    None,
                    None,
                ]

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
        self,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
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
        self,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
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
                    True,
                    123,
                    "Test Project",
                    identifier,
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
        self,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
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
                    mock_op_instance.execute_query_to_json_file.side_effect = (
                        exception_or_response
                    )

                    # Should re-raise the exception
                    with pytest.raises(Exception) as cm:
                        migration._get_existing_project_details("test-project")

                    assert "Rails query failed" in str(cm.value)
                else:
                    # Mock invalid response format
                    mock_op_instance.execute_query_to_json_file.return_value = (
                        exception_or_response
                    )
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
        self,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test that _get_existing_project_details handles empty identifiers safely."""
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value

        migration = ProjectMigration(mock_jira_instance, mock_op_instance)

        # Test empty string (valid string type but empty content)
        mock_op_instance.execute_query_to_json_file.return_value = [
            False,
            None,
            None,
            None,
        ]

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


def test_bulk_migrate_projects_security_injection_prevention(
    project_migration,
    mock_jira_projects,
    mock_op_client,
) -> None:
    """Test that bulk_migrate_projects prevents Ruby code injection in create_script construction."""
    # Set up test data with malicious project identifiers and names
    malicious_projects = [
        {
            "key": "TEST'; exit 1; echo 'injection",
            "name": "Project #{system('rm -rf /')}'",
            "description": "'; exec('malicious code'); 'safe",
        },
        {
            "key": "EVIL#{`rm -rf /`}",
            "name": "#{Rails.env}",
            "description": "'; User.delete_all; '",
        },
        {
            "key": "BAD';DROP TABLE projects;--",
            "name": "'; system('cat /etc/passwd'); 'safe",
            "description": "#{Rails.application.secrets.secret_key_base}",
        },
    ]

    project_migration.jira_projects = malicious_projects
    project_migration.op_projects = []
    project_migration.account_mapping = {}
    project_migration.project_account_mapping = {}
    project_migration.company_mapping = {}

    # Mock OpenProject client methods to return empty data
    mock_op_client.get_projects.return_value = []

    # Mock _get_existing_project_details to return None (project doesn't exist)
    project_migration._get_existing_project_details = Mock(return_value=None)

    # Mock the Rails execution to capture the script
    executed_scripts = []

    def mock_execute_query(script):
        executed_scripts.append(script)
        # Return success response
        return {"id": 123, "name": "Test Project", "identifier": "test-project"}

    mock_op_client.execute_query_to_json_file.side_effect = mock_execute_query

    # Execute the migration
    result = project_migration.bulk_migrate_projects()

    # Verify result is successful
    assert result.success is True

    # Verify that all executed scripts are safe and properly escaped
    assert len(executed_scripts) == 3, "Should have created 3 projects"

    for script in executed_scripts:
        # Check that no dangerous Ruby patterns are present in the final script
        assert "#{" not in script, "Ruby interpolation should be escaped"
        assert "system(" not in script, "System calls should be escaped"
        assert "exec(" not in script, "Exec calls should be escaped"
        assert "exit " not in script, "Exit commands should be escaped"
        assert "DROP TABLE" not in script, "SQL injection should be escaped"
        assert "delete_all" not in script, "Rails destructive methods should be escaped"
        assert (
            "'; " not in script or script.count("'; ") <= 1
        ), "SQL-style injections should be escaped"

        # Verify proper escaping is applied - single quotes should be escaped with backslashes
        assert "\\'" in script or "'" not in script, "Single quotes should be escaped"

        # Verify dangerous patterns are properly neutralized by escaping
        # We should see backslashes used as escape characters, not literal backslashes being double-escaped
        malicious_content = "\n".join([str(p) for p in malicious_projects])

        if "#{" in malicious_content:
            assert "\\#" in script, "Ruby interpolation start should be escaped"
        if "{" in malicious_content:
            assert "\\{" in script, "Opening braces should be escaped"
        if "}" in malicious_content:
            assert "\\}" in script, "Closing braces should be escaped"

    # Verify the scripts contain expected structure
    for script in executed_scripts:
        assert "Project.create!" in script, "Should create projects"
        assert "p.enabled_module_names" in script, "Should enable modules"
        assert "p.save!" in script, "Should save projects"


def test_bulk_migrate_projects_ruby_escape_function(project_migration) -> None:
    """Test the ruby_escape function within bulk_migrate_projects method."""
    # Create a test project with various special characters
    test_project = {
        "key": "TEST",
        "name": 'Test\'s "Project" with\\backslash and\nnewline',
        "description": "Description with 'quotes' and \\slashes\rand returns",
    }

    project_migration.jira_projects = [test_project]
    project_migration.op_projects = []
    project_migration.account_mapping = {}
    project_migration.project_account_mapping = {}
    project_migration.company_mapping = {}

    # Mock OpenProject client methods to return empty data
    project_migration.op_client.get_projects.return_value = []

    # Mock _get_existing_project_details to return None (project doesn't exist)
    project_migration._get_existing_project_details = Mock(return_value=None)

    # Mock the Rails execution to capture the script
    executed_script = None

    def mock_execute_query(script):
        nonlocal executed_script
        executed_script = script
        return {"id": 123, "name": "Test Project", "identifier": "test"}

    project_migration.op_client.execute_query_to_json_file.side_effect = (
        mock_execute_query
    )

    # Execute the migration
    result = project_migration.bulk_migrate_projects()

    # Verify the migration was successful
    assert result.success is True

    # Verify the script was generated with proper escaping
    assert executed_script is not None

    # Check that special characters are properly escaped
    assert "Test\\'s" in executed_script, "Single quotes should be escaped"
    assert (
        '"Project"' in executed_script
    ), "Double quotes are preserved in single-quoted Ruby strings"
    assert "with\\\\backslash" in executed_script, "Backslashes should be escaped"
    assert "and\\nnewline" in executed_script, "Newlines should be escaped"
    assert "\\rand" in executed_script, "Carriage returns should be escaped"


def test_bulk_migrate_projects_empty_and_none_values(project_migration) -> None:
    """Test bulk_migrate_projects handles empty and None values correctly."""
    test_project = {
        "key": "TEST",
        "name": "",  # Empty name
        "description": None,  # None description
    }

    project_migration.jira_projects = [test_project]
    project_migration.op_projects = []
    project_migration.account_mapping = {}
    project_migration.project_account_mapping = {}
    project_migration.company_mapping = {}

    # Mock OpenProject client methods to return empty data
    project_migration.op_client.get_projects.return_value = []

    # Mock _get_existing_project_details to return None
    project_migration._get_existing_project_details = Mock(return_value=None)

    # Mock the Rails execution
    executed_script = None

    def mock_execute_query(script):
        nonlocal executed_script
        executed_script = script
        return {"id": 123, "name": "Test Project", "identifier": "test"}

    project_migration.op_client.execute_query_to_json_file.side_effect = (
        mock_execute_query
    )

    # Execute the migration
    result = project_migration.bulk_migrate_projects()

    # Verify the migration was successful
    assert result.success is True

    # Verify the script handles empty values safely
    assert executed_script is not None
    assert "name: ''" in executed_script, "Empty name should be handled"
    assert (
        "description: ''" in executed_script
    ), "None description should become empty string"


def test_determine_project_modules_tempo_accounts(project_migration) -> None:
    jira_project = {"key": "TEST", "has_tempo_account": True}
    modules = project_migration._determine_project_modules(jira_project)
    assert "time_tracking" in modules
    assert "costs" in modules


def test_determine_project_modules_category_enables_news(project_migration) -> None:
    jira_project = {"key": "TEST", "project_category_name": "NR: IT Services"}
    modules = project_migration._determine_project_modules(jira_project)
    assert "calendar" in modules
    assert "news" in modules


def test_persist_project_metadata_upserts_attributes(project_migration) -> None:
    project_migration.op_client.upsert_project_attribute.reset_mock()

    jira_project = {
        "project_category_name": "NR: IT Services",
        "project_type_key": "service_desk",
        "browse_url": "https://jira.example.com/browse/TEST",
        "avatar_url": "https://jira.example.com/secure/projectavatar?avatarId=123",
    }

    project_migration._persist_project_metadata(42, jira_project)

    called_names = {
        call.kwargs["name"] for call in project_migration.op_client.upsert_project_attribute.call_args_list
    }

    assert PROJECT_CATEGORY_CF_NAME in called_names
    assert PROJECT_TYPE_CF_NAME in called_names
    assert PROJECT_URL_CF_NAME in called_names
    assert PROJECT_AVATAR_CF_NAME in called_names


def test_persist_project_metadata_sanitizes_values(project_migration) -> None:
    project_migration.op_client.upsert_project_attribute.reset_mock()

    jira_project = {
        "project_category_name": "R&D's Initiatives",
        "project_type_key": "software",
        "browse_url": "https://jira.example.com/browse/RND",
        "avatar_url": "https://jira.example.com/avatar?id=1&size=64x64",
    }

    project_migration._persist_project_metadata(99, jira_project)

    calls = project_migration.op_client.upsert_project_attribute.call_args_list
    category_val = None
    avatar_val = None
    for call in calls:
        if call.kwargs.get("name") == PROJECT_CATEGORY_CF_NAME:
            category_val = call.kwargs.get("value")
        if call.kwargs.get("name") == PROJECT_AVATAR_CF_NAME:
            avatar_val = call.kwargs.get("value")

    assert category_val is not None
    assert "R\'D" in category_val
    assert avatar_val is not None
    assert "avatar" in avatar_val

@patch("src.migrations.project_migration.logger")
def test_assign_project_lead_happy_path(mock_logger: MagicMock) -> None:
    """Assign project lead should grant role membership and persist provenance."""

    migration = ProjectMigration.__new__(ProjectMigration)
    migration.op_client = MagicMock()
    migration._extract_jira_lead = Mock(return_value=("sebastian", "Sebastian Mendel"))
    migration._lookup_op_user_id = Mock(return_value=42)
    migration._get_role_id = Mock(side_effect=lambda name: 7 if name == "project admin" else None)

    migration._assign_project_lead(303202, {"key": "SRVAC"})

    migration.op_client.assign_user_roles.assert_called_once_with(
        project_id=303202,
        user_id=42,
        role_ids=[7],
    )
    migration.op_client.upsert_project_attribute.assert_called_once_with(
        project_id=303202,
        name=PROJECT_LEAD_CF_NAME,
        value="42",
        field_format="user",
    )
    mock_logger.debug.assert_not_called()


@patch("src.migrations.project_migration.logger")
def test_assign_project_lead_missing_user_mapping(mock_logger: MagicMock) -> None:
    """Skip assignment when the Jira lead cannot be mapped to OpenProject."""

    migration = ProjectMigration.__new__(ProjectMigration)
    migration.op_client = MagicMock()
    migration._extract_jira_lead = Mock(return_value=("ghost.user", "Ghost"))
    migration._lookup_op_user_id = Mock(return_value=None)
    migration._get_role_id = Mock(return_value=7)

    migration._assign_project_lead(1, {"key": "SRVAC"})

    migration.op_client.assign_user_roles.assert_not_called()
    migration.op_client.upsert_project_attribute.assert_called_once()
    call = migration.op_client.upsert_project_attribute.call_args
    assert call.kwargs["field_format"] == "string"
    assert "ghost.user" in call.kwargs["value"]
    mock_logger.debug.assert_called_once()


@patch("src.migrations.project_migration.logger")
def test_assign_project_lead_role_fallback_and_error_logging(
    mock_logger: MagicMock,
) -> None:
    """Fallback to member role and log errors while still persisting provenance."""

    migration = ProjectMigration.__new__(ProjectMigration)
    migration.op_client = MagicMock()
    migration._extract_jira_lead = Mock(return_value=("tappert", None))
    migration._lookup_op_user_id = Mock(return_value=24)
    migration._get_role_id = Mock(side_effect=[None, 5])
    migration.op_client.assign_user_roles.return_value = {
        "success": False,
        "error": "role missing",
    }

    migration._assign_project_lead(99, {"key": "SRVAC"})

    migration.op_client.assign_user_roles.assert_called_once_with(
        project_id=99,
        user_id=24,
        role_ids=[5],
    )
    migration.op_client.upsert_project_attribute.assert_called_once_with(
        project_id=99,
        name=PROJECT_LEAD_CF_NAME,
        value="24",
        field_format="user",
    )
    assert any("Failed to assign project lead" in str(call.args[0]) for call in mock_logger.debug.call_args_list)
