"""
Tests for the main entry point (src/main.py).
"""

import argparse
import unittest
from unittest.mock import MagicMock, patch

from src.main import main


class TestMainEntryPoint(unittest.TestCase):
    """Test cases for the main entry point."""

    @patch("src.main.argparse.ArgumentParser.parse_args")
    @patch("src.main.run_migration")
    @patch("src.config.migration_config")
    def test_migrate_command(
        self,
        mock_migration_config: MagicMock,
        mock_run_migration: MagicMock,
        mock_parse_args: MagicMock,
    ) -> None:
        """Test the 'migrate' command."""
        # Set up mock args
        mock_args = argparse.Namespace(
            command="migrate",
            dry_run=True,
            components=["users", "projects"],
            no_backup=False,
            force=False,
            restore=None,
            tmux=False,
        )
        mock_parse_args.return_value = mock_args

        # Set up mock config values
        mock_migration_config.get.side_effect = lambda key, default=None: {
            "dry_run": True,
            "no_backup": False,
            "force": False,
        }.get(key, default)

        # Set up mock result
        mock_run_migration.return_value = {
            "overall": {"status": "success"},
            "components": {
                "users": {"status": "success"},
                "projects": {"status": "success"},
            },
        }

        # Test main function with mocked exit
        with patch("sys.exit") as mock_exit:
            main()

            # Check that run_migration was called with correct arguments
            mock_run_migration.assert_called_once_with(components=["users", "projects"])

            # Check that sys.exit was called with 0 (success)
            mock_exit.assert_called_once_with(0)

    @patch("src.main.argparse.ArgumentParser.parse_args")
    @patch("src.main.argparse.ArgumentParser.print_help")
    def test_no_command(self, mock_print_help: MagicMock, mock_parse_args: MagicMock) -> None:
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
