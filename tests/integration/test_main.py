import pytest

pytestmark = pytest.mark.integration


"""Tests for the main entry point (src/main.py)."""

import argparse
import unittest
from unittest.mock import MagicMock, patch

from src.main import main


class TestMainEntryPoint:
    """Test main entry point functions."""

    @patch("src.migration.run_migration")
    @patch("src.main.validate_database_configuration")
    @patch("os.environ.get")
    def test_migrate_command(
        self,
        mock_env_get: MagicMock,
        mock_validate_db: MagicMock,
        mock_run_migration: MagicMock,
    ) -> None:
        """Test the main migration command."""
        from src.models.component_results import ComponentResult
        from src.models.migration_results import MigrationResult

        # Mock database configuration validation to pass
        mock_validate_db.return_value = None

        # Mock environment variables to simulate correct setup
        def mock_get(key: str, default: str | None = None) -> str | None:
            return {
                "J2O_JIRA_URL": "https://jira.example.com",
                "J2O_JIRA_EMAIL": "test@example.com",
                "J2O_JIRA_API_TOKEN": "test-token",
                "J2O_OPENPROJECT_URL": "https://openproject.example.com",
                "J2O_OPENPROJECT_API_TOKEN": "op-token",
                "J2O_SSH_HOST": "localhost",
                "J2O_SSH_USERNAME": "test",
                "J2O_SSH_PRIVATE_KEY_PATH": "/path/to/key",
                "J2O_OPENPROJECT_CONTAINER_NAME": "openproject_web",
                "POSTGRES_PASSWORD": "test-password",
            }.get(key, default)

        mock_env_get.side_effect = mock_get

        # Set up mock result with proper MigrationResult object
        mock_run_migration.return_value = MigrationResult(
            overall={"status": "success"},
            components={
                "users": ComponentResult(
                    success=True,
                    message="Users migrated successfully",
                    details={"migrated": 5, "errors": 0},
                ),
                "projects": ComponentResult(
                    success=True,
                    message="Projects migrated successfully",
                    details={"migrated": 3, "errors": 0},
                ),
            },
        )

        # Test main function with mocked command line args and exit
        with patch("sys.argv", ["j2o", "migrate", "--components", "users,projects"]):
            with patch("sys.exit") as mock_exit:
                main()

                # Check that sys.exit was called with 0 (success)
                mock_exit.assert_called_once_with(0)

    @patch("src.main.argparse.ArgumentParser.parse_args")
    @patch("src.main.argparse.ArgumentParser.print_help")
    def test_no_command(
        self,
        mock_print_help: MagicMock,
        mock_parse_args: MagicMock,
    ) -> None:
        """Test behavior when no command is provided."""
        # Set up mock args with no command
        mock_args = argparse.Namespace(command=None)
        mock_parse_args.return_value = mock_args

        # Test main function with mocked exit
        with patch("sys.exit") as mock_exit:
            main()

            # Check that help was printed and exit code 1 was used
            mock_print_help.assert_called_once()
            mock_exit.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
