"""
Tests for the account migration component.
"""

import json
import unittest
from unittest.mock import MagicMock, mock_open, patch

from typing import Any

from src.migrations.account_migration import AccountMigration


class TestAccountMigration(unittest.TestCase):
    """Test cases for the AccountMigration class."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        # Sample Tempo accounts data
        self.tempo_accounts = [
            {
                "id": "101",
                "key": "ACCT1",
                "name": "Account One",
                "companyId": "201",
                "default_project": {"key": "PROJ1"},
            },
            {
                "id": "102",
                "key": "ACCT2",
                "name": "Account Two",
                "companyId": "202",
                "default_project": {"key": "PROJ2"},
            },
        ]

        # Sample OpenProject projects data
        self.op_projects = [
            {
                "id": 1,
                "name": "Account One",
                "identifier": "account-one",
                "_links": {"parent": {"href": "/api/v3/projects/10"}},
            },
            {
                "id": 3,
                "name": "Some Other Project",
                "identifier": "some-other-project",
                "_links": {},
            },
        ]

        # Expected account mapping
        self.expected_mapping = {
            "101": {
                "tempo_id": "101",
                "tempo_key": "ACCT1",
                "tempo_name": "Account One",
                "company_id": "201",
                "default_project_key": "PROJ1",
                "openproject_id": 1,
                "openproject_identifier": "account-one",
                "openproject_name": "Account One",
                "parent_id": "10",
                "matched_by": "name",
            },
            "102": {
                "tempo_id": "102",
                "tempo_key": "ACCT2",
                "tempo_name": "Account Two",
                "company_id": "202",
                "default_project_key": "PROJ2",
                "openproject_id": None,
                "openproject_identifier": None,
                "openproject_name": None,
                "parent_id": None,
                "matched_by": "none",
            },
        }

        # Sample OpenProject Rails Client response for custom field creation
        self.custom_field_creation_response = {
            "status": "success",
            "output": 42,  # The ID of the created custom field
        }

        # Sample OpenProject Rails Client response for custom field activation
        self.custom_field_activation_response = {"status": "success", "output": True}

    @patch("src.migrations.account_migration.JiraClient")
    @patch("src.migrations.account_migration.OpenProjectClient")
    @patch("src.migrations.account_migration.OpenProjectRailsClient")
    @patch("src.migrations.account_migration.config.get_path")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_extract_tempo_accounts(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_get_path: MagicMock,
        mock_rails_client: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the extract_tempo_accounts method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_jira_instance.get_tempo_accounts.return_value = self.tempo_accounts

        mock_op_instance = mock_op_client.return_value
        mock_rails_instance = mock_rails_client.return_value

        mock_get_path.return_value = "/tmp/test_data"
        mock_exists.return_value = False  # Force new extraction

        # Initialize migration
        migration = AccountMigration(
            mock_jira_instance, mock_op_instance, mock_rails_instance
        )

        # Call extract_tempo_accounts
        result = migration.extract_tempo_accounts()

        # Verify calls
        mock_jira_instance.get_tempo_accounts.assert_called_once_with(expand=True)

        # Verify data was extracted
        self.assertEqual(len(result), 2)
        self.assertEqual(migration.tempo_accounts, self.tempo_accounts)

    @patch("src.migrations.account_migration.JiraClient")
    @patch("src.migrations.account_migration.OpenProjectClient")
    @patch("src.migrations.account_migration.OpenProjectRailsClient")
    @patch("src.migrations.account_migration.config.get_path")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_extract_openproject_projects(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_get_path: MagicMock,
        mock_rails_client: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the extract_openproject_projects method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value
        mock_op_instance.get_projects.return_value = self.op_projects
        mock_rails_instance = mock_rails_client.return_value

        mock_get_path.return_value = "/tmp/test_data"
        mock_exists.return_value = True

        # Initialize migration
        migration = AccountMigration(
            mock_jira_instance, mock_op_instance, mock_rails_instance
        )

        # Call extract_openproject_projects
        result = migration.extract_openproject_projects()

        # Verify calls
        mock_op_instance.get_projects.assert_called_once()

        # Verify data was extracted
        self.assertEqual(len(result), 2)
        self.assertEqual(migration.op_projects, self.op_projects)

    @patch("src.migrations.account_migration.JiraClient")
    @patch("src.migrations.account_migration.OpenProjectClient")
    @patch("src.migrations.account_migration.OpenProjectRailsClient")
    @patch("src.migrations.account_migration.config.get_path")
    @patch("src.migrations.account_migration.config.migration_config")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_create_account_mapping(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_migration_config: MagicMock,
        mock_get_path: MagicMock,
        mock_rails_client: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the create_account_mapping method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_jira_instance.get_tempo_accounts.return_value = self.tempo_accounts

        mock_op_instance = mock_op_client.return_value
        mock_op_instance.get_projects.return_value = self.op_projects

        mock_rails_instance = mock_rails_client.return_value
        mock_migration_config.get.return_value = False  # Not force mode
        mock_get_path.return_value = "/tmp/test_data"
        mock_exists.return_value = True

        # Mock the JSON file loading for jira_project_mapping
        project_mapping_mock = mock_open(
            read_data=json.dumps(
                {"PROJ1": {"openproject_id": "3"}, "PROJ2": {"openproject_id": None}}
            )
        )
        # This patch will be used when loading jira_project_mapping.json
        with patch("builtins.open", project_mapping_mock):
            # Initialize migration
            migration = AccountMigration(
                mock_jira_instance, mock_op_instance, mock_rails_instance
            )

            # Mock _load_from_json to return the project mapping
            migration._load_from_json = MagicMock()
            migration._load_from_json.return_value = {
                "PROJ1": {"openproject_id": "3"},
                "PROJ2": {"openproject_id": None},
            }

            # Set the extracted data
            migration.tempo_accounts = self.tempo_accounts
            migration.op_projects = self.op_projects

            # Mock _save_to_json method to avoid file I/O
            with patch.object(migration, "_save_to_json"):
                # Call create_account_mapping
                result = migration.create_account_mapping()

                # Verify mappings
                self.assertIn("101", result)
                self.assertIn("102", result)
                self.assertEqual(result["101"]["tempo_name"], "Account One")
                self.assertEqual(result["101"]["openproject_id"], 1)
                self.assertEqual(result["101"]["matched_by"], "name")
                self.assertIsNone(result["102"]["openproject_id"])
                self.assertEqual(result["102"]["matched_by"], "none")

    @patch("src.migrations.account_migration.JiraClient")
    @patch("src.migrations.account_migration.OpenProjectClient")
    @patch("src.migrations.account_migration.OpenProjectRailsClient")
    @patch("src.migrations.account_migration.config.get_path")
    @patch("src.migrations.account_migration.config.migration_config")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_create_account_custom_field(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_migration_config: MagicMock,
        mock_get_path: MagicMock,
        mock_rails_client: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the create_account_custom_field method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_jira_instance.get_tempo_accounts.return_value = self.tempo_accounts

        mock_op_instance = mock_op_client.return_value
        mock_rails_instance = mock_rails_client.return_value
        mock_rails_instance.get_custom_field_id_by_name.return_value = None
        mock_rails_instance.execute.side_effect = [
            self.custom_field_creation_response,
            self.custom_field_activation_response,
        ]

        mock_migration_config.get.return_value = False  # Not dry run mode
        mock_get_path.return_value = "/tmp/test_data"
        mock_exists.return_value = True

        # Initialize migration
        migration = AccountMigration(
            mock_jira_instance, mock_op_instance, mock_rails_instance
        )

        # Set the extracted data
        migration.tempo_accounts = self.tempo_accounts

        # Mock _save_custom_field_id method to avoid file I/O
        with patch.object(migration, "_save_custom_field_id"):
            # Call create_account_custom_field
            result = migration.create_account_custom_field()

            # Verify Rails client was called to check for existing custom field
            mock_rails_instance.get_custom_field_id_by_name.assert_called_once_with(
                "Tempo Account"
            )

            # Verify Rails client was called to create the custom field
            mock_rails_instance.execute.assert_called()

            # Verify the result is the custom field ID
            self.assertEqual(result, 42)

    @patch("src.migrations.account_migration.JiraClient")
    @patch("src.migrations.account_migration.OpenProjectClient")
    @patch("src.migrations.account_migration.OpenProjectRailsClient")
    @patch("src.migrations.account_migration.config.get_path")
    @patch("src.migrations.account_migration.config.migration_config")
    @patch("src.migrations.account_migration.ProgressTracker")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    def test_migrate_accounts(
        self,
        mock_file: MagicMock,
        mock_exists: MagicMock,
        mock_tracker: MagicMock,
        mock_migration_config: MagicMock,
        mock_get_path: MagicMock,
        mock_rails_client: MagicMock,
        mock_op_client: MagicMock,
        mock_jira_client: MagicMock,
    ) -> None:
        """Test the migrate_accounts method."""
        # Setup mocks
        mock_jira_instance = mock_jira_client.return_value
        mock_op_instance = mock_op_client.return_value
        mock_rails_instance = mock_rails_client.return_value

        mock_migration_config.get.return_value = False  # Not dry run mode
        mock_get_path.return_value = "/tmp/test_data"
        mock_exists.return_value = True

        # Mock the progress tracker context manager
        mock_tracker_instance = MagicMock()
        mock_tracker.return_value.__enter__.return_value = mock_tracker_instance

        # Initialize migration
        migration = AccountMigration(
            mock_jira_instance, mock_op_instance, mock_rails_instance
        )

        # Create a deep copy of the mapping so we can modify it
        import copy

        account_mapping = copy.deepcopy(self.expected_mapping)

        # Set the data
        migration.tempo_accounts = self.tempo_accounts
        migration.account_mapping = account_mapping
        # Set account_custom_field_id to None so it calls create_account_custom_field
        migration.account_custom_field_id = None

        # Mock the required methods to avoid actual calls
        migration.extract_tempo_accounts = MagicMock(return_value=self.tempo_accounts)
        migration.create_account_custom_field = MagicMock(return_value=42)
        migration.create_account_mapping = MagicMock(return_value=account_mapping)

        # Define a side effect for _save_to_json to update the account_mapping with custom_field_id
        def save_to_json_side_effect(data: Any, filename: Any) -> Any:
            if filename == "account_mapping.json":
                # Update the account mapping with custom field ID
                for account_id in account_mapping:
                    account_mapping[account_id]["custom_field_id"] = 42

        # Mock _save_to_json method with our side effect
        with patch.object(
            migration, "_save_to_json", side_effect=save_to_json_side_effect
        ):
            # Call migrate_accounts
            result = migration.migrate_accounts()

            # Verify methods were called
            migration.create_account_custom_field.assert_called_once()

            # Verify the result contains the accounts with custom_field_id
            self.assertIn("101", result)
            self.assertIn("102", result)
            self.assertEqual(result["101"]["custom_field_id"], 42)
            self.assertEqual(result["102"]["custom_field_id"], 42)


if __name__ == "__main__":
    unittest.main()
